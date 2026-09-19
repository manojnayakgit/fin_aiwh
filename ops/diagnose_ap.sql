-- Why is every AP invoice line orphaned? Run in Snowsight.
-- The seed has 8,000 invoices and 19,967 lines with zero orphans, so whatever
-- this shows is warehouse state, not the data generator.

SELECT 'AP_INVOICE'      AS tbl, COUNT(*) AS rows_now, COUNT(DISTINCT INVOICE_ID) AS distinct_keys,
       MIN(LOADED_AT) AS oldest, MAX(LOADED_AT) AS newest
FROM FIN_AIWH.RAW.AP_INVOICE
UNION ALL
SELECT 'AP_INVOICE_LINE', COUNT(*), COUNT(DISTINCT INVOICE_ID),
       MIN(LOADED_AT), MAX(LOADED_AT)
FROM FIN_AIWH.RAW.AP_INVOICE_LINE;

-- How many lines point at an invoice that is not there
SELECT COUNT(*) AS orphan_lines
FROM FIN_AIWH.RAW.AP_INVOICE_LINE l
LEFT JOIN FIN_AIWH.RAW.AP_INVOICE i ON l.INVOICE_ID = i.INVOICE_ID
WHERE i.INVOICE_ID IS NULL;

-- A sample of both sides, to see whether the keys simply look different
SELECT 'invoice' AS side, INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE      ORDER BY INVOICE_ID LIMIT 3;
SELECT 'line'    AS side, INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE_LINE ORDER BY INVOICE_ID LIMIT 3;
