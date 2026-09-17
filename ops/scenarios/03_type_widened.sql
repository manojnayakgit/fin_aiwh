-- Scenario: a text column is widened upstream.
-- Expected: TYPE_CHANGED / LOW. Existing values still fit, but any downstream
-- column still sized to the old width will silently truncate.
ALTER TABLE FIN_AIWH.RAW.AP_INVOICE
  ALTER COLUMN INVOICE_NUMBER SET DATA TYPE VARCHAR(128);
