-- Scenario: the scale on a monetary column moves from 2 to 4 decimal places.
-- Expected: TYPE_CHANGED / BREAKING. This is the quiet one. Nothing errors,
-- every total just stops agreeing with the subledger.
-- Snowflake will not narrow or rescale in place, so the table is rebuilt.
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE AS
SELECT
    INVOICE_ID, VENDOR_ID, ENTITY_CODE, INVOICE_NUMBER, INVOICE_DATE, DUE_DATE,
    CURRENCY_CODE,
    CAST(GROSS_AMOUNT AS NUMBER(18,4)) AS GROSS_AMOUNT,
    CAST(TAX_AMOUNT   AS NUMBER(18,4)) AS TAX_AMOUNT,
    STATUS, SOURCE_SYSTEM, LOADED_AT
FROM FIN_AIWH.RAW.AP_INVOICE;
