-- Scenario: upstream adds a nullable column nobody told us about.
-- Expected: COLUMN_ADDED / LOW. Nothing downstream breaks. The contract is
-- simply out of date and should adopt the column on the next merge.
ALTER TABLE FIN_AIWH.RAW.AP_INVOICE
  ADD COLUMN APPROVER_ID VARCHAR(32)
  COMMENT 'Approver added by the ERP upgrade, undeclared';
