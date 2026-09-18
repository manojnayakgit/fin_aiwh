"""Loading must decide whether it can succeed before it truncates anything."""
from control.load import plan_load

CSV = ["INVOICE_ID", "VENDOR_ID", "GROSS_AMOUNT", "LOADED_AT"]


def live(*cols):
    """(name, nullable, default) triples as INFORMATION_SCHEMA would report them."""
    return [{"COLUMN_NAME": n, "IS_NULLABLE": "YES" if nl else "NO", "COLUMN_DEFAULT": d}
            for n, nl, d in cols]


def test_exact_match_loads_every_csv_column():
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None)))
    assert why is None and cols == CSV


def test_an_adopted_nullable_column_missing_from_the_seed_is_fine():
    """Scenario 01 adopted APPROVER_ID into v2. The v1 seed must still load."""
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("APPROVER_ID", True, None)))
    assert why is None and "APPROVER_ID" not in cols


def test_a_not_null_column_the_seed_lacks_is_refused_before_truncate():
    """Scenario 02 added REVENUE_STREAM NOT NULL. A load would leave it null and fail
    after the truncate, which is the failure that emptied AP_INVOICE live."""
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("REVENUE_STREAM", False, None)))
    assert cols == [] and "REVENUE_STREAM" in why


def test_a_not_null_column_with_a_default_is_fine():
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("SOURCE", False, "'ERP'")))
    assert why is None


def test_a_csv_column_the_table_lacks_is_refused():
    cols, why = plan_load(CSV + ["GHOST"], live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                                ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None)))
    assert cols == [] and "GHOST" in why
