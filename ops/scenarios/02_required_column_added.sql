-- Scenario: upstream adds a column and makes it mandatory.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: COLUMN_ADDED / MEDIUM. Readers are fine, writers are not.
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ADD COLUMN IF NOT EXISTS REVENUE_STREAM VARCHAR(16);
UPDATE FIN_AIWH.RAW.AR_INVOICE SET REVENUE_STREAM = 'PRODUCT' WHERE REVENUE_STREAM IS NULL;
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN REVENUE_STREAM SET NOT NULL;
