-- Scenario: the AR receipts feed stops. Nothing errors, the table just stops
-- moving. Every aging number stays plausible and drifts further from true.
-- Re-runnable: setting an old timestamp older is a no-op.
-- Expected: STALE / MEDIUM on AR_RECEIPT.LOADED_AT, contract allows 24h.
UPDATE FIN_AIWH.RAW.AR_RECEIPT
SET LOADED_AT = DATEADD(DAY, -3, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
WHERE LOADED_AT > DATEADD(DAY, -3, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ);
