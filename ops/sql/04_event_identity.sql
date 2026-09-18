-- Separates what an event IS from what it is ABOUT.
--
-- EVENT_ID used to be the fingerprint: sha256 of (dataset, change, object,
-- after state). A deterministic key is right for "do not raise this twice
-- while it is open". It is wrong as a row identity: when a divergence is
-- dismissed and later re-raised, the new row got the same EVENT_ID as the old
-- one, Snowflake does not enforce primary keys, and every status update is
-- WHERE EVENT_ID = ..., so all lifecycles of one divergence moved together.
--
-- Now: FINGERPRINT is the dedupe key and may repeat across lifecycles.
--      EVENT_ID is unique per raise.
-- Safe to re-run.

ALTER TABLE FIN_AIWH.META.DRIFT_EVENT ADD COLUMN IF NOT EXISTS FINGERPRINT VARCHAR(32);

-- Existing rows: the old EVENT_ID was the fingerprint.
UPDATE FIN_AIWH.META.DRIFT_EVENT SET FINGERPRINT = EVENT_ID WHERE FINGERPRINT IS NULL;

-- Repair rows that share an EVENT_ID. Keep the newest as the live one, give
-- the older ones their own id and put them back to DISMISSED, which is the
-- state they were in before a later lifecycle's update swept them along.
CREATE OR REPLACE TEMPORARY TABLE FIN_AIWH.META._DUP AS
SELECT EVENT_ID, RUN_ID, DETECTED_AT,
       ROW_NUMBER() OVER (PARTITION BY EVENT_ID ORDER BY DETECTED_AT DESC) AS RN
FROM FIN_AIWH.META.DRIFT_EVENT;

UPDATE FIN_AIWH.META.DRIFT_EVENT e
SET EVENT_ID    = SUBSTR(SHA2(e.EVENT_ID || ':' || d.RUN_ID, 256), 1, 32),
    STATUS      = 'DISMISSED',
    RESOLVED_AT = COALESCE(e.RESOLVED_AT, d.DETECTED_AT)
FROM FIN_AIWH.META._DUP d
WHERE e.EVENT_ID = d.EVENT_ID AND e.RUN_ID = d.RUN_ID AND d.RN > 1;

DROP TABLE IF EXISTS FIN_AIWH.META._DUP;

-- Prove it: zero rows shared by more than one event.
SELECT EVENT_ID, COUNT(*) AS N FROM FIN_AIWH.META.DRIFT_EVENT
GROUP BY EVENT_ID HAVING COUNT(*) > 1;
