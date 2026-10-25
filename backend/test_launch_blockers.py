"""
Automated Launch-Blocker Test Suite for XeroxQueue.

Verifies the 3 P0 Launch Blockers:
  1. Environment-based API URL & dynamic CORS configuration:
     - Development defaults (localhost & 127.0.0.1 ports 5173-5175).
     - Comma-separated parsing, whitespace trimming, trailing slash removal.
     - Rejection of wildcard '*' origins (raises RuntimeError).
     - Production mode fail-fast safeguard (raises RuntimeError if missing/empty).
  2. Campus delivery destination collection & database persistence:
     - GET /api/locations canonical fallback vs active database rows.
     - POST /api/orders validation: room details (1-255 chars), location ID derivation,
       canonical fallback validation, rejection of invalid/inactive/arbitrary locations.
     - Exact SQL parameter binding for destination columns & delivery fees.
     - Pickup orders setting destination columns to NULL and ₹0 fee with no delivery job.
  3. Safety & Isolation Guarantee:
     - 100% in-memory simulation; zero live MySQL connections or modifications.
     - Hardened with dual poison guards: invalid port in env + patched pymysql.connect.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

# Ensure backend directory is in sys.path
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

from fastapi import HTTPException
import main
from main import (
    DEFAULT_ALLOWED_ORIGINS,
    CANONICAL_FALLBACK_LOCATIONS,
    get_allowed_origins,
    get_campus_locations,
    health_check,
    create_order,
    PrintOrder,
)


# ---------------------------------------------------------------------------
# In-Memory Database Simulation for Launch-Blocker Tests
# ---------------------------------------------------------------------------

class MockLaunchBlockerDB:
    """In-memory database state for launch blocker testing."""

    def __init__(self, existing_tables=None, columns=None, active_locations=None, uploads=None):
        self.existing_tables = set(existing_tables) if existing_tables is not None else {
            "print_orders",
            "uploaded_documents",
            "campus_locations",
            "delivery_jobs",
        }
        self.columns = columns or {
            "print_orders": {
                "id", "student_db_id", "student_name", "file_name",
                "stored_file_name", "copies", "print_type", "print_sides",
                "fulfillment_type", "delivery_fee", "delivery_location_id",
                "delivery_location_name", "delivery_room_details", "delivery_time_window"
            }
        }
        self.active_locations = list(active_locations) if active_locations is not None else []
        self.uploads = list(uploads) if uploads is not None else []
        self.executed_queries = []
        self.commit_count = 0
        self.rollback_count = 0


class MockLaunchBlockerCursor:
    """Cursor that records executed SQL queries and returns targeted mock data."""

    def __init__(self, db: MockLaunchBlockerDB):
        self.db = db
        self.rowcount = 0
        self.lastrowid = 1001
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

        # 2. Column check: INFORMATION_SCHEMA.COLUMNS
        if "FROM INFORMATION_SCHEMA.COLUMNS" in clean_query:
            table_name = params[1] if params and len(params) > 1 else None
            cols = self.db.columns.get(table_name, set())
            self._result_all = [{"COLUMN_NAME": col} for col in cols]
            self.rowcount = len(self._result_all)
            return

        # 3. Upload ownership check: SELECT id FROM uploaded_documents WHERE stored_file_name = %s AND student_db_id = %s
        if "FROM uploaded_documents" in clean_query:
            stored_file, student_id = params[0], params[1]
            matched = next(
                (u for u in self.db.uploads if u.get("stored_file_name") == stored_file and u.get("student_db_id") == student_id),
                None
            )
            if matched:
                self._result_one = {"id": matched.get("id", 1)}
                self.rowcount = 1
            else:
                self._result_one = None
                self.rowcount = 0
            return

        # 4. Campus locations lookup: SELECT ... FROM campus_locations WHERE is_active = 1
        if "FROM campus_locations" in clean_query:
            self._result_all = [dict(loc) for loc in self.db.active_locations]
            self.rowcount = len(self._result_all)
            return

        # 5. Insert into print_orders
        if "INSERT INTO print_orders" in clean_query:
            self.lastrowid = 501
            self.rowcount = 1
            return

        # 6. Insert into delivery_jobs
        if "INSERT INTO delivery_jobs" in clean_query:
            self.lastrowid = 801
            self.rowcount = 1
            return

        raise RuntimeError(f"Unexpected unmocked SQL query in test: {clean_query} with params {params}")

    def fetchone(self):
        return self._result_one

    def fetchall(self):
        return self._result_all


class MockLaunchBlockerConnection:
    """Mock database connection."""

    def __init__(self, db: MockLaunchBlockerDB):
        self.db = db

    def cursor(self):
        return MockLaunchBlockerCursor(self.db)

    def commit(self):
        self.db.commit_count += 1

    def rollback(self):
        self.db.rollback_count += 1

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Test Suite 1: CORS & Dynamic Configuration
# ---------------------------------------------------------------------------

class TestCORSConfiguration(unittest.TestCase):
    """Verifies CORS origin parsing, development fallbacks, and production fail-fast safeguards."""

    def setUp(self):
        self._orig_env = {
            "APP_ENV": os.environ.get("APP_ENV"),
            "ENVIRONMENT": os.environ.get("ENVIRONMENT"),
            "ALLOWED_ORIGINS": os.environ.get("ALLOWED_ORIGINS"),
        }

    def tearDown(self):
        for key, val in self._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_default_development_origins_when_unset(self):
        """In development with unset ALLOWED_ORIGINS, returns default localhost & 127.0.0.1 ports."""
        os.environ["APP_ENV"] = "development"
        os.environ.pop("ALLOWED_ORIGINS", None)

        origins = get_allowed_origins()
        self.assertEqual(origins, DEFAULT_ALLOWED_ORIGINS)
        self.assertIn("http://localhost:5173", origins)
        self.assertIn("http://127.0.0.1:5173", origins)
        self.assertIn("http://localhost:5174", origins)

    def test_custom_allowed_origins_parsing(self):
        """Trims whitespace, splits commas, and strips trailing slashes."""
        os.environ["APP_ENV"] = "development"
        os.environ["ALLOWED_ORIGINS"] = " https://app.example.com/ , http://localhost:3000/ , https://preview.edu "

        origins = get_allowed_origins()
        self.assertEqual(
            origins,
            ["https://app.example.com", "http://localhost:3000", "https://preview.edu"],
        )

    def test_wildcard_origin_rejected_in_all_environments(self):
        """Wildcard '*' origin is rejected with RuntimeError because credentials are enabled."""
        os.environ["APP_ENV"] = "development"
        os.environ["ALLOWED_ORIGINS"] = "*"
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("Wildcard '*' origins are strictly forbidden", str(ctx.exception))

        os.environ["ALLOWED_ORIGINS"] = "https://app.example.com, *"
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("Wildcard '*' origins are strictly forbidden", str(ctx.exception))

    def test_production_fail_fast_when_missing(self):
        """In production, missing ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "production"
        os.environ.pop("ALLOWED_ORIGINS", None)

        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

    def test_production_fail_fast_when_empty_or_whitespace(self):
        """In production, empty or whitespace ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "production"
        os.environ["ALLOWED_ORIGINS"] = "   "

        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

    def test_production_valid_custom_origins(self):
        """In production with valid explicit origins, returns stripped list."""
        os.environ["APP_ENV"] = "production"
        os.environ["ALLOWED_ORIGINS"] = "https://xeroxqueue.campus.edu/, https://admin.campus.edu"

        origins = get_allowed_origins()
        self.assertEqual(
            origins,
            ["https://xeroxqueue.campus.edu", "https://admin.campus.edu"],
        )

    def test_staging_fail_fast_when_missing(self):
        """In staging, missing ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "staging"
        os.environ.pop("ALLOWED_ORIGINS", None)

        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))
        self.assertIn("in staging", str(ctx.exception))

    def test_staging_fail_fast_when_empty_or_whitespace(self):
        """In staging, empty or whitespace ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "staging"
        os.environ["ALLOWED_ORIGINS"] = "   "

        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))
        self.assertIn("in staging", str(ctx.exception))

    def test_stage_alias_fail_fast_when_missing_or_blank(self):
        """In stage alias, missing or blank ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "stage"
        os.environ.pop("ALLOWED_ORIGINS", None)
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

        os.environ["ALLOWED_ORIGINS"] = ""
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

    def test_prod_alias_fail_fast_when_missing_or_blank(self):
        """In prod alias, missing or blank ALLOWED_ORIGINS raises RuntimeError."""
        os.environ["APP_ENV"] = "prod"
        os.environ.pop("ALLOWED_ORIGINS", None)
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

        os.environ["ALLOWED_ORIGINS"] = "   "
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("ALLOWED_ORIGINS must be set to a comma-separated list", str(ctx.exception))

    def test_staging_valid_explicit_origins(self):
        """In staging with valid explicit origins, returns stripped list."""
        os.environ["APP_ENV"] = "staging"
        os.environ["ALLOWED_ORIGINS"] = "https://staging.xeroxqueue.edu, https://admin.staging.xeroxqueue.edu/"

        origins = get_allowed_origins()
        self.assertEqual(
            origins,
            ["https://staging.xeroxqueue.edu", "https://admin.staging.xeroxqueue.edu"],
        )

    def test_staging_wildcard_rejected(self):
        """In staging, wildcard origin is strictly rejected."""
        os.environ["APP_ENV"] = "staging"
        os.environ["ALLOWED_ORIGINS"] = "*"
        with self.assertRaises(RuntimeError) as ctx:
            get_allowed_origins()
        self.assertIn("Wildcard '*' origins are strictly forbidden", str(ctx.exception))

    def test_development_retains_localhost_fallback_when_blank_or_missing(self):
        """In development, missing or blank origins safely retains localhost defaults."""
        os.environ["APP_ENV"] = "development"
        os.environ["ALLOWED_ORIGINS"] = "   "
        origins = get_allowed_origins()
        self.assertEqual(origins, DEFAULT_ALLOWED_ORIGINS)


# ---------------------------------------------------------------------------
# Test Suite 2: Health Check Endpoints
# ---------------------------------------------------------------------------

class TestHealthCheckEndpoints(unittest.TestCase):
    """Verifies that both /health and /api/health routes are registered and return safe responses."""

    def test_health_routes_registered_and_return_safe_payload(self):
        """Confirm /health and /api/health are both registered and return expected payload without DB access."""
        registered_routes = {
            getattr(route, "path", None): getattr(route, "endpoint", None)
            for route in main.app.routes
        }

        # 1. Both routes exist in the FastAPI app
        self.assertIn("/health", registered_routes)
        self.assertIn("/api/health", registered_routes)

        # 2. Both routes point to the same handler
        self.assertEqual(registered_routes["/health"], health_check)
        self.assertEqual(registered_routes["/api/health"], health_check)

        # 3. Direct invocation returns the expected safe payload
        response = health_check()
        self.assertEqual(
            response,
            {
                "status": "healthy",
                "service": "XeroxQueue API",
            },
        )

        # 4. Confirm zero secrets, DB credentials, or sensitive diagnostics are exposed
        for val in response.values():
            self.assertNotIn("password", str(val).lower())
            self.assertNotIn("secret", str(val).lower())
            self.assertNotIn("/", str(val))


# ---------------------------------------------------------------------------
# Test Suite 3: Campus Locations & Delivery Order Destination Persistence
# ---------------------------------------------------------------------------

class TestCampusDeliveryEndpoints(unittest.TestCase):
    """Verifies location resolution, room validation, destination persistence, and fee rules."""

    @classmethod
    def setUpClass(cls):
        # Layer 1 Poison Guard: point to unreachable port
        cls._orig_env = {
            "DB_HOST": os.environ.get("DB_HOST"),
            "DB_PORT": os.environ.get("DB_PORT"),
            "DB_USER": os.environ.get("DB_USER"),
            "DB_PASSWORD": os.environ.get("DB_PASSWORD"),
            "DB_NAME": os.environ.get("DB_NAME"),
        }
        os.environ["DB_HOST"] = "127.0.0.1"
        os.environ["DB_PORT"] = "9"
        os.environ["DB_USER"] = "prohibited_launch_user"
        os.environ["DB_PASSWORD"] = "prohibited_launch_password"
        os.environ["DB_NAME"] = "prohibited_launch_db"

    @classmethod
    def tearDownClass(cls):
        for key, val in cls._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def setUp(self):
        # Layer 2 Poison Guard: fail immediately on unmocked live MySQL connection
        def _blocked_connect(*args, **kwargs):
            raise RuntimeError("LIVE DB ACCESS PROHIBITED: Test attempted an unmocked live MySQL connection!")

        self.db_guard = patch("pymysql.connect", side_effect=_blocked_connect)
        self.main_db_guard = patch("main.pymysql.connect", side_effect=_blocked_connect)
        self.db_guard.start()
        self.main_db_guard.start()
        self.addCleanup(self.db_guard.stop)
        self.addCleanup(self.main_db_guard.stop)

        # Standard test student
        self.test_student = {
            "id": 101,
            "full_name": "Rohan Sharma",
            "student_id": "241fa04101",
            "email": "rohan@campus.edu",
        }

    # --- GET /api/locations tests ---

    def test_locations_returns_canonical_fallback_when_table_missing(self):
        """When campus_locations table does not exist, returns 6 canonical hubs with source='fallback'."""
        db = MockLaunchBlockerDB(existing_tables={"print_orders"})
        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = get_campus_locations()

        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["count"], 6)
        self.assertEqual(len(res["locations"]), 6)
        for loc in res["locations"]:
            self.assertIsNone(loc["id"])
            self.assertTrue(loc["name"])
            self.assertTrue(loc["location_code"])

    def test_locations_returns_canonical_fallback_when_table_empty(self):
        """When campus_locations exists but contains 0 active records, returns canonical fallback list."""
        db = MockLaunchBlockerDB(existing_tables={"campus_locations"}, active_locations=[])
        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = get_campus_locations()

        self.assertEqual(res["source"], "fallback")
        self.assertEqual(res["count"], 6)

    def test_locations_returns_database_records_when_present(self):
        """When campus_locations has active rows, returns database rows with source='database'."""
        db_locations = [
            {"id": 1, "location_code": "HOSTEL_A", "name": "Hostel Alpha", "description": "Gate 1", "building_type": "hostel"},
            {"id": 2, "location_code": "LIB", "name": "Central Library", "description": "Lobby", "building_type": "library"},
        ]
        db = MockLaunchBlockerDB(existing_tables={"campus_locations"}, active_locations=db_locations)
        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = get_campus_locations()

        self.assertEqual(res["source"], "database")
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["locations"][0]["id"], 1)
        self.assertEqual(res["locations"][0]["name"], "Hostel Alpha")

    # --- POST /api/orders validation and persistence tests ---

    def test_order_without_stored_file_rejected(self):
        """Ordering without a valid stored_file_name raises HTTP 400."""
        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="assignment.pdf",
            stored_file_name=None,
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="pickup",
        )
        db = MockLaunchBlockerDB()
        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            with self.assertRaises(HTTPException) as ctx:
                create_order(order, current_student=self.test_student)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Please upload a PDF before placing your order", ctx.exception.detail)

    def test_order_with_unowned_pdf_rejected(self):
        """Attempting to order using a PDF uploaded by another student raises HTTP 403."""
        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="assignment.pdf",
            stored_file_name="doc_foreign_student.pdf",
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="pickup",
        )
        # Upload is owned by student ID 999, not 101
        db = MockLaunchBlockerDB(uploads=[{"stored_file_name": "doc_foreign_student.pdf", "student_db_id": 999}])
        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            with self.assertRaises(HTTPException) as ctx:
                create_order(order, current_student=self.test_student)
            self.assertEqual(ctx.exception.status_code, 403)
            self.assertIn("does not belong to your account", ctx.exception.detail)

    def test_delivery_missing_or_blank_room_details_rejected(self):
        """Fulfillment type delivery with missing or whitespace room details raises HTTP 400."""
        db = MockLaunchBlockerDB(uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}])

        for invalid_room in [None, "", "   ", "\t\n"]:
            order = PrintOrder(
                student_name="Rohan Sharma",
                file_name="assignment.pdf",
                stored_file_name="doc_valid.pdf",
                copies=1,
                print_type="black_white",
                print_sides="single",
                fulfillment_type="delivery",
                delivery_location_name="Boys Hostel Block A",
                delivery_room_details=invalid_room,
            )
            with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
                with self.assertRaises(HTTPException) as ctx:
                    create_order(order, current_student=self.test_student)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("Delivery room/details must be provided", ctx.exception.detail)

    def test_delivery_room_details_exceeding_255_chars_rejected(self):
        """Room details exceeding 255 chars raises HTTP 400."""
        db = MockLaunchBlockerDB(uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}])
        long_room = "A" * 256

        # Pydantic schema validation catches max_length=255
        with self.assertRaises(Exception):
            PrintOrder(
                student_name="Rohan Sharma",
                file_name="assignment.pdf",
                stored_file_name="doc_valid.pdf",
                copies=1,
                print_type="black_white",
                print_sides="single",
                fulfillment_type="delivery",
                delivery_location_name="Boys Hostel Block A",
                delivery_room_details=long_room,
            )

    def test_delivery_with_active_db_locations_valid_id(self):
        """With active DB locations, valid delivery_location_id derives name and binds into SQL."""
        db_locations = [
            {"id": 42, "location_code": "ACAD_1", "name": "Academic Complex 1"},
        ]
        db = MockLaunchBlockerDB(
            active_locations=db_locations,
            uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}],
        )

        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="assignment.pdf",
            stored_file_name="doc_valid.pdf",
            copies=2,
            print_type="colour",
            print_sides="double",
            fulfillment_type="delivery",
            delivery_location_id=42,
            delivery_room_details="Room 304, 3rd Floor",
            delivery_time_window="Morning (9:00 AM - 12:00 PM)",
        )

        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = create_order(order, current_student=self.test_student)

        self.assertEqual(res["order_id"], 501)
        self.assertEqual(res["fulfillment_type"], "delivery")
        self.assertEqual(res["delivery_fee"], 5.00)
        self.assertEqual(res["delivery_location_name"], "Academic Complex 1")
        self.assertEqual(res["delivery_room_details"], "Room 304, 3rd Floor")
        self.assertEqual(res["delivery_time_window"], "Morning (9:00 AM - 12:00 PM)")

        # Verify SQL queries executed
        order_insert = next(q for q in db.executed_queries if "INSERT INTO print_orders" in q["query"])
        params = order_insert["params"]
        # Params: student_db_id, student_name, file_name, stored_file_name, copies, print_type, print_sides,
        #         fulfillment_type, delivery_fee, delivery_location_id, delivery_location_name,
        #         delivery_room_details, delivery_time_window
        self.assertEqual(params[0], 101)
        self.assertEqual(params[7], "delivery")
        self.assertEqual(params[8], 5.00)
        self.assertEqual(params[9], 42)
        self.assertEqual(params[10], "Academic Complex 1")
        self.assertEqual(params[11], "Room 304, 3rd Floor")
        self.assertEqual(params[12], "Morning (9:00 AM - 12:00 PM)")

        # Verify delivery_jobs insert
        job_insert = next(q for q in db.executed_queries if "INSERT INTO delivery_jobs" in q["query"])
        job_params = job_insert["params"]
        self.assertEqual(job_params[0], 501)  # order_id
        self.assertEqual(job_params[1], 5.00)  # delivery_fee

    def test_delivery_with_active_db_locations_invalid_id_rejected(self):
        """With active DB locations, submitting an invalid or inactive ID raises HTTP 400."""
        db_locations = [
            {"id": 42, "location_code": "ACAD_1", "name": "Academic Complex 1"},
        ]
        db = MockLaunchBlockerDB(
            active_locations=db_locations,
            uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}],
        )

        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="assignment.pdf",
            stored_file_name="doc_valid.pdf",
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="delivery",
            delivery_location_id=999,
            delivery_room_details="Room 101",
        )

        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            with self.assertRaises(HTTPException) as ctx:
                create_order(order, current_student=self.test_student)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Invalid or inactive campus delivery location ID: 999", ctx.exception.detail)

    def test_delivery_fallback_mode_valid_canonical_name(self):
        """When campus_locations table is empty, a canonical fallback location name is accepted."""
        db = MockLaunchBlockerDB(
            active_locations=[],
            uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}],
        )

        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="notes.pdf",
            stored_file_name="doc_valid.pdf",
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="delivery",
            delivery_location_name="Boys Hostel Block A",
            delivery_room_details="Room 204",
        )

        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = create_order(order, current_student=self.test_student)

        self.assertEqual(res["order_id"], 501)
        self.assertEqual(res["delivery_location_name"], "Boys Hostel Block A")
        self.assertEqual(res["delivery_room_details"], "Room 204")
        self.assertEqual(res["delivery_fee"], 5.00)

        # Confirm bound SQL params: delivery_location_id must be None
        order_insert = next(q for q in db.executed_queries if "INSERT INTO print_orders" in q["query"])
        params = order_insert["params"]
        self.assertIsNone(params[9])  # delivery_location_id
        self.assertEqual(params[10], "Boys Hostel Block A")
        self.assertEqual(params[11], "Room 204")

    def test_delivery_fallback_mode_arbitrary_untrusted_name_rejected(self):
        """When campus_locations table is empty, an unrecognized arbitrary location name is rejected."""
        db = MockLaunchBlockerDB(
            active_locations=[],
            uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}],
        )

        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="notes.pdf",
            stored_file_name="doc_valid.pdf",
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="delivery",
            delivery_location_name="Off Campus Apartment Block X",
            delivery_room_details="Room 204",
        )

        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            with self.assertRaises(HTTPException) as ctx:
                create_order(order, current_student=self.test_student)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Invalid campus delivery location", ctx.exception.detail)

    def test_pickup_order_explicitly_clears_destination_and_sets_zero_fee(self):
        """Self pickup orders force delivery destination fields to NULL, fee to ₹0, and creates NO delivery job."""
        db = MockLaunchBlockerDB(
            uploads=[{"stored_file_name": "doc_valid.pdf", "student_db_id": 101}],
        )

        order = PrintOrder(
            student_name="Rohan Sharma",
            file_name="notes.pdf",
            stored_file_name="doc_valid.pdf",
            copies=1,
            print_type="black_white",
            print_sides="single",
            fulfillment_type="pickup",
            # Even if client accidentally sends delivery fields, backend forces them to None
            delivery_location_id=1,
            delivery_location_name="Accidental Location",
            delivery_room_details="Accidental Room",
            delivery_time_window="Accidental Window",
        )

        with patch.object(main, "get_db_connection", return_value=MockLaunchBlockerConnection(db)):
            res = create_order(order, current_student=self.test_student)

        self.assertEqual(res["order_id"], 501)
        self.assertEqual(res["fulfillment_type"], "pickup")
        self.assertEqual(res["delivery_fee"], 0.00)
        self.assertNotIn("delivery_location_name", res)
        self.assertNotIn("delivery_room_details", res)
        self.assertNotIn("delivery_job_id", res)

        # Inspect SQL insert query params
        order_insert = next(q for q in db.executed_queries if "INSERT INTO print_orders" in q["query"])
        params = order_insert["params"]
        self.assertEqual(params[7], "pickup")
        self.assertEqual(params[8], 0.00)  # delivery_fee
        self.assertIsNone(params[9])       # delivery_location_id
        self.assertIsNone(params[10])      # delivery_location_name
        self.assertIsNone(params[11])      # delivery_room_details
        self.assertIsNone(params[12])      # delivery_time_window

        # Verify NO insert into delivery_jobs occurred
        delivery_job_inserts = [q for q in db.executed_queries if "INSERT INTO delivery_jobs" in q["query"]]
        self.assertEqual(len(delivery_job_inserts), 0)


if __name__ == "__main__":
    unittest.main()
