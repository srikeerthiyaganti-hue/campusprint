# XeroxQueue Staging Deployment Runbook

Comprehensive operations runbook, security standards, and rollback procedures for deploying XeroxQueue to a controlled staging environment.

---

## 1. Environment & Architecture Specifications

### 1.1 Environment Mode & CORS Fail-Fast Configuration
In staging, the backend enforces fail-fast configuration validation during startup.
- **`APP_ENV=staging`** (or `stage`) must be set in `/opt/xeroxqueue/backend/.env`.
- **`ALLOWED_ORIGINS`** must be an explicit, comma-separated list of fully qualified staging origins, e.g.:
  ```env
  APP_ENV=staging
  ALLOWED_ORIGINS=https://staging.xeroxqueue.example.com
  ```
- **Fail-Fast Security Behavior**:
  - Missing, blank, or whitespace-only `ALLOWED_ORIGINS` causes startup termination (`RuntimeError`).
  - Wildcard origins (`*`) are strictly rejected (`RuntimeError`) because `allow_credentials=True` is enabled.
  - Development localhost defaults (`http://localhost:5173`, etc.) are prohibited and never applied in staging/production.

### 1.2 Database Instance & Naming Requirement
- **Database Name**: `xeroxqueue_db`
- **Instance Isolation**: Staging must run on a **physically or logically isolated MySQL instance** (e.g. dedicated staging database VM, staging container, or separate cloud RDS instance).
- **Hardcoded Schema Rationale**: Schema migration `backend/migrations/001_campus_delivery_system.sql` and rollback script `001_campus_delivery_system_rollback.sql` contain hardcoded `USE xeroxqueue_db;` directives. Deploying to an isolated database host running a database named `xeroxqueue_db` ensures 100% compatibility with standard migration scripts while eliminating any risk of cross-environment data mutation with production.

---

## 2. Server Prerequisites & Filesystem Permissions

### 2.1 Directory Structure & Ownership
Deploy all application components under `/opt/xeroxqueue` owned by the web service user `www-data:www-data`:

```bash
# Create directory structure
sudo mkdir -p /opt/xeroxqueue/backend
sudo mkdir -p /opt/xeroxqueue/backend/uploads
sudo mkdir -p /opt/xeroxqueue/frontend/dist
sudo mkdir -p /opt/xeroxqueue/backups
sudo mkdir -p /opt/xeroxqueue/logs

# Assign ownership to service user
sudo chown -R www-data:www-data /opt/xeroxqueue

# Enforce secure least-privilege permissions
sudo find /opt/xeroxqueue -type d -exec chmod 750 {} +
sudo find /opt/xeroxqueue -type f -exec chmod 640 {} +
sudo chmod 750 /opt/xeroxqueue/backend/uploads
```

### 2.2 Python Virtual Environment
Build an isolated virtual environment using the pinned dependencies in `backend/requirements.txt`:

```bash
cd /opt/xeroxqueue/backend
sudo -u www-data python3 -m venv .venv
sudo -u www-data .venv/bin/pip install --upgrade pip
sudo -u www-data .venv/bin/pip install -r requirements.txt
```

---

---

## 3. Pre-Migration Backup & Restoration Verification

Always verify database connectivity, create a consistent transactional dump of `xeroxqueue_db`, and perform a verified restore into a disposable verification database before executing database migrations.

### 3.0 Operator Preflight & Host Isolation Guard
Before executing any database commands, confirm that you are targeting the staging instance:
```bash
echo "=== TARGET ENVIRONMENT PREFLIGHT ==="
echo "Target DB Host: ${DB_HOST}"
echo "Target DB Name: ${DB_NAME:-xeroxqueue_db}"
echo "Target User:    ${DB_USER}"
# Verify this is NOT the production database host before proceeding!
```

### 3.1 Snapshot Creation
```bash
BACKUP_FILE="/opt/xeroxqueue/backups/xeroxqueue_db_pre_001_$(date +%Y%m%d_%H%M%S).sql"

mysqldump -h "${DB_HOST}" -u "${DB_USER}" -p \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  xeroxqueue_db > "${BACKUP_FILE}"

sudo chown www-data:www-data "${BACKUP_FILE}"
sudo chmod 600 "${BACKUP_FILE}"
```

### 3.2 Backup Integrity Check
Verify the dump completed cleanly before proceeding:
```bash
# Verify dump completion marker
tail -n 10 "${BACKUP_FILE}" | grep "Dump completed"

# Verify non-empty file size
test -s "${BACKUP_FILE}" && echo "Backup file created successfully." || echo "ERROR: Backup file is empty!"
```

### 3.3 Isolated Restoration Test in Disposable Database (`xeroxqueue_verify`)
> [!IMPORTANT]
> **Never test restores over your live `xeroxqueue_db`**. Always validate recovery in a disposable sandbox database first to guarantee the backup is not corrupted.

```bash
# 1. Create disposable verification database
mysql -h "${DB_HOST}" -u "${DB_USER}" -p -e "CREATE DATABASE IF NOT EXISTS xeroxqueue_verify;"

# 2. Restore sanitized copy into xeroxqueue_verify (substituting target DB name)
sed 's/USE `xeroxqueue_db`;/USE `xeroxqueue_verify`;/g; s/`xeroxqueue_db`/`xeroxqueue_verify`/g' "${BACKUP_FILE}" | \
  mysql -h "${DB_HOST}" -u "${DB_USER}" -p xeroxqueue_verify

# 3. Validate tables and representative record counts
mysql -h "${DB_HOST}" -u "${DB_USER}" -p -e "
  SELECT table_name, table_rows
  FROM information_schema.tables
  WHERE table_schema = 'xeroxqueue_verify';
"

# 4. Clean up disposable test database
mysql -h "${DB_HOST}" -u "${DB_USER}" -p -e "DROP DATABASE xeroxqueue_verify;"
echo "Restoration verification passed. Backup is confirmed restorable."
```

### 3.4 Off-Host Backup Copy
Ensure an off-host copy exists in external storage (e.g. S3, dedicated backup vault, or remote host):
```bash
# Example off-host sync
scp "${BACKUP_FILE}" backup-operator@backup-host.internal:/secure-backups/xeroxqueue/
```

---

## 4. Migration Execution & Verification

### 4.1 Apply Migration
Execute migration `001_campus_delivery_system.sql` against the staging database:

```bash
mysql -h "${DB_HOST}" -u "${DB_USER}" -p xeroxqueue_db < /opt/xeroxqueue/backend/migrations/001_campus_delivery_system.sql
```

### 4.2 Migration Verification Queries
Run these queries in MySQL client to confirm schema changes:

```sql
-- 1. Confirm new tables exist
SELECT TABLE_NAME, TABLE_ROWS
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'xeroxqueue_db'
  AND TABLE_NAME IN ('campus_locations', 'delivery_partners', 'delivery_jobs');

-- 2. Confirm columns added to print_orders
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'xeroxqueue_db'
  AND TABLE_NAME = 'print_orders'
  AND COLUMN_NAME IN ('delivery_option', 'delivery_location_id', 'delivery_location_name',
                      'delivery_room_details', 'delivery_fee', 'delivery_deadline');

-- 3. Confirm canonical campus locations seeded
SELECT id, name, building, is_active FROM campus_locations;
```

---

## 5. Service Configuration & Process Management

### 5.1 Systemd Backend Unit Setup
1. Copy template to systemd directory:
   ```bash
   sudo cp /opt/xeroxqueue/deployment/systemd.xeroxqueue-backend.service.template /etc/systemd/system/xeroxqueue-backend.service
   ```
2. Inspect and verify paths and user in `/etc/systemd/system/xeroxqueue-backend.service`:
   - `User=www-data`
   - `Group=www-data`
   - `WorkingDirectory=/opt/xeroxqueue/backend`
   - `ExecStart=/opt/xeroxqueue/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2`
3. Reload systemd and start service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now xeroxqueue-backend.service
   sudo systemctl status xeroxqueue-backend.service
   ```

### 5.2 Nginx Reverse Proxy Setup
1. Copy template to Nginx sites directory:
   ```bash
   sudo cp /opt/xeroxqueue/deployment/nginx.staging.conf.template /etc/nginx/sites-available/xeroxqueue-staging.conf
   ```
2. Configure domain name and TLS certificates in `/etc/nginx/sites-available/xeroxqueue-staging.conf`.
3. Enable site configuration:
   ```bash
   sudo ln -s /etc/nginx/sites-available/xeroxqueue-staging.conf /etc/nginx/sites-enabled/
   ```
4. Test Nginx syntax and reload:
   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   ```

### 5.3 Safe Orphan PDF Cleanup Tool
Use `backend/cleanup_orphans.py` to identify and purge unreferenced uploaded PDFs.
The script protects files referenced in the database, enforces a 2-hour grace period for in-flight uploads, never follows symlinks, and fails closed if the database is unreachable.

```bash
# 1. Dry-run audit (default - zero files deleted)
sudo -u www-data /opt/xeroxqueue/backend/.venv/bin/python /opt/xeroxqueue/backend/cleanup_orphans.py

# 2. Live execution (requires operator decision to delete confirmed orphans)
sudo -u www-data /opt/xeroxqueue/backend/.venv/bin/python /opt/xeroxqueue/backend/cleanup_orphans.py --execute
```

### 5.4 Multi-Worker Rate Limiting Topology
The backend provides an in-process sliding-window rate limiter for `/api/auth/*` and `/api/upload`.
In multi-worker production deployments (e.g. 2+ Uvicorn workers managed by systemd or Gunicorn), in-process rate limits protect each worker process individually.
For globally unified rate limits across all cluster workers, Nginx reverse-proxy rate limiting (`limit_req_zone` defined in `nginx.staging.conf.template`) provides the primary perimeter defense. For distributed multi-server clusters, an external Redis backend is recommended.

---

## 6. End-to-End Staging Verification Checklist

Execute these checks to validate staging readiness:

| Phase | Check | Verification Command / Step | Expected Result |
| :--- | :--- | :--- | :--- |
| **1. Liveness** | Backend Liveness | `curl -fsS http://127.0.0.1:8000/api/health` | `{"status":"healthy","service":"XeroxQueue API"}` (HTTP 200) |
| **2. Readiness** | Database Readiness | `curl -fsS http://127.0.0.1:8000/api/ready` | `{"status":"ready","service":"XeroxQueue API","database":"connected"}` (HTTP 200) |
| **3. Public** | Reverse Proxy Liveness | `curl -fsS https://<STAGING_DOMAIN>/health` | `{"status":"healthy","service":"XeroxQueue API"}` (HTTP 200) |
| **4. CORS** | Fail-Fast Validation | Inspect systemd journal on restart | Startup succeeds only with explicit `ALLOWED_ORIGINS` |
| **5. Headers** | Security Headers | `curl -I https://<STAGING_DOMAIN>/api/health` | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-Request-ID` |
| **6. Student** | PDF Upload & Quoting | Upload PDF via student portal | Page count, sheet count, and pricing calculated |
| **7. Order** | Delivery Fee Isolation | Create delivery order (`₹5.00` fee) vs self-pickup (`₹0.00`) | `delivery_fee` correctly stored; pickup creates 0 delivery jobs |
| **8. Partner** | Application Registration | Submit partner registration via `/api/partner/auth/signup` | Partner created with `approval_status='pending'` |
| **9. Admin** | Partner Approval | Approve partner via admin dashboard | Status changes to `approved`, partner can view jobs |
| **10. Privacy** | Partner Data Shield | Fetch jobs via `/api/partner/jobs` | Student delivery location visible; PDF file paths omitted |
| **11. CAS** | Job Claiming & Pickup | Partner accepts and marks job picked up | Atomic CAS update; version increments |
| **12. CAS** | Idempotent Delivery | Partner confirms delivery; retry same request | Exactly 1 counter increment; retry returns HTTP 409 |
| **13. Safety** | Cancellation Cascade | Admin cancels order with available/assigned job | Job transitions to `cancelled`, `cancelled_by='admin'` |
| **14. Safety** | In-Transit Protection | Admin cancels order with picked_up job | Job remains `picked_up` for physical triage |
| **15. Safety** | Cancelled Action Guard | Partner attempts accept/pickup/delivery on cancelled order | HTTP 409 Conflict returned |
| **16. Security** | Cross-Partner Precedence | Partner B attempts action on Partner A's cancelled job | HTTP 403 Forbidden returned (not 409) |

---

## 7. Rollback Runbook

If any critical defects are detected during staging validation, follow this rollback procedure:

> [!WARNING]
> **MySQL DDL Implicit Commit**: MySQL DDL statements (`ALTER TABLE`, `CREATE TABLE`, `DROP TABLE`) cause an **implicit commit**. They cannot be rolled back via SQL transactions (`ROLLBACK`). If a migration fails halfway through execution, the database remains in a partially migrated state. Always take a pre-migration snapshot before running migrations.

> [!CAUTION]
> **Data Loss Warning on Rollback**: Executing `001_campus_delivery_system_rollback.sql` permanently drops `delivery_jobs`, `delivery_partners`, and `campus_locations`. All partner profiles, delivery history, and commission records will be permanently erased. If data needs to be preserved, export those tables first.

### 7.1 Database Rollback

#### Option A: Clean Schema Rollback (Recommended if tables contain only test data)
```bash
mysql -h "${DB_HOST}" -u "${DB_USER}" -p xeroxqueue_db < /opt/xeroxqueue/backend/migrations/001_campus_delivery_system_rollback.sql
```

#### Option B: Full Pre-Migration Snapshot Restore
```bash
# Stop backend service to prevent in-flight writes
sudo systemctl stop xeroxqueue-backend.service

# Restore database from pre-migration snapshot
mysql -h "${DB_HOST}" -u "${DB_USER}" -p xeroxqueue_db < /opt/xeroxqueue/backups/xeroxqueue_db_pre_001_*.sql
```

### 7.2 Application Code Rollback
```bash
cd /opt/xeroxqueue
sudo -u www-data git checkout <PREVIOUS_STABLE_COMMIT_HASH>

# Restart backend process
sudo systemctl restart xeroxqueue-backend.service

# Confirm service status
sudo systemctl status xeroxqueue-backend.service
```

### 7.3 Post-Rollback Health Verification
```bash
curl -fsS https://<STAGING_DOMAIN>/api/health
```
Confirm response is `{"status": "healthy", "service": "xeroxqueue-backend"}` and verify application is operating normally on the previous baseline.

---

## 8. Durable PDF Storage Runbook (Local vs S3 / Cloudflare R2)

### 8.1 Overview & Architecture
XeroxQueue supports two storage backends for PDF documents:
1. `STORAGE_BACKEND=local`: Files are saved to the server's local disk under `backend/uploads/`.
   - **Recommended for**: Local development and single-server VPS deployments (e.g. Hetzner, DigitalOcean) with persistent SSD storage.
   - **Critical Warning**: On ephemeral container hosting (e.g., Render, Railway, Heroku), the local disk is wiped clean whenever the container restarts or redeploys, resulting in total loss of student PDFs.
2. `STORAGE_BACKEND=s3`: Documents are streamed to and from S3-compatible object storage (e.g., Cloudflare R2, AWS S3, MinIO).
   - **Recommended for**: Containerized deployments, multi-server clusters, or serverless topologies where persistent local disk is not available.
   - **Cloudflare R2 Benefits**: 10 GB free storage/month with **$0 egress fees**, eliminating surprise bandwidth bills.

### 8.2 Environment Variables
| Variable | Required In | Example / Description |
|:---|:---:|:---|
| `STORAGE_BACKEND` | All | `local` (default) or `s3` |
| `S3_ENDPOINT_URL` | S3 Mode | `https://<account_id>.r2.cloudflarestorage.com` |
| `S3_BUCKET_NAME` | S3 Mode | `xeroxqueue-pdfs` |
| `S3_ACCESS_KEY_ID` | S3 Mode | R2 / S3 access key ID (never commit or expose) |
| `S3_SECRET_ACCESS_KEY` | S3 Mode | R2 / S3 secret access key (never commit or expose) |
| `S3_REGION` | S3 Mode | `auto` (for Cloudflare R2) or AWS region (e.g., `us-east-1`) |

### 8.3 Security & Operational Guarantees
- **Strict Configuration Validation**: If `STORAGE_BACKEND=s3` is specified, the application validates all four required variables on startup. If any are missing, it fails fast with a clear error. It **never silently falls back to local storage**.
- **Credential Protection**: S3 credentials, endpoints, and bucket names are never logged, exposed in error responses, or leaked to the frontend.
- **Backend Streaming**: All downloads are streamed through the backend with strict admin authorization checks. Storage buckets remain private with zero public access required.
- **Atomic Failure Cleanup**: If database insertion fails during upload, only the newly created storage object is deleted; pre-existing files are never touched.
- **Orphan Cleanup Safety**: Automatic bulk deletion of cloud objects is intentionally disabled in `cleanup_orphans.py` to prevent catastrophic data loss in cloud storage.

### 8.4 Safe Future Migration Guide: Moving Local Files to S3
Existing local PDFs in `backend/uploads/` do **not** automatically move to S3 when switching `STORAGE_BACKEND`. To migrate existing local files safely without service interruption:

```bash
# 1. Install AWS CLI if not present
# sudo apt-get install -y awscli

# 2. Perform a dry-run sync to review candidates
AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID}" \
AWS_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY}" \
aws s3 sync /opt/xeroxqueue/backend/uploads/ "s3://${S3_BUCKET_NAME}/" \
  --endpoint-url "${S3_ENDPOINT_URL}" \
  --dryrun

# 3. Perform live copy (DO NOT delete local files until verified)
AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID}" \
AWS_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY}" \
aws s3 sync /opt/xeroxqueue/backend/uploads/ "s3://${S3_BUCKET_NAME}/" \
  --endpoint-url "${S3_ENDPOINT_URL}"

# 4. Verify object counts match database records
# 5. Switch STORAGE_BACKEND=s3 in .env and restart backend
sudo systemctl restart xeroxqueue-backend.service
```
