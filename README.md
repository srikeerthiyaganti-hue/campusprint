# XeroxQueue

**Campus Cloud-Print & Delivery Platform**

XeroxQueue is a full-stack web application designed for campus print shops. Students can upload documents, configure print settings (copies, color, duplex), submit print orders, track status in real-time, or request campus delivery to designated hostel/academic hubs. Administrators manage the printing queue, verify documents, and assign deliveries to student delivery partners.

---

## Tech Stack

- **Frontend**: React 19 + Vite 8 (Single Page Application with student portal, admin dashboard, and delivery partner portal)
- **Backend**: FastAPI + Python 3.10+ (ASGI high-performance asynchronous REST API)
- **Database**: MySQL 8.0+
- **PDF Processing**: `pypdf` with layered magic-bytes validation
- **Storage Layer**: Dual-backend storage abstraction supporting local filesystem (`backend/uploads/`) and S3-compatible object storage (e.g. Cloudflare R2, AWS S3)
- **Deployment**: Nginx reverse proxy + systemd service supervisor on Ubuntu Linux

---

## Getting Started (Local Development)

### Prerequisites
- Python 3.10+ with `venv`
- Node.js 18+ and `npm`
- MySQL 8.0+ server running locally

### 1. Database Setup
```sql
CREATE DATABASE xeroxqueue_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'xerox_user'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON xeroxqueue_db.* TO 'xerox_user'@'localhost';
FLUSH PRIVILEGES;
```

Apply database migrations:
```bash
mysql -u xerox_user -p xeroxqueue_db < backend/migrations/001_campus_delivery_system.sql
```

### 2. Backend Setup
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy example environment configuration
cp .env.example .env
# Edit .env with your local MySQL credentials
```

Start the FastAPI development server:
```bash
uvicorn main:app --reload --port 8000
```
Backend will be available at `http://127.0.0.1:8000`. API docs at `http://127.0.0.1:8000/docs`.

### 3. Frontend Setup
```bash
cd frontend
npm install
npm run dev
```
Frontend will be available at `http://localhost:5173`.

---

## Durable PDF Storage Configuration

XeroxQueue supports two storage backends for PDF documents:

### 1. Local Filesystem (`STORAGE_BACKEND=local`)
- **Default for local development**.
- Stores uploaded files directly in `backend/uploads/`.
- Requires zero cloud credentials or external network setup.
- **Limitation**: On ephemeral cloud hosts (e.g. Render, Railway, Heroku), local disk storage is wiped clean on container restarts or redeployments.

### 2. S3-Compatible Object Storage (`STORAGE_BACKEND=s3`)
- **Recommended for cloud deployments** (e.g. Cloudflare R2, AWS S3, MinIO).
- Eliminates ephemeral filesystem loss on containerized hosts.
- Cloudflare R2 provides 10 GB free storage/month with **$0 egress bandwidth fees**.

#### Required Environment Variables for S3 Mode:
```env
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
S3_BUCKET_NAME=xeroxqueue-pdfs
S3_ACCESS_KEY_ID=your_access_key_id
S3_SECRET_ACCESS_KEY=your_secret_access_key
S3_REGION=auto
```

#### Storage Guarantees:
- **Strict Validation**: Fails fast on startup if any S3 configuration is incomplete; **never silently falls back to local storage**.
- **Zero Credential Leakage**: S3 credentials, endpoints, and bucket names are never exposed in API errors, logs, or frontend responses.
- **Backend Streaming**: All downloads are streamed through the backend with strict role authorization checks; buckets remain private.
- **Atomic Cleanup**: If database insertion fails during upload, only the newly created file/object is deleted; pre-existing files are never touched.

---

## Testing & Quality Assurance

Run the automated test suite (dual poison-pill database isolation ensures zero live database contact):
```bash
backend/.venv/bin/python -m unittest discover -s backend -p "test_*.py" -v
```

Lint frontend code:
```bash
npm --prefix frontend run lint
```

Build production frontend bundle:
```bash
npm --prefix frontend run build
```

---

## Orphan Cleanup Tool

Identify and prune unreferenced PDFs safely:
```bash
# Dry-run inspection (default - deletes 0 files)
python backend/cleanup_orphans.py --dry-run

# Explicit execution (deletes only confirmed orphans older than 2 hours)
python backend/cleanup_orphans.py --execute --grace-hours 2.0
```
*Note: S3 cloud object cleanup is intentionally disabled to safeguard cloud assets against accidental bulk deletion.*

---

## Deployment

Refer to [`deployment/README.md`](deployment/README.md) for production/staging runbooks, Nginx reverse proxy templates, systemd service units, and safe migration guides.
