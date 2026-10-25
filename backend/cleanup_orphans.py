#!/usr/bin/env python3
"""
Safe Orphan PDF Cleanup Tool for XeroxQueue

Safety Features:
- Dry-run by default (--dry-run). Requires explicit --execute flag to perform deletion.
- Fails closed: If database connection or inspection fails, halts immediately with no deletions.
- Grace period: Preserves recently uploaded files (default: 2 hours) to avoid race conditions with in-flight uploads.
- Directory containment: Restricts operations to the target uploads directory and refuses to follow symlinks.
- Comprehensive reporting: Summarizes scanned, referenced, protected, and orphaned files.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional, Set
import pymysql
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME", "xeroxqueue_db"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def fetch_referenced_filenames(conn) -> Set[str]:
    """
    Queries the database for all stored_file_name values referenced in
    uploaded_documents and print_orders. Fails closed if query errors.
    """
    referenced = set()
    with conn.cursor() as cursor:
        # Check uploaded_documents
        cursor.execute(
            """
            SELECT DISTINCT stored_file_name
            FROM uploaded_documents
            WHERE stored_file_name IS NOT NULL AND stored_file_name != ''
            """
        )
        for row in cursor.fetchall():
            name = row.get("stored_file_name")
            if name:
                referenced.add(str(name).strip())

        # Check print_orders
        cursor.execute(
            """
            SELECT DISTINCT stored_file_name
            FROM print_orders
            WHERE stored_file_name IS NOT NULL AND stored_file_name != ''
            """
        )
        for row in cursor.fetchall():
            name = row.get("stored_file_name")
            if name:
                referenced.add(str(name).strip())

    return referenced


def scan_and_clean_orphans(
    uploads_dir: Path,
    referenced_files: Set[str],
    grace_hours: float = 2.0,
    execute: bool = False,
) -> dict:
    """
    Scans the uploads directory, identifies orphaned files, and optionally deletes them.
    Returns a summary dictionary.
    """
    uploads_dir = uploads_dir.resolve()
    if not uploads_dir.exists() or not uploads_dir.is_dir():
        raise FileNotFoundError(f"Uploads directory does not exist or is not a directory: {uploads_dir}")

    now = time.time()
    grace_seconds = grace_hours * 3600.0

    summary = {
        "scanned": 0,
        "referenced_matches": 0,
        "grace_protected": 0,
        "orphans_found": 0,
        "orphans_deleted": 0,
        "bytes_freed": 0,
        "skipped_symlinks": 0,
        "orphan_files": [],
    }

    for entry in sorted(uploads_dir.iterdir()):
        # Only process files directly within uploads_dir
        if entry.is_symlink():
            summary["skipped_symlinks"] += 1
            continue

        if not entry.is_file():
            continue

        # Ignore hidden/metadata files like .gitkeep or .DS_Store
        if entry.name.startswith("."):
            continue

        summary["scanned"] += 1
        filename = entry.name

        # Check if referenced in database
        if filename in referenced_files:
            summary["referenced_matches"] += 1
            continue

        # Check age against grace period
        try:
            stat = entry.stat()
            file_age_seconds = now - stat.st_mtime
            file_size = stat.st_size
        except OSError:
            continue

        if file_age_seconds < grace_seconds:
            summary["grace_protected"] += 1
            continue

        # File is an unreferenced orphan older than grace period
        summary["orphans_found"] += 1
        summary["bytes_freed"] += file_size
        summary["orphan_files"].append(
            {
                "filename": filename,
                "size_bytes": file_size,
                "age_hours": round(file_age_seconds / 3600.0, 2),
            }
        )

        if execute:
            try:
                entry.unlink()
                summary["orphans_deleted"] += 1
            except OSError as exc:
                print(f"[ERROR] Failed to delete {filename}: {exc}", file=sys.stderr)

    return summary


def run_cleanup(
    backend: Optional[str] = None,
    uploads_dir: Optional[Path] = None,
    grace_hours: float = 2.0,
    execute: bool = False,
    conn=None,
) -> dict:
    """
    Storage-aware orphan cleanup runner.
    If targeting S3, bulk deletion is disabled by policy to prevent accidental data loss.
    If targeting local, performs safe filesystem scan and cleanup.
    """
    if backend is None:
        backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    else:
        backend = backend.strip().lower()

    if backend == "s3":
        return {
            "status": "disabled",
            "backend": "s3",
            "message": "S3 cloud object cleanup is intentionally disabled to safeguard cloud assets against accidental bulk deletion.",
            "orphans_found": 0,
            "orphans_deleted": 0,
            "bytes_freed": 0,
        }

    # Local backend cleanup
    if uploads_dir is None:
        uploads_dir = Path(__file__).resolve().parent / "uploads"
    uploads_path = Path(uploads_dir).resolve()

    should_close_conn = False
    if conn is None:
        conn = get_db_connection()
        should_close_conn = True

    try:
        referenced = fetch_referenced_filenames(conn)
    finally:
        if should_close_conn:
            conn.close()

    summary = scan_and_clean_orphans(
        uploads_dir=uploads_path,
        referenced_files=referenced,
        grace_hours=grace_hours,
        execute=execute,
    )
    summary["backend"] = "local"
    summary["status"] = "success"
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Safely identify and remove orphaned PDF uploads in XeroxQueue.",
    )
    parser.add_argument(
        "--backend",
        choices=["local", "s3"],
        default=None,
        help="Storage backend to target (defaults to STORAGE_BACKEND env var or 'local')",
    )
    parser.add_argument(
        "--uploads-dir",
        type=str,
        default=str(Path(__file__).resolve().parent / "uploads"),
        help="Path to uploads directory for local backend (default: backend/uploads)",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=2.0,
        help="Minimum age in hours to qualify for cleanup (default: 2.0)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate cleanup without deleting any files (default)",
    )
    group.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually delete confirmed orphaned files",
    )

    args = parser.parse_args()
    execute_mode = args.execute
    target_backend = (args.backend or os.getenv("STORAGE_BACKEND", "local")).strip().lower()

    print("==================================================")
    print(" XeroxQueue — Orphan PDF Cleanup Tool")
    print("==================================================")
    print(f" Target backend: {target_backend}")
    print(f" Mode:           {'[LIVE EXECUTE - DELETION ENABLED]' if execute_mode else '[DRY-RUN - NO FILES WILL BE DELETED]'}")
    print("--------------------------------------------------")

    if target_backend == "s3":
        print("[STORAGE NOTICE] Active storage backend is S3 / Cloud Object Storage.")
        print("Automatic bulk deletion of cloud objects is intentionally DISABLED")
        print("to safeguard production assets against accidental, irreversible loss.")
        print("To manage cloud storage retention safely, use cloud provider lifecycle rules")
        print("(e.g., Cloudflare R2 bucket lifecycle rules) or manually verified prefix deletion.")
        print("No S3 objects were deleted or modified.")
        print("==================================================")
        sys.exit(0)

    print(f" Uploads dir:    {args.uploads_dir}")
    print(f" Grace period:   {args.grace_hours} hours")
    print("--------------------------------------------------")

    # Step 1: Connect to database (fails closed)
    print("[1/3] Connecting to database to fetch referenced files...")
    try:
        conn = get_db_connection()
        try:
            referenced = fetch_referenced_filenames(conn)
            print(f"      Success: Found {len(referenced)} referenced PDF files in database.")
        finally:
            conn.close()
    except Exception as exc:
        print(f"[FAIL-CLOSED] Database inspection failed: {exc}", file=sys.stderr)
        print("Aborting orphan cleanup to prevent accidental data loss.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Scan uploads directory
    uploads_path = Path(args.uploads_dir)
    print(f"[2/3] Scanning filesystem directory: {uploads_path}...")
    try:
        summary = scan_and_clean_orphans(
            uploads_dir=uploads_path,
            referenced_files=referenced,
            grace_hours=args.grace_hours,
            execute=execute_mode,
        )
    except Exception as exc:
        print(f"[ERROR] Scan failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Print summary report
    print("[3/3] Cleanup summary:")
    print(f"      Files scanned:            {summary['scanned']}")
    print(f"      Database-referenced:      {summary['referenced_matches']}")
    print(f"      Grace-period protected:   {summary['grace_protected']}")
    print(f"      Symlinks skipped:         {summary['skipped_symlinks']}")
    print(f"      Orphan files identified:  {summary['orphans_found']}")
    if execute_mode:
        print(f"      Orphan files deleted:     {summary['orphans_deleted']}")
        print(f"      Disk space reclaimed:     {summary['bytes_freed'] / (1024*1024):.2f} MB")
    else:
        print(f"      Files eligible for deletion: {summary['orphans_found']}")
        print(f"      Disk space reclaimable:      {summary['bytes_freed'] / (1024*1024):.2f} MB")
        print("      No files were deleted (dry-run mode). Use --execute to delete.")

    if summary["orphan_files"] and not execute_mode:
        print("\nIdentified Orphan Candidates:")
        for orphan in summary["orphan_files"][:10]:
            print(f"  - {orphan['filename']} ({orphan['size_bytes'] / 1024:.1f} KB, age: {orphan['age_hours']}h)")
        if len(summary["orphan_files"]) > 10:
            print(f"  ... and {len(summary['orphan_files']) - 10} more.")

    print("==================================================")


if __name__ == "__main__":
    main()
