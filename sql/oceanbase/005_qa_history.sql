-- Additive QA history migration, executed by the verified incremental upgrader.
-- Only xunjia_business is addressed. Existing business and SQLite rows remain.
CREATE TABLE IF NOT EXISTS `xunjia_business`.`xj2_qa_session` (
  `id` VARCHAR(64) NOT NULL PRIMARY KEY,
  `owner` VARCHAR(160) NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `updated_at` DOUBLE NOT NULL,
  KEY `idx_xj2_qa_session_owner` (`owner`,`updated_at`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS `xunjia_business`.`xj2_qa_turn` (
  `id` VARCHAR(64) NOT NULL PRIMARY KEY,
  `owner` VARCHAR(160) NOT NULL,
  `session_id` VARCHAR(64) NOT NULL,
  `request_id` VARCHAR(128) NOT NULL,
  `question` TEXT NOT NULL,
  `state` VARCHAR(16) NOT NULL,
  `result` LONGTEXT NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `updated_at` DOUBLE NOT NULL,
  UNIQUE KEY `uq_xj2_qa_request` (`owner`,`request_id`),
  KEY `idx_xj2_qa_turn_session` (`session_id`,`created_at`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS `xunjia_business`.`xj2_qa_meta` (
  `key` VARCHAR(192) NOT NULL PRIMARY KEY,
  `value` DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS `xunjia_business`.`xj2_qa_import` (
  `source` VARCHAR(160) NOT NULL PRIMARY KEY,
  `session_id` VARCHAR(64) NOT NULL,
  `imported_at` DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
