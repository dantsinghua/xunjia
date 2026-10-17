-- Explicit one-time migration for an existing qb_live_best_quote table.
-- Inspect SHOW COLUMNS and run only the missing ADD COLUMN statements.
-- New installations use schema_qb_live.sql and must not run this migration.
-- Existing values remain NULL: legacy rows without a verified quote type
-- cannot participate in yield comparisons. Valuation date is never inferred.
ALTER TABLE qb_live_best_quote ADD COLUMN bid_quote_type INT NULL;
ALTER TABLE qb_live_best_quote ADD COLUMN ofr_quote_type INT NULL;
ALTER TABLE qb_live_best_quote ADD COLUMN cnbd_date DATE NULL;
