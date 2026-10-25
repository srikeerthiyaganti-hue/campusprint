-- =============================================================================
-- XeroxQueue Campus Delivery System — Database Migration
-- Version: 001
-- Description: Adds campus delivery capabilities to XeroxQueue without
--              disrupting existing student accounts, print orders, or admin workflows.
-- Target Database: MySQL 9.x (Compatible with MySQL 8.0+)
-- Database: xeroxqueue_db
-- =============================================================================

-- SAFETY PRECHECK: Ensure the target database is selected
USE `xeroxqueue_db`;

-- Set session parameters for strict SQL mode and UTF8mb4
SET NAMES utf8mb4;
SET @OLD_FOREIGN_KEY_CHECKS = @@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS = 1;

-- -----------------------------------------------------------------------------
-- Step 0: Preflight Validation Check
-- Purpose: Confirms selected database is xeroxqueue_db, verifies baseline
--          core tables (students, print_orders, uploaded_documents) exist,
--          and detects partial execution, existing conflicts, or prior completion
--          before initiating any schema modifications.
-- Failure Behavior:
--   - If DATABASE() is not xeroxqueue_db or any baseline table is missing,
--     SIGNAL SQLSTATE '45000' halts script execution immediately.
--   - If Migration 001 is already fully applied (all 4 tables, 14 columns, 1 FK),
--     SIGNAL SQLSTATE '45000' halts execution safely without duplicating columns or errors.
--   - If the database is in a partially applied state (some tables/columns exist),
--     SIGNAL SQLSTATE '45000' halts execution immediately and instructs the operator
--     to run 001_campus_delivery_system_rollback.sql rather than continuing blindly.
-- Cleanup Behavior:
--   The temporary procedure is dropped immediately after execution. If an error
--   interrupts execution, DROP PROCEDURE IF EXISTS at the start ensures clean rerun.
-- -----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS `preflight_migration_check`;

DELIMITER //
CREATE PROCEDURE `preflight_migration_check`()
BEGIN
    DECLARE v_delivery_tables_count INT DEFAULT 0;
    DECLARE v_delivery_cols_count INT DEFAULT 0;
    DECLARE v_delivery_fk_count INT DEFAULT 0;

    -- 1. Confirm current database context
    IF DATABASE() != 'xeroxqueue_db' THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration Preflight FAILED: Current database is not xeroxqueue_db. Aborting migration.';
    END IF;

    -- 2. Confirm baseline core tables exist before any schema changes
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'students'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration Preflight FAILED: Baseline table students does not exist. Aborting migration.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'print_orders'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration Preflight FAILED: Baseline table print_orders does not exist. Aborting migration.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'uploaded_documents'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration Preflight FAILED: Baseline table uploaded_documents does not exist. Aborting migration.';
    END IF;

    -- 3. Detect partial execution, pre-existing conflicting objects, or completed migration
    SELECT COUNT(*) INTO v_delivery_tables_count
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = 'xeroxqueue_db'
      AND TABLE_NAME IN ('campus_locations', 'delivery_partners', 'delivery_jobs', 'delivery_payouts');

    SELECT COUNT(*) INTO v_delivery_cols_count
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'xeroxqueue_db'
      AND TABLE_NAME = 'print_orders'
      AND COLUMN_NAME IN (
          'fulfillment_type', 'delivery_fee', 'delivery_location_id', 'delivery_location_name',
          'delivery_room_details', 'delivery_time_window', 'delivery_deadline', 'pickup_code',
          'delivery_code', 'verification_attempts', 'max_verification_attempts',
          'code_invalidated', 'code_generated_at', 'code_expires_at'
      );

    SELECT COUNT(*) INTO v_delivery_fk_count
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = 'xeroxqueue_db'
      AND TABLE_NAME = 'print_orders'
      AND CONSTRAINT_NAME = 'fk_print_orders_location';

    -- Halting condition A: Migration 001 is already fully applied
    IF v_delivery_tables_count = 4 AND v_delivery_cols_count = 14 AND v_delivery_fk_count = 1 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration Preflight NOTICE: Migration 001 is already fully applied to xeroxqueue_db. Halting execution without changes.';
    END IF;

    -- Halting condition B: Partially applied state or conflicting objects exist
    IF v_delivery_tables_count > 0 OR v_delivery_cols_count > 0 OR v_delivery_fk_count > 0 THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Migration FAILED: Partial state detected. Review and clean up before rerunning.';
    END IF;
END //
DELIMITER ;

CALL `preflight_migration_check`();
DROP PROCEDURE IF EXISTS `preflight_migration_check`;

-- -----------------------------------------------------------------------------
-- 1. TABLE: campus_locations
-- Purpose: Defines approved campus delivery hubs (hostels, academic blocks, etc.)
--          and permitted operational delivery windows.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `campus_locations` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `location_code` VARCHAR(50) NOT NULL COMMENT 'Unique identifier code, e.g., HOSTEL_A, LIB_FOYER',
  `name` VARCHAR(100) NOT NULL COMMENT 'Human-readable location title, e.g., Boys Hostel Block A',
  `description` VARCHAR(255) DEFAULT NULL COMMENT 'Optional delivery notes or landmark directions',
  `building_type` ENUM('hostel', 'academic', 'library', 'administrative', 'other') NOT NULL DEFAULT 'hostel',
  `is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'Flag to toggle delivery availability',
  `operating_hours_start` TIME DEFAULT NULL COMMENT 'Earliest permitted delivery time, e.g. 09:00:00',
  `operating_hours_end` TIME DEFAULT NULL COMMENT 'Latest permitted delivery time, e.g. 21:00:00',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_campus_locations_code` (`location_code`),
  UNIQUE KEY `uq_campus_locations_name` (`name`),
  KEY `idx_campus_locations_active` (`is_active`),
  KEY `idx_campus_locations_type` (`building_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Approved campus delivery locations';

-- -----------------------------------------------------------------------------
-- 2. TABLE: delivery_partners
-- Purpose: Manages registered student delivery partners, vetting approval,
--          availability state, and aggregated delivery performance.
-- Privacy Notice: Delivery partners do not have access to stored PDF files.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `delivery_partners` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `full_name` VARCHAR(100) NOT NULL,
  `phone_number` VARCHAR(20) NOT NULL,
  `student_id` VARCHAR(50) NOT NULL COMMENT 'College roll number / ID card number',
  `email` VARCHAR(255) NOT NULL COMMENT 'College email automatically derived as vu.<student_id>@gmail.com',
  `password_hash` VARCHAR(255) NOT NULL COMMENT 'Argon2id password hash',
  `student_db_id` INT DEFAULT NULL COMMENT 'Optional link to students.id if registered as a student user',
  `approval_status` ENUM('pending', 'approved', 'rejected') NOT NULL DEFAULT 'pending' COMMENT 'Explicit vetting state: pending review, approved, or rejected',
  `is_approved` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Quick boolean flag (1 if approved, 0 if pending/rejected)',
  `approved_by` VARCHAR(100) DEFAULT NULL COMMENT 'Admin username who approved the partner',
  `approved_at` DATETIME DEFAULT NULL,
  `rejected_by` VARCHAR(100) DEFAULT NULL COMMENT 'Admin username who rejected or revoked the partner',
  `rejected_at` DATETIME DEFAULT NULL,
  `rejection_reason` VARCHAR(255) DEFAULT NULL COMMENT 'Reason for application rejection or revocation',
  `is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'Allows account suspension or deactivation',
  `availability_status` ENUM('offline', 'available', 'busy') NOT NULL DEFAULT 'offline',
  `last_active_at` DATETIME DEFAULT NULL,
  `total_deliveries_completed` INT NOT NULL DEFAULT 0,
  `rating` DECIMAL(3,2) DEFAULT '5.00',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_delivery_partners_phone` (`phone_number`),
  UNIQUE KEY `uq_delivery_partners_student_id` (`student_id`),
  UNIQUE KEY `uq_delivery_partners_email` (`email`),
  UNIQUE KEY `uq_delivery_partners_student_db_id` (`student_db_id`),
  KEY `idx_delivery_partners_approval` (`approval_status`, `is_approved`, `is_active`),
  KEY `idx_delivery_partners_availability` (`availability_status`),
  CONSTRAINT `fk_delivery_partners_student` FOREIGN KEY (`student_db_id`) REFERENCES `students` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Vetted student delivery partners';

-- -----------------------------------------------------------------------------
-- 3. ALTER TABLE: print_orders
-- Purpose: Adds non-breaking fulfillment fields to the existing print_orders table.
-- Backward Compatibility:
--   - Default fulfillment_type is 'pickup' (₹0.00 delivery fee).
--   - All pre-existing orders remain valid self-pickup orders without any modification.
--   - Old API payloads omitting delivery fields continue to work flawlessly.
-- -----------------------------------------------------------------------------
ALTER TABLE `print_orders`
  ADD COLUMN `fulfillment_type` ENUM('pickup', 'delivery') NOT NULL DEFAULT 'pickup' AFTER `print_sides`,
  ADD COLUMN `delivery_fee` DECIMAL(6,2) NOT NULL DEFAULT 0.00 AFTER `fulfillment_type`,
  ADD COLUMN `delivery_location_id` INT DEFAULT NULL AFTER `delivery_fee`,
  ADD COLUMN `delivery_location_name` VARCHAR(100) DEFAULT NULL AFTER `delivery_location_id`,
  ADD COLUMN `delivery_room_details` VARCHAR(255) DEFAULT NULL AFTER `delivery_location_name`,
  ADD COLUMN `delivery_time_window` VARCHAR(100) DEFAULT NULL AFTER `delivery_room_details`,
  ADD COLUMN `delivery_deadline` DATETIME DEFAULT NULL AFTER `delivery_time_window`,
  ADD COLUMN `pickup_code` VARCHAR(16) DEFAULT NULL AFTER `delivery_deadline`,
  ADD COLUMN `delivery_code` VARCHAR(16) DEFAULT NULL AFTER `pickup_code`,
  ADD COLUMN `verification_attempts` INT NOT NULL DEFAULT 0 AFTER `delivery_code`,
  ADD COLUMN `max_verification_attempts` INT NOT NULL DEFAULT 5 AFTER `verification_attempts`,
  ADD COLUMN `code_invalidated` TINYINT(1) NOT NULL DEFAULT 0 AFTER `max_verification_attempts`,
  ADD COLUMN `code_generated_at` DATETIME DEFAULT NULL AFTER `code_invalidated`,
  ADD COLUMN `code_expires_at` DATETIME DEFAULT NULL AFTER `code_generated_at`,
  ADD KEY `idx_print_orders_fulfillment` (`fulfillment_type`),
  ADD KEY `idx_print_orders_deadline` (`delivery_deadline`),
  ADD KEY `idx_print_orders_pickup_code` (`pickup_code`),
  ADD KEY `idx_print_orders_delivery_code` (`delivery_code`);

-- Add foreign key constraint after columns and indexes are created
ALTER TABLE `print_orders`
  ADD CONSTRAINT `fk_print_orders_location` FOREIGN KEY (`delivery_location_id`) REFERENCES `campus_locations` (`id`) ON DELETE SET NULL;

-- -----------------------------------------------------------------------------
-- 4. TABLE: delivery_jobs
-- Purpose: Manages delivery lifecycle independently from print production stages.
-- Concurrency & Idempotency:
--   - order_id has a UNIQUE constraint (guarantees exactly one delivery job per order).
--   - version INT column supports optimistic locking during job assignment.
--   - Partner commission is stored per job and clearly marked pending business decision.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `delivery_jobs` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `order_id` INT NOT NULL COMMENT '1:1 link to print_orders.id',
  `partner_id` INT DEFAULT NULL COMMENT 'Assigned delivery partner, NULL if available',
  `status` ENUM('available', 'assigned', 'picked_up', 'delivered', 'cancelled') NOT NULL DEFAULT 'available',
  `version` INT NOT NULL DEFAULT 1 COMMENT 'Optimistic locking counter for concurrency safety',
  `delivery_fee` DECIMAL(6,2) NOT NULL DEFAULT 5.00 COMMENT 'Customer delivery fee charged (₹5.00)',
  `partner_commission` DECIMAL(6,2) NOT NULL DEFAULT 0.00 COMMENT 'Configurable partner commission - pending business decision',
  `commission_status` ENUM('pending', 'credited', 'cancelled') NOT NULL DEFAULT 'pending',
  `accepted_at` DATETIME DEFAULT NULL,
  `picked_up_at` DATETIME DEFAULT NULL,
  `delivered_at` DATETIME DEFAULT NULL,
  `cancelled_at` DATETIME DEFAULT NULL,
  `cancellation_reason` VARCHAR(255) DEFAULT NULL,
  `cancelled_by` ENUM('partner', 'admin', 'system') DEFAULT NULL,
  `delayed_notification_sent` TINYINT(1) NOT NULL DEFAULT 0,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_delivery_jobs_order_id` (`order_id`),
  KEY `idx_delivery_jobs_status` (`status`),
  KEY `idx_delivery_jobs_partner_status` (`partner_id`, `status`),
  KEY `idx_delivery_jobs_commission_status` (`commission_status`),
  KEY `idx_delivery_jobs_created_at` (`created_at`),
  CONSTRAINT `fk_delivery_jobs_order` FOREIGN KEY (`order_id`) REFERENCES `print_orders` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_delivery_jobs_partner` FOREIGN KEY (`partner_id`) REFERENCES `delivery_partners` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Delivery jobs lifecycle and partner assignments';

-- -----------------------------------------------------------------------------
-- 5. TABLE: delivery_payouts
-- Purpose: Financial ledger recording partner commission earnings and settlements.
-- Audit Safety:
--   - job_id has a UNIQUE constraint (prevents double-crediting of commissions).
--   - ON DELETE RESTRICT on partner_id, job_id, and order_id guarantees audit trail retention.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `delivery_payouts` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `payout_reference` VARCHAR(64) NOT NULL COMMENT 'Unique business reference, e.g. PAY-20261023-XXXX',
  `partner_id` INT NOT NULL,
  `job_id` INT NOT NULL COMMENT '1:1 link to delivery_jobs.id (guarantees single payout per job)',
  `order_id` INT NOT NULL,
  `amount` DECIMAL(6,2) NOT NULL COMMENT 'Commission amount credited to partner',
  `status` ENUM('accrued', 'processing', 'paid', 'withheld', 'cancelled') NOT NULL DEFAULT 'accrued',
  `credited_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `settled_at` DATETIME DEFAULT NULL,
  `settled_by_admin` VARCHAR(100) DEFAULT NULL COMMENT 'Admin username who settled payout',
  `payment_method` VARCHAR(50) DEFAULT NULL COMMENT 'e.g. UPI, Cash, Bank Transfer',
  `transaction_reference` VARCHAR(100) DEFAULT NULL COMMENT 'Bank/UPI transaction reference number',
  `admin_notes` VARCHAR(255) DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_delivery_payouts_ref` (`payout_reference`),
  UNIQUE KEY `uq_delivery_payouts_job_id` (`job_id`),
  KEY `idx_delivery_payouts_partner_status` (`partner_id`, `status`),
  KEY `idx_delivery_payouts_status` (`status`),
  KEY `idx_delivery_payouts_credited_at` (`credited_at`),
  CONSTRAINT `fk_delivery_payouts_partner` FOREIGN KEY (`partner_id`) REFERENCES `delivery_partners` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_delivery_payouts_job` FOREIGN KEY (`job_id`) REFERENCES `delivery_jobs` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_delivery_payouts_order` FOREIGN KEY (`order_id`) REFERENCES `print_orders` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Partner commission earnings and payout records';

-- -----------------------------------------------------------------------------
-- 6. CONFIGURABLE SAMPLE SEED DATA (DISABLED BY DEFAULT)
-- Purpose: Provides placeholder campus locations for local development/testing.
-- Notice: In production, do not insert placeholder data. Configure authentic
--         campus hubs through admin setup.
-- -----------------------------------------------------------------------------
-- UNCOMMENT BELOW ONLY IF SEEDING DEVELOPMENT / TEST ENVIRONMENTS:
-- INSERT INTO `campus_locations` (`location_code`, `name`, `description`, `building_type`, `is_active`, `operating_hours_start`, `operating_hours_end`)
-- VALUES
--   ('SAMPLE_HOSTEL_A', 'Sample Campus Hostel A (Placeholder)', 'Configurable sample delivery location for testing', 'hostel', 1, '09:00:00', '21:00:00'),
--   ('SAMPLE_LIBRARY', 'Sample Central Library (Placeholder)', 'Configurable sample delivery location for testing', 'library', 1, '09:00:00', '20:00:00')
-- ON DUPLICATE KEY UPDATE `name` = VALUES(`name`);

-- -----------------------------------------------------------------------------
-- 7. Restore original session foreign key check setting
-- -----------------------------------------------------------------------------
SET FOREIGN_KEY_CHECKS = @OLD_FOREIGN_KEY_CHECKS;
