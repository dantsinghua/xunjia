-- Explicit OceanBase MySQL migration; execute only in the verified xunjia_* schema.
-- Preserve a backup and SHOW CREATE TABLE / SHOW INDEX results before any DDL.
-- On repeat runs, skip this ADD COLUMN if INFORMATION_SCHEMA.COLUMNS has it.
-- NULL means legacy collector provenance is unknown. Never fill from the current binding.
ALTER TABLE xj2_thread ADD COLUMN workbench_id VARCHAR(160) NULL;

-- Identify the exact old UNIQUE index by its ordered column signature, NOT by a guessed name:
SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') AS ordered_columns
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='xj2_thread' AND NON_UNIQUE=0
GROUP BY INDEX_NAME
HAVING ordered_columns='owner_id,account_uid,peer_uid';

-- Replace EXACT_OLD_UNIQUE_NAME with that single verified index name.
-- If no old signature remains, do not DROP another index. Verify the new signature instead.
-- Execute the following as one ALTER after that inspection; never drop/rebuild the table.
-- ALTER TABLE xj2_thread DROP INDEX `EXACT_OLD_UNIQUE_NAME`,
--   ADD UNIQUE KEY uk_xj2_thread_pair (owner_id,workbench_id,account_uid,peer_uid);

-- Any later provenance backfill must use an audited per-thread ID -> workbench mapping.
-- Do not infer it from xj2_binding, latest selection, dates or a shared account UID.
