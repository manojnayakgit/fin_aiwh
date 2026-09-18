-- Scenario: both content breaches fixed at source.
-- Re-runnable.
-- Expected: the next `agent` run closes both issues and dismisses the events.

-- 09 reversed. Snowflake has no row id, so a duplicate that is an exact copy
-- cannot be deleted "except one" in place. Rebuild the table from its own
-- distinct rows. The schema is untouched, so no drift is raised.
CREATE OR REPLACE TEMPORARY TABLE FIN_AIWH.RAW._AP_INVOICE_DEDUP AS
SELECT DISTINCT * FROM FIN_AIWH.RAW.AP_INVOICE;
DELETE FROM FIN_AIWH.RAW.AP_INVOICE;
INSERT INTO FIN_AIWH.RAW.AP_INVOICE SELECT * FROM FIN_AIWH.RAW._AP_INVOICE_DEDUP;
DROP TABLE IF EXISTS FIN_AIWH.RAW._AP_INVOICE_DEDUP;

-- 10 reversed: the feed catches up.
UPDATE FIN_AIWH.RAW.AR_RECEIPT SET LOADED_AT = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
