-- Explicit migration for an existing, selected xunjia_* OceanBase MySQL schema.
-- Back up SHOW CREATE TABLE xj2_command and existing data before applying.
-- Check INFORMATION_SCHEMA.COLUMNS first; run ADD COLUMN only if absent.
-- Repeated deployment must skip the existing account_epoch column.
-- No rows, historical results, source records or account bindings are deleted.
ALTER TABLE xj2_command ADD COLUMN account_epoch BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS xj2_session_reset (
        reset_id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(160) NOT NULL,
        workbench_id VARCHAR(160) NOT NULL, account_uid VARCHAR(80) NOT NULL,
        binding_version BIGINT NOT NULL, request_id VARCHAR(160) NOT NULL,
        request_hash VARCHAR(64) NOT NULL, account_epoch BIGINT NOT NULL,
        state VARCHAR(24) NOT NULL, code VARCHAR(160) NOT NULL,
        request_json LONGTEXT NOT NULL, snapshot_hash VARCHAR(64) NOT NULL,
        snapshot_json LONGTEXT NOT NULL, local_cut_json LONGTEXT NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE(owner_id,request_id), UNIQUE(owner_id,workbench_id,account_uid,account_epoch), KEY idx_xj2_reset_scope (owner_id,account_uid,state)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
