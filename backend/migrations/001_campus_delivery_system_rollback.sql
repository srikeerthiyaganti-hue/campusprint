-- =============================================================================
-- XeroxQueue Campus Delivery System — Database Migration Rollback
-- Version: 001 Rollback (Idempotent Partial-Failure Safe with Preflight Checks)
-- Target Database: MySQL 9.x (Compatible with MySQL 8.0+)
-- Database: xeroxqueue_db
--
-- =============================================================================
-- CRITICAL DATA LOSS & OPERATIONAL WARNING:
-- =============================================================================
-- DO NOT RUN THIS SCRIPT IN A LIVE/PRODUCTION ENVIRONMENT ONCE REAL CAMPUS
-- DELIVERY OPERATIONS HAVE COMMENCED, UNLESS YOU EXPLICITLY INTEND TO
-- PERMANENTLY DESTROY ALL DELIVERY DATA.
--
-- This rollback script is NOT "safe to run at any time":
-- 1. It permanently DROPS the delivery tables:
--      - `delivery_payouts` (Partner financial audit ledger)
--      - `delivery_jobs` (Delivery assignment and status records)
--      - `delivery_partners` (Partner accounts and vetting records)
--      - `campus_locations` (Approved delivery hubs)
-- 2. It permanently DROPS all delivery-specific columns from `print_orders`.
--    If students have placed campus delivery orders, their delivery address,
--    room details, time window, and delivery fee data will be PERMANENTLY LOST.
-- 3. OBJECT ISOLATION & DATA PRESERVATION GUARANTEE:
--    - The script names and drops ONLY the 4 delivery tables, 1 foreign key,
--      and 14 delivery columns created by Migration 001. No wildcard drops or
--      dynamic table operations exist.
--    - Core student accounts (`students`), document records (`uploaded_documents`),
--      filesystem PDFs (`backend/uploads/`), and original print order fields
--      (`id`, `student_name`, `file_name`, `copies`, `print_type`, `print_sides`,
--      `status`, `created_at`, `stored_file_name`, `student_db_id`) are preserved.
--
-- SESSION VARIABLE RESTORATION NOTICE:
-- - On normal completion, Step 5 executes `SET FOREIGN_KEY_CHECKS = @OLD_FOREIGN_KEY_CHECKS;`.
-- - In case of premature termination (e.g. fatal error or connection disconnect):
--   In CLI batch execution (`mysql < script.sql`), the session ends when the process exits.
--   However, if executed in an interactive client, persistent terminal, or connection pool,
--   MySQL DOES NOT automatically reset session variables if execution stops before Step 5.
--   The administrator must manually execute `SET FOREIGN_KEY_CHECKS = <baseline>;` or
--   reconnect the session.
--
-- MANDATORY PRE-ROLLBACK CHECKLIST:
-- [ ] 1. Confirm that no unsettled partner payouts or active deliveries exist.
-- [ ] 2. Stop or pause backend and frontend services to prevent concurrent writes.
-- [ ] 3. Create a verified, restorable backup before running (see README instructions).
-- [ ] 4. Verify that the backup file is non-empty, contains trailing dump markers,
--        and has been verified via test restoration in an isolated sandbox.
-- =============================================================================

USE `xeroxqueue_db`;

SET NAMES utf8mb4;
SET @OLD_FOREIGN_KEY_CHECKS = @@FOREIGN_KEY_CHECKS;
SET FOREIGN_KEY_CHECKS = 1;

-- -----------------------------------------------------------------------------
-- Step 0: Preflight Validation Check
-- Purpose: Verifies current session is connected to xeroxqueue_db and that
--          baseline core tables exist before initiating any destructive actions.
-- Failure Behavior:
--   If DATABASE() is not xeroxqueue_db or any baseline table is missing,
--   SIGNAL SQLSTATE '45000' halts script execution immediately. No tables or
--   columns will be dropped.
-- -----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS `preflight_rollback_check`;

DELIMITER //
CREATE PROCEDURE `preflight_rollback_check`()
BEGIN
    -- 1. Verify current database context
    IF DATABASE() != 'xeroxqueue_db' THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Rollback Preflight FAILED: Current database is not xeroxqueue_db. Aborting rollback.';
    END IF;

    -- 2. Verify baseline core tables exist before any drop operations
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'print_orders'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Rollback Preflight FAILED: Baseline table print_orders does not exist. Aborting rollback.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'students'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Rollback Preflight FAILED: Baseline table students does not exist. Aborting rollback.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'xeroxqueue_db' AND TABLE_NAME = 'uploaded_documents'
    ) THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Rollback Preflight FAILED: Baseline table uploaded_documents does not exist. Aborting rollback.';
    END IF;
END //
DELIMITER ;

CALL `preflight_rollback_check`();
DROP PROCEDURE IF EXISTS `preflight_rollback_check`;

-- -----------------------------------------------------------------------------
-- Step 1: Drop dependent delivery tables in reverse dependency order
-- Isolation: Only drops the 3 specific delivery tables created by Migration 001.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `delivery_payouts`;
DROP TABLE IF EXISTS `delivery_jobs`;
DROP TABLE IF EXISTS `delivery_partners`;

-- -----------------------------------------------------------------------------
-- Step 2: Temporary helper procedure for idempotent foreign key removal
-- Safely removes foreign key on print_orders only if it currently exists.
-- Cleanup Guarantee:
--   - Dropped immediately after use on line 145.
--   - If aborted mid-run, DROP PROCEDURE IF EXISTS on line 120 ensures subsequent
--     runs replace and clean it up deterministically.
-- -----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS `drop_print_orders_fk_if_exists`;

DELIMITER //
CREATE PROCEDURE `drop_print_orders_fk_if_exists`(
    IN in_fk_name VARCHAR(64)
)
BEGIN
    DECLARE fk_count INT DEFAULT 0;

    SELECT COUNT(*) INTO fk_count
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'print_orders'
      AND CONSTRAINT_NAME = in_fk_name
      AND CONSTRAINT_TYPE = 'FOREIGN KEY';

    IF fk_count > 0 THEN
        SET @drop_fk_sql = CONCAT('ALTER TABLE `print_orders` DROP FOREIGN KEY `', in_fk_name, '`');
        PREPARE stmt_fk FROM @drop_fk_sql;
        EXECUTE stmt_fk;
        DEALLOCATE PREPARE stmt_fk;
    END IF;
END //
DELIMITER ;

-- Execute safe removal of foreign key constraint
CALL `drop_print_orders_fk_if_exists`('fk_print_orders_location');

-- Remove temporary foreign key helper procedure immediately
DROP PROCEDURE IF EXISTS `drop_print_orders_fk_if_exists`;

-- -----------------------------------------------------------------------------
-- Step 3: Temporary helper procedure for idempotent column removal
-- Safely drops added columns on print_orders only if they currently exist.
-- Avoids failure if migration was previously interrupted halfway.
-- Cleanup Guarantee:
--   - Dropped immediately after use on line 205.
--   - If aborted mid-run, DROP PROCEDURE IF EXISTS on line 158 ensures subsequent
--     runs replace and clean it up deterministically.
-- -----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS `drop_print_orders_col_if_exists`;

DELIMITER //
CREATE PROCEDURE `drop_print_orders_col_if_exists`(
    IN in_col_name VARCHAR(64)
)
BEGIN
    DECLARE col_count INT DEFAULT 0;

    SELECT COUNT(*) INTO col_count
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'print_orders'
      AND COLUMN_NAME = in_col_name;

    IF col_count > 0 THEN
        SET @drop_col_sql = CONCAT('ALTER TABLE `print_orders` DROP COLUMN `', in_col_name, '`');
        PREPARE stmt_col FROM @drop_col_sql;
        EXECUTE stmt_col;
        DEALLOCATE PREPARE stmt_col;
    END IF;
END //
DELIMITER ;

-- Execute safe removal of each delivery-specific column
CALL `drop_print_orders_col_if_exists`('fulfillment_type');
CALL `drop_print_orders_col_if_exists`('delivery_fee');
CALL `drop_print_orders_col_if_exists`('delivery_location_id');
CALL `drop_print_orders_col_if_exists`('delivery_location_name');
CALL `drop_print_orders_col_if_exists`('delivery_room_details');
CALL `drop_print_orders_col_if_exists`('delivery_time_window');
CALL `drop_print_orders_col_if_exists`('delivery_deadline');
CALL `drop_print_orders_col_if_exists`('pickup_code');
CALL `drop_print_orders_col_if_exists`('delivery_code');
CALL `drop_print_orders_col_if_exists`('verification_attempts');
CALL `drop_print_orders_col_if_exists`('max_verification_attempts');
CALL `drop_print_orders_col_if_exists`('code_invalidated');
CALL `drop_print_orders_col_if_exists`('code_generated_at');
CALL `drop_print_orders_col_if_exists`('code_expires_at');

-- Remove temporary column helper procedure immediately
DROP PROCEDURE IF EXISTS `drop_print_orders_col_if_exists`;

-- -----------------------------------------------------------------------------
-- Step 4: Drop campus_locations table
-- Isolation: Only drops the campus_locations table created by Migration 001.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `campus_locations`;

-- -----------------------------------------------------------------------------
-- Step 5: Restore original session foreign key check setting
-- Notice: This executes only if the script reaches normal completion.
-- -----------------------------------------------------------------------------
SET FOREIGN_KEY_CHECKS = @OLD_FOREIGN_KEY_CHECKS;

-- =============================================================================
-- Rollback Execution Complete.
-- Baseline print orders, student accounts, and PDF documents are intact.
-- =============================================================================
