-- Scenario: a new source table lands in the warehouse with no contract.
-- Expected: DATASET_UNGOVERNED / MEDIUM. It cannot be modelled until someone
-- either writes a contract for it or rejects it.
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_ACCRUAL (
    ACCRUAL_ID     VARCHAR(32)   NOT NULL,
    ENTITY_CODE    VARCHAR(8)    NOT NULL,
    GL_ACCOUNT     VARCHAR(16)   NOT NULL,
    PERIOD         VARCHAR(7)    NOT NULL,
    ACCRUAL_AMOUNT NUMBER(18,2)  NOT NULL,
    CURRENCY_CODE  VARCHAR(3)    NOT NULL,
    LOADED_AT      TIMESTAMP_NTZ NOT NULL
) COMMENT = 'Landed by the close automation team without review';

INSERT INTO FIN_AIWH.RAW.AP_ACCRUAL
SELECT 'ACR' || SEQ8(), 'UK01', '500100', '2026-08',
       UNIFORM(1000, 90000, RANDOM())::NUMBER(18,2), 'GBP', SYSDATE()
FROM TABLE(GENERATOR(ROWCOUNT => 400));
