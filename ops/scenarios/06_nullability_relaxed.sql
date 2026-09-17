-- Scenario: upstream stops guaranteeing a value.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: NULLABILITY_RELAXED / BREAKING. Every aggregate that assumed a
-- status exists now has a silent bucket of nulls.
-- Dropping NOT NULL on an already nullable column succeeds, so this is safe to repeat.
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS DROP NOT NULL;
