"""Shields: deterministic, refuse to guess, honest about what they do not fix."""
import pytest

from control.contracts import load_contracts
from control.shield import MARK, Shield, apply, plan, pr_body, stale


@pytest.fixture
def ap_payment():
    return next(c for c in load_contracts() if c.dataset == "RAW.AP_PAYMENT")


@pytest.fixture
def ar_invoice():
    return next(c for c in load_contracts() if c.dataset == "RAW.AR_INVOICE")


def ev(dataset, change, col):
    return {"DATASET_KEY": dataset, "CHANGE_TYPE": change, "OBJECT_NAME": col, "SEVERITY": "BREAKING"}


STG_AP_PAYMENT = """select
    payment_id,
    invoice_id,
    payment_date,
    upper(currency_code)   as currency_code,
    paid_amount,
    upper(payment_method)  as payment_method,
    bank_ref,
    loaded_at
from {{ source('raw', 'AP_PAYMENT') }}
"""

STG_AR_INVOICE = """select
    invoice_id,
    gross_amount,
    upper(status)             as status,
    loaded_at
from {{ source('raw', 'AR_INVOICE') }}
"""


def test_dropped_column_becomes_a_typed_null(ap_payment):
    sh = plan([ev("RAW.AP_PAYMENT", "COLUMN_REMOVED", "BANK_REF")], ap_payment, "https://x/issues/3")
    apply(sh, STG_AP_PAYMENT)
    assert sh.ok, sh.errors
    assert "null::varchar(64) as bank_ref,  -- shield: BANK_REF dropped upstream" in sh.staging_after
    assert "https://x/issues/3" in sh.staging_after
    assert "source('raw'" in sh.staging_after


def test_type_change_is_cast_back_keeping_the_original_expression(ar_invoice):
    sh = plan([ev("RAW.AR_INVOICE", "TYPE_CHANGED", "GROSS_AMOUNT")], ar_invoice, None)
    apply(sh, STG_AR_INVOICE)
    assert sh.ok, sh.errors
    assert "cast(gross_amount as number(18,2)) as gross_amount," in sh.staging_after


def test_type_change_on_an_aliased_expression_wraps_the_expression(ar_invoice):
    sh = plan([ev("RAW.AR_INVOICE", "TYPE_CHANGED", "STATUS")], ar_invoice, None)
    apply(sh, STG_AR_INVOICE)
    assert sh.ok, sh.errors
    assert "cast(upper(status) as varchar(16)) as status," in sh.staging_after


def test_relaxed_nullability_passes_through_and_adds_a_failing_test(ar_invoice):
    sh = plan([ev("RAW.AR_INVOICE", "NULLABILITY_RELAXED", "STATUS")], ar_invoice, "https://x/issues/4")
    apply(sh, STG_AR_INVOICE)
    assert sh.ok, sh.errors
    assert "upper(status)             as status," in sh.staging_after, "expression must be untouched"
    assert sh.staging_after.startswith(MARK)
    files = sh.test_files()
    assert len(files) == 1
    path, sql = next(iter(files.items()))
    assert path.name == "shield_ar_invoice_status_not_null.sql"
    assert "where status is null" in sql
    assert "ref('stg_ar_invoice')" in sql


def test_a_column_missing_from_the_model_is_refused_not_guessed(ap_payment):
    sh = plan([ev("RAW.AP_PAYMENT", "COLUMN_REMOVED", "BANK_REF")], ap_payment, None)
    apply(sh, "select payment_id from {{ source('raw', 'AP_PAYMENT') }}\n")
    assert not sh.ok
    assert any("refusing to guess" in e for e in sh.errors)


def test_unshieldable_changes_are_listed_not_attempted(ap_payment):
    sh = plan([ev("RAW.AP_PAYMENT", "DATASET_MISSING", None),
               ev("RAW.AP_PAYMENT", "COLUMN_REMOVED", "BANK_REF")], ap_payment, None)
    assert len(sh.patches) == 1
    assert sh.unshieldable and "DATASET_MISSING" in sh.unshieldable[0]


def test_no_contract_means_no_shield():
    sh = plan([ev("RAW.AP_ACCRUAL", "COLUMN_REMOVED", "X")], None, None)
    assert not sh.ok and "no contract" in sh.errors[0]


def test_pr_body_is_honest(ap_payment):
    sh = plan([ev("RAW.AP_PAYMENT", "COLUMN_REMOVED", "BANK_REF")], ap_payment, "https://x/issues/3")
    apply(sh, STG_AP_PAYMENT)
    body = pr_body(sh, "https://x/issues/3", None)
    assert "does not fix the data" in body
    assert "carries no values" in body
    assert "https://x/issues/3" in body


def test_stale_detection_flags_a_shield_whose_drift_is_gone(tmp_path, monkeypatch):
    from control import shield as s
    monkeypatch.setattr(s, "STAGING", tmp_path)
    (tmp_path / "stg_ap_payment.sql").write_text(
        "select\n    null::varchar(64) as bank_ref,  -- shield: BANK_REF dropped upstream\n"
        "from {{ source('raw', 'AP_PAYMENT') }}\n")
    assert s.stale(active_columns=set()) == [("AP_PAYMENT", "BANK_REF", "BANK_REF dropped upstream")]
    assert s.stale(active_columns={("AP_PAYMENT", "BANK_REF")}) == []
