-- Content governance prerequisites. Run once as ACCOUNTADMIN.
--
-- Data Metric Functions need two grants the bootstrap did not give, and the
-- freshness check needs a custom DMF because SNOWFLAKE.CORE.FRESHNESS refuses
-- TIMESTAMP_NTZ, which is every LOADED_AT in RAW.

-- 1. Let the engineering role call and attach DMFs.
GRANT EXECUTE DATA METRIC FUNCTION ON ACCOUNT TO ROLE FIN_AIWH_ENG;
GRANT DATABASE ROLE SNOWFLAKE.DATA_METRIC_USER TO ROLE FIN_AIWH_ENG;
GRANT DATABASE ROLE SNOWFLAKE.DATA_QUALITY_MONITORING_VIEWER TO ROLE FIN_AIWH_ENG;

-- 2. Freshness in hours, on the timestamp type RAW actually uses.
--    Returns how far behind now the newest row is. The contract's
--    max_lag_hours is compared against this directly.
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.FRESHNESS_NTZ_HOURS(
    arg_t TABLE(arg_c TIMESTAMP_NTZ)
)
RETURNS NUMBER
AS
$$
    SELECT TIMESTAMPDIFF(HOUR, MAX(arg_c), CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
    FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.FRESHNESS_NTZ_HOURS(TABLE(TIMESTAMP_NTZ)) TO ROLE FIN_AIWH_ENG;

-- 3. Prove it, as ACCOUNTADMIN, before handing to the CLI.
SELECT SNOWFLAKE.CORE.DUPLICATE_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE)  AS dup_invoice_ids,
       SNOWFLAKE.CORE.NULL_COUNT(SELECT GROSS_AMOUNT FROM FIN_AIWH.RAW.AP_INVOICE)      AS null_amounts,
       FIN_AIWH.META.FRESHNESS_NTZ_HOURS(SELECT LOADED_AT FROM FIN_AIWH.RAW.AP_INVOICE) AS hours_behind;
