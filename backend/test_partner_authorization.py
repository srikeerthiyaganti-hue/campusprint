"""
Non-destructive In-Memory Authorization Tests for XeroxQueue Delivery Partners.
Tests authorization dependencies and GET /api/partner/jobs without touching the live MySQL database.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

# Ensure backend directory is in python path and backend/.env is loaded
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import main


class QueryAwareMockCursor:
    """In-memory database cursor mock that routes queries to specific dataset handlers."""

    def __init__(self, partners_db=None, jobs_db=None, existing_tables=None):
        self.partners_db = partners_db or {}
        self.jobs_db = jobs_db or []
        self.existing_tables = existing_tables or {"delivery_partners", "delivery_jobs", "print_orders"}
        self._current_result_one = None
        self._current_result_all = []
        self.executed_queries = []
        self.last_query = None
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute(self, query: str, params=None):
        self.last_query = query
        self.last_params = params
        self.executed_queries.append({"query": query, "params": params})
        self._current_result_one = None
        self._current_result_all = []

        clean_query = " ".join(query.strip().split())

        # 1. Schema check: INFORMATION_SCHEMA.TABLES
        if "FROM INFORMATION_SCHEMA.TABLES" in clean_query:
            table_name = params[1] if params and len(params) > 1 else None
            self._current_result_one = {"1": 1} if table_name in self.existing_tables else None
            return

        # 2. Partner lookup: SELECT * FROM delivery_partners WHERE id = %s
        if "FROM delivery_partners" in clean_query and "WHERE id = %s" in clean_query:
            partner_id = params[0] if params else None
            partner = self.partners_db.get(partner_id)
            self._current_result_one = dict(partner) if partner else None
            return

        # 3. Delivery jobs lookup: SELECT ... FROM delivery_jobs
        if "FROM delivery_jobs" in clean_query:
            self._current_result_all = [dict(j) for j in self.jobs_db]
            return

        raise RuntimeError(f"Unexpected unmocked SQL query executed in test: {query}")

    def fetchone(self):
        return self._current_result_one

    def fetchall(self):
        return self._current_result_all


class QueryAwareMockConnection:
    """In-memory database connection mock returning QueryAwareMockCursor."""

    def __init__(self, cursor_obj: QueryAwareMockCursor):
        self._cursor = cursor_obj

    def cursor(self):
        return self._cursor

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


class TestDeliveryPartnerAuthorization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Layer 1: Poison environment variables to invalid localhost port
        cls._orig_env = {
            "DB_HOST": os.environ.get("DB_HOST"),
            "DB_PORT": os.environ.get("DB_PORT"),
            "DB_USER": os.environ.get("DB_USER"),
            "DB_PASSWORD": os.environ.get("DB_PASSWORD"),
            "DB_NAME": os.environ.get("DB_NAME"),
        }
        os.environ["DB_HOST"] = "127.0.0.1"
        os.environ["DB_PORT"] = "9"  # Discard port, refuses connections
        os.environ["DB_USER"] = "prohibited_test_user"
        os.environ["DB_PASSWORD"] = "prohibited_test_password"
        os.environ["DB_NAME"] = "prohibited_test_db"

    @classmethod
    def tearDownClass(cls):
        # Restore environment variables
        for key, val in cls._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def setUp(self):
        # Layer 2: Poison pymysql.connect to immediately raise RuntimeError
        def _blocked_connect(*args, **kwargs):
            raise RuntimeError("LIVE DB ACCESS PROHIBITED: Test attempted an unmocked live MySQL connection!")

        self.db_guard = patch("pymysql.connect", side_effect=_blocked_connect)
        self.main_db_guard = patch("main.pymysql.connect", side_effect=_blocked_connect)
        self.db_guard.start()
        self.main_db_guard.start()
        self.addCleanup(self.db_guard.stop)
        self.addCleanup(self.main_db_guard.stop)

    def test_00_database_isolation_enforced(self):
        """0. Prove that unmocked database calls fail immediately without touching MySQL."""
        with self.assertRaises(RuntimeError) as ctx:
            main.get_db_connection()
        self.assertIn("LIVE DB ACCESS PROHIBITED", str(ctx.exception))

    def test_01_unauthenticated_request(self):
        """1. Verify unauthenticated requests are rejected by HTTPBearer and dependency guard."""
        # A. Route inspection: confirm GET /api/partner/jobs exists
        route = next(
            (r for r in main.app.routes if getattr(r, "path", None) == "/api/partner/jobs" and "GET" in r.methods),
            None,
        )
        self.assertIsNotNone(route, "Route GET /api/partner/jobs must exist in FastAPI app.")

        # B. HTTP Request Layer: HTTPBearer rejects raw request with missing Authorization header
        import asyncio
        empty_req = Request(scope={"type": "http", "headers": []})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.security(empty_req))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Not authenticated")

        # C. Dependency Layer: get_current_partner rejects invalid credentials
        bad_creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid_malformed_token")
        with self.assertRaises(HTTPException) as ctx:
            main.get_current_partner(credentials=bad_creds)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid or expired partner token", ctx.exception.detail)

    def test_02_student_token_rejected(self):
        """2. Verify student tokens cannot access partner delivery jobs (HTTP 403)."""
        # A. Real student login token (omits role claim)
        student_jwt = main.create_access_token({
            "id": 1,
            "student_id": "241fa04072",
        })
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=student_jwt)
        with self.assertRaises(HTTPException) as ctx:
            main.get_current_partner(credentials=creds)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Delivery partner access required", ctx.exception.detail)

        # B. Explicit student role token
        now = datetime.now(timezone.utc)
        explicit_student_jwt = jwt.encode(
            {
                "sub": "student:1",
                "student_id": "241fa04072",
                "role": "student",
                "iat": now,
                "exp": now + timedelta(minutes=30),
            },
            main.JWT_SECRET_KEY,
            algorithm=main.JWT_ALGORITHM,
        )
        creds_explicit = HTTPAuthorizationCredentials(scheme="Bearer", credentials=explicit_student_jwt)
        with self.assertRaises(HTTPException) as ctx:
            main.get_current_partner(credentials=creds_explicit)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Delivery partner access required", ctx.exception.detail)

    @patch("main.get_db_connection")
    def test_03_pending_partner_rejected(self, mock_conn):
        """3. Verify partner with approval_status='pending' cannot access jobs (HTTP 403)."""
        partner_data = {
            "id": 101,
            "full_name": "Pending Partner",
            "student_id": "241fa04001",
            "approval_status": "pending",
            "is_approved": 0,
            "is_active": 1,
        }
        cursor = QueryAwareMockCursor(partners_db={101: partner_data})
        mock_conn.return_value = QueryAwareMockConnection(cursor)

        partner_jwt = main.create_partner_access_token(partner_data)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=partner_jwt)

        partner = main.get_current_partner(credentials=creds)
        self.assertEqual(partner["approval_status"], "pending")

        with self.assertRaises(HTTPException) as ctx:
            main.get_current_approved_partner(partner=partner)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("pending administrator approval", ctx.exception.detail)

    @patch("main.get_db_connection")
    def test_04_rejected_partner_rejected(self, mock_conn):
        """4. Verify partner with approval_status='rejected' cannot access jobs (HTTP 403)."""
        partner_data = {
            "id": 102,
            "full_name": "Rejected Partner",
            "student_id": "241fa04002",
            "approval_status": "rejected",
            "is_approved": 0,
            "is_active": 1,
        }
        cursor = QueryAwareMockCursor(partners_db={102: partner_data})
        mock_conn.return_value = QueryAwareMockConnection(cursor)

        partner_jwt = main.create_partner_access_token(partner_data)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=partner_jwt)

        partner = main.get_current_partner(credentials=creds)
        self.assertEqual(partner["approval_status"], "rejected")

        with self.assertRaises(HTTPException) as ctx:
            main.get_current_approved_partner(partner=partner)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("rejected or revoked", ctx.exception.detail)

    @patch("main.get_db_connection")
    def test_05_inactive_partner_rejected(self, mock_conn):
        """5. Verify partner with is_active=0 is rejected immediately (HTTP 403)."""
        partner_data = {
            "id": 103,
            "full_name": "Inactive Partner",
            "student_id": "241fa04003",
            "approval_status": "approved",
            "is_approved": 1,
            "is_active": 0,
        }
        cursor = QueryAwareMockCursor(partners_db={103: partner_data})
        mock_conn.return_value = QueryAwareMockConnection(cursor)

        partner_jwt = main.create_partner_access_token(partner_data)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=partner_jwt)

        with self.assertRaises(HTTPException) as ctx:
            main.get_current_partner(credentials=creds)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("deactivated or revoked", ctx.exception.detail)

    @patch("main.get_db_connection")
    def test_06_approved_active_partner_success(self, mock_conn):
        """6. Verify approved, active partner access and verify SQL query parameters and structure."""
        caller_partner_id = 104
        partner_data = {
            "id": caller_partner_id,
            "full_name": "Approved Partner",
            "student_id": "241fa04004",
            "approval_status": "approved",
            "is_approved": 1,
            "is_active": 1,
        }
        # Pre-filtered dataset matching the exact 15 columns selected by backend/main.py lines 1276-1288
        mock_jobs = [
            {
                "id": 1,
                "order_id": 10,
                "status": "available",
                "delivery_fee": 5.00,
                "partner_commission": 0.00,
                "accepted_at": None,
                "picked_up_at": None,
                "delivered_at": None,
                "created_at": datetime.now(timezone.utc),
                "partner_id": None,
                "student_name": "Student A",
                "delivery_location_name": "Hostel Block A",
                "delivery_room_details": "Room 101",
                "delivery_time_window": "12:00-14:00",
                "delivery_deadline": None,
            },
            {
                "id": 2,
                "order_id": 11,
                "status": "assigned",
                "delivery_fee": 5.00,
                "partner_commission": 0.00,
                "accepted_at": datetime.now(timezone.utc),
                "picked_up_at": None,
                "delivered_at": None,
                "created_at": datetime.now(timezone.utc),
                "partner_id": caller_partner_id,
                "student_name": "Student B",
                "delivery_location_name": "Library Foyer",
                "delivery_room_details": "Ground Floor",
                "delivery_time_window": None,
                "delivery_deadline": None,
            },
        ]

        cursor = QueryAwareMockCursor(partners_db={caller_partner_id: partner_data}, jobs_db=mock_jobs)
        mock_conn.return_value = QueryAwareMockConnection(cursor)

        partner_jwt = main.create_partner_access_token(partner_data)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=partner_jwt)

        partner = main.get_current_partner(credentials=creds)
        approved = main.get_current_approved_partner(partner=partner)
        response = main.get_partner_delivery_jobs(current_partner=approved)

        # 1. Assert response structure matches backend contract
        self.assertIsInstance(response, dict)
        self.assertEqual(response["count"], 2)
        self.assertEqual(len(response["jobs"]), 2)

        # 2. Parameter binding verification: prove backend bound caller's partner ID
        self.assertEqual(
            cursor.last_params,
            (caller_partner_id,),
            f"Backend must bind caller's partner ID ({caller_partner_id}) to SQL query parameter.",
        )

        # 3. SQL logic verification: verify exact WHERE condition and ordering
        executed_sql = " ".join(cursor.last_query.split())
        self.assertIn(
            "WHERE (dj.status = 'available' AND dj.partner_id IS NULL) "
            "OR (dj.status IN ('assigned', 'picked_up') AND dj.partner_id = %s)",
            executed_sql,
            "Backend SQL query must strictly exclude jobs assigned to other partners.",
        )
        self.assertIn("ORDER BY dj.created_at DESC", executed_sql)

        # 4. Privacy verification: confirm sensitive fields are omitted from returned jobs
        for job in response["jobs"]:
            self.assertNotIn("stored_file_name", job, "Stored PDF filenames must never be exposed to partners.")
            self.assertNotIn("file_path", job, "Filesystem paths must never be exposed to partners.")

    @patch("main.get_db_connection")
    def test_07_database_backed_state_overrides_jwt_claims(self, mock_conn):
        """7. Verify authorization checks the live DB record, preventing bypass via forged JWT claims."""
        now = datetime.now(timezone.utc)
        # Forged JWT claiming 'approved' status
        forged_jwt = jwt.encode(
            {
                "sub": "partner:105",
                "partner_id": 105,
                "student_id": "241fa04005",
                "role": "delivery_partner",
                "approval_status": "approved",
                "iat": now,
                "exp": now + timedelta(minutes=30),
            },
            main.JWT_SECRET_KEY,
            algorithm=main.JWT_ALGORITHM,
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=forged_jwt)

        # Database record indicates account is still pending
        partner_db_record = {
            "id": 105,
            "full_name": "Revoked Partner",
            "student_id": "241fa04005",
            "is_active": 1,
            "approval_status": "pending",
            "is_approved": 0,
        }
        cursor = QueryAwareMockCursor(partners_db={105: partner_db_record})
        mock_conn.return_value = QueryAwareMockConnection(cursor)

        partner = main.get_current_partner(credentials=creds)
        # Confirms DB 'pending' value overrode forged JWT 'approved' claim
        self.assertEqual(partner["approval_status"], "pending")

        with self.assertRaises(HTTPException) as ctx:
            main.get_current_approved_partner(partner=partner)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("pending administrator approval", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
