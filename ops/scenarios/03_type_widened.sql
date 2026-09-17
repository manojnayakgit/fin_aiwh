-- Scenario: a text column is widened upstream.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: TYPE_CHANGED / LOW. Existing values still fit, but any downstream
-- column still sized to the old width will silently truncate.
ALTER TABLE FIN_AIWH.RAW.AP_INVOICE
  ALTER COLUMN INVOICE_NUMBER SET DATA TYPE VARCHAR(128);
