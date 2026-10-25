"""
Automated Hardening and Resilience Regression Test Suite for XeroxQueue.

Verifies:
1. Security headers middleware (nosniff, DENY, Referrer-Policy, XSS protection, permissions).
2. Request ID middleware (preserves custom X-Request-ID, generates UUID if absent).
3. Readiness probe (/ready, /api/ready): returns 200 when DB is up, 503 when DB is down.
4. Layered PDF validation: rejects spoofed non-PDF files claiming to be .pdf.
5. In-process rate limiting: SlidingWindowRateLimiter limits rapid requests and sets retry-after.
6. Order status state machine: blocks invalid state changes out of cancelled status.
7. Admin action audit logging: records structured JSON audit trail.
8. Safe orphan PDF cleanup tool:
   - Dry-run mode protects all files.
   - Database-referenced files are preserved.
   - Recent files within grace period are preserved.
   - Symlinks are never followed or deleted.
   - Fails closed if database connection fails.
9. Dual poison pill database isolation: proves unmocked MySQL access fails immediately.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import load_dotenv

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import cleanup_orphans
import main


def _blocked_connect(*args, **kwargs):
    raise RuntimeError(
        "LIVE DB ACCESS PROHIBITED: Test attempted unmocked database connection."
    )


class TestHardeningAndResilience(unittest.TestCase):
    """Automated regression tests for application hardening, security, and resilience."""

    def setUp(self):
        # Enforce dual poison pill
        self.db_patcher = patch("pymysql.connect", side_effect=_blocked_connect)
        self.db_patcher.start()
        main.auth_rate_limiter.reset()
        main.upload_rate_limiter.reset()

    def tearDown(self):
        self.db_patcher.stop()

    # -----------------------------------------------------------------------
    # 0. Database Isolation Guard
    # -----------------------------------------------------------------------

    def test_00_database_isolation_enforced(self):
        """0. Verify that unmocked database calls fail immediately without touching MySQL."""
        with self.assertRaises(RuntimeError) as ctx:
            main.get_db_connection()
        self.assertIn("LIVE DB ACCESS PROHIBITED", str(ctx.exception))

    # -----------------------------------------------------------------------
    # 1. Security Headers Middleware
    # -----------------------------------------------------------------------

    def test_01_security_headers_middleware(self):
        """1. Verify that security headers are injected into every HTTP response."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/orders",
            "headers": [(b"host", b"testserver")],
        }
        req = Request(scope)

        async def dummy_next(r):
            return Response(content="ok", media_type="text/plain")

        response = asyncio.run(main.security_and_tracing_middleware(req, dummy_next))

        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            response.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin"
        )
        self.assertEqual(response.headers.get("X-XSS-Protection"), "1; mode=block")
        self.assertIn("geolocation=()", response.headers.get("Permissions-Policy", ""))

    # -----------------------------------------------------------------------
    # 2. Request ID Middleware
    # -----------------------------------------------------------------------

    def test_02_request_id_middleware(self):
        """2. Verify Request ID propagation and generation."""
        # Case A: Client provides custom Request ID
        scope_with_id = {
            "type": "http",
            "method": "GET",
            "path": "/api/orders",
            "headers": [
                (b"host", b"testserver"),
                (b"x-request-id", b"custom-trace-id-999"),
            ],
        }
        req_a = Request(scope_with_id)

        async def dummy_next(r):
            return Response(content="ok", media_type="text/plain")

        res_a = asyncio.run(main.security_and_tracing_middleware(req_a, dummy_next))
        self.assertEqual(res_a.headers.get("X-Request-ID"), "custom-trace-id-999")

        # Case B: Client does not provide Request ID -> system generates valid hex ID
        scope_without_id = {
            "type": "http",
            "method": "GET",
            "path": "/api/orders",
            "headers": [(b"host", b"testserver")],
        }
        req_b = Request(scope_without_id)
        res_b = asyncio.run(main.security_and_tracing_middleware(req_b, dummy_next))
        generated_id = res_b.headers.get("X-Request-ID")
        self.assertIsNotNone(generated_id)
        self.assertEqual(len(generated_id), 32)  # uuid4 hex length

    # -----------------------------------------------------------------------
    # 3. Readiness Probe Endpoints
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_03_readiness_probe_healthy(self, mock_conn):
        """3. When database ping succeeds, /ready and /api/ready return HTTP 200 ready."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"1": 1}
        mock_conn_inst = MagicMock()
        mock_conn_inst.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.return_value = mock_conn_inst

        # Test both /ready and /api/ready route registrations
        routes = {
            getattr(r, "path", None): getattr(r, "endpoint", None)
            for r in main.app.routes
        }
        self.assertIn("/ready", routes)
        self.assertIn("/api/ready", routes)

        response = main.readiness_check()
        self.assertEqual(
            response,
            {
                "status": "ready",
                "service": "XeroxQueue API",
                "database": "connected",
            },
        )
        mock_cursor.execute.assert_called_with("SELECT 1")
        mock_conn_inst.close.assert_called_once()

    @patch("main.get_db_connection")
    def test_04_readiness_probe_database_down(self, mock_conn):
        """4. When database connection fails, /ready returns HTTP 503 Service Unavailable."""
        mock_conn.side_effect = RuntimeError("Could not connect to MySQL server")

        response = main.readiness_check()
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 503)

        import json

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["database"], "disconnected")
        self.assertIn("Database service is not ready", body["error"])

    # -----------------------------------------------------------------------
    # 4. Layered PDF Validation
    # -----------------------------------------------------------------------

    def test_05_pdf_upload_spoofed_file_rejected_by_magic_bytes(self):
        """5. A text file masquerading with .pdf extension is rejected by magic bytes check."""
        mock_student = {"id": 1, "full_name": "Test Student"}

        class FakeUploadFile:
            def __init__(self, filename, content):
                self.filename = filename
                self.content_type = "application/pdf"
                self._content = content
                self._read = False

            async def read(self, size):
                if not self._read:
                    self._read = True
                    return self._content
                return b""

            async def close(self):
                pass

        # Text file pretending to be PDF
        fake_pdf = FakeUploadFile(
            "fake_document.pdf", b"This is plain ASCII text pretending to be a PDF"
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.upload_pdf(file=fake_pdf, current_student=mock_student))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid PDF file format", ctx.exception.detail)

    # -----------------------------------------------------------------------
    # 5. In-Process Rate Limiter
    # -----------------------------------------------------------------------

    def test_06_in_process_sliding_window_rate_limiter(self):
        """6. Verify rate limiter allows up to limit and blocks subsequent bursts."""
        limiter = main.SlidingWindowRateLimiter(
            requests_per_window=3, window_seconds=60
        )
        test_key = "test_ip_192.168.1.10"

        # First 3 attempts must be allowed
        allowed_1, rem_1, retry_1 = limiter.check_rate_limit(test_key)
        self.assertTrue(allowed_1)
        self.assertEqual(rem_1, 2)
        self.assertEqual(retry_1, 0)

        allowed_2, rem_2, retry_2 = limiter.check_rate_limit(test_key)
        self.assertTrue(allowed_2)
        self.assertEqual(rem_2, 1)

        allowed_3, rem_3, retry_3 = limiter.check_rate_limit(test_key)
        self.assertTrue(allowed_3)
        self.assertEqual(rem_3, 0)

        # 4th attempt must be rejected
        allowed_4, rem_4, retry_4 = limiter.check_rate_limit(test_key)
        self.assertFalse(allowed_4)
        self.assertEqual(rem_4, 0)
        self.assertGreaterEqual(retry_4, 1)

        # Different key is not affected
        allowed_other, _, _ = limiter.check_rate_limit("other_ip_192.168.1.20")
        self.assertTrue(allowed_other)

        # Reset clears counters
        limiter.reset()
        allowed_after_reset, _, _ = limiter.check_rate_limit(test_key)
        self.assertTrue(allowed_after_reset)

    # -----------------------------------------------------------------------
    # 6. Order Status Transition State Machine Guard
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_07_cannot_uncancel_cancelled_order(self, mock_conn):
        """7. Verifies that an order in 'cancelled' status cannot be transitioned back to pending."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"id": 101, "status": "cancelled"}
        mock_conn_inst = MagicMock()
        mock_conn_inst.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.return_value = mock_conn_inst

        admin = {"username": "admin"}
        update = main.OrderStatusUpdate(status="pending")

        with self.assertRaises(HTTPException) as ctx:
            main.update_order_status(order_id=101, update=update, current_admin=admin)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Cannot change status of cancelled order #101", ctx.exception.detail)
        mock_conn_inst.rollback.assert_called()

    @patch("main.get_db_connection")
    def test_08_order_status_idempotent_no_op(self, mock_conn):
        """8. Updating order to its current status is an idempotent no-op without re-writing SQL."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"id": 102, "status": "printing"}
        mock_conn_inst = MagicMock()
        mock_conn_inst.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.return_value = mock_conn_inst

        admin = {"username": "admin"}
        update = main.OrderStatusUpdate(status="printing")

        res = main.update_order_status(
            order_id=102, update=update, current_admin=admin
        )
        self.assertEqual(res["status"], "printing")
        self.assertEqual(res["order_id"], 102)

    # -----------------------------------------------------------------------
    # 7. Admin Action Audit Logging
    # -----------------------------------------------------------------------

    def test_09_admin_action_audit_logging(self):
        """9. Verify log_admin_action records structured entries into audit logger."""
        with patch.object(main.admin_audit_logger, "info") as mock_log:
            main.log_admin_action(
                admin_username="superadmin",
                action="test_audit_event",
                target_type="print_order",
                target_id=777,
                details={"reason": "audit test verification"},
            )
            mock_log.assert_called_once()
            log_line = mock_log.call_args[0][0]

            import json

            entry = json.loads(log_line)
            self.assertEqual(entry["admin_username"], "superadmin")
            self.assertEqual(entry["action"], "test_audit_event")
            self.assertEqual(entry["target_type"], "print_order")
            self.assertEqual(entry["target_id"], "777")
            self.assertEqual(entry["details"]["reason"], "audit test verification")
            self.assertIn("timestamp", entry)

    # -----------------------------------------------------------------------
    # 8. Safe Orphan PDF Cleanup Tool Tests
    # -----------------------------------------------------------------------

    def test_10_orphan_cleanup_dry_run_preserves_all_files(self):
        """10. In dry-run mode, orphan cleanup identifies candidates but deletes zero files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            uploads_dir = Path(temp_dir)

            # Create test files
            doc1 = uploads_dir / "referenced_doc.pdf"
            doc1.write_bytes(b"%PDF-1.4 dummy content")

            doc2 = uploads_dir / "old_orphan.pdf"
            doc2.write_bytes(b"%PDF-1.4 dummy content")
            # Set mtime to 5 hours ago
            past_time = time.time() - (5 * 3600)
            os.utime(doc2, (past_time, past_time))

            doc3 = uploads_dir / "fresh_uncommitted.pdf"
            doc3.write_bytes(b"%PDF-1.4 dummy content")
            # Set mtime to 10 minutes ago (within 2h grace period)
            recent_time = time.time() - (600)
            os.utime(doc3, (recent_time, recent_time))

            referenced = {"referenced_doc.pdf"}

            # Dry-run execution
            summary = cleanup_orphans.scan_and_clean_orphans(
                uploads_dir=uploads_dir,
                referenced_files=referenced,
                grace_hours=2.0,
                execute=False,
            )

            self.assertEqual(summary["scanned"], 3)
            self.assertEqual(summary["referenced_matches"], 1)
            self.assertEqual(summary["grace_protected"], 1)
            self.assertEqual(summary["orphans_found"], 1)
            self.assertEqual(summary["orphans_deleted"], 0)  # Dry-run!

            # Assert all 3 files still exist on disk
            self.assertTrue(doc1.exists())
            self.assertTrue(doc2.exists())
            self.assertTrue(doc3.exists())

    def test_11_orphan_cleanup_execute_mode_deletes_only_eligible_orphans(self):
        """11. In execute mode, only unreferenced files older than grace period are deleted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            uploads_dir = Path(temp_dir)

            doc1 = uploads_dir / "referenced_doc.pdf"
            doc1.write_bytes(b"%PDF-1.4 dummy content")

            doc2 = uploads_dir / "old_orphan.pdf"
            doc2.write_bytes(b"%PDF-1.4 dummy content")
            past_time = time.time() - (5 * 3600)
            os.utime(doc2, (past_time, past_time))

            doc3 = uploads_dir / "fresh_uncommitted.pdf"
            doc3.write_bytes(b"%PDF-1.4 dummy content")
            recent_time = time.time() - (600)
            os.utime(doc3, (recent_time, recent_time))

            referenced = {"referenced_doc.pdf"}

            # Execute mode
            summary = cleanup_orphans.scan_and_clean_orphans(
                uploads_dir=uploads_dir,
                referenced_files=referenced,
                grace_hours=2.0,
                execute=True,
            )

            self.assertEqual(summary["orphans_found"], 1)
            self.assertEqual(summary["orphans_deleted"], 1)

            # Assert doc2 is deleted, while doc1 and doc3 are preserved
            self.assertTrue(doc1.exists(), "Referenced document must not be deleted")
            self.assertFalse(doc2.exists(), "Old orphan must be deleted")
            self.assertTrue(
                doc3.exists(), "Fresh upload within grace period must be preserved"
            )

    def test_12_orphan_cleanup_refuses_to_follow_symlinks(self):
        """12. Orphan cleanup refuses to follow symlinks to avoid directory traversal or data loss."""
        with tempfile.TemporaryDirectory() as temp_dir:
            uploads_dir = Path(temp_dir)
            outside_file = uploads_dir.parent / "important_outside_file.txt"
            outside_file.write_bytes(b"critical system data")

            symlink_file = uploads_dir / "suspicious_link.pdf"
            try:
                symlink_file.symlink_to(outside_file)
            except OSError:
                self.skipTest("Symlinks not supported on this platform")

            summary = cleanup_orphans.scan_and_clean_orphans(
                uploads_dir=uploads_dir,
                referenced_files=set(),
                grace_hours=0.0,
                execute=True,
            )

            self.assertEqual(summary["skipped_symlinks"], 1)
            self.assertEqual(summary["orphans_deleted"], 0)
            self.assertTrue(outside_file.exists(), "Outside file must not be affected")

    def test_13_orphan_cleanup_fails_closed_on_database_error(self):
        """13. If database inspection fails, fetch_referenced_filenames fails closed."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("Database connection timed out")
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        with self.assertRaises(RuntimeError) as ctx:
            cleanup_orphans.fetch_referenced_filenames(mock_conn)

        self.assertIn("Database connection timed out", str(ctx.exception))

    def test_14_get_client_identifier_rejects_spoofed_forwarded_for_from_untrusted_direct_peer(self):
        """14. Direct untrusted peers cannot spoof X-Forwarded-For to evade rate limits."""
        # Untrusted direct peer
        scope_untrusted = {
            "type": "http",
            "client": ("198.51.100.22", 54321),
            "headers": [(b"x-forwarded-for", b"203.0.113.195, 10.0.0.1")],
        }
        req_untrusted = Request(scope_untrusted)
        self.assertEqual(main.get_client_identifier(req_untrusted), "198.51.100.22")

        # Trusted direct proxy (e.g. localhost Nginx)
        scope_trusted = {
            "type": "http",
            "client": ("127.0.0.1", 54321),
            "headers": [(b"x-forwarded-for", b"203.0.113.195, 10.0.0.1")],
        }
        req_trusted = Request(scope_trusted)
        self.assertEqual(main.get_client_identifier(req_trusted), "203.0.113.195")

    def test_15_filename_sanitization_removes_control_characters(self):
        """15. Uploaded PDF filename is sanitized of null bytes and control chars before DB insertion."""
        from io import BytesIO
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        file = UploadFile(
            filename="assignment\x00\x1b[31m_final\x7f.pdf",
            file=BytesIO(b"%PDF-1.4 mock content"),
            headers={"content-type": "application/pdf"},
        )

        with patch("main.get_db_connection", return_value=mock_conn):
            with patch("pathlib.Path.open") as mock_open:
                mock_file = MagicMock()
                mock_open.return_value.__enter__.return_value = mock_file
                with patch("main.PdfReader") as mock_reader_cls:
                    mock_reader = MagicMock()
                    mock_reader.is_encrypted = False
                    mock_reader.pages = [MagicMock()]
                    mock_reader_cls.return_value = mock_reader

                    result = asyncio.run(
                        main.upload_pdf(
                            file=file,
                            current_student={"id": 42},
                            request=None,
                        )
                    )

                    self.assertEqual(result["file_name"], "assignment[31m_final.pdf")
                    mock_cursor.execute.assert_called_once()
                    insert_args = mock_cursor.execute.call_args[0][1]
                    self.assertEqual(insert_args[2], "assignment[31m_final.pdf")

    def test_16_get_db_connection_uses_default_db_name_and_timeouts(self):
        """16. Verify get_db_connection applies fallback DB_NAME and socket timeouts."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_NAME", None)
            with patch("pymysql.connect") as mock_connect:
                main.get_db_connection()
                mock_connect.assert_called_once()
                kwargs = mock_connect.call_args[1]
                self.assertEqual(kwargs.get("database"), "xeroxqueue_db")
                self.assertEqual(kwargs.get("connect_timeout"), 5)
                self.assertEqual(kwargs.get("read_timeout"), 10)
                self.assertEqual(kwargs.get("write_timeout"), 10)


if __name__ == "__main__":
    unittest.main()
