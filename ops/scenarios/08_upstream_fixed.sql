-- Scenario: the source team repairs both breaking changes.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: the two BREAKING events stop being raised, and `detect` reports the
-- two installed shields as stale. Nothing here touches AP_ACCRUAL, which is now
-- contracted, and nothing reverts REVENUE_STREAM, which is a MEDIUM the project
-- should adopt rather than push back upstream.
--
-- This is the other half of scenarios 05 and 06. A shield is a temporary
-- measure by definition, so the system has to be able to take one back out.

-- 05 reversed: the dropped column returns. The values do not. The shield said
-- exactly that, and re-adding the column as nullable is the honest repair:
-- the shape is restored, the history is still missing.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT
  ADD COLUMN IF NOT EXISTS BANK_REF VARCHAR(64)
  COMMENT 'Bank reference, restored by the source team after being dropped';

-- 06 reversed: the guarantee comes back. This fails if any null slipped in
-- while the guarantee was gone, which is the correct outcome: the source team
-- must clean those rows before it can promise NOT NULL again.
UPDATE FIN_AIWH.RAW.AR_INVOICE SET STATUS = 'OPEN' WHERE STATUS IS NULL;
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS SET NOT NULL;
