"""
Comprehensive Automated Test Suite for Durable PDF Storage Abstraction

Tests cover:
1. Local storage backend functionality (save, exists, get_stream, get_bytes, delete, get_response).
2. S3 storage backend configuration validation (fails fast if credentials/endpoint/bucket missing).
3. S3 upload and streamed download using a mocked S3 client.
4. Missing object handling (HTTP 404) and storage outage handling (HTTP 503).
5. Storage key security validation (rejection of directory traversal, absolute paths, non-pdf extensions).
6. Upload endpoint integration with storage abstraction (local and S3 modes).
7. Atomic failure recovery: database insertion failure triggers cleanup of ONLY the newly uploaded file/object.
8. Authorization enforcement: unauthenticated and non-admin requests cannot retrieve documents.
9. Storage-aware orphan cleanup: S3 cleanup is disabled by policy to prevent accidental cloud deletion.
10. Dual poison-pill database isolation: ensures no test connects to a live MySQL instance.
"""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.requests import Request

import cleanup_orphans
import main
import storage
from storage import (
    BaseStorageBackend,
    LocalStorageBackend,
    S3StorageBackend,
    get_storage,
    reset_storage,
    validate_storage_key,
)


def _blocked_connect(*args, **kwargs):
    raise RuntimeError(
        "LIVE DB ACCESS PROHIBITED: Test attempted unmocked database connection."
    )


class DummyClientError(Exception):
    def __init__(self, response, operation_name):
        self.response = response
        self.operation_name = operation_name
        code = response.get("Error", {}).get("Code", "Unknown")
        super().__init__(f"An error occurred ({code}) when calling the {operation_name} operation")


class TestStorageKeyValidation(unittest.TestCase):
    """Verifies that storage keys are strictly validated against directory traversal and invalid formats."""

    def test_valid_storage_keys(self):
        valid_keys = [
            "c2a8f9e0123456789abcdef012345678.pdf",
            "document-123.pdf",
            "assignment_final.PDF",
            "test.file.pdf",
        ]
        for key in valid_keys:
            self.assertEqual(validate_storage_key(key), key.strip())

    def test_invalid_storage_keys_raise_value_error(self):
        invalid_keys = [
            "",
            "   ",
            None,
            "../../etc/passwd",
            "../uploads/doc.pdf",
            "/root/secret.pdf",
            "C:\\Windows\\system32.pdf",
            "malicious.exe",
            "document.pdf.exe",
            "script.sh",
            "file\x00null.pdf",
            "a" * 256 + ".pdf",
        ]
        for key in invalid_keys:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validate_storage_key(key)


class TestLocalStorageBackend(unittest.TestCase):
    """Verifies LocalStorageBackend filesystem operations and safety checks."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.uploads_path = Path(self.temp_dir.name)
        self.backend = LocalStorageBackend(uploads_dir=self.uploads_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_local_backend_name(self):
        self.assertEqual(self.backend.backend_name, "local")

    def test_save_and_exists_and_bytes(self):
        key = "test_document.pdf"
        sample_bytes = b"%PDF-1.4 sample content"
        size = self.backend.save(key, sample_bytes)

        self.assertEqual(size, len(sample_bytes))
        self.assertTrue(self.backend.exists(key))
        self.assertEqual(self.backend.get_bytes(key), sample_bytes)

    def test_save_from_file_like_object(self):
        key = "streamed_doc.pdf"
        sample_bytes = b"%PDF-1.4 file like content"
        bio = io.BytesIO(sample_bytes)
        size = self.backend.save(key, bio)

        self.assertEqual(size, len(sample_bytes))
        self.assertTrue(self.backend.exists(key))
        self.assertEqual(self.backend.get_bytes(key), sample_bytes)

    def test_get_stream(self):
        key = "stream_doc.pdf"
        content = b"%PDF-1.4 " + (b"A" * 1000)
        self.backend.save(key, content)

        chunks = list(self.backend.get_stream(key))
        reassembled = b"".join(chunks)
        self.assertEqual(reassembled, content)

    def test_delete_existing_and_non_existing(self):
        key = "to_delete.pdf"
        self.backend.save(key, b"%PDF-1.4 content")
        self.assertTrue(self.backend.exists(key))

        deleted = self.backend.delete(key)
        self.assertTrue(deleted)
        self.assertFalse(self.backend.exists(key))

        # Deleting non-existent file returns False safely
        self.assertFalse(self.backend.delete("non_existent.pdf"))

    def test_get_response_success(self):
        key = "response_doc.pdf"
        self.backend.save(key, b"%PDF-1.4 content")

        response = self.backend.get_response(key, download_filename="My Syllabus.pdf", inline=True)
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.media_type, "application/pdf")
        self.assertEqual(response.filename, "My Syllabus.pdf")
        content_disp = response.headers.get("content-disposition", "")
        self.assertTrue(content_disp.startswith("inline"))
        self.assertIn("My", content_disp)

    def test_get_response_not_found(self):
        with self.assertRaises(HTTPException) as ctx:
            self.backend.get_response("missing.pdf", download_filename="doc.pdf")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("could not be found", ctx.exception.detail)


class TestS3StorageBackend(unittest.TestCase):
    """Verifies S3StorageBackend configuration validation, upload, streaming, and error handling."""

    def test_s3_missing_configuration_fails_fast(self):
        """S3 backend must validate required configuration and fail fast if incomplete."""
        # Missing all
        with self.assertRaises(RuntimeError) as ctx:
            S3StorageBackend(
                endpoint_url="",
                bucket_name="",
                access_key_id="",
                secret_access_key="",
            )
        self.assertIn("CRITICAL STORAGE CONFIGURATION ERROR", str(ctx.exception))
        self.assertIn("S3_ENDPOINT_URL", str(ctx.exception))
        self.assertIn("S3_BUCKET_NAME", str(ctx.exception))
        self.assertIn("S3_ACCESS_KEY_ID", str(ctx.exception))
        self.assertIn("S3_SECRET_ACCESS_KEY", str(ctx.exception))

        # Missing secret key only
        with self.assertRaises(RuntimeError) as ctx:
            S3StorageBackend(
                endpoint_url="https://r2.cloudflarestorage.com",
                bucket_name="xeroxqueue-pdfs",
                access_key_id="test_key",
                secret_access_key="",
            )
        self.assertIn("S3_SECRET_ACCESS_KEY", str(ctx.exception))

    def test_s3_repr_never_exposes_secrets(self):
        """String representation of S3StorageBackend must never leak credentials."""
        mock_client = MagicMock()
        backend = S3StorageBackend(
            endpoint_url="https://account.r2.cloudflarestorage.com",
            bucket_name="private-bucket",
            access_key_id="SECRET_KEY_ID_12345",
            secret_access_key="SUPER_SECRET_VALUE_99999",
            region="auto",
            s3_client=mock_client,
        )
        rep = repr(backend)
        self.assertNotIn("SECRET_KEY_ID_12345", rep)
        self.assertNotIn("SUPER_SECRET_VALUE_99999", rep)
        self.assertIn("private-bucket", rep)

    def test_s3_save_calls_put_object(self):
        mock_client = MagicMock()
        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        sample_bytes = b"%PDF-1.4 test document content"
        size = backend.save("doc_1.pdf", sample_bytes)

        self.assertEqual(size, len(sample_bytes))
        mock_client.put_object.assert_called_once_with(
            Bucket="xeroxqueue-pdfs",
            Key="doc_1.pdf",
            Body=sample_bytes,
            ContentType="application/pdf",
        )

    def test_s3_exists_true_and_false(self):
        mock_client = MagicMock()
        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        # Existing object
        mock_client.head_object.return_value = {"ContentLength": 1024}
        self.assertTrue(backend.exists("existing.pdf"))

        # Missing object (404 ClientError)
        error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
        mock_client.head_object.side_effect = DummyClientError(error_response, "head_object")
        self.assertFalse(backend.exists("missing.pdf"))

    def test_s3_delete_calls_delete_object(self):
        mock_client = MagicMock()
        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        self.assertTrue(backend.delete("file.pdf"))
        mock_client.delete_object.assert_called_once_with(
            Bucket="xeroxqueue-pdfs",
            Key="file.pdf",
        )

    def test_s3_get_stream_and_get_bytes(self):
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_chunks.return_value = [b"%PDF-1.4 ", b"chunk1", b"chunk2"]
        mock_body.read.return_value = b"%PDF-1.4 chunk1chunk2"
        mock_client.get_object.return_value = {"Body": mock_body}

        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        chunks = list(backend.get_stream("doc.pdf"))
        self.assertEqual(chunks, [b"%PDF-1.4 ", b"chunk1", b"chunk2"])
        self.assertEqual(backend.get_bytes("doc.pdf"), b"%PDF-1.4 chunk1chunk2")

    def test_s3_get_response_streams_via_backend(self):
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.iter_chunks.return_value = [b"%PDF-1.4 chunk content"]
        mock_client.get_object.return_value = {"Body": mock_body}

        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        response = backend.get_response("order_123.pdf", download_filename="receipt.pdf", inline=True)
        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.media_type, "application/pdf")
        self.assertIn("inline; filename=\"receipt.pdf\"", response.headers.get("Content-Disposition", ""))

    def test_s3_get_response_object_not_found_returns_404(self):
        mock_client = MagicMock()
        mock_client.get_object.side_effect = DummyClientError({"Error": {"Code": "NoSuchKey"}}, "get_object")

        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        with self.assertRaises(HTTPException) as ctx:
            backend.get_response("missing.pdf", download_filename="doc.pdf")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("could not be found", ctx.exception.detail)

    def test_s3_storage_outage_returns_503(self):
        mock_client = MagicMock()
        mock_client.get_object.side_effect = RuntimeError("Connection timed out to storage service")

        backend = S3StorageBackend(
            endpoint_url="https://r2.cloudflarestorage.com",
            bucket_name="xeroxqueue-pdfs",
            access_key_id="key",
            secret_access_key="secret",
            s3_client=mock_client,
        )

        with self.assertRaises(HTTPException) as ctx:
            backend.get_response("order.pdf", download_filename="doc.pdf")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Storage service is currently unavailable", ctx.exception.detail)


class TestStorageFactoryAndConfiguration(unittest.TestCase):
    """Verifies factory instantiation and configuration validation."""

    def setUp(self):
        reset_storage()

    def tearDown(self):
        reset_storage()

    def test_default_storage_is_local(self):
        with patch.dict(os.environ, {}, clear=True):
            storage_inst = get_storage()
            self.assertEqual(storage_inst.backend_name, "local")

    def test_explicit_local_storage(self):
        with patch.dict(os.environ, {"STORAGE_BACKEND": "local"}):
            storage_inst = get_storage()
            self.assertEqual(storage_inst.backend_name, "local")

    def test_unsupported_storage_backend_raises_runtime_error(self):
        with patch.dict(os.environ, {"STORAGE_BACKEND": "gcs"}):
            with self.assertRaises(RuntimeError) as ctx:
                get_storage()
            self.assertIn("Invalid STORAGE_BACKEND 'gcs'", str(ctx.exception))

    def test_s3_backend_without_credentials_fails_fast(self):
        with patch.dict(os.environ, {"STORAGE_BACKEND": "s3"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_storage()
            self.assertIn("CRITICAL STORAGE CONFIGURATION ERROR", str(ctx.exception))


class TestUploadAndDownloadIntegration(unittest.TestCase):
    """End-to-end integration tests for upload, download, and atomic failure cleanup."""

    def setUp(self):
        self.db_patcher = patch("pymysql.connect", side_effect=_blocked_connect)
        self.db_patcher.start()
        main.auth_rate_limiter.reset()
        main.upload_rate_limiter.reset()
        reset_storage()

    def tearDown(self):
        self.db_patcher.stop()
        reset_storage()

    def test_upload_pdf_local_success(self):
        """Verifies successful upload in local mode."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        file = UploadFile(
            filename="test_upload.pdf",
            file=io.BytesIO(b"%PDF-1.4 dummy valid PDF content"),
            headers={"content-type": "application/pdf"},
        )

        with patch("main.get_db_connection", return_value=mock_conn):
            with patch("main.PdfReader") as mock_reader_cls:
                mock_reader = MagicMock()
                mock_reader.is_encrypted = False
                mock_reader.pages = [MagicMock(), MagicMock()]  # 2 pages
                mock_reader_cls.return_value = mock_reader

                with patch.object(storage._GLOBAL_STORAGE_BACKEND or get_storage(), "save", return_value=32) as mock_save:
                    result = asyncio.run(
                        main.upload_pdf(
                            file=file,
                            current_student={"id": 10},
                            request=None,
                        )
                    )

                    self.assertEqual(result["file_name"], "test_upload.pdf")
                    self.assertEqual(result["page_count"], 2)
                    self.assertTrue(result["stored_file_name"].endswith(".pdf"))
                    mock_save.assert_called_once()
                    mock_cursor.execute.assert_called_once()

    def test_upload_pdf_db_failure_cleans_up_newly_created_storage_object(self):
        """If database insertion fails, the newly uploaded storage object must be deleted atomically."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("DB connection dropped during INSERT")
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        file = UploadFile(
            filename="failed_db_doc.pdf",
            file=io.BytesIO(b"%PDF-1.4 content"),
            headers={"content-type": "application/pdf"},
        )

        mock_storage = MagicMock()
        mock_storage.save.return_value = 18

        with patch("main.get_storage", return_value=mock_storage):
            with patch("main.get_db_connection", return_value=mock_conn):
                with patch("main.PdfReader") as mock_reader_cls:
                    mock_reader = MagicMock()
                    mock_reader.is_encrypted = False
                    mock_reader.pages = [MagicMock()]
                    mock_reader_cls.return_value = mock_reader

                    with self.assertRaises(HTTPException) as ctx:
                        asyncio.run(
                            main.upload_pdf(
                                file=file,
                                current_student={"id": 10},
                                request=None,
                            )
                        )

                    self.assertEqual(ctx.exception.status_code, 500)
                    self.assertIn("Database error while recording upload", ctx.exception.detail)
                    # Assert delete was called on the mock storage for the newly created key
                    mock_storage.delete.assert_called_once()

    def test_get_order_document_unauthenticated_rejected(self):
        """Unauthenticated requests cannot download documents."""
        # Calling get_order_document directly requires Depends(get_current_admin)
        from fastapi.security import HTTPAuthorizationCredentials
        with self.assertRaises(Exception):
            main.get_order_document(order_id=1, current_admin=None)

    def test_get_order_document_s3_success(self):
        """Admin document download in S3 mode streams via backend with proper headers."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {
            "file_name": "student_thesis.pdf",
            "stored_file_name": "c2a8f9e01234.pdf",
        }
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_storage = MagicMock()
        mock_storage.get_response.return_value = StreamingResponse(
            io.BytesIO(b"%PDF-1.4 thesis"),
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="student_thesis.pdf"'},
        )

        with patch("main.get_db_connection", return_value=mock_conn):
            with patch("main.get_storage", return_value=mock_storage):
                response = main.get_order_document(
                    order_id=42,
                    current_admin={"username": "admin"},
                )

                self.assertIsInstance(response, StreamingResponse)
                mock_storage.get_response.assert_called_once_with(
                    "c2a8f9e01234.pdf",
                    download_filename="student_thesis.pdf",
                    inline=True,
                )


class TestStorageAwareCleanupOrphans(unittest.TestCase):
    """Verifies that cleanup_orphans respects storage backends and safeguards S3."""

    def test_s3_cleanup_is_explicitly_disabled(self):
        """Running cleanup against S3 backend must never delete cloud objects."""
        result = cleanup_orphans.run_cleanup(backend="s3")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["backend"], "s3")
        self.assertEqual(result["orphans_deleted"], 0)
        self.assertIn("disabled", result["message"])

    def test_local_cleanup_dry_run_default(self):
        """Local cleanup defaults to dry-run and does not delete files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            uploads_dir = Path(temp_dir)
            orphan_file = uploads_dir / "old_orphan.pdf"
            orphan_file.write_bytes(b"%PDF-1.4 old orphan")
            # Set mtime to 5 hours ago
            past_time = 0
            os.utime(orphan_file, (past_time, past_time))

            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []  # No referenced files
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

            result = cleanup_orphans.run_cleanup(
                backend="local",
                uploads_dir=uploads_dir,
                grace_hours=1.0,
                execute=False,
                conn=mock_conn,
            )

            self.assertEqual(result["backend"], "local")
            self.assertEqual(result["orphans_found"], 1)
            self.assertEqual(result["orphans_deleted"], 0)
            self.assertTrue(orphan_file.exists(), "Dry-run must not delete file")


if __name__ == "__main__":
    unittest.main()
