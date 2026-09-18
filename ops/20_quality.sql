-- Content governance prerequisites. Run once as ACCOUNTADMIN.
--
-- Data Metric Functions need two grants the bootstrap did not give, and the
-- freshness check needs a custom DMF because SNOWFLAKE.CORE.FRESHNESS refuses
-- TIMESTAMP_NTZ, which is every LOADED_AT in RAW.

-- 1. Let the engineering role attach DMFs to tables it owns. Calling a
--    system DMF directly needs no grant: every role has USAGE on them.
GRANT EXECUTE DATA METRIC FUNCTION ON ACCOUNT TO ROLE FIN_AIWH_ENG;
GRANT DATABASE ROLE SNOWFLAKE.DATA_METRIC_USER TO ROLE FIN_AIWH_ENG;

-- 1b. Optional. Only needed to read Snowflake's own history view,
--     SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS, which the CLI does not
--     use: detect measures synchronously. This is an APPLICATION role.
GRANT APPLICATION ROLE SNOWFLAKE.DATA_QUALITY_MONITORING_VIEWER TO ROLE FIN_AIWH_ENG;

-- 2. Freshness. A DMF body must be deterministic, so it cannot know what
--    time it is: that is why Snowflake ships FRESHNESS as a special case.
--    This one returns the newest timestamp as epoch seconds, and the caller
--    subtracts it from now. Two consequences, both fine:
--      - detect compares against SYSDATE() in the same statement
--      - attached, Snowflake's history records the newest load time, not a lag
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.NEWEST_EPOCH_NTZ(
    arg_t TABLE(arg_c TIMESTAMP_NTZ)
)
RETURNS NUMBER
AS
$$
    SELECT DATE_PART(EPOCH_SECOND, MAX(arg_c)) FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.NEWEST_EPOCH_NTZ(TABLE(TIMESTAMP_NTZ)) TO ROLE FIN_AIWH_ENG;

-- 2b. Null count for BOOLEAN columns. SNOWFLAKE.CORE.NULL_COUNT refuses a
--     BOOLEAN argument, and refuses it cast to text or number as well. A custom
--     DMF with the type declared is the honest fix and can be attached.
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.NULL_COUNT_BOOL(
    arg_t TABLE(arg_c BOOLEAN)
)
RETURNS NUMBER
AS
$$
    SELECT COUNT_IF(arg_c IS NULL) FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.NULL_COUNT_BOOL(TABLE(BOOLEAN)) TO ROLE FIN_AIWH_ENG;

-- 2c. Duplicate count over a composite key. SNOWFLAKE.CORE.DUPLICATE_COUNT
--     takes one column. FX_RATE's key is four. The caller concatenates the key
--     columns into one text value; this counts rows beyond the first per value.
--     A DMF argument must fit the declared type, and an expression comes out
--     at the account's maximum VARCHAR width, so both sides are bound to 4000.
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.DUPLICATE_COUNT_KEY(
    arg_t TABLE(arg_c VARCHAR(4000))
)
RETURNS NUMBER
AS
$$
    SELECT COUNT(*) - COUNT(DISTINCT arg_c) FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.DUPLICATE_COUNT_KEY(TABLE(VARCHAR(4000))) TO ROLE FIN_AIWH_ENG;

-- 3. Prove it, as ACCOUNTADMIN, before handing to the CLI.
SELECT SNOWFLAKE.CORE.DUPLICATE_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE)  AS dup_invoice_ids,
       SNOWFLAKE.CORE.NULL_COUNT(SELECT GROSS_AMOUNT FROM FIN_AIWH.RAW.AP_INVOICE)      AS null_amounts,
       FIN_AIWH.META.NULL_COUNT_BOOL(SELECT IS_ACTIVE FROM FIN_AIWH.RAW.AP_VENDOR)          AS null_flags,
       FIN_AIWH.META.DUPLICATE_COUNT_KEY(
         SELECT CONCAT_WS('\u001f', RATE_DATE::VARCHAR, FROM_CURRENCY::VARCHAR, TO_CURRENCY::VARCHAR, RATE_TYPE::VARCHAR)::VARCHAR(4000)
         FROM FIN_AIWH.RAW.FX_RATE)                                                            AS dup_fx_keys,
       (DATE_PART(EPOCH_SECOND, SYSDATE())
          - FIN_AIWH.META.NEWEST_EPOCH_NTZ(SELECT LOADED_AT FROM FIN_AIWH.RAW.AP_INVOICE)) / 3600
                                                                                           AS hours_behind;
