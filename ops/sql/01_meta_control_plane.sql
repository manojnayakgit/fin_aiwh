-- fin_aiwh :: control plane
-- The registry of record for what each dataset is contractually allowed to look like,
-- what it actually looks like, and every divergence between the two.

-- Every name is fully qualified so the file does not depend on session state.

-- Registered contract versions. Append only. A contract is never edited in place;
-- a change lands as a new version with a new hash, traceable to a git commit.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.CONTRACT_REGISTRY (
    CONTRACT_KEY      VARCHAR      NOT NULL,   -- e.g. RAW.AP_INVOICE
    VERSION           NUMBER       NOT NULL,
    SPEC_HASH         VARCHAR(64)  NOT NULL,   -- sha256 of the canonical YAML
    SPEC              VARIANT      NOT NULL,   -- full parsed contract
    OWNER             VARCHAR,
    CLASSIFICATION    VARCHAR,
    GIT_SHA           VARCHAR(40),
    REGISTERED_AT     TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    REGISTERED_BY     VARCHAR      NOT NULL DEFAULT CURRENT_USER(),
    IS_ACTIVE         BOOLEAN      NOT NULL DEFAULT TRUE
);

-- Point in time snapshot of what the warehouse actually holds.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.OBSERVED_SCHEMA (
    RUN_ID            VARCHAR      NOT NULL,
    OBSERVED_AT       TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    DATASET_KEY       VARCHAR      NOT NULL,
    COLUMN_NAME       VARCHAR      NOT NULL,
    ORDINAL_POSITION  NUMBER,
    DATA_TYPE         VARCHAR,
    IS_NULLABLE       BOOLEAN,
    NUMERIC_PRECISION NUMBER,
    NUMERIC_SCALE     NUMBER,
    CHARACTER_LENGTH  NUMBER
);

-- Every divergence between contract and reality, classified and triaged.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.DRIFT_EVENT (
    EVENT_ID          VARCHAR      NOT NULL,
    RUN_ID            VARCHAR      NOT NULL,
    DETECTED_AT       TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    DATASET_KEY       VARCHAR      NOT NULL,
    CONTRACT_VERSION  NUMBER,
    CHANGE_TYPE       VARCHAR      NOT NULL,   -- COLUMN_ADDED, COLUMN_REMOVED, TYPE_CHANGED, ...
    SEVERITY          VARCHAR      NOT NULL,   -- LOW, MEDIUM, BREAKING
    OBJECT_NAME       VARCHAR,                 -- column or dataset the change applies to
    BEFORE_STATE      VARIANT,
    AFTER_STATE       VARIANT,
    RATIONALE         VARCHAR,                 -- why this severity, in plain language
    STATUS            VARCHAR      NOT NULL DEFAULT 'OPEN',  -- OPEN, PROPOSED, MERGED, DISMISSED
    RESOLVED_AT       TIMESTAMP_NTZ,
    RESOLUTION_REF    VARCHAR,                 -- PR url once the agent acts on it
    IMPACT            VARIANT                  -- what breaks downstream, from dbt lineage
);

-- One row per detector execution, so runs are auditable even when nothing drifted.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.RUN_LOG (
    RUN_ID            VARCHAR      NOT NULL,
    STARTED_AT        TIMESTAMP_NTZ NOT NULL,
    FINISHED_AT       TIMESTAMP_NTZ,
    RUN_TYPE          VARCHAR      NOT NULL,   -- REGISTER, DETECT
    DATASETS_SCANNED  NUMBER,
    EVENTS_RAISED     NUMBER,
    STATUS            VARCHAR,                 -- SUCCESS, FAILED
    DETAIL            VARIANT
);

-- Convenience view: the active contract for each dataset.
CREATE OR REPLACE VIEW FIN_AIWH.META.ACTIVE_CONTRACT AS
SELECT * EXCLUDE (RN) FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY CONTRACT_KEY ORDER BY VERSION DESC) AS RN
    FROM FIN_AIWH.META.CONTRACT_REGISTRY r
    WHERE IS_ACTIVE
) WHERE RN = 1;

-- Convenience view: what needs a human or an agent right now.
CREATE OR REPLACE VIEW FIN_AIWH.META.OPEN_DRIFT AS
SELECT DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME, RATIONALE,
       IMPACT:marts AS AFFECTED_MARTS, IMPACT:models AS AFFECTED_MODELS,
       DETECTED_AT, EVENT_ID
FROM FIN_AIWH.META.DRIFT_EVENT
WHERE STATUS = 'OPEN'
ORDER BY CASE SEVERITY WHEN 'BREAKING' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, DETECTED_AT;
