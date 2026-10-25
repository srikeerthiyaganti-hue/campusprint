from fastapi import UploadFile, File, HTTPException, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pathlib import Path
import uuid
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
import hmac
import pymysql
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import time
import logging
import json
import threading
from collections import defaultdict
import jwt
from pwdlib import PasswordHash
from pydantic import EmailStr
from pydantic import BaseModel, Field
from typing import Literal, Optional, Set, Tuple
import re
import io
import ssl
from storage import get_storage
from pypdf import PdfReader
load_dotenv()
def get_db_connection():
    ca_pem = os.getenv("DB_SSL_CA", "").strip()
    if ca_pem:
        ssl_context = ssl.create_default_context(cadata=ca_pem)
    else:
        ca_path = os.path.join(os.path.dirname(__file__), "ca.pem")
        ssl_context = ssl.create_default_context(cafile=ca_path)
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME", "xeroxqueue_db"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        ssl=ssl_context,
    )



def check_table_exists(conn, table_name: str) -> bool:
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                LIMIT 1
                """,
                (os.getenv("DB_NAME", "xeroxqueue_db"), table_name),
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


def check_columns_in_table(conn, table_name: str) -> Set[str]:
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (os.getenv("DB_NAME", "xeroxqueue_db"), table_name),
            )
            rows = cursor.fetchall()
            return {row["COLUMN_NAME"] for row in rows}
    except Exception:
        return set()

app = FastAPI(
    title="XeroxQueue API",
    description="Backend for the college printing and Xerox ordering platform",
    version="1.0.0",
)


DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:5175",
]


def get_allowed_origins() -> list[str]:
    app_env = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).lower()
    raw = os.getenv("ALLOWED_ORIGINS", "")

    if raw.strip():
        parsed = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
        if any(o == "*" for o in parsed):
            raise RuntimeError(
                "CRITICAL STARTUP CONFIG ERROR: Wildcard '*' origins are strictly forbidden in ALLOWED_ORIGINS when allow_credentials=True."
            )
        if parsed:
            return parsed

    if app_env in ("production", "prod", "staging", "stage"):
        raise RuntimeError(
            f"CRITICAL STARTUP CONFIG ERROR: ALLOWED_ORIGINS must be set to a comma-separated list of explicit domain origins in {app_env}."
        )

    return DEFAULT_ALLOWED_ORIGINS


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


logger = logging.getLogger("xeroxqueue")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

# Structured admin audit log setup
LOGS_DIR = Path(__file__).resolve().parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
audit_log_file = LOGS_DIR / "admin_audit.log"
admin_audit_logger = logging.getLogger("xeroxqueue.audit")
if not admin_audit_logger.handlers:
    try:
        file_handler = logging.FileHandler(str(audit_log_file), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        admin_audit_logger.addHandler(file_handler)
        admin_audit_logger.setLevel(logging.INFO)
    except Exception as exc:
        logger.warning(f"Could not initialize file handler for admin audit log: {exc}")


def log_admin_action(
    admin_username: str,
    action: str,
    target_type: str,
    target_id: any,
    details: Optional[dict] = None,
):
    """
    Records an administrative action into the structured local audit log.
    Provides local structured traceability for administrative state changes.
    (For full non-repudiation in multi-node production, logs should be shipped
    to an external append-only log store).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "admin_username": admin_username,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id),
        "details": details or {},
    }
    try:
        admin_audit_logger.info(json.dumps(entry))
    except Exception as exc:
        logger.error(f"Failed to record admin audit entry: {exc}")


# ---------- RATE LIMITING INFRASTRUCTURE ----------

class SlidingWindowRateLimiter:
    """
    Thread-safe in-process sliding-window rate limiter for brute-force and abuse protection.

    NOTE ON MULTI-WORKER TOPOLOGY:
    In a multi-process or multi-worker deployment (e.g. Gunicorn + multiple Uvicorn workers),
    in-memory limiters are local to each process worker. For globally consistent rate
    limiting across multiple cluster instances or workers, use an external store like Redis
    or enforce rate limits at the Nginx reverse-proxy tier (e.g. `limit_req_zone`).
    This in-process limiter provides baseline per-worker protection.
    """

    def __init__(self, requests_per_window: int = 10, window_seconds: int = 60, max_keys: int = 10000):
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._records = defaultdict(list)
        self._lock = threading.Lock()

    def check_rate_limit(self, key: str) -> Tuple[bool, int, int]:
        """
        Returns (is_allowed, remaining_requests, retry_after_seconds)
        """
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            # Memory protection: prune stale keys if map exceeds max_keys
            if len(self._records) > self.max_keys:
                stale_keys = [k for k, timestamps in self._records.items() if not timestamps or timestamps[-1] < cutoff]
                for k in stale_keys:
                    del self._records[k]

            timestamps = self._records[key]
            # Prune timestamps older than window
            self._records[key] = [t for t in timestamps if t > cutoff]
            current_count = len(self._records[key])

            if current_count >= self.requests_per_window:
                earliest = self._records[key][0]
                retry_after = max(1, int(self.window_seconds - (now - earliest)))
                return False, 0, retry_after

            self._records[key].append(now)
            remaining = self.requests_per_window - (current_count + 1)
            return True, remaining, 0

    def reset(self):
        """Reset all rate limiter state (used in testing)."""
        with self._lock:
            self._records.clear()


auth_rate_limiter = SlidingWindowRateLimiter(requests_per_window=10, window_seconds=60)
upload_rate_limiter = SlidingWindowRateLimiter(requests_per_window=20, window_seconds=60)


TRUSTED_PROXIES = {
    ip.strip()
    for ip in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if ip.strip()
}


def get_client_identifier(request: Optional[Request]) -> str:
    """
    Extracts client IP, respecting X-Forwarded-For only when the direct connection
    originates from a trusted proxy (e.g. local Nginx). Untrusted direct clients
    cannot spoof X-Forwarded-For to evade rate limiting.
    """
    if not request:
        return "127.0.0.1"
    direct_ip = request.client.host if request.client else "127.0.0.1"
    if direct_ip in TRUSTED_PROXIES:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
    return direct_ip


# ---------- SECURITY & TRACING MIDDLEWARE ----------

@app.middleware("http")
async def security_and_tracing_middleware(request: Request, call_next):
    # 1. Request ID (reuse client/proxy ID if valid alphanumeric, or generate uuid4)
    incoming_req_id = request.headers.get("X-Request-ID")
    if incoming_req_id and re.match(r"^[a-zA-Z0-9_\-\.]{1,64}$", incoming_req_id):
        request_id = incoming_req_id
    else:
        request_id = uuid.uuid4().hex

    request.state.request_id = request_id
    start_time = time.time()

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[{request_id}] {request.method} {request.url.path} 500 ERROR ({duration_ms:.2f}ms)")
        raise exc

    duration_ms = (time.time() - start_time) * 1000

    # 2. Attach Request ID to response
    response.headers["X-Request-ID"] = request_id

    # 3. Security Headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"

    # 4. Structured Operational Logging (exclude health probes to reduce log noise)
    if request.url.path not in ("/health", "/api/health"):
        logger.info(f"[{request_id}] {request.method} {request.url.path} {response.status_code} ({duration_ms:.2f}ms)")

    return response


# ---------- STUDENT AUTHENTICATION ----------

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is missing. Check backend/.env"
    )

password_hasher = PasswordHash.recommended()
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60


class StudentSignUp(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    student_id: str = Field(min_length=2, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class StudentLogin(BaseModel):
    identifier: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


def create_access_token(student: dict) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(student["id"]),
        "student_id": student["student_id"],
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }

    return jwt.encode(
        payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
security = HTTPBearer()


def get_current_student(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )
        if payload.get("role") in ("admin", "delivery_partner"):
            raise HTTPException(
                status_code=403,
                detail="Student portal access required. Partner or admin credentials cannot be used here.",
            )
        student_db_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired login token. Please log in again.",
        )

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, full_name, student_id, email
                FROM students
                WHERE id = %s
                """,
                (student_db_id,),
            )
            student = cursor.fetchone()

        if not student:
            raise HTTPException(
                status_code=401,
                detail="Student account not found. Please log in again.",
            )

        return student
    finally:
        conn.close()

@app.post("/api/auth/signup", status_code=201)
def student_signup(data: StudentSignUp, request: Request = None):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = auth_rate_limiter.check_rate_limit(f"signup:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many registration attempts. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(auth_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    full_name = data.full_name.strip()
    student_id = data.student_id.strip()
    email = str(data.email).strip().lower()

    if not full_name or not student_id:
        raise HTTPException(
            status_code=400,
            detail="Name and student ID cannot be empty.",
        )

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO students
                    (full_name, student_id, email, password_hash)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    full_name,
                    student_id,
                    email,
                    password_hasher.hash(data.password),
                ),
            )

            student_db_id = cursor.lastrowid

        conn.commit()

        return {
            "message": "Registration successful. Please log in.",
            "student": {
                "id": student_db_id,
                "full_name": full_name,
                "student_id": student_id,
                "email": email,
            },
        }

    except pymysql.err.IntegrityError as exc:
        conn.rollback()

        if exc.args and exc.args[0] == 1062:
            raise HTTPException(
                status_code=409,
                detail="Student ID or email is already registered.",
            )

        raise HTTPException(
            status_code=500,
            detail="Could not register the student.",
        )

    finally:
        conn.close()


@app.post("/api/auth/login")
def student_login(data: StudentLogin, request: Request = None):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = auth_rate_limiter.check_rate_limit(f"student_login:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(auth_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    identifier = data.identifier.strip()

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, full_name, student_id, email, password_hash
                FROM students
                WHERE student_id = %s OR email = %s
                LIMIT 1
                """,
                (identifier, identifier.lower()),
            )

            student = cursor.fetchone()

        if (
            not student
            or not password_hasher.verify(
                data.password,
                student["password_hash"],
            )
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid student ID/email or password.",
            )

        token = create_access_token(student)

        return {
            "message": "Login successful.",
            "access_token": token,
            "token_type": "bearer",
            "student": {
                "id": student["id"],
                "full_name": student["full_name"],
                "student_id": student["student_id"],
                "email": student["email"],
            },
        }

    finally:
        conn.close()


@app.get("/")
def home():
    return {
        "message": "Welcome to XeroxQueue!",
        "status": "running",
    }


@app.get("/health")
@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "service": "XeroxQueue API",
    }


@app.get("/ready")
@app.get("/api/ready")
def readiness_check():
    """
    Readiness probe: validates that backend process can connect to and query MySQL.
    Returns HTTP 200 if database is reachable and ready to serve requests.
    Returns HTTP 503 if database connection fails or times out.
    """
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError("Database query returned empty result")
            return {
                "status": "ready",
                "service": "XeroxQueue API",
                "database": "connected",
            }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"Readiness check failed: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "XeroxQueue API",
                "database": "disconnected",
                "error": "Database service is not ready to accept traffic.",
            },
        )


@app.get("/api/orders")
def get_orders(
    current_student: dict = Depends(get_current_student),
):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            if check_table_exists(conn, "delivery_jobs"):
                cursor.execute(
                    """
                    SELECT po.*,
                           dj.id AS delivery_job_id,
                           dj.status AS delivery_job_status,
                           dj.partner_id AS delivery_partner_id,
                           dj.accepted_at AS delivery_accepted_at,
                           dj.picked_up_at AS delivery_picked_up_at,
                           dj.delivered_at AS delivery_delivered_at
                    FROM print_orders po
                    LEFT JOIN delivery_jobs dj ON po.id = dj.order_id
                    WHERE po.student_db_id = %s
                    ORDER BY po.created_at DESC
                    """,
                    (current_student["id"],),
                )
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM print_orders
                    WHERE student_db_id = %s
                    ORDER BY created_at DESC
                    """,
                    (current_student["id"],),
                )
            orders = cursor.fetchall()

        return {
            "count": len(orders),
            "orders": orders,
        }
    finally:
        conn.close()


CANONICAL_FALLBACK_LOCATIONS = [
    {
        "id": None,
        "location_code": "HOSTEL_A",
        "name": "Boys Hostel Block A",
        "description": "Ground floor reception / security desk",
        "building_type": "hostel",
    },
    {
        "id": None,
        "location_code": "HOSTEL_B",
        "name": "Girls Hostel Block B",
        "description": "Main gate security checkpoint",
        "building_type": "hostel",
    },
    {
        "id": None,
        "location_code": "LIB_FOYER",
        "name": "Central Library Foyer",
        "description": "Library entrance turnstile lobby",
        "building_type": "library",
    },
    {
        "id": None,
        "location_code": "ACAD_BLOCK_1",
        "name": "Academic Block 1",
        "description": "Main entrance lobby, near stairs",
        "building_type": "academic",
    },
    {
        "id": None,
        "location_code": "MGMT_BLOCK",
        "name": "Management Block",
        "description": "Department reception counter",
        "building_type": "academic",
    },
    {
        "id": None,
        "location_code": "CANTEEN",
        "name": "Campus Food Court / Canteen",
        "description": "Outdoor pickup table",
        "building_type": "other",
    },
]


@app.get("/api/locations")
def get_campus_locations():
    conn = get_db_connection()
    try:
        if check_table_exists(conn, "campus_locations"):
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, location_code, name, description, building_type
                    FROM campus_locations
                    WHERE is_active = 1
                    ORDER BY name ASC
                    """
                )
                db_locations = cursor.fetchall()
                if db_locations:
                    return {"count": len(db_locations), "locations": db_locations, "source": "database"}
        return {
            "count": len(CANONICAL_FALLBACK_LOCATIONS),
            "locations": CANONICAL_FALLBACK_LOCATIONS,
            "source": "fallback",
        }
    finally:
        conn.close()


from pydantic import BaseModel, Field
from typing import Literal, Optional


class PrintOrder(BaseModel):
    student_name: str = Field(min_length=1, max_length=100)
    file_name: str = Field(min_length=1, max_length=255)
    stored_file_name: Optional[str] = None
    copies: int = Field(ge=1, le=100)
    print_type: Literal["black_white", "colour"]
    print_sides: Literal["single", "double"]
    fulfillment_type: Literal["pickup", "delivery"] = "pickup"
    delivery_location_id: Optional[int] = None
    delivery_location_name: Optional[str] = Field(default=None, max_length=100)
    delivery_room_details: Optional[str] = Field(default=None, max_length=255)
    delivery_time_window: Optional[str] = Field(default=None, max_length=100)



class OrderStatusUpdate(BaseModel):
    status: Literal[
        "pending",
        "accepted",
        "printing",
        "ready",
        "completed",
        "cancelled",
    ]


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired admin token. Please log in again.",
        )

    configured_username = os.getenv("ADMIN_USERNAME", "")
    if (
        payload.get("role") != "admin"
        or not configured_username
        or payload.get("sub") != f"admin:{configured_username}"
    ):
        raise HTTPException(
            status_code=403,
            detail="Administrator access required.",
        )

    return {"username": configured_username}


@app.post("/api/admin/login")
def admin_login(data: AdminLogin, request: Request = None):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = auth_rate_limiter.check_rate_limit(f"admin_login:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many admin login attempts. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(auth_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    username = os.getenv("ADMIN_USERNAME", "")
    password = os.getenv("ADMIN_PASSWORD", "")

    if not username or not password:
        raise HTTPException(
            status_code=503,
            detail="Admin credentials are not configured.",
        )

    if not (
        hmac.compare_digest(data.username, username)
        and hmac.compare_digest(data.password, password)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin username or password.",
        )

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": f"admin:{username}",
            "role": "admin",
            "iat": now,
            "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    return {
        "message": "Admin login successful.",
        "access_token": token,
        "token_type": "bearer",
        "admin": {"username": username},
    }

@app.get("/api/admin/orders/{order_id}/document")
def get_order_document(
    order_id: int,
    current_admin: dict = Depends(get_current_admin),
):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_name, stored_file_name
                FROM print_orders
                WHERE id = %s
                """,
                (order_id,),
            )
            order = cursor.fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="Print order not found.",
            )

        stored_name = order.get("stored_file_name")

        if (
            not stored_name
            or Path(stored_name).name != stored_name
            or not stored_name.lower().endswith(".pdf")
        ):
            raise HTTPException(
                status_code=404,
                detail="No valid PDF is attached to this order.",
            )

        storage = get_storage()
        download_filename = Path(order.get("file_name") or "document.pdf").name
        return storage.get_response(stored_name, download_filename=download_filename, inline=True)

    finally:
        conn.close()

@app.get("/api/admin/orders")
def get_all_orders(
    current_admin: dict = Depends(get_current_admin),
):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            if check_table_exists(conn, "delivery_jobs"):
                cursor.execute(
                    """
                    SELECT po.*,
                           dj.id AS delivery_job_id,
                           dj.status AS delivery_job_status,
                           dj.partner_id AS delivery_partner_id,
                           dp.full_name AS delivery_partner_name,
                           dp.phone_number AS delivery_partner_phone
                    FROM print_orders po
                    LEFT JOIN delivery_jobs dj ON po.id = dj.order_id
                    LEFT JOIN delivery_partners dp ON dj.partner_id = dp.id
                    ORDER BY po.created_at DESC
                    """
                )
            else:
                cursor.execute(
                    "SELECT * FROM print_orders ORDER BY created_at DESC"
                )
            orders = cursor.fetchall()

        for order in orders:
            order["page_count"] = None
            order["physical_sheets"] = None
            order["rate_per_sheet"] = None
            order["estimated_price"] = None

            stored_name = order.get("stored_file_name")

            if (
                not stored_name
                or Path(stored_name).name != stored_name
                or not stored_name.lower().endswith(".pdf")
            ):
                continue

            try:
                page_count = get_storage().get_page_count(stored_name)
                if not page_count or page_count < 1:
                    continue

                copies = max(1, int(order.get("copies") or 1))
                pages_per_sheet = (
                    2 if order.get("print_sides") == "double" else 1
                )

                sheets_per_copy = (
                    page_count + pages_per_sheet - 1
                ) // pages_per_sheet

                physical_sheets = sheets_per_copy * copies

                if order.get("print_type") == "black_white":
                    rate_per_sheet = 2
                elif order.get("print_type") == "colour":
                    rate_per_sheet = 10
                else:
                    continue

                order["page_count"] = page_count
                order["physical_sheets"] = physical_sheets
                order["rate_per_sheet"] = rate_per_sheet
                order["estimated_price"] = (
                    physical_sheets * rate_per_sheet
                )

            except Exception:
                # Keep the order visible if its PDF cannot be read.
                continue

        return {"count": len(orders), "orders": orders}

    finally:
        conn.close()


@app.patch("/api/orders/{order_id}/status")
def update_order_status(
    order_id: int,
    update: OrderStatusUpdate,
    current_admin: dict = Depends(get_current_admin),
):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, status FROM print_orders WHERE id = %s",
                (order_id,),
            )
            order_row = cursor.fetchone()
            if not order_row:
                raise HTTPException(
                    status_code=404,
                    detail="Order not found",
                )

            current_status = order_row.get("status")

            # Idempotent no-op if status is unchanged
            if current_status == update.status:
                return {
                    "message": "Order status updated successfully",
                    "order_id": order_id,
                    "status": update.status,
                }

            # Enforce terminal state immutability for cancelled orders
            if current_status == "cancelled":
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot change status of cancelled order #{order_id}.",
                )

            cursor.execute(
                "UPDATE print_orders SET status = %s WHERE id = %s",
                (update.status, order_id),
            )

            if update.status == "cancelled" and check_table_exists(connection, "delivery_jobs"):
                cursor.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'cancelled',
                        cancelled_at = NOW(),
                        cancelled_by = 'admin',
                        cancellation_reason = 'Print order cancelled by shop administrator',
                        version = version + 1
                    WHERE order_id = %s
                      AND status IN ('available', 'assigned')
                    """,
                    (order_id,),
                )

        connection.commit()

        log_admin_action(
            admin_username=current_admin.get("username", "admin"),
            action="update_order_status",
            target_type="print_order",
            target_id=order_id,
            details={"from_status": current_status, "to_status": update.status},
        )

        return {
            "message": "Order status updated successfully",
            "order_id": order_id,
            "status": update.status,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.post("/api/orders", status_code=201)
def create_order(
    order: PrintOrder,
    current_student: dict = Depends(get_current_student),
):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            if not order.stored_file_name:
                raise HTTPException(
                    status_code=400,
                    detail="Please upload a PDF before placing your order.",
                )

            if order.stored_file_name:
                if (
                    Path(order.stored_file_name).name != order.stored_file_name
                    or not order.stored_file_name.endswith(".pdf")
                    or len(order.stored_file_name) > 255
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid stored PDF filename.",
                    )

                cursor.execute(
                    """
                    SELECT id
                    FROM uploaded_documents
                    WHERE stored_file_name = %s
                      AND student_db_id = %s
                    """,
                    (order.stored_file_name, current_student["id"]),
                )
                owned_upload = cursor.fetchone()

                if not owned_upload:
                    raise HTTPException(
                        status_code=403,
                        detail="This PDF upload does not belong to your account. Please upload the PDF again.",
                    )


            # Validate fulfillment details
            delivery_location_id = None
            delivery_location_name = None
            delivery_room_details = None
            delivery_time_window = None
            delivery_fee = 5.00 if order.fulfillment_type == "delivery" else 0.00

            if order.fulfillment_type == "delivery":
                # Validate room details (must be non-empty, trimmed string 1-255 characters)
                raw_room = (order.delivery_room_details or "").strip()
                if not raw_room or len(raw_room) > 255:
                    raise HTTPException(
                        status_code=400,
                        detail="Delivery room/details must be provided (1-255 characters) for campus delivery.",
                    )
                delivery_room_details = raw_room

                # Optional time window (sanitized, max 100 characters)
                raw_window = (order.delivery_time_window or "").strip()
                delivery_time_window = raw_window[:100] if raw_window else None

                # Check if campus_locations exists and has active records
                has_campus_locations_table = check_table_exists(conn, "campus_locations")
                active_db_locations = []
                if has_campus_locations_table:
                    cursor.execute(
                        "SELECT id, location_code, name FROM campus_locations WHERE is_active = 1"
                    )
                    active_db_locations = cursor.fetchall()

                if active_db_locations:
                    # Database-backed validation mode
                    if order.delivery_location_id is not None:
                        matched = next((l for l in active_db_locations if l["id"] == order.delivery_location_id), None)
                        if not matched:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Invalid or inactive campus delivery location ID: {order.delivery_location_id}.",
                            )
                        delivery_location_id = matched["id"]
                        delivery_location_name = matched["name"]
                    else:
                        raw_loc_name = (order.delivery_location_name or "").strip()
                        matched = next((l for l in active_db_locations if l["name"].lower() == raw_loc_name.lower()), None)
                        if not matched:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Campus location '{raw_loc_name}' is not an active delivery hub. Please select a valid campus location.",
                            )
                        delivery_location_id = matched["id"]
                        delivery_location_name = matched["name"]
                else:
                    # Server-defined canonical fallback validation mode
                    raw_loc_name = (order.delivery_location_name or "").strip()
                    matched_fallback = next(
                        (f for f in CANONICAL_FALLBACK_LOCATIONS if f["name"].lower() == raw_loc_name.lower() or f["location_code"].lower() == raw_loc_name.lower()),
                        None,
                    )
                    if not matched_fallback:
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid campus delivery location. Please select a recognized campus hub from the list.",
                        )
                    delivery_location_id = None
                    delivery_location_name = matched_fallback["name"]
            else:
                # Pickup orders: explicitly no destination fields, no delivery fee
                delivery_location_id = None
                delivery_location_name = None
                delivery_room_details = None
                delivery_time_window = None

            # Inspect existing columns on print_orders
            cols = check_columns_in_table(conn, "print_orders")
            has_delivery_schema = "fulfillment_type" in cols
            has_location_cols = "delivery_location_name" in cols and has_delivery_schema

            if has_location_cols:
                cursor.execute(
                    """
                    INSERT INTO print_orders
                        (student_db_id, student_name, file_name,
                         stored_file_name, copies, print_type, print_sides,
                         fulfillment_type, delivery_fee,
                         delivery_location_id, delivery_location_name,
                         delivery_room_details, delivery_time_window)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        current_student["id"],
                        current_student["full_name"],
                        order.file_name,
                        order.stored_file_name,
                        order.copies,
                        order.print_type,
                        order.print_sides,
                        order.fulfillment_type,
                        delivery_fee,
                        delivery_location_id,
                        delivery_location_name,
                        delivery_room_details,
                        delivery_time_window,
                    ),
                )
            elif has_delivery_schema:
                cursor.execute(
                    """
                    INSERT INTO print_orders
                        (student_db_id, student_name, file_name,
                         stored_file_name, copies, print_type, print_sides,
                         fulfillment_type, delivery_fee)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        current_student["id"],
                        current_student["full_name"],
                        order.file_name,
                        order.stored_file_name,
                        order.copies,
                        order.print_type,
                        order.print_sides,
                        order.fulfillment_type,
                        delivery_fee,
                    ),
                )
            else:
                # Baseline schema fallback: migration 001 not yet executed
                cursor.execute(
                    """
                    INSERT INTO print_orders
                        (student_db_id, student_name, file_name,
                         stored_file_name, copies, print_type, print_sides)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        current_student["id"],
                        current_student["full_name"],
                        order.file_name,
                        order.stored_file_name,
                        order.copies,
                        order.print_type,
                        order.print_sides,
                    ),
                )
            order_id = cursor.lastrowid

            delivery_job_id = None
            if order.fulfillment_type == "delivery":
                if check_table_exists(conn, "delivery_jobs"):
                    cursor.execute(
                        """
                        INSERT INTO delivery_jobs
                            (order_id, status, delivery_fee, partner_commission, commission_status, version)
                        VALUES (%s, 'available', %s, 0.00, 'pending', 1)
                        """,
                        (order_id, delivery_fee),
                    )
                    delivery_job_id = cursor.lastrowid

        conn.commit()

        response_data = {
            "message": "Print order created successfully",
            "order_id": order_id,
            "status": "pending",
            "fulfillment_type": order.fulfillment_type,
            "delivery_fee": delivery_fee,
        }
        if delivery_job_id is not None:
            response_data["delivery_job_id"] = delivery_job_id
        if delivery_location_name is not None:
            response_data["delivery_location_name"] = delivery_location_name
        if delivery_room_details is not None:
            response_data["delivery_room_details"] = delivery_room_details
        if delivery_time_window is not None:
            response_data["delivery_time_window"] = delivery_time_window
        if not has_delivery_schema:
            response_data["schema_status"] = "pending_migration_001"
            response_data["schema_note"] = (
                "Delivery selection was accepted. Database storage of fulfillment_type "
                "depends on running migration 001_campus_delivery_system.sql."
            )

        return response_data
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


@app.post("/api/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    current_student: dict = Depends(get_current_student),
    request: Request = None,
):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = upload_rate_limiter.check_rate_limit(f"upload:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many upload requests. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(upload_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are allowed.",
        )

    original_name = Path(file.filename or "").name
    # Sanitize filename: strip control characters, null bytes, and cap length
    original_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name).strip()[:255]

    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="The filename must end in .pdf.",
        )

    saved_name = f"{uuid.uuid4().hex}.pdf"
    storage = get_storage()
    chunks = []
    size = 0

    try:
        first_chunk = True
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            if first_chunk:
                # Layered validation: verify PDF magic bytes header %PDF-
                if not chunk.startswith(b"%PDF-"):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid PDF file format. The file does not have a valid PDF header.",
                    )
                first_chunk = False

            size += len(chunk)

            if size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail="PDF must be 10 MB or smaller.",
                )

            chunks.append(chunk)

        if size == 0:
            raise HTTPException(
                status_code=400,
                detail="The uploaded PDF is empty.",
            )

        pdf_bytes = b"".join(chunks)

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))

            if reader.is_encrypted:
                raise HTTPException(
                    status_code=400,
                    detail="Password-protected PDFs are not supported.",
                )

            page_count = len(reader.pages)

            if page_count < 1:
                raise HTTPException(
                    status_code=400,
                    detail="The PDF contains no pages.",
                )

        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="The uploaded file is not a valid readable PDF.",
            )

        # Store through storage abstraction
        try:
            storage.save(saved_name, pdf_bytes, content_type="application/pdf")
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Failed to store uploaded document {saved_name}: {exc.__class__.__name__}")
            raise HTTPException(
                status_code=500,
                detail="Failed to store uploaded document. Please try again.",
            )

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO uploaded_documents
                        (student_db_id, stored_file_name, original_file_name)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        current_student["id"],
                        saved_name,
                        original_name,
                    ),
                )
            conn.commit()
        except Exception as exc:
            # Database recording failure: attempt to clean up only that newly created file/object
            logger.error(f"Failed to record upload in database for {saved_name}: {exc.__class__.__name__}")
            try:
                storage.delete(saved_name)
            except Exception as del_exc:
                logger.warning(f"Failed to clean up newly stored file {saved_name} after DB error: {del_exc.__class__.__name__}")
            raise HTTPException(
                status_code=500,
                detail="Database error while recording upload. The file was not saved.",
            )
        finally:
            conn.close()

        return {
            "message": "PDF uploaded successfully.",
            "file_name": original_name,
            "stored_file_name": saved_name,
            "size_bytes": size,
            "page_count": page_count,
        }

    finally:
        await file.close()


# ---------- DELIVERY PARTNER SYSTEM (PHASE 2) ----------

def normalize_student_id(student_id: str) -> str:
    return student_id.strip().lower()


def derive_college_email(student_id: str) -> str:
    norm_id = normalize_student_id(student_id)
    return f"vu.{norm_id}@gmail.com"


def validate_phone_number(phone: str) -> bool:
    cleaned = re.sub(r"[\s\-]", "", phone.strip())
    return bool(re.match(r"^\+?[0-9]{8,15}$", cleaned))


class PartnerSignUp(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    student_id: str = Field(min_length=2, max_length=50)
    phone_number: str = Field(min_length=8, max_length=20)
    password: str = Field(min_length=8, max_length=128)


class PartnerLogin(BaseModel):
    identifier: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class PartnerApprovalUpdate(BaseModel):
    status: Optional[Literal["approved", "rejected", "pending"]] = None
    action: Optional[Literal["approve", "reject", "revoke", "pending"]] = None
    rejection_reason: Optional[str] = Field(default=None, max_length=255)


def create_partner_access_token(partner: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"partner:{partner['id']}",
        "partner_id": partner["id"],
        "student_id": partner["student_id"],
        "role": "delivery_partner",
        "approval_status": partner.get("approval_status", "pending"),
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_partner(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )
        if payload.get("role") != "delivery_partner":
            raise HTTPException(
                status_code=403,
                detail="Delivery partner access required. Other credentials cannot be used here.",
            )
        partner_id = payload.get("partner_id")
        if not partner_id:
            sub = str(payload.get("sub", ""))
            if sub.startswith("partner:"):
                partner_id = int(sub.split(":")[1])
            else:
                partner_id = int(sub)
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired partner token. Please sign in again.",
        )

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_partners"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery partner database table does not exist yet. Please execute migration 001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM delivery_partners
                WHERE id = %s
                """,
                (partner_id,),
            )
            partner = cursor.fetchone()

        if not partner:
            raise HTTPException(
                status_code=401,
                detail="Delivery partner account not found. Please sign in again.",
            )

        if not partner.get("is_active", 1):
            raise HTTPException(
                status_code=403,
                detail="Delivery partner account has been deactivated or revoked. Please contact administrator.",
            )

        status = partner.get("approval_status")
        if not status:
            status = "approved" if partner.get("is_approved") else "pending"
        partner["approval_status"] = status

        return partner
    finally:
        conn.close()


def get_current_approved_partner(
    partner: dict = Depends(get_current_partner),
) -> dict:
    status = partner.get("approval_status") or (
        "approved" if partner.get("is_approved") else "pending"
    )
    if status == "pending":
        raise HTTPException(
            status_code=403,
            detail="Your delivery partner application is pending administrator approval. You cannot access delivery jobs until approved.",
        )
    if status == "rejected":
        raise HTTPException(
            status_code=403,
            detail="Your delivery partner application has been rejected or revoked. Contact administrator for details.",
        )
    if status != "approved" or not partner.get("is_approved"):
        raise HTTPException(
            status_code=403,
            detail="Approved delivery partner access required.",
        )
    return partner


@app.post("/api/partner/auth/signup", status_code=201)
def partner_signup(data: PartnerSignUp, request: Request = None):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = auth_rate_limiter.check_rate_limit(f"partner_signup:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many partner registration attempts. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(auth_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    full_name = data.full_name.strip()
    norm_student_id = normalize_student_id(data.student_id)
    raw_phone = data.phone_number.strip()
    clean_phone = re.sub(r"[\s\-]", "", raw_phone)

    if not full_name:
        raise HTTPException(status_code=400, detail="Full name cannot be empty.")
    if not norm_student_id:
        raise HTTPException(status_code=400, detail="Student ID cannot be empty.")
    if not validate_phone_number(clean_phone):
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number format. Please enter an 8 to 15 digit phone number.",
        )

    derived_email = derive_college_email(norm_student_id)

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_partners"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery partner schema has not been applied yet. Database table 'delivery_partners' is missing. Please run backend/migrations/001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        cols = check_columns_in_table(conn, "delivery_partners")

        with conn.cursor() as cursor:
            # Check unique constraints explicitly before insert
            cursor.execute(
                """
                SELECT id, student_id, phone_number, email
                FROM delivery_partners
                WHERE student_id = %s OR phone_number = %s OR email = %s
                LIMIT 1
                """,
                (norm_student_id, clean_phone, derived_email),
            )
            existing = cursor.fetchone()
            if existing:
                if existing.get("student_id") == norm_student_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Student ID '{norm_student_id}' is already registered as a delivery partner.",
                    )
                if existing.get("phone_number") == clean_phone:
                    raise HTTPException(
                        status_code=409,
                        detail="This phone number is already registered to another delivery partner.",
                    )
                if existing.get("email") == derived_email:
                    raise HTTPException(
                        status_code=409,
                        detail=f"College email '{derived_email}' is already registered as a delivery partner.",
                    )

            # Check if student exists in students table to link student_db_id without creating duplicate
            student_db_id = None
            if "student_db_id" in cols:
                cursor.execute(
                    "SELECT id FROM students WHERE student_id = %s LIMIT 1",
                    (norm_student_id,),
                )
                student_record = cursor.fetchone()
                if student_record:
                    student_db_id = student_record["id"]

            hashed_pw = password_hasher.hash(data.password)

            if "approval_status" in cols:
                cursor.execute(
                    """
                    INSERT INTO delivery_partners
                        (full_name, phone_number, student_id, email, password_hash,
                         student_db_id, approval_status, is_approved, is_active, availability_status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, 1, 'offline')
                    """,
                    (full_name, clean_phone, norm_student_id, derived_email, hashed_pw, student_db_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO delivery_partners
                        (full_name, phone_number, student_id, email, password_hash,
                         student_db_id, is_approved, is_active, availability_status)
                    VALUES (%s, %s, %s, %s, %s, %s, 0, 1, 'offline')
                    """,
                    (full_name, clean_phone, norm_student_id, derived_email, hashed_pw, student_db_id),
                )

            partner_id = cursor.lastrowid
        conn.commit()

        return {
            "message": "Delivery partner application submitted successfully. Your account is pending administrator approval.",
            "status": "pending",
            "partner": {
                "id": partner_id,
                "full_name": full_name,
                "student_id": norm_student_id,
                "phone_number": clean_phone,
                "email": derived_email,
                "approval_status": "pending",
                "is_approved": False,
                "student_account_linked": student_db_id is not None,
            },
        }
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        if exc.args and exc.args[0] == 1062:
            raise HTTPException(
                status_code=409,
                detail="Student ID, phone number, or derived college email is already registered.",
            )
        raise HTTPException(
            status_code=500,
            detail="Could not submit delivery partner application due to a database constraint error.",
        )
    finally:
        conn.close()


@app.post("/api/partner/auth/login")
def partner_login(data: PartnerLogin, request: Request = None):
    if request:
        client_id = get_client_identifier(request)
        allowed, remaining, retry_after = auth_rate_limiter.check_rate_limit(f"partner_login:{client_id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many partner login attempts. Please try again later.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(auth_rate_limiter.requests_per_window),
                    "X-RateLimit-Remaining": "0",
                },
            )

    raw_ident = data.identifier.strip()
    norm_ident = normalize_student_id(raw_ident)
    derived_ident_email = derive_college_email(norm_ident)
    clean_phone = re.sub(r"[\s\-]", "", raw_ident)

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_partners"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery partner schema has not been applied yet. Database table 'delivery_partners' is missing. Please run backend/migrations/001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM delivery_partners
                WHERE student_id = %s OR phone_number = %s OR email = %s OR email = %s
                LIMIT 1
                """,
                (norm_ident, clean_phone, raw_ident.lower(), derived_ident_email),
            )
            partner = cursor.fetchone()

        if not partner or not password_hasher.verify(data.password, partner["password_hash"]):
            raise HTTPException(
                status_code=401,
                detail="Invalid student ID, phone number, college email, or password.",
            )

        if not partner.get("is_active", 1):
            raise HTTPException(
                status_code=403,
                detail="Your delivery partner account has been deactivated or revoked by an administrator.",
            )

        status = partner.get("approval_status")
        if not status:
            status = "approved" if partner.get("is_approved") else "pending"

        partner["approval_status"] = status
        token = create_partner_access_token(partner)

        return {
            "message": "Partner login successful.",
            "access_token": token,
            "token_type": "bearer",
            "partner": {
                "id": partner["id"],
                "full_name": partner["full_name"],
                "student_id": partner["student_id"],
                "phone_number": partner["phone_number"],
                "email": partner["email"],
                "approval_status": status,
                "is_approved": bool(partner.get("is_approved")),
                "availability_status": partner.get("availability_status", "offline"),
                "total_deliveries_completed": partner.get("total_deliveries_completed", 0),
                "rating": str(partner.get("rating", "5.00")),
            },
        }
    finally:
        conn.close()


@app.get("/api/partner/profile")
def get_partner_profile(
    current_partner: dict = Depends(get_current_partner),
):
    status = current_partner.get("approval_status") or (
        "approved" if current_partner.get("is_approved") else "pending"
    )
    return {
        "partner": {
            "id": current_partner["id"],
            "full_name": current_partner["full_name"],
            "student_id": current_partner["student_id"],
            "phone_number": current_partner["phone_number"],
            "email": current_partner["email"],
            "approval_status": status,
            "is_approved": bool(current_partner.get("is_approved")),
            "availability_status": current_partner.get("availability_status", "offline"),
            "total_deliveries_completed": current_partner.get("total_deliveries_completed", 0),
            "rating": str(current_partner.get("rating", "5.00")),
            "created_at": current_partner.get("created_at"),
            "approved_at": current_partner.get("approved_at"),
            "approved_by": current_partner.get("approved_by"),
            "rejected_at": current_partner.get("rejected_at"),
            "rejected_by": current_partner.get("rejected_by"),
            "rejection_reason": current_partner.get("rejection_reason"),
        }
    }


@app.get("/api/partner/jobs")
def get_partner_delivery_jobs(
    current_partner: dict = Depends(get_current_approved_partner),
):
    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_jobs"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery jobs table does not exist yet. Please execute migration 001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            # Query delivery jobs - zero access to PDF files or student document paths
            cursor.execute(
                """
                SELECT dj.id, dj.order_id, dj.status, dj.delivery_fee, dj.partner_commission,
                       dj.accepted_at, dj.picked_up_at, dj.delivered_at, dj.created_at,
                       dj.partner_id,
                       po.student_name, po.delivery_location_name, po.delivery_room_details,
                       po.delivery_time_window, po.delivery_deadline
                FROM delivery_jobs dj
                JOIN print_orders po ON dj.order_id = po.id AND po.status != 'cancelled'
                WHERE (dj.status = 'available' AND dj.partner_id IS NULL)
                   OR (dj.status IN ('assigned', 'picked_up') AND dj.partner_id = %s)
                ORDER BY dj.created_at DESC
                """,
                (current_partner["id"],),
            )
            jobs = cursor.fetchall()

        return {"count": len(jobs), "jobs": jobs}
    finally:
        conn.close()


@app.post("/api/partner/jobs/{job_id}/accept")
def accept_delivery_job(
    job_id: int,
    current_partner: dict = Depends(get_current_approved_partner),
):
    if job_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid delivery job ID.")

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_jobs"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery jobs table does not exist yet. Please execute migration 001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            # Atomic conditional update: only claim if status is 'available', partner_id IS NULL, and order is not cancelled
            cursor.execute(
                """
                UPDATE delivery_jobs dj
                JOIN print_orders po ON dj.order_id = po.id
                SET dj.partner_id = %s,
                    dj.status = 'assigned',
                    dj.accepted_at = NOW(),
                    dj.version = dj.version + 1
                WHERE dj.id = %s
                  AND dj.status = 'available'
                  AND dj.partner_id IS NULL
                  AND po.status != 'cancelled'
                """,
                (current_partner["id"], job_id),
            )
            claimed = cursor.rowcount

            if claimed == 0:
                # Check why the claim could not be completed
                cursor.execute(
                    """
                    SELECT dj.id, dj.status, dj.partner_id, po.status AS order_status
                    FROM delivery_jobs dj
                    LEFT JOIN print_orders po ON dj.order_id = po.id
                    WHERE dj.id = %s
                    """,
                    (job_id,),
                )
                existing = cursor.fetchone()
                conn.rollback()

                if not existing:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Delivery job #{job_id} not found.",
                    )
                if existing.get("order_status") == "cancelled" or existing.get("status") == "cancelled":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot accept delivery job #{job_id} because the print order has been cancelled.",
                    )
                if existing.get("partner_id") == current_partner["id"]:
                    raise HTTPException(
                        status_code=409,
                        detail=f"You have already accepted delivery job #{job_id}.",
                    )
                if existing.get("partner_id") is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has already been claimed by another delivery partner.",
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"Delivery job #{job_id} is not available for claiming (current status: '{existing.get('status')}').",
                )

        conn.commit()
        return {
            "message": f"Delivery job #{job_id} successfully accepted.",
            "job_id": job_id,
            "status": "assigned",
            "partner_id": current_partner["id"],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/partner/jobs/{job_id}/pickup")
def confirm_job_pickup(
    job_id: int,
    current_partner: dict = Depends(get_current_approved_partner),
):
    if job_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid delivery job ID.")

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_jobs"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery jobs table does not exist yet. Please execute migration 001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            # Atomic conditional update: only assigned partner can transition from 'assigned' to 'picked_up' if order is not cancelled
            cursor.execute(
                """
                UPDATE delivery_jobs dj
                JOIN print_orders po ON dj.order_id = po.id
                SET dj.status = 'picked_up',
                    dj.picked_up_at = NOW(),
                    dj.version = dj.version + 1
                WHERE dj.id = %s
                  AND dj.partner_id = %s
                  AND dj.status = 'assigned'
                  AND po.status != 'cancelled'
                """,
                (job_id, current_partner["id"]),
            )
            updated = cursor.rowcount

            if updated == 0:
                cursor.execute(
                    """
                    SELECT dj.id, dj.status, dj.partner_id, po.status AS order_status
                    FROM delivery_jobs dj
                    LEFT JOIN print_orders po ON dj.order_id = po.id
                    WHERE dj.id = %s
                    """,
                    (job_id,),
                )
                existing = cursor.fetchone()
                conn.rollback()

                if not existing:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Delivery job #{job_id} not found.",
                    )
                if existing.get("status") == "available" or existing.get("partner_id") is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has not been accepted yet.",
                    )
                if existing.get("partner_id") != current_partner["id"]:
                    raise HTTPException(
                        status_code=403,
                        detail=f"You are not the assigned delivery partner for job #{job_id}.",
                    )
                if existing.get("order_status") == "cancelled" or existing.get("status") == "cancelled":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot mark job #{job_id} as picked up because the print order has been cancelled.",
                    )
                if existing.get("status") == "picked_up":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has already been marked as picked up.",
                    )
                if existing.get("status") == "delivered":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has already been completed and delivered.",
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot mark job #{job_id} as picked up from current status '{existing.get('status')}'.",
                )

        conn.commit()
        return {
            "message": f"Delivery job #{job_id} marked as picked up from the print shop.",
            "job_id": job_id,
            "status": "picked_up",
            "partner_id": current_partner["id"],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/partner/jobs/{job_id}/deliver")
def confirm_job_delivery(
    job_id: int,
    current_partner: dict = Depends(get_current_approved_partner),
):
    if job_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid delivery job ID.")

    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_jobs"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Delivery jobs table does not exist yet. Please execute migration 001_campus_delivery_system.sql on xeroxqueue_db.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            # Atomic conditional update: only assigned partner can transition from 'picked_up' to 'delivered' if order is not cancelled
            cursor.execute(
                """
                UPDATE delivery_jobs dj
                JOIN print_orders po ON dj.order_id = po.id
                SET dj.status = 'delivered',
                    dj.delivered_at = NOW(),
                    dj.version = dj.version + 1
                WHERE dj.id = %s
                  AND dj.partner_id = %s
                  AND dj.status = 'picked_up'
                  AND po.status != 'cancelled'
                """,
                (job_id, current_partner["id"]),
            )
            updated = cursor.rowcount

            if updated == 0:
                cursor.execute(
                    """
                    SELECT dj.id, dj.status, dj.partner_id, po.status AS order_status
                    FROM delivery_jobs dj
                    LEFT JOIN print_orders po ON dj.order_id = po.id
                    WHERE dj.id = %s
                    """,
                    (job_id,),
                )
                existing = cursor.fetchone()
                conn.rollback()

                if not existing:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Delivery job #{job_id} not found.",
                    )
                if existing.get("status") == "available" or existing.get("partner_id") is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has not been accepted or picked up yet.",
                    )
                if existing.get("partner_id") != current_partner["id"]:
                    raise HTTPException(
                        status_code=403,
                        detail=f"You are not the assigned delivery partner for job #{job_id}.",
                    )
                if existing.get("order_status") == "cancelled" or existing.get("status") == "cancelled":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot mark job #{job_id} as delivered because the print order has been cancelled.",
                    )
                if existing.get("status") == "delivered":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} has already been marked as delivered.",
                    )
                if existing.get("status") == "assigned":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Delivery job #{job_id} must be marked as picked up before delivery can be confirmed.",
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot mark job #{job_id} as delivered from current status '{existing.get('status')}'.",
                )

            # Increment partner's completed deliveries count within the same transaction
            cursor.execute(
                """
                UPDATE delivery_partners
                SET total_deliveries_completed = total_deliveries_completed + 1
                WHERE id = %s
                """,
                (current_partner["id"],),
            )

        conn.commit()
        return {
            "message": f"Delivery job #{job_id} successfully confirmed as delivered to the student.",
            "job_id": job_id,
            "status": "delivered",
            "partner_id": current_partner["id"],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/admin/partners")
def admin_get_partners(
    status: Optional[str] = None,
    current_admin: dict = Depends(get_current_admin),
):
    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_partners"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Table 'delivery_partners' does not exist in xeroxqueue_db. Migration 001_campus_delivery_system.sql has not been executed yet.",
                    "schema_status": "pending_migration_001",
                },
            )

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, full_name, phone_number, student_id, email,
                       approval_status, is_approved, is_active, availability_status,
                       total_deliveries_completed, rating, created_at, approved_at,
                       approved_by, rejected_at, rejected_by, rejection_reason
                FROM delivery_partners
                ORDER BY created_at DESC
                """
            )
            partners = cursor.fetchall()

        for p in partners:
            app_status = p.get("approval_status")
            if not app_status:
                app_status = "approved" if p.get("is_approved") else "pending"
            p["approval_status"] = app_status

        if status and status != "all":
            partners = [p for p in partners if p.get("approval_status") == status]

        return {"count": len(partners), "partners": partners}
    finally:
        conn.close()


@app.patch("/api/admin/partners/{partner_id}/approval")
def admin_update_partner_approval(
    partner_id: int,
    data: PartnerApprovalUpdate,
    current_admin: dict = Depends(get_current_admin),
):
    conn = get_db_connection()
    try:
        if not check_table_exists(conn, "delivery_partners"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Table 'delivery_partners' does not exist in xeroxqueue_db. Migration 001_campus_delivery_system.sql has not been executed yet.",
                    "schema_status": "pending_migration_001",
                },
            )

        target_status = data.status
        if data.action:
            if data.action == "approve":
                target_status = "approved"
            elif data.action in ("reject", "revoke"):
                target_status = "rejected"
            elif data.action == "pending":
                target_status = "pending"

        if target_status not in ("approved", "rejected", "pending"):
            raise HTTPException(
                status_code=400,
                detail="Invalid approval status. Must be 'approved', 'rejected', or 'pending'.",
            )

        admin_username = current_admin["username"]
        cols = check_columns_in_table(conn, "delivery_partners")

        with conn.cursor() as cursor:
            cursor.execute("SELECT id, full_name, student_id FROM delivery_partners WHERE id = %s", (partner_id,))
            partner = cursor.fetchone()
            if not partner:
                raise HTTPException(status_code=404, detail="Delivery partner not found.")

            if target_status == "approved":
                if "approval_status" in cols:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET approval_status = 'approved',
                            is_approved = 1,
                            is_active = 1,
                            approved_by = %s,
                            approved_at = NOW(),
                            rejected_by = NULL,
                            rejected_at = NULL,
                            rejection_reason = NULL
                        WHERE id = %s
                        """,
                        (admin_username, partner_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET is_approved = 1,
                            is_active = 1,
                            approved_by = %s,
                            approved_at = NOW()
                        WHERE id = %s
                        """,
                        (admin_username, partner_id),
                    )
                message = f"Delivery partner #{partner_id} ({partner['full_name']}) has been approved."

            elif target_status == "rejected":
                reason = data.rejection_reason or "Application rejected by administrator."
                if "approval_status" in cols:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET approval_status = 'rejected',
                            is_approved = 0,
                            rejected_by = %s,
                            rejected_at = NOW(),
                            rejection_reason = %s
                        WHERE id = %s
                        """,
                        (admin_username, reason, partner_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET is_approved = 0,
                            is_active = 0
                        WHERE id = %s
                        """,
                        (partner_id,),
                    )
                message = f"Delivery partner #{partner_id} ({partner['full_name']}) has been rejected/revoked."

            else:  # pending
                if "approval_status" in cols:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET approval_status = 'pending',
                            is_approved = 0,
                            approved_by = NULL,
                            approved_at = NULL,
                            rejected_by = NULL,
                            rejected_at = NULL,
                            rejection_reason = NULL
                        WHERE id = %s
                        """,
                        (partner_id,),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE delivery_partners
                        SET is_approved = 0,
                            is_active = 1
                        WHERE id = %s
                        """,
                        (partner_id,),
                    )
                message = f"Delivery partner #{partner_id} ({partner['full_name']}) reset to pending status."

        conn.commit()

        log_admin_action(
            admin_username=admin_username,
            action=f"partner_approval_{target_status}",
            target_type="delivery_partner",
            target_id=partner_id,
            details={
                "status": target_status,
                "partner_name": partner.get("full_name"),
                "student_id": partner.get("student_id"),
                "rejection_reason": data.rejection_reason if target_status == "rejected" else None,
            },
        )

        return {
            "message": message,
            "partner_id": partner_id,
            "status": target_status,
            "updated_by": admin_username,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
