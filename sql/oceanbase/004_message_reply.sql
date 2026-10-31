-- Explicit OceanBase MySQL migration; execute only in the verified xunjia_* schema.
-- Preserve a backup and SHOW CREATE TABLE xj2_message before any DDL.
-- On repeat runs, skip this ADD COLUMN if INFORMATION_SCHEMA.COLUMNS already has it:
--   SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
--   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='xj2_message' AND COLUMN_NAME='reply_json';
-- NULL means no captured quote (no quote, legacy row, or row stored before this column). Never backfill.
ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;
