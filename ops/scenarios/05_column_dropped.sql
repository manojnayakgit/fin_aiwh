-- Scenario: a contracted column disappears.
-- Expected: COLUMN_REMOVED / BREAKING. Bank reconciliation loses its join key.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT DROP COLUMN BANK_REF;
