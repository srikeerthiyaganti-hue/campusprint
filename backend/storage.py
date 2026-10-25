"""
Centralized Storage Abstraction for XeroxQueue

Supports two backends:
1. 'local': Local filesystem storage (default for local development).
2. 's3': S3-compatible object storage (suitable for Cloudflare R2, AWS S3, MinIO).

Design Principles:
- Local mode requires zero cloud credentials.
- S3 mode strictly validates required configuration and fails fast if incomplete.
- Never silently falls back to local storage when S3 is configured.
- Never exposes credentials in logs, API responses, or error messages.
- Enforces strict key validation to prevent directory traversal or arbitrary object keys.
- Stream downloads through the backend with proper content-type and content-disposition headers.
"""

import io
import logging
import os
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Iterable, Optional, Union

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pypdf import PdfReader

logger = logging.getLogger("xeroxqueue.storage")

# Valid key pattern: alphanumeric, dash, underscore, dot; must end with .pdf; max 255 chars
SAFE_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]{1,255}$")


def validate_storage_key(key: str) -> str:
    """
    Validates that a storage key is a safe, simple filename ending with .pdf.
    Rejects directory traversal, null bytes, slashes, and non-PDF extensions.
    """
    if not key or not isinstance(key, str):
        raise ValueError("Invalid storage key: key must be a non-empty string.")

    cleaned_key = key.strip()

    if "/" in cleaned_key or "\\" in cleaned_key or ".." in cleaned_key:
        raise ValueError(f"Invalid storage key '{cleaned_key}': directory traversal is strictly prohibited.")

    if not SAFE_KEY_PATTERN.match(cleaned_key) or not cleaned_key.lower().endswith(".pdf"):
        raise ValueError(f"Invalid storage key '{cleaned_key}': key must be a valid PDF filename.")

    return cleaned_key


class BaseStorageBackend(ABC):
    """Abstract base class for XeroxQueue PDF storage backends."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Name of the backend ('local' or 's3')."""
        pass

    @abstractmethod
    def save(self, key: str, data: Union[bytes, BinaryIO], content_type: str = "application/pdf") -> int:
        """
        Saves binary data under the given key.
        Returns the number of bytes stored.
        """
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Checks whether an object with the given key exists in storage."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """
        Deletes the object with the given key.
        Returns True if deleted, False if it did not exist.
        """
        pass

    @abstractmethod
    def get_stream(self, key: str) -> Iterable[bytes]:
        """Returns an iterable of byte chunks for the given key."""
        pass

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Reads all bytes for the given key."""
        pass

    @abstractmethod
    def get_response(self, key: str, download_filename: str, inline: bool = True) -> Response:
        """
        Returns a FastAPI Response (FileResponse or StreamingResponse) for the document.
        Raises HTTPException(404) if not found, or HTTPException(503) if storage unavailable.
        """
        pass

    def get_page_count(self, key: str) -> Optional[int]:
        """
        Calculates page count of the stored PDF document.
        Returns integer page count, or None if file cannot be read.
        """
        validated_key = validate_storage_key(key)
        return _cached_page_count(self.backend_name, validated_key, self)


class LocalStorageBackend(BaseStorageBackend):
    """Local filesystem storage backend."""

    def __init__(self, uploads_dir: Optional[Union[str, Path]] = None):
        if uploads_dir is None:
            uploads_dir = Path(__file__).resolve().parent / "uploads"
        self.uploads_dir = Path(uploads_dir).resolve()
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return "local"

    def _get_path(self, key: str) -> Path:
        validated_key = validate_storage_key(key)
        target = (self.uploads_dir / validated_key).resolve()
        # Enforce containment inside uploads_dir
        if not str(target).startswith(str(self.uploads_dir)):
            raise ValueError(f"Path traversal detected: {key}")
        return target

    def save(self, key: str, data: Union[bytes, BinaryIO], content_type: str = "application/pdf") -> int:
        target_path = self._get_path(key)
        if isinstance(data, bytes):
            target_path.write_bytes(data)
            return len(data)
        elif hasattr(data, "read"):
            data.seek(0)
            content = data.read()
            target_path.write_bytes(content)
            return len(content)
        else:
            raise TypeError("Data must be bytes or a readable binary file-like object.")

    def exists(self, key: str) -> bool:
        try:
            target_path = self._get_path(key)
            return target_path.is_file()
        except ValueError:
            return False

    def delete(self, key: str) -> bool:
        try:
            target_path = self._get_path(key)
            if target_path.is_file():
                target_path.unlink()
                return True
            return False
        except (ValueError, OSError) as exc:
            logger.warning(f"Failed to delete local file {key}: {exc}")
            return False

    def get_stream(self, key: str) -> Iterable[bytes]:
        target_path = self._get_path(key)
        if not target_path.is_file():
            raise FileNotFoundError(f"Local file not found: {key}")

        def chunk_generator():
            with target_path.open("rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return chunk_generator()

    def get_bytes(self, key: str) -> bytes:
        target_path = self._get_path(key)
        if not target_path.is_file():
            raise FileNotFoundError(f"Local file not found: {key}")
        return target_path.read_bytes()

    def get_response(self, key: str, download_filename: str, inline: bool = True) -> Response:
        try:
            target_path = self._get_path(key)
        except ValueError:
            raise HTTPException(status_code=404, detail="The requested document key is invalid.")

        if not target_path.is_file():
            raise HTTPException(status_code=404, detail="The uploaded PDF file could not be found.")

        safe_filename = Path(download_filename or "document.pdf").name
        return FileResponse(
            path=str(target_path),
            media_type="application/pdf",
            filename=safe_filename,
            content_disposition_type="inline" if inline else "attachment",
        )


class S3StorageBackend(BaseStorageBackend):
    """
    S3-compatible object storage backend (e.g., Cloudflare R2, AWS S3, MinIO).
    Validates required configuration on startup and streams downloads via backend.
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
        s3_client=None,
    ):
        # Validate configuration
        missing = []
        if not endpoint_url or not endpoint_url.strip():
            missing.append("S3_ENDPOINT_URL")
        if not bucket_name or not bucket_name.strip():
            missing.append("S3_BUCKET_NAME")
        if not access_key_id or not access_key_id.strip():
            missing.append("S3_ACCESS_KEY_ID")
        if not secret_access_key or not secret_access_key.strip():
            missing.append("S3_SECRET_ACCESS_KEY")

        if missing:
            raise RuntimeError(
                f"CRITICAL STORAGE CONFIGURATION ERROR: S3 storage requires the following missing environment variable(s): {', '.join(missing)}."
            )

        self.endpoint_url = endpoint_url.strip()
        self.bucket_name = bucket_name.strip()
        self.access_key_id = access_key_id.strip()
        self.secret_access_key = secret_access_key.strip()
        self.region = (region or "auto").strip()

        if s3_client is not None:
            self._client = s3_client
        else:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise RuntimeError(
                    "CRITICAL STORAGE ERROR: 'boto3' is required for S3StorageBackend. "
                    "Please install runtime dependencies from backend/requirements.txt."
                )

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=15,
                ),
            )

    @property
    def backend_name(self) -> str:
        return "s3"

    def __repr__(self) -> str:
        # Prevent leaking credentials or endpoint in string representations
        return f"<S3StorageBackend bucket={self.bucket_name} region={self.region}>"

    def save(self, key: str, data: Union[bytes, BinaryIO], content_type: str = "application/pdf") -> int:
        validated_key = validate_storage_key(key)
        if isinstance(data, bytes):
            body = data
            size = len(data)
        elif hasattr(data, "read"):
            data.seek(0)
            body = data.read()
            size = len(body)
        else:
            raise TypeError("Data must be bytes or a readable binary file-like object.")

        try:
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=validated_key,
                Body=body,
                ContentType=content_type,
            )
            return size
        except Exception as exc:
            # Mask credentials and internal endpoint details
            logger.error(f"S3 put_object failed for key {validated_key}: {exc.__class__.__name__}")
            raise HTTPException(
                status_code=503,
                detail="Storage service is currently unable to save the document. Please try again.",
            )

    def exists(self, key: str) -> bool:
        try:
            validated_key = validate_storage_key(key)
            self._client.head_object(Bucket=self.bucket_name, Key=validated_key)
            return True
        except Exception as exc:
            # Check for 404/NoSuchKey without leaking details
            err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("404", "NoSuchKey", "NotFound"):
                return False
            # If ClientError with 404 in message
            if "404" in str(exc) or "NoSuchKey" in str(exc):
                return False
            logger.warning(f"S3 head_object error for {key}: {exc.__class__.__name__}")
            return False

    def delete(self, key: str) -> bool:
        try:
            validated_key = validate_storage_key(key)
            self._client.delete_object(Bucket=self.bucket_name, Key=validated_key)
            return True
        except Exception as exc:
            logger.warning(f"S3 delete_object failed for {key}: {exc.__class__.__name__}")
            return False

    def get_stream(self, key: str) -> Iterable[bytes]:
        validated_key = validate_storage_key(key)
        try:
            response = self._client.get_object(Bucket=self.bucket_name, Key=validated_key)
            body = response["Body"]
            if hasattr(body, "iter_chunks"):
                return body.iter_chunks(chunk_size=65536)
            elif hasattr(body, "read"):
                def chunk_generator():
                    while True:
                        chunk = body.read(65536)
                        if not chunk:
                            break
                        yield chunk
                return chunk_generator()
            else:
                return [body]
        except Exception as exc:
            err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("404", "NoSuchKey", "NotFound") or "404" in str(exc) or "NoSuchKey" in str(exc):
                raise FileNotFoundError(f"S3 object not found: {validated_key}")
            logger.error(f"S3 get_object failed for {validated_key}: {exc.__class__.__name__}")
            raise RuntimeError("Storage service error during retrieval.")

    def get_bytes(self, key: str) -> bytes:
        validated_key = validate_storage_key(key)
        try:
            response = self._client.get_object(Bucket=self.bucket_name, Key=validated_key)
            body = response["Body"]
            return body.read()
        except Exception as exc:
            err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("404", "NoSuchKey", "NotFound") or "404" in str(exc) or "NoSuchKey" in str(exc):
                raise FileNotFoundError(f"S3 object not found: {validated_key}")
            logger.error(f"S3 get_object bytes failed for {validated_key}: {exc.__class__.__name__}")
            raise RuntimeError("Storage service error during retrieval.")

    def get_response(self, key: str, download_filename: str, inline: bool = True) -> Response:
        try:
            validated_key = validate_storage_key(key)
        except ValueError:
            raise HTTPException(status_code=404, detail="The requested document key is invalid.")

        try:
            stream = self.get_stream(validated_key)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="The uploaded PDF file could not be found.")
        except Exception:
            raise HTTPException(status_code=503, detail="Storage service is currently unavailable. Please try again.")

        safe_filename = Path(download_filename or "document.pdf").name
        disposition_type = "inline" if inline else "attachment"
        headers = {
            "Content-Disposition": f'{disposition_type}; filename="{safe_filename}"',
            "Content-Type": "application/pdf",
        }

        return StreamingResponse(
            stream,
            media_type="application/pdf",
            headers=headers,
        )


def _cached_page_count(backend_name: str, key: str, backend: BaseStorageBackend) -> Optional[int]:
    """Inspects PDF and returns page count."""
    try:
        if backend_name == "local" and isinstance(backend, LocalStorageBackend):
            path = backend._get_path(key)
            if not path.is_file():
                return None
            reader = PdfReader(str(path))
            return len(reader.pages)
        else:
            data = backend.get_bytes(key)
            reader = PdfReader(io.BytesIO(data))
            return len(reader.pages)
    except Exception as exc:
        logger.debug(f"Could not calculate page count for {key}: {exc}")
        return None


# Global singleton instance
_GLOBAL_STORAGE_BACKEND: Optional[BaseStorageBackend] = None


def get_storage() -> BaseStorageBackend:
    """
    Factory that returns the configured BaseStorageBackend singleton.
    Reads STORAGE_BACKEND ('local' or 's3') and creates the appropriate backend.
    """
    global _GLOBAL_STORAGE_BACKEND
    if _GLOBAL_STORAGE_BACKEND is not None:
        return _GLOBAL_STORAGE_BACKEND

    backend_type = os.getenv("STORAGE_BACKEND", "local").strip().lower()

    if backend_type == "local":
        uploads_dir = os.getenv("LOCAL_UPLOADS_DIR")
        _GLOBAL_STORAGE_BACKEND = LocalStorageBackend(uploads_dir=uploads_dir)
        logger.info(f"Initialized storage backend: local (path: {_GLOBAL_STORAGE_BACKEND.uploads_dir})")
        return _GLOBAL_STORAGE_BACKEND

    elif backend_type == "s3":
        endpoint_url = os.getenv("S3_ENDPOINT_URL", "")
        bucket_name = os.getenv("S3_BUCKET_NAME", "")
        access_key_id = os.getenv("S3_ACCESS_KEY_ID", "")
        secret_access_key = os.getenv("S3_SECRET_ACCESS_KEY", "")
        region = os.getenv("S3_REGION", "auto")

        _GLOBAL_STORAGE_BACKEND = S3StorageBackend(
            endpoint_url=endpoint_url,
            bucket_name=bucket_name,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region=region,
        )
        logger.info(f"Initialized storage backend: s3 (bucket: {bucket_name}, region: {region})")
        return _GLOBAL_STORAGE_BACKEND

    else:
        raise RuntimeError(
            f"CRITICAL CONFIGURATION ERROR: Invalid STORAGE_BACKEND '{backend_type}'. "
            f"Supported options are 'local' or 's3'."
        )


def reset_storage():
    """Resets the cached global storage backend instance (used for testing)."""
    global _GLOBAL_STORAGE_BACKEND
    _GLOBAL_STORAGE_BACKEND = None
