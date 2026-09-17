-- Scenario: upstream stops guaranteeing a value.
-- Expected: NULLABILITY_RELAXED / BREAKING. Every aggregate that assumed a
-- status exists now has a silent bucket of nulls.
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS DROP NOT NULL;
