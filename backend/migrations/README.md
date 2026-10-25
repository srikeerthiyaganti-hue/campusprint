# XeroxQueue Database Migrations

This folder contains versioned SQL migration and rollback scripts for XeroxQueue.

---

## Migration 001: Campus Delivery System

- **Migration Script:** `001_campus_delivery_system.sql`
- **Rollback Script:** `001_campus_delivery_system_rollback.sql`
- **Target Database:** MySQL 9.x / MySQL 8.0+ (`xeroxqueue_db`)
- **Status:** **PREPARED / PENDING USER APPROVAL** (Not yet executed)

### Objectives
1. Provide a campus delivery infrastructure for XeroxQueue while maintaining 100% backward compatibility with existing print shop operations.
2. Ensure existing orders and future self-pickup orders seamlessly default to ₹0.00 delivery fee.
3. Preserve existing student accounts, order histories, document uploads, and administrative access controls.
4. Establish an isolated domain for vetted delivery partners with strict privacy boundaries (zero access to uploaded PDF documents).

---

## Schema Architecture Summary

```
                       ┌─────────────────────────┐
                       │        students         │
                       └────────────┬────────────┘
                                    │ 1
                                    │
                                    │ N
                       ┌────────────▼────────────┐
                       │      print_orders       │
                       │  + fulfillment_type     │
                       │  + delivery_fee         │
                       │  + delivery_location_id ┼───────┐
                       │  + pickup_code          │       │
                       │  + delivery_code        │       │
                       └────────────┬────────────┘       │
                                    │ 1                  │ N
                                    │                    │
                                    │ 1                  │ 1
┌─────────────────────────┐    ┌────▼────────────────────▼┐   ┌──▼──────────────────────┐
│    delivery_partners    │    │      delivery_jobs       │   │    campus_locations     │
│  (Vetting, Availability)│◄───┤ (Status, Lock Version)   │   │  (Hubs & Time Windows)  │
└────────────┬────────────┘ N  └────────────┬─────────────┘   └─────────────────────────┘
             │ 1                            │ 1
             │                              │
             │ N                            │ 1
             └──────────────►┌──────────────▼─────────────┐
                             │      delivery_payouts      │
                             │ (Audit Ledger, Settlement) │
                             └────────────────────────────┘
```

### 1. `campus_locations`
- Stores approved campus delivery hubs (hostels, academic buildings, central library).
- Includes operational hours (`operating_hours_start`, `operating_hours_end`) and active status flag.

### 2. `delivery_partners`
- Stores student delivery partner credentials, contact details, approval state, and availability (`offline`, `available`, `busy`).
- **College Email Format:** Automatically derived from student ID as `vu.<student_id>@gmail.com`.
- **Application Statuses:** Explicit `approval_status ENUM('pending', 'approved', 'rejected')` tracks vetting lifecycle alongside audit metadata (`approved_by`, `approved_at`, `rejected_by`, `rejected_at`, `rejection_reason`).
- **Privacy Boundary:** Delivery partners have **no access** to uploaded PDF files or the admin document retrieval route.

### 3. `print_orders` (Fulfillment Fields Added)
- `fulfillment_type`: `ENUM('pickup', 'delivery') NOT NULL DEFAULT 'pickup'`.
- `delivery_fee`: `DECIMAL(6,2) NOT NULL DEFAULT 0.00`.
- `delivery_location_id`: Foreign key to `campus_locations.id` (`ON DELETE SET NULL`).
- `delivery_location_name`, `delivery_room_details`, `delivery_time_window`, `delivery_deadline`.
- `pickup_code` & `delivery_code`: Cryptographically generated verification handshakes.
- `verification_attempts`, `max_verification_attempts` (default 5), `code_invalidated`: Brute-force protection.

### 4. `delivery_jobs`
- Tracks delivery lifecycle: `available` $\rightarrow$ `assigned` $\rightarrow$ `picked_up` $\rightarrow$ `delivered` (or `cancelled`).
- `version INT NOT NULL DEFAULT 1`: Optimistic locking counter preventing concurrent race conditions when partners accept jobs.
- `partner_commission`: Configurable commission per delivery (pending business decision).

### 5. `delivery_payouts`
- Audit ledger for partner earnings.
- `UNIQUE KEY uq_delivery_payouts_job_id` ensures idempotent commission crediting (a job can never be credited twice).
- `ON DELETE RESTRICT` maintains financial audit integrity.

---

## Concurrency & Security Guarantees

### 1. Statement-Level Lock Acquisition Order
To minimize the probability of deadlocks, all endpoints modifying multiple delivery tables must acquire row-level locks in this exact deterministic order using primary-key queries:
$$\mathbf{print\_orders} \longrightarrow \mathbf{delivery\_jobs} \longrightarrow \mathbf{delivery\_partners} \longrightarrow \mathbf{delivery\_payouts}$$

```sql
-- Step 1: Lock print_orders first
SELECT id, delivery_code, verification_attempts, max_verification_attempts,
       code_invalidated, code_expires_at, status
FROM print_orders
WHERE id = :order_id
FOR UPDATE;

-- Step 2: Lock delivery_jobs second
SELECT id, order_id, partner_id, status, commission_status, partner_commission, version
FROM delivery_jobs
WHERE id = :job_id
FOR UPDATE;
```

### 2. Delivery Confirmation & Payout Transaction Specification
The delivery completion endpoint validates all prerequisites under row lock before executing any writes. All successful completion, commission, payout-ledger, and partner-count writes occur within the **exact same atomic transaction**:

1. **Pre-Validation Checks (under locks, before any modifications):**
   - **Order/Job Link:** Assert `delivery_jobs.order_id == print_orders.id`.
   - **Assigned Partner:** Assert `delivery_jobs.partner_id == current_partner_id`.
   - **Job State:** Assert `delivery_jobs.status == 'picked_up'` and `delivery_jobs.commission_status == 'pending'`.
   - **Code Invalidation:** Assert `print_orders.code_invalidated == 0`.
   - **Attempt Limit:** Assert `print_orders.verification_attempts < print_orders.max_verification_attempts`.
   - **Expiry:** Assert `NOW() <= print_orders.code_expires_at`.
   - **Submitted Code:** If `submitted_code != print_orders.delivery_code`, trigger failed attempt recording (see Section 3).

2. **Atomic Completion Writes (executed only if code matches):**
   ```sql
   -- Write 1: Conditional state transition on delivery_jobs (assert cursor.rowcount == 1)
   UPDATE delivery_jobs
   SET status = 'delivered',
       delivered_at = NOW(),
       commission_status = 'credited',
       version = version + 1
   WHERE id = :job_id
     AND partner_id = :partner_id
     AND status = 'picked_up'
     AND commission_status = 'pending'
     AND version = :job_version;

   -- Write 2: Complete order (assert cursor.rowcount == 1)
   UPDATE print_orders
   SET status = 'completed'
   WHERE id = :order_id AND status = 'ready';

   -- Write 3: Insert single financial payout record (Unique Key uq_delivery_payouts_job_id prevents duplicate credits)
   INSERT INTO delivery_payouts (payout_reference, partner_id, job_id, order_id, amount, status, credited_at)
   VALUES (:payout_reference, :partner_id, :job_id, :order_id, :commission_amount, 'accrued', NOW());

   -- Write 4: Increment partner delivery counter (assert cursor.rowcount == 1)
   UPDATE delivery_partners
   SET total_deliveries_completed = total_deliveries_completed + 1
   WHERE id = :partner_id;
   ```
   If `cursor.rowcount == 0` on Write 1 or Write 2, a concurrent transition occurred; the backend executes `conn.rollback()` and returns `HTTP 409 Conflict`. Otherwise, `conn.commit()` commits all completion writes atomically.

### 3. Durable Failed Attempt Recording & Serialization
- When `submitted_code != print_orders.delivery_code`:
  1. Increment the attempt counter in `print_orders` under the active row lock:
     ```sql
     UPDATE print_orders
     SET verification_attempts = verification_attempts + 1,
         code_invalidated = IF(verification_attempts + 1 >= max_verification_attempts, 1, 0)
     WHERE id = :order_id;
     ```
  2. Commit this attempt increment immediately (`conn.commit()`).
  3. Raise `HTTP 400 Bad Request` (or `HTTP 403 Forbidden` if `code_invalidated == 1`).
  4. **Isolation:** Because Writes 1–4 were never executed, no delivery completion or payout is recorded.
- **Serialization of Concurrent Requests:**
  - Concurrent requests for the same order queue on `SELECT ... FROM print_orders WHERE id = :order_id FOR UPDATE`.
  - Attempt 1 acquires the lock. Attempt 2 waits.
  - If Attempt 1 was incorrect, it increments `verification_attempts`, commits, and releases the lock.
  - Attempt 2 unblocks, acquires the lock, and reads the updated count. If Attempt 1 reached the limit (5), Attempt 2 reads `code_invalidated = 1` and is rejected with `HTTP 403`, even if Attempt 2 provided the correct code.
  - If Attempt 1 was correct, it completes the job and order. Attempt 2 reads `status = 'delivered'` and is rejected with `HTTP 409 Conflict`.

### 4. Deadlock & Lock Timeout Handling
While consistent lock hierarchy significantly reduces lock contention, deadlocks (`MySQL Error 1213: ER_LOCK_DEADLOCK`) and lock wait timeouts (`MySQL Error 1205: ER_LOCK_WAIT_TIMEOUT`) can still arise under heavy concurrency (e.g. index gap locking, foreign-key cascade checks).
- **Safe Handling Protocol:**
  1. Catch `pymysql.err.OperationalError` with error codes `1213` and `1205`.
  2. Immediately issue `conn.rollback()` to release any partially held locks.
  3. Implement an automated transaction retry with exponential backoff and jitter (e.g. up to 3 retries: 50ms, 150ms, 300ms).
  4. On retry, the transaction starts fresh from Step 1 under new row locks.
  5. **Duplicate Payout Protection on Retry:** If an initial transaction succeeded in committing before a network timeout or disconnect, the retry will read `commission_status = 'credited'`. Write 1 affects 0 rows, triggering an immediate clean rollback and preventing duplicate payouts.

---

## Migration Execution Instructions (When Approved)

### Step 1: Create a Verified, Restorable Pre-Migration Backup
Do NOT pass database passwords directly on the command line (prevents exposure in `ps` and shell history). Use the interactive `-p` prompt:

> [!IMPORTANT]
> **Execution Method:** Execute these verification instructions as a standalone, saved shell script (e.g. `backups/verify_restore.sh`) rather than pasting individual commands interactively into a terminal. In an interactive shell, paste errors or premature returns can execute subsequent commands regardless of prior failures; running as a script ensures that `exit 1` terminates the entire sequence immediately upon any check failure.

> [!WARNING]
> **STRUCTURAL INSPECTION & ISOLATION LIMITS:**
> 1. A standard `mysqldump` file contains `USE \`xeroxqueue_db\`;` and `CREATE DATABASE ... \`xeroxqueue_db\``. Sourcing an un-sanitized dump file into another database context will switch execution back to and **overwrite the live `xeroxqueue_db`**!
> 2. **String replacement alone is not proof of safety:** Absence of the string `xeroxqueue_db` from `grep` is a mandatory sanity check, but text replacement via `sed` cannot guarantee isolation or structural validity if the dump contains complex constructs (e.g. dynamic SQL, views with hardcoded schema references, or cross-database triggers).
> 3. The sanitized dump MUST be inspected for `CREATE DATABASE`, `USE`, database-qualified object references (`schema.table`), and routine/trigger definitions.
> 4. If the backup contains advanced constructs that cannot be safely isolated by simple text substitution, **STOP immediately**. Do not attempt local same-server substitution; instead, test the restoration on a physically or containerized isolated MySQL instance (e.g., Docker or a dedicated staging server) where the dump can be restored under its native database name without risking the production database.
> 5. **Privacy:** Dumps contain sensitive student PII (full names, roll numbers, email addresses, and Argon2 password hashes). Never store unencrypted dumps in world-readable shared locations like `/tmp`. Store temporary dumps in `backups/` with restricted permissions (`chmod 600`) and clean them up immediately after testing.

```bash
# 1A. Capture dump file
BACKUP_FILE="backups/pre_migration_$(date +%Y%m%d_%H%M%S).sql"

# Check exit status of mysqldump immediately; do not continue if capture fails
if ! mysqldump -u <DB_USER> -p --single-transaction --routines --triggers xeroxqueue_db > "$BACKUP_FILE"; then
    echo "ERROR: mysqldump command failed (exit code $?). Backup capture incomplete. Aborting."
    rm -f "$BACKUP_FILE"
    exit 1
fi

# 1B. Verify dump file integrity before any restoration attempt
# Check file existence and non-zero size:
if [ ! -f "$BACKUP_FILE" ] || [ ! -s "$BACKUP_FILE" ]; then
    echo "ERROR: Backup file '$BACKUP_FILE' does not exist or is empty (0 bytes). Aborting."
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Check for completed dump marker emitted by mysqldump:
if ! tail -n 5 "$BACKUP_FILE" | grep -q -- "-- Dump completed on"; then
    echo "ERROR: Backup file '$BACKUP_FILE' is truncated or missing '-- Dump completed on' marker. Aborting."
    exit 1
fi
echo "Backup capture verified: mysqldump succeeded, file is non-empty, and contains completed dump marker."

# 1C. Safe Test-Restoration in an Isolated Sandbox
SANDBOX_SQL="backups/sandbox_verify.sql"
SANDBOX_DB="xq_verify_sandbox"
SANDBOX_CREATED=0

# Cleanup handler:
# - Always removes temporary sanitized SQL file if present.
# - ONLY drops the sandbox database if THIS script successfully created it (SANDBOX_CREATED=1).
# - Warns explicitly if teardown fails so an administrator can manually review.
cleanup() {
    local exit_code=$?
    if [ -f "$SANDBOX_SQL" ]; then
        rm -f "$SANDBOX_SQL"
    fi
    if [ "$SANDBOX_CREATED" -eq 1 ]; then
        echo "Tearing down created sandbox database '$SANDBOX_DB'..."
        if ! mysql -u <DB_USER> -p -e "DROP DATABASE IF EXISTS \`$SANDBOX_DB\`;"; then
            echo "CRITICAL WARNING: Failed to drop sandbox database '$SANDBOX_DB'! Manual administrator cleanup required."
        fi
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

# Step 1: Create sanitized copy substituting database name across all references
sed 's/xeroxqueue_db/xq_verify_sandbox/g' "$BACKUP_FILE" > "$SANDBOX_SQL"
chmod 600 "$SANDBOX_SQL"

# Step 2: Fail-closed verification gate
# Check for ANY remaining occurrence of xeroxqueue_db in the sanitized file.
if grep -i "xeroxqueue_db" "$SANDBOX_SQL"; then
    echo "CRITICAL SAFETY ERROR: Unreplaced 'xeroxqueue_db' reference detected in sanitized dump!"
    echo "Aborting sandbox restore immediately to prevent live database modification."
    exit 1
fi

# Step 3: Pre-existence guard for sandbox database
# DO NOT automatically overwrite or drop an existing sandbox database.
EXISTING_SANDBOX=$(mysql -u <DB_USER> -p -N -s -e "SHOW DATABASES LIKE '$SANDBOX_DB';")
if [ "$EXISTING_SANDBOX" = "$SANDBOX_DB" ]; then
    echo "CRITICAL: Database '$SANDBOX_DB' already exists on the MySQL server!"
    echo "To prevent overwriting or deleting unreviewed sandbox data, execution is aborted."
    echo "Perform manual review and explicitly remove or rename '$SANDBOX_DB' before re-running."
    exit 1
fi

# Step 4: Execute sandbox test-restoration (ONLY reached if ALL checks above pass)
echo "Preflight checks passed. Creating clean sandbox database '$SANDBOX_DB'..."

if ! mysql -u <DB_USER> -p -e "CREATE DATABASE \`$SANDBOX_DB\`;"; then
    echo "ERROR: Failed to create sandbox database '$SANDBOX_DB' (exit code $?). Aborting."
    exit 1
fi
# Mark database as created by this run only after successful creation
SANDBOX_CREATED=1

echo "Sourcing sanitized dump into '$SANDBOX_DB'..."
if ! mysql -u <DB_USER> -p "$SANDBOX_DB" < "$SANDBOX_SQL"; then
    echo "ERROR: Failed to source sanitized dump into '$SANDBOX_DB' (exit code $?). Aborting."
    exit 1
fi

echo "Verifying baseline tables and row counts in '$SANDBOX_DB'..."
if ! mysql -u <DB_USER> -p -e "SELECT count(*) AS students FROM \`$SANDBOX_DB\`.students; SELECT count(*) AS orders FROM \`$SANDBOX_DB\`.print_orders;"; then
    echo "ERROR: Baseline table verification query failed in '$SANDBOX_DB' (exit code $?). Aborting."
    exit 1
fi

# Success milestone reached only when restore and verification queries all returned exit code 0:
echo "Sandbox test-restore and row verification completed successfully."
# The cleanup trap handles dropping the created sandbox database and removing $SANDBOX_SQL on normal exit.
```

### Step 2: Run Migration Script
```bash
mysql -u <DB_USER> -p xeroxqueue_db < backend/migrations/001_campus_delivery_system.sql
```

### Step 3: Migration Verification Checklist
After execution, verify the schema changes in MySQL:
- [ ] Verify `campus_locations` table exists: `DESCRIBE campus_locations;`
- [ ] Verify `delivery_partners` table exists: `DESCRIBE delivery_partners;`
- [ ] Verify `print_orders` has the new columns: `DESCRIBE print_orders;`
- [ ] Verify existing orders remain unaffected:
  ```sql
  SELECT id, student_name, fulfillment_type, delivery_fee, status FROM print_orders LIMIT 5;
  -- Output must show fulfillment_type = 'pickup' and delivery_fee = 0.00
  ```
- [ ] Verify `delivery_jobs` table exists: `DESCRIBE delivery_jobs;`
- [ ] Verify `delivery_payouts` table exists: `DESCRIBE delivery_payouts;`
- [ ] Verify foreign key constraints are active:
  ```sql
  SELECT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME
  FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
  WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND REFERENCED_TABLE_NAME IS NOT NULL;
  ```

---

## Rollback Execution Instructions (Emergency Reversal)

> [!CAUTION]
> **CRITICAL DATA LOSS & OPERATIONAL WARNING:**
> Do NOT execute this rollback script in a live/production system once real campus delivery operations have commenced unless you explicitly intend to permanently discard all delivery data.
> - Rollback permanently drops `delivery_payouts`, `delivery_jobs`, `delivery_partners`, and `campus_locations`.
> - Rollback drops all delivery columns from `print_orders`. Delivery address details and fulfillment records will be **irreversibly erased**.
> - Baseline student accounts (`students`), core print order data, and PDF files are preserved.

### Pre-Rollback Mandatory Checklist
- [ ] 1. Verify that no active delivery operations or unsettled partner payouts are pending.
- [ ] 2. Stop or pause backend and frontend services to prevent concurrent traffic during rollback.
- [ ] 3. Capture a verified, test-restored pre-rollback database backup (follow Step 1A–1C above):
  ```bash
  mysqldump -u <DB_USER> -p --single-transaction --routines --triggers xeroxqueue_db > backups/pre_rollback_backup_$(date +%Y%m%d_%H%M%S).sql
  ```

### Step 1: Run Rollback Script
```bash
mysql -u <DB_USER> -p xeroxqueue_db < backend/migrations/001_campus_delivery_system_rollback.sql
```

### Step 2: Preflight Failure Behavior
The script initiates an automated preflight check (`preflight_rollback_check`):
- Verifies that `DATABASE() == 'xeroxqueue_db'`.
- Verifies that baseline tables (`students`, `print_orders`, `uploaded_documents`) exist.
- **If any check fails:** The script issues `SIGNAL SQLSTATE '45000'`, halting execution immediately. No tables or columns are modified.

### Step 3: Helper-Procedure Cleanup & Session Variable Behavior
- Helper procedures (`drop_print_orders_fk_if_exists`, `drop_print_orders_col_if_exists`) are dropped immediately upon normal completion.
- If execution is aborted unexpectedly, subsequent recovery runs issue `DROP PROCEDURE IF EXISTS` at the start of each block, replacing and cleaning them up deterministically.
- Session `FOREIGN_KEY_CHECKS` is restored to `@OLD_FOREIGN_KEY_CHECKS` on normal completion. If the script aborts prematurely in an interactive shell or persistent connection pool, MySQL does **not** automatically reset session variables; the administrator must manually execute `SET FOREIGN_KEY_CHECKS = 1;` or reconnect.

### Step 4: Rollback Verification Checklist
- [ ] Verify `delivery_payouts`, `delivery_jobs`, `delivery_partners`, and `campus_locations` tables are dropped:
  ```sql
  SHOW TABLES;
  ```
- [ ] Verify temporary helper procedures were cleaned up:
  ```sql
  SHOW PROCEDURE STATUS WHERE Db = 'xeroxqueue_db';
  ```
- [ ] Verify `print_orders` columns are reverted while preserving core order records:
  ```sql
  SELECT id, student_name, copies, print_type, print_sides, status FROM print_orders LIMIT 5;
  ```
- [ ] Verify existing students and uploads are fully intact:
  ```sql
  SELECT COUNT(*) FROM students;
  SELECT COUNT(*) FROM uploaded_documents;
  ```

---

## Unresolved Business Decisions

The migration has been designed with configuration flexibility to allow these decisions to be made without further database alterations:

1. **Partner Commission Split:**
   - The student delivery fee is fixed at ₹5.00 (`delivery_fee` in `print_orders`).
   - The default partner commission is set to `0.00` in the database schema (`partner_commission` in `delivery_jobs`) pending your business decision on the exact split (e.g. ₹4.00 partner / ₹1.00 platform, or ₹5.00 full partner commission).
2. **Approved Campus Locations:**
   - Sample placeholder locations in the SQL script are commented out by default. Real campus buildings, hostels, and operating hours should be provided or configured.
3. **Delivery Windows & Lead Times:**
   - Standard suggested delivery windows (e.g., 10:00–12:00, 14:00–16:00, 18:00–20:00) or on-demand delivery cutoff thresholds can be configured in backend settings.
4. **Partner Vetting Policy:**
   - `delivery_partners.is_approved` defaults to `0` (false), requiring print shop admin approval before partners can view or accept delivery jobs.
