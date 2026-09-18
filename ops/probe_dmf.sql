-- Can this account use Data Metric Functions at all?
-- DMFs are an Enterprise Edition feature. Run this in Snowsight as ACCOUNTADMIN.
-- If step 2 errors, DMFs are not available and the quality layer needs the
-- fallback design instead.

-- 1. what edition and region are we on
SELECT CURRENT_VERSION() AS version, CURRENT_ACCOUNT() AS account, CURRENT_REGION() AS region;

-- 2. can we call a system DMF directly
SELECT SNOWFLAKE.CORE.NULL_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE) AS null_ids;

-- 3. can we call the freshness DMF, which is what the contract block needs
SELECT SNOWFLAKE.CORE.FRESHNESS(SELECT LOADED_AT FROM FIN_AIWH.RAW.AP_INVOICE) AS seconds_stale;

-- 4. can the service role be granted the privilege the scheduled checks need
SHOW GRANTS TO ROLE FIN_AIWH_SVC;
