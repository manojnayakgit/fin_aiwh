-- Scenario: a contracted column disappears.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: COLUMN_REMOVED / BREAKING. Bank reconciliation loses its join key.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT DROP COLUMN IF EXISTS BANK_REF;
