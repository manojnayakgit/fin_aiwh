-- Undo every scenario: rebuild RAW to the version 1 shape.
-- Reload the data afterwards with:  python -m control.cli load
DROP TABLE IF EXISTS FIN_AIWH.RAW.AP_ACCRUAL;
