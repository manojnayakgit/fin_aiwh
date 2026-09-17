-- Scenario: the scale on the monetary columns moves from 2 to 4 decimal places.
-- Expected: TYPE_CHANGED / BREAKING on GROSS_AMOUNT and TAX_AMOUNT, nothing else.
-- This is the quiet one. Nothing errors, every total just stops agreeing with
-- the subledger.
--
-- Snowflake will not rescale a NUMBER in place, so the table is rebuilt. Note
-- the explicit column list: a bare CREATE TABLE AS SELECT drops every NOT NULL
-- constraint, which the detector would correctly report as ten extra breaks.
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL,
    VENDOR_ID       VARCHAR(32)   NOT NULL,
    ENTITY_CODE     VARCHAR(8)    NOT NULL,
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL,
    INVOICE_DATE    DATE          NOT NULL,
    DUE_DATE        DATE          NOT NULL,
    CURRENCY_CODE   VARCHAR(3)    NOT NULL,
    GROSS_AMOUNT    NUMBER(18,4)  NOT NULL,
    TAX_AMOUNT      NUMBER(18,4),
    STATUS          VARCHAR(16)   NOT NULL,
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL,
    LOADED_AT       TIMESTAMP_NTZ NOT NULL
) AS
SELECT
    INVOICE_ID, VENDOR_ID, ENTITY_CODE, INVOICE_NUMBER, INVOICE_DATE, DUE_DATE,
    CURRENCY_CODE, GROSS_AMOUNT, TAX_AMOUNT, STATUS, SOURCE_SYSTEM, LOADED_AT
FROM FIN_AIWH.RAW.AP_INVOICE;
