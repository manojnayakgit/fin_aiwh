-- Scenario: a duplicate primary key lands in the invoice header.
-- Re-runnable: the duplicate is a copy of one fixed row, inserted only if absent.
-- Expected: DUPLICATE_KEY / BREAKING on AP_INVOICE.INVOICE_ID. The schema is
-- untouched, so schema detection sees nothing. Only the content check does.
-- Every join on INVOICE_ID now fans out; the aging pack double counts one
-- invoice, quietly.
INSERT INTO FIN_AIWH.RAW.AP_INVOICE
SELECT * FROM FIN_AIWH.RAW.AP_INVOICE
WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)
  AND (SELECT COUNT(*) FROM FIN_AIWH.RAW.AP_INVOICE
       WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)) = 1;
