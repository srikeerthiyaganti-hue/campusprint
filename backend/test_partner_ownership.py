"""
Non-destructive In-Memory Delivery Ownership Isolation Tests for XeroxQueue.

Verifies cross-partner isolation, atomic Compare-And-Swap (CAS) state transitions,
and completion counter idempotency in:
  - POST /api/partner/jobs/{job_id}/pickup  (confirm_job_pickup)
  - POST /api/partner/jobs/{job_id}/deliver (confirm_job_delivery)

Safety Guarantee:
  - 100% in-memory; zero live MySQL connections or modifications.
  - Hardened with dual poison guards: invalid port in env + patched pymysql.connect.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

# Ensure backend directory is in python path and backend/.env is loaded
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

from fastapi import HTTPException
import main


# ---------------------------------------------------------------------------
# In-Memory Transactional Database Simulation
# ---------------------------------------------------------------------------

class StatefulDeliveryMockDB:
    """In-memory transactional database simulating MySQL InnoDB row states."""

    def __init__(self, partners=None, jobs=None, orders=None, existing_tables=None):
        self.existing_tables = set(existing_tables) if existing_tables else {
            "delivery_jobs",
            "delivery_partners",
            "print_orders",
        }
        # Committed state
        self.partners = {p["id"]: dict(p) for p in (partners or [])}
        self.jobs = {j["id"]: dict(j) for j in (jobs or [])}
        self.orders = {o["id"]: dict(o) for o in (orders or [])}
        # Staged uncommitted changes (within active transaction)
        self.staged_partners = {k: dict(v) for k, v in self.partners.items()}
        self.staged_jobs = {k: dict(v) for k, v in self.jobs.items()}
        self.staged_orders = {k: dict(v) for k, v in self.orders.items()}
        self.executed_queries = []
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self):
        """Commit transaction: persist staged changes to committed state."""
        self.partners = {k: dict(v) for k, v in self.staged_partners.items()}
        self.jobs = {k: dict(v) for k, v in self.staged_jobs.items()}
        self.orders = {k: dict(v) for k, v in self.staged_orders.items()}
        self.commit_count += 1

    def rollback(self):
        """Rollback transaction: discard staged changes."""
        self.staged_partners = {k: dict(v) for k, v in self.partners.items()}
        self.staged_jobs = {k: dict(v) for k, v in self.jobs.items()}
        self.staged_orders = {k: dict(v) for k, v in self.orders.items()}
        self.rollback_count += 1


class StatefulMockCursor:
    """Cursor that executes real CAS query matching against StatefulDeliveryMockDB."""

    def __init__(self, db: StatefulDeliveryMockDB):
        self.db = db
        self.rowcount = 0
        self._result_one = None
        self._result_all = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute(self, query: str, params=None):
        clean_query = " ".join(query.strip().split())
        self.db.executed_queries.append({"query": clean_query, "params": params})
        self.rowcount = 0
        self._result_one = None
        self._result_all = []

        # 1. Schema check: INFORMATION_SCHEMA.TABLES
        if "FROM INFORMATION_SCHEMA.TABLES" in clean_query:
            table_name = params[1] if params and len(params) > 1 else (params[0] if params else None)
            self._result_one = {"1": 1} if table_name in self.db.existing_tables else None
            self.rowcount = 1 if self._result_one else 0
            return

        # 2. Print Order Lookup: SELECT id, status FROM print_orders WHERE id = %s
        if "FROM print_orders WHERE id = %s" in clean_query:
            order_id = params[0]
            if order_id in self.db.staged_orders:
                order = self.db.staged_orders[order_id]
                self._result_one = {"id": order_id, "status": order.get("status", "pending")}
                self.rowcount = 1
            else:
                self._result_one = None
                self.rowcount = 0
            return

        # 3. Print Order Status Update: UPDATE print_orders SET status = %s WHERE id = %s
        if "UPDATE print_orders SET status = %s WHERE id = %s" in clean_query:
            status, order_id = params[0], params[1]
            if order_id in self.db.staged_orders:
                self.db.staged_orders[order_id]["status"] = status
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # 4. Delivery Job Cancellation Cascade:
        # UPDATE delivery_jobs SET status = 'cancelled' ... WHERE order_id = %s AND status IN ('available', 'assigned')
        if "UPDATE delivery_jobs" in clean_query and "status = 'cancelled'" in clean_query and "order_id = %s" in clean_query:
            order_id = params[0]
            count = 0
            for job in self.db.staged_jobs.values():
                if job.get("order_id") == order_id and job.get("status") in ("available", "assigned"):
                    job["status"] = "cancelled"
                    job["cancelled_at"] = datetime.now(timezone.utc)
                    job["cancelled_by"] = "admin"
                    job["cancellation_reason"] = "Print order cancelled by shop administrator"
                    job["version"] = job.get("version", 0) + 1
                    count += 1
            self.rowcount = count
            return

        # 5. Accept Job CAS Update:
        # UPDATE delivery_jobs dj JOIN print_orders po ... SET dj.partner_id = %s, dj.status = 'assigned' ...
        if "UPDATE delivery_jobs" in clean_query and ("SET dj.partner_id = %s" in clean_query or "SET partner_id = %s" in clean_query):
            partner_id, job_id = params[0], params[1]
            job = self.db.staged_jobs.get(job_id)
            order = self.db.staged_orders.get(job.get("order_id")) if job else None
            order_cancelled = order is not None and order.get("status") == "cancelled"
            if job and job.get("partner_id") is None and job.get("status") == "available" and not order_cancelled:
                job["status"] = "assigned"
                job["partner_id"] = partner_id
                job["accepted_at"] = datetime.now(timezone.utc)
                job["version"] = job.get("version", 0) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # 6. Pickup CAS Update:
        # UPDATE delivery_jobs dj JOIN print_orders po ... SET dj.status = 'picked_up' ...
        if "UPDATE delivery_jobs" in clean_query and ("SET dj.status = 'picked_up'" in clean_query or "SET status = 'picked_up'" in clean_query):
            job_id, partner_id = params[0], params[1]
            job = self.db.staged_jobs.get(job_id)
            order = self.db.staged_orders.get(job.get("order_id")) if job else None
            order_cancelled = order is not None and order.get("status") == "cancelled"
            if job and job.get("partner_id") == partner_id and job.get("status") == "assigned" and not order_cancelled:
                job["status"] = "picked_up"
                job["picked_up_at"] = datetime.now(timezone.utc)
                job["version"] = job.get("version", 0) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # 7. Delivery CAS Update:
        # UPDATE delivery_jobs dj JOIN print_orders po ... SET dj.status = 'delivered' ...
        if "UPDATE delivery_jobs" in clean_query and ("SET dj.status = 'delivered'" in clean_query or "SET status = 'delivered'" in clean_query):
            job_id, partner_id = params[0], params[1]
            job = self.db.staged_jobs.get(job_id)
            order = self.db.staged_orders.get(job.get("order_id")) if job else None
            order_cancelled = order is not None and order.get("status") == "cancelled"
            if job and job.get("partner_id") == partner_id and job.get("status") == "picked_up" and not order_cancelled:
                job["status"] = "delivered"
                job["delivered_at"] = datetime.now(timezone.utc)
                job["version"] = job.get("version", 0) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # 8. Partner completed delivery counter increment:
        # UPDATE delivery_partners SET total_deliveries_completed = total_deliveries_completed + 1 WHERE id = %s
        if "UPDATE delivery_partners" in clean_query and "total_deliveries_completed" in clean_query:
            partner_id = params[0]
            partner = self.db.staged_partners.get(partner_id)
            if partner:
                partner["total_deliveries_completed"] = partner.get("total_deliveries_completed", 0) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            return

        # 9. Diagnostic Job Query with order_status:
        # SELECT dj.id, dj.status, dj.partner_id, po.status AS order_status FROM delivery_jobs ...
        if "FROM delivery_jobs" in clean_query and "WHERE dj.id = %s" in clean_query:
            job_id = params[0]
            job = self.db.staged_jobs.get(job_id)
            if job:
                order = self.db.staged_orders.get(job.get("order_id"))
                self._result_one = {
                    "id": job["id"],
                    "status": job["status"],
                    "partner_id": job.get("partner_id"),
                    "order_status": order.get("status") if order else None,
                }
                self.rowcount = 1
            else:
                self._result_one = None
                self.rowcount = 0
            return

        # 10. Legacy Diagnostic Job Query: SELECT id, status, partner_id FROM delivery_jobs WHERE id = %s
        if "FROM delivery_jobs" in clean_query and "WHERE id = %s" in clean_query:
            job_id = params[0]
            job = self.db.staged_jobs.get(job_id)
            if job:
                order = self.db.staged_orders.get(job.get("order_id"))
                self._result_one = {
                    "id": job["id"],
                    "status": job["status"],
                    "partner_id": job.get("partner_id"),
                    "order_status": order.get("status") if order else None,
                }
                self.rowcount = 1
            else:
                self._result_one = None
                self.rowcount = 0
            return

        # 11. Partner Account Lookup: SELECT * FROM delivery_partners WHERE id = %s
        if "FROM delivery_partners" in clean_query and "WHERE id = %s" in clean_query:
            partner_id = params[0]
            partner = self.db.staged_partners.get(partner_id)
            if partner:
                self._result_one = dict(partner)
                self.rowcount = 1
            else:
                self._result_one = None
                self.rowcount = 0
            return

        # 12. Partner Job Listings Query:
        # SELECT dj.id, dj.order_id ... FROM delivery_jobs dj JOIN print_orders po ... WHERE po.status != 'cancelled' ...
        if "FROM delivery_jobs dj" in clean_query and "JOIN print_orders po" in clean_query:
            partner_id = params[0]
            res = []
            for job in self.db.staged_jobs.values():
                order = self.db.staged_orders.get(job.get("order_id"))
                if order and order.get("status") == "cancelled":
                    continue
                is_available = job.get("status") == "available" and job.get("partner_id") is None
                is_my_job = job.get("status") in ("assigned", "picked_up") and job.get("partner_id") == partner_id
                if is_available or is_my_job:
                    row = dict(job)
                    if order:
                        row.update({
                            "student_name": order.get("student_name", "Student"),
                            "delivery_location_name": order.get("delivery_location_name", "Hostel"),
                            "delivery_room_details": order.get("delivery_room_details", "Room 101"),
                            "delivery_time_window": order.get("delivery_time_window"),
                            "delivery_deadline": order.get("delivery_deadline"),
                        })
                    res.append(row)
            self._result_all = res
            self.rowcount = len(res)
            return

        raise RuntimeError(f"Unexpected unmocked SQL query in test: {query} with params {params}")

    def fetchone(self):
        return self._result_one

    def fetchall(self):
        return self._result_all


class StatefulMockConnection:
    """Mock database connection returning StatefulMockCursor."""

    def __init__(self, db: StatefulDeliveryMockDB):
        self.db = db

    def cursor(self):
        return StatefulMockCursor(self.db)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Test Suite: Cross-Partner Ownership & State Transition Isolation
# ---------------------------------------------------------------------------

class TestPartnerDeliveryOwnership(unittest.TestCase):

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
        os.environ["DB_USER"] = "prohibited_ownership_user"
        os.environ["DB_PASSWORD"] = "prohibited_ownership_password"
        os.environ["DB_NAME"] = "prohibited_ownership_db"

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

        # Standard test partner records (isolated synthetic IDs)
        self.partner_a = {
            "id": 201,
            "full_name": "Delivery Partner Alpha",
            "student_id": "241fa04201",
            "phone_number": "+919876543201",
            "approval_status": "approved",
            "is_approved": 1,
            "is_active": 1,
            "total_deliveries_completed": 0,
        }
        self.partner_b = {
            "id": 202,
            "full_name": "Delivery Partner Beta",
            "student_id": "241fa04202",
            "phone_number": "+919876543202",
            "approval_status": "approved",
            "is_approved": 1,
            "is_active": 1,
            "total_deliveries_completed": 0,
        }
        self.admin = {"id": 1, "username": "admin", "role": "admin"}

    def _create_mock_job(self, job_id, status, partner_id, version=1, order_id=None):
        """Helper to create realistic delivery job records."""
        now = datetime.now(timezone.utc)
        return {
            "id": job_id,
            "order_id": order_id if order_id is not None else (500 + job_id),
            "status": status,
            "partner_id": partner_id,
            "delivery_fee": 5.00,
            "partner_commission": 0.00,
            "accepted_at": now if status in ("assigned", "picked_up", "delivered") else None,
            "picked_up_at": now if status in ("picked_up", "delivered") else None,
            "delivered_at": now if status == "delivered" else None,
            "cancelled_at": None,
            "cancelled_by": None,
            "cancellation_reason": None,
            "created_at": now,
            "version": version,
        }

    def _create_mock_order(self, order_id, status="pending", delivery_option="delivery"):
        """Helper to create realistic print order records."""
        now = datetime.now(timezone.utc)
        return {
            "id": order_id,
            "status": status,
            "delivery_option": delivery_option,
            "delivery_fee": 5.00 if delivery_option == "delivery" else 0.00,
            "student_name": f"Student {order_id}",
            "delivery_location_name": "Hostel Block C",
            "delivery_room_details": f"Room {order_id}",
            "delivery_time_window": "18:00 - 19:00",
            "delivery_deadline": None,
            "created_at": now,
        }

    # -----------------------------------------------------------------------
    # Guard Test
    # -----------------------------------------------------------------------

    def test_00_database_isolation_enforced(self):
        """0. Prove that unmocked database calls fail immediately without touching MySQL."""
        with self.assertRaises(RuntimeError) as ctx:
            main.get_db_connection()
        self.assertIn("LIVE DB ACCESS PROHIBITED", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Requirement 1: Partner B cannot pick up Partner A's assigned job
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_01_partner_b_cannot_pickup_partner_a_assigned_job(self, mock_conn):
        """1. Verify Partner B cannot pick up a job assigned to Partner A (HTTP 403, state unchanged)."""
        job_id = 301
        job_record = self._create_mock_job(job_id=job_id, status="assigned", partner_id=self.partner_a["id"], version=1)

        db = StatefulDeliveryMockDB(
            partners=[self.partner_a, self.partner_b],
            jobs=[job_record],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner B attempts to pick up Partner A's assigned job
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_pickup(job_id=job_id, current_partner=self.partner_b)

        # 1. Assert HTTP Status and diagnostic message
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("You are not the assigned delivery partner", ctx.exception.detail)
        self.assertIn(f"job #{job_id}", ctx.exception.detail)

        # 2. Verify SQL parameters bound by backend
        executed_updates = [q for q in db.executed_queries if "UPDATE delivery_jobs" in q["query"]]
        self.assertEqual(len(executed_updates), 1)
        # Parameters to CAS update: (job_id, current_partner["id"])
        self.assertEqual(executed_updates[0]["params"], (job_id, self.partner_b["id"]))

        # 3. Verify state post-condition: Job remains assigned to Partner A, NOT picked up
        persisted_job = db.jobs[job_id]
        self.assertEqual(persisted_job["status"], "assigned")
        self.assertEqual(persisted_job["partner_id"], self.partner_a["id"])
        self.assertIsNone(persisted_job["picked_up_at"])
        self.assertEqual(persisted_job["version"], 1)

        # 4. Verify transaction was rolled back, never committed
        self.assertEqual(db.commit_count, 0)
        self.assertGreaterEqual(db.rollback_count, 1)

    # -----------------------------------------------------------------------
    # Requirement 2: Partner B cannot deliver Partner A's picked-up job
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_02_partner_b_cannot_deliver_partner_a_picked_up_job(self, mock_conn):
        """2. Verify Partner B cannot deliver a job picked up by Partner A (HTTP 403, counters unchanged)."""
        job_id = 302
        job_record = self._create_mock_job(job_id=job_id, status="picked_up", partner_id=self.partner_a["id"], version=2)

        db = StatefulDeliveryMockDB(
            partners=[self.partner_a, self.partner_b],
            jobs=[job_record],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner B attempts to confirm delivery of Partner A's job
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=job_id, current_partner=self.partner_b)

        # 1. Assert HTTP Status and diagnostic error message
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("You are not the assigned delivery partner", ctx.exception.detail)
        self.assertIn(f"job #{job_id}", ctx.exception.detail)

        # 2. Verify state post-condition: Job remains picked_up, NOT delivered
        persisted_job = db.jobs[job_id]
        self.assertEqual(persisted_job["status"], "picked_up")
        self.assertEqual(persisted_job["partner_id"], self.partner_a["id"])
        self.assertIsNone(persisted_job["delivered_at"])
        self.assertEqual(persisted_job["version"], 2)

        # 3. Verify neither partner's completed delivery counter was modified
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.partners[self.partner_b["id"]]["total_deliveries_completed"], 0)

        # 4. Verify transaction was rolled back
        self.assertEqual(db.commit_count, 0)
        self.assertGreaterEqual(db.rollback_count, 1)

    # -----------------------------------------------------------------------
    # Requirement 3: Partner A performs valid pickup and delivery transitions
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_03_partner_a_valid_pickup_and_delivery_lifecycle(self, mock_conn):
        """3. Verify Partner A can pick up and deliver its assigned job (HTTP 200, exactly one counter increment)."""
        job_id = 303
        job_record = self._create_mock_job(job_id=job_id, status="assigned", partner_id=self.partner_a["id"], version=1)
        job_record["picked_up_at"] = None
        job_record["delivered_at"] = None

        db = StatefulDeliveryMockDB(
            partners=[self.partner_a, self.partner_b],
            jobs=[job_record],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Step 3a: Partner A picks up the job
        pickup_response = main.confirm_job_pickup(job_id=job_id, current_partner=self.partner_a)

        self.assertIsInstance(pickup_response, dict)
        self.assertEqual(pickup_response["job_id"], job_id)
        self.assertEqual(pickup_response["status"], "picked_up")
        self.assertEqual(pickup_response["partner_id"], self.partner_a["id"])

        # Verify job is now picked up in DB with incremented version
        self.assertEqual(db.jobs[job_id]["status"], "picked_up")
        self.assertIsNotNone(db.jobs[job_id]["picked_up_at"])
        self.assertEqual(db.jobs[job_id]["version"], 2)
        # Pickup must NOT increment delivery counter
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.commit_count, 1)

        # Step 3b: Partner A confirms delivery to the student
        delivery_response = main.confirm_job_delivery(job_id=job_id, current_partner=self.partner_a)

        self.assertIsInstance(delivery_response, dict)
        self.assertEqual(delivery_response["job_id"], job_id)
        self.assertEqual(delivery_response["status"], "delivered")
        self.assertEqual(delivery_response["partner_id"], self.partner_a["id"])

        # Verify job is now delivered in DB with incremented version
        self.assertEqual(db.jobs[job_id]["status"], "delivered")
        self.assertIsNotNone(db.jobs[job_id]["delivered_at"])
        self.assertEqual(db.jobs[job_id]["version"], 3)

        # Verify Partner A's counter was incremented exactly once (0 -> 1)
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 1)
        # Partner B's counter remains 0
        self.assertEqual(db.partners[self.partner_b["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.commit_count, 2)

    # -----------------------------------------------------------------------
    # Requirement 4: Duplicate delivery retries return 409 and do not re-increment
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_04_duplicate_delivery_retry_rejected_with_409(self, mock_conn):
        """4. Verify retrying delivery on an already-delivered job returns HTTP 409 and does not increment counter."""
        job_id = 304
        job_record = self._create_mock_job(job_id=job_id, status="delivered", partner_id=self.partner_a["id"], version=3)
        partner_a_completed = dict(self.partner_a)
        partner_a_completed["total_deliveries_completed"] = 1

        db = StatefulDeliveryMockDB(
            partners=[partner_a_completed, self.partner_b],
            jobs=[job_record],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner A retries delivery on an already completed job (e.g. network retry or double-click)
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=job_id, current_partner=partner_a_completed)

        # 1. Assert HTTP Status is 409 Conflict
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("already been marked as delivered", ctx.exception.detail)
        self.assertIn(f"job #{job_id}", ctx.exception.detail)

        # 2. Verify counter is NOT incremented again (must stay 1, not 2)
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 1)

        # 3. Verify counter update query was NEVER executed
        counter_updates = [q for q in db.executed_queries if "UPDATE delivery_partners" in q["query"]]
        self.assertEqual(len(counter_updates), 0, "Counter update query must not be executed when CAS update fails.")

        # 4. Verify transaction was rolled back
        self.assertEqual(db.commit_count, 0)
        self.assertGreaterEqual(db.rollback_count, 1)

    # -----------------------------------------------------------------------
    # Requirement 5: Partner B delivering Partner A's already-delivered job returns 403
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_05_cross_partner_delivery_on_completed_job_rejected_with_403(self, mock_conn):
        """5. Verify Partner B delivering Partner A's completed job returns HTTP 403 and changes nothing."""
        job_id = 305
        job_record = self._create_mock_job(job_id=job_id, status="delivered", partner_id=self.partner_a["id"], version=3)
        partner_a_completed = dict(self.partner_a)
        partner_a_completed["total_deliveries_completed"] = 1

        db = StatefulDeliveryMockDB(
            partners=[partner_a_completed, self.partner_b],
            jobs=[job_record],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner B attempts delivery on Partner A's already completed job
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=job_id, current_partner=self.partner_b)

        # 1. Assert HTTP Status is 403 Forbidden (ownership check precedes status check)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("You are not the assigned delivery partner", ctx.exception.detail)
        self.assertIn(f"job #{job_id}", ctx.exception.detail)

        # 2. Verify counters remain completely untouched
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 1)
        self.assertEqual(db.partners[self.partner_b["id"]]["total_deliveries_completed"], 0)

        # 3. Verify job state remains delivered with original owner
        self.assertEqual(db.jobs[job_id]["status"], "delivered")
        self.assertEqual(db.jobs[job_id]["partner_id"], self.partner_a["id"])

        # 4. Verify transaction was rolled back
        self.assertEqual(db.commit_count, 0)
        self.assertGreaterEqual(db.rollback_count, 1)

    # -----------------------------------------------------------------------
    # Complementary Boundary Tests
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_06_invalid_job_id_and_not_found(self, mock_conn):
        """6. Verify job_id <= 0 returns HTTP 400 and non-existent job returns HTTP 404."""
        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Zero or negative job ID returns 400 Bad Request immediately (before DB query)
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_pickup(job_id=0, current_partner=self.partner_a)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid delivery job ID", ctx.exception.detail)

        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=-5, current_partner=self.partner_a)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid delivery job ID", ctx.exception.detail)

        # Non-existent job ID returns 404 Not Found
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_pickup(job_id=99999, current_partner=self.partner_a)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not found", ctx.exception.detail)

        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=99999, current_partner=self.partner_a)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not found", ctx.exception.detail)

    @patch("main.get_db_connection")
    def test_07_premature_delivery_before_pickup_rejected_with_409(self, mock_conn):
        """7. Verify Partner A cannot deliver a job before picking it up (HTTP 409)."""
        job_id = 307
        job_record = self._create_mock_job(job_id=job_id, status="assigned", partner_id=self.partner_a["id"], version=1)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job_record])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner A attempts delivery while job is still in 'assigned' status
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=job_id, current_partner=self.partner_a)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("must be marked as picked up before delivery can be confirmed", ctx.exception.detail)
        self.assertEqual(db.jobs[job_id]["status"], "assigned")
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.commit_count, 0)

    # -----------------------------------------------------------------------
    # Cancellation Safety Tests (Phase 4.9)
    # -----------------------------------------------------------------------

    @patch("main.get_db_connection")
    def test_08_cancel_order_with_available_job_cancels_job(self, mock_conn):
        """8. Admin cancels an order with an available delivery job; job transitions to cancelled."""
        order_id = 501
        job_id = 401
        order = self._create_mock_order(order_id=order_id, status="pending")
        job = self._create_mock_job(job_id=job_id, status="available", partner_id=None, order_id=order_id, version=1)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job], orders=[order])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        res = main.update_order_status(
            order_id=order_id,
            update=main.OrderStatusUpdate(status="cancelled"),
            current_admin=self.admin,
        )

        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(res["order_id"], order_id)
        # Persisted order is cancelled
        self.assertEqual(db.orders[order_id]["status"], "cancelled")
        # Cascade cancelled the available delivery job
        persisted_job = db.jobs[job_id]
        self.assertEqual(persisted_job["status"], "cancelled")
        self.assertEqual(persisted_job["cancelled_by"], "admin")
        self.assertEqual(persisted_job["cancellation_reason"], "Print order cancelled by shop administrator")
        self.assertIsNotNone(persisted_job["cancelled_at"])
        self.assertEqual(persisted_job["version"], 2)
        self.assertGreaterEqual(db.commit_count, 1)

    @patch("main.get_db_connection")
    def test_09_cancel_order_with_assigned_job_cancels_job(self, mock_conn):
        """9. Admin cancels an order with an assigned delivery job; job transitions to cancelled."""
        order_id = 502
        job_id = 402
        order = self._create_mock_order(order_id=order_id, status="printing")
        job = self._create_mock_job(job_id=job_id, status="assigned", partner_id=self.partner_a["id"], order_id=order_id, version=1)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job], orders=[order])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        res = main.update_order_status(
            order_id=order_id,
            update=main.OrderStatusUpdate(status="cancelled"),
            current_admin=self.admin,
        )

        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(db.orders[order_id]["status"], "cancelled")
        persisted_job = db.jobs[job_id]
        self.assertEqual(persisted_job["status"], "cancelled")
        self.assertEqual(persisted_job["cancelled_by"], "admin")
        self.assertEqual(persisted_job["cancellation_reason"], "Print order cancelled by shop administrator")
        self.assertIsNotNone(persisted_job["cancelled_at"])
        self.assertEqual(persisted_job["version"], 2)
        self.assertGreaterEqual(db.commit_count, 1)

    @patch("main.get_db_connection")
    def test_10_cancel_order_preserves_picked_up_and_delivered_jobs(self, mock_conn):
        """10. Cancelling an order with picked_up or delivered job does NOT cancel the delivery job."""
        order_id_1 = 503
        job_id_1 = 403
        order_1 = self._create_mock_order(order_id=order_id_1, status="ready")
        job_1 = self._create_mock_job(job_id=job_id_1, status="picked_up", partner_id=self.partner_a["id"], order_id=order_id_1, version=2)

        order_id_2 = 504
        job_id_2 = 404
        order_2 = self._create_mock_order(order_id=order_id_2, status="completed")
        job_2 = self._create_mock_job(job_id=job_id_2, status="delivered", partner_id=self.partner_a["id"], order_id=order_id_2, version=3)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job_1, job_2], orders=[order_1, order_2])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Cancel order 1 (job is picked_up)
        main.update_order_status(
            order_id=order_id_1,
            update=main.OrderStatusUpdate(status="cancelled"),
            current_admin=self.admin,
        )
        self.assertEqual(db.orders[order_id_1]["status"], "cancelled")
        # Job 1 must remain picked_up
        self.assertEqual(db.jobs[job_id_1]["status"], "picked_up")
        self.assertIsNone(db.jobs[job_id_1]["cancelled_by"])
        self.assertEqual(db.jobs[job_id_1]["version"], 2)

        # Cancel order 2 (job is delivered)
        main.update_order_status(
            order_id=order_id_2,
            update=main.OrderStatusUpdate(status="cancelled"),
            current_admin=self.admin,
        )
        self.assertEqual(db.orders[order_id_2]["status"], "cancelled")
        # Job 2 must remain delivered
        self.assertEqual(db.jobs[job_id_2]["status"], "delivered")
        self.assertIsNone(db.jobs[job_id_2]["cancelled_by"])
        self.assertEqual(db.jobs[job_id_2]["version"], 3)

    @patch("main.get_db_connection")
    def test_11_partner_cannot_accept_job_of_cancelled_order(self, mock_conn):
        """11. Partner cannot accept a delivery job whose parent order is cancelled (HTTP 409 Conflict)."""
        order_id = 505
        job_id = 405
        order = self._create_mock_order(order_id=order_id, status="cancelled")
        job = self._create_mock_job(job_id=job_id, status="available", partner_id=None, order_id=order_id, version=1)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job], orders=[order])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        with self.assertRaises(HTTPException) as ctx:
            main.accept_delivery_job(job_id=job_id, current_partner=self.partner_a)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("print order has been cancelled", ctx.exception.detail)
        self.assertEqual(db.jobs[job_id]["status"], "available")
        self.assertIsNone(db.jobs[job_id]["partner_id"])
        self.assertEqual(db.commit_count, 0)

    @patch("main.get_db_connection")
    def test_12_partner_cannot_pickup_job_of_cancelled_order(self, mock_conn):
        """12. Partner cannot pick up a delivery job whose parent order is cancelled (HTTP 409 Conflict)."""
        order_id = 506
        job_id = 406
        order = self._create_mock_order(order_id=order_id, status="cancelled")
        job = self._create_mock_job(job_id=job_id, status="assigned", partner_id=self.partner_a["id"], order_id=order_id, version=1)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job], orders=[order])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_pickup(job_id=job_id, current_partner=self.partner_a)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("print order has been cancelled", ctx.exception.detail)
        self.assertEqual(db.jobs[job_id]["status"], "assigned")
        self.assertIsNone(db.jobs[job_id]["picked_up_at"])
        self.assertEqual(db.commit_count, 0)

    @patch("main.get_db_connection")
    def test_13_partner_cannot_deliver_job_of_cancelled_order(self, mock_conn):
        """13. Partner cannot deliver a job whose parent order is cancelled (HTTP 409 Conflict, counters untouched)."""
        order_id = 507
        job_id = 407
        order = self._create_mock_order(order_id=order_id, status="cancelled")
        job = self._create_mock_job(job_id=job_id, status="picked_up", partner_id=self.partner_a["id"], order_id=order_id, version=2)

        db = StatefulDeliveryMockDB(partners=[self.partner_a], jobs=[job], orders=[order])
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=job_id, current_partner=self.partner_a)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("print order has been cancelled", ctx.exception.detail)
        self.assertEqual(db.jobs[job_id]["status"], "picked_up")
        self.assertIsNone(db.jobs[job_id]["delivered_at"])
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.commit_count, 0)

    @patch("main.get_db_connection")
    def test_14_cancelled_orders_excluded_from_partner_job_listings(self, mock_conn):
        """14. Partner job listing endpoint excludes jobs whose parent print order is cancelled."""
        order_cancelled_1 = self._create_mock_order(order_id=508, status="cancelled")
        job_cancelled_avail = self._create_mock_job(job_id=408, status="available", partner_id=None, order_id=508)

        order_cancelled_2 = self._create_mock_order(order_id=509, status="cancelled")
        job_cancelled_assigned = self._create_mock_job(job_id=409, status="assigned", partner_id=self.partner_a["id"], order_id=509)

        order_active_1 = self._create_mock_order(order_id=510, status="pending")
        job_active_avail = self._create_mock_job(job_id=410, status="available", partner_id=None, order_id=510)

        order_active_2 = self._create_mock_order(order_id=511, status="printing")
        job_active_assigned = self._create_mock_job(job_id=411, status="assigned", partner_id=self.partner_a["id"], order_id=511)

        db = StatefulDeliveryMockDB(
            partners=[self.partner_a],
            jobs=[job_cancelled_avail, job_cancelled_assigned, job_active_avail, job_active_assigned],
            orders=[order_cancelled_1, order_cancelled_2, order_active_1, order_active_2],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        res = main.get_partner_delivery_jobs(current_partner=self.partner_a)
        self.assertEqual(res["count"], 2)
        returned_job_ids = [j["id"] for j in res["jobs"]]
        self.assertIn(410, returned_job_ids)
        self.assertIn(411, returned_job_ids)
        self.assertNotIn(408, returned_job_ids)
        self.assertNotIn(409, returned_job_ids)

    @patch("main.get_db_connection")
    def test_15_cross_partner_attempt_on_cancelled_order_returns_403_not_409(self, mock_conn):
        """15. Cross-partner attempts on cancelled orders return HTTP 403 Forbidden, NOT HTTP 409 Conflict."""
        order_1 = self._create_mock_order(order_id=512, status="cancelled")
        job_1 = self._create_mock_job(job_id=412, status="assigned", partner_id=self.partner_a["id"], order_id=512)

        order_2 = self._create_mock_order(order_id=513, status="cancelled")
        job_2 = self._create_mock_job(job_id=413, status="picked_up", partner_id=self.partner_a["id"], order_id=513, version=2)

        db = StatefulDeliveryMockDB(
            partners=[self.partner_a, self.partner_b],
            jobs=[job_1, job_2],
            orders=[order_1, order_2],
        )
        mock_conn.side_effect = lambda: StatefulMockConnection(db)

        # Partner B attempts pickup on Partner A's job for a cancelled order -> 403 Forbidden
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_pickup(job_id=412, current_partner=self.partner_b)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("You are not the assigned delivery partner", ctx.exception.detail)

        # Partner B attempts delivery on Partner A's job for a cancelled order -> 403 Forbidden
        with self.assertRaises(HTTPException) as ctx:
            main.confirm_job_delivery(job_id=413, current_partner=self.partner_b)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("You are not the assigned delivery partner", ctx.exception.detail)

        # Ensure counters and state unchanged
        self.assertEqual(db.partners[self.partner_a["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.partners[self.partner_b["id"]]["total_deliveries_completed"], 0)
        self.assertEqual(db.commit_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
