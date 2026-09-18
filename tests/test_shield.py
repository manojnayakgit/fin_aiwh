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


# ------------------------------------------------------------------ retirement
# The inverse of apply(). Held to the same bar: derived from the shield's own
# line, and refuses anything it does not recognise.

from control.shield import Retirement, plan_retirement, retire_body, retire_line

ISSUE = "https://github.com/manojnayakgit/fin_aiwh/issues/3"


def test_a_null_shield_restores_the_bare_column():
    line = f"    null::varchar(64) as bank_ref,  {MARK} BANK_REF dropped upstream, restored as NULL, see {ISSUE}"
    assert retire_line(line) == "    bank_ref,"


def test_a_null_shield_on_the_last_column_keeps_no_comma():
    line = f"    null::varchar(64) as bank_ref  {MARK} BANK_REF dropped upstream, see {ISSUE}"
    assert retire_line(line) == "    bank_ref"


def test_a_cast_shield_restores_the_original_expression():
    line = f"    cast(paid_amount as number(18,2)) as paid_amount,  {MARK} PAID_AMOUNT type changed"
    assert retire_line(line) == "    paid_amount,"


def test_a_cast_shield_over_an_expression_keeps_the_expression():
    line = f"    cast(upper(code) as varchar(8)) as code,  {MARK} CODE type changed"
    assert retire_line(line) == "    upper(code) as code,"


def test_an_unrecognised_shield_line_is_refused():
    assert retire_line(f"    coalesce(x, 0) as x,  {MARK} X something odd") is None


def test_retiring_the_shielded_ap_payment_model_round_trips(tmp_path, monkeypatch):
    """The live shielded model must come back to exactly the plain select."""
    from control import shield
    staging = tmp_path / "staging"; staging.mkdir()
    tests = tmp_path / "tests"; tests.mkdir()
    monkeypatch.setattr(shield, "STAGING", staging)
    monkeypatch.setattr(shield, "TESTS", tests)
    shielded = STG_AP_PAYMENT.replace(
        "    bank_ref,\n",
        f"    null::varchar(64) as bank_ref,  {MARK} BANK_REF dropped upstream, restored as NULL, see {ISSUE}\n")
    (staging / "stg_ap_payment.sql").write_text(shielded)

    r = plan_retirement("AP_PAYMENT", {"BANK_REF"})
    assert r.ok, r.errors
    assert r.staging_after == STG_AP_PAYMENT
    assert r.issues == [ISSUE]
    assert r.drop_tests == []


def test_retiring_a_pass_through_shield_drops_its_guard_test(tmp_path, monkeypatch):
    from control import shield
    staging = tmp_path / "staging"; staging.mkdir()
    tests = tmp_path / "tests"; tests.mkdir()
    monkeypatch.setattr(shield, "STAGING", staging)
    monkeypatch.setattr(shield, "TESTS", tests)
    plain = "select\n    invoice_id,\n    upper(status) as status\nfrom {{ source('raw', 'AR_INVOICE') }}\n"
    (staging / "stg_ar_invoice.sql").write_text(
        f"{MARK} STATUS may now be null upstream, guarded by a test, see {ISSUE}\n\n" + plain)
    guard = tests / "shield_ar_invoice_status_not_null.sql"
    guard.write_text("select status from {{ ref('stg_ar_invoice') }} where status is null\n")

    r = plan_retirement("AR_INVOICE", {"STATUS"})
    assert r.ok, r.errors
    assert r.staging_after == plain
    assert r.drop_tests == [guard]


def test_retirement_refuses_a_column_with_no_shield(tmp_path, monkeypatch):
    from control import shield
    staging = tmp_path / "staging"; staging.mkdir()
    monkeypatch.setattr(shield, "STAGING", staging)
    (staging / "stg_ap_payment.sql").write_text(STG_AP_PAYMENT)
    r = plan_retirement("AP_PAYMENT", {"BANK_REF"})
    assert not r.ok
    assert "no shield marker" in r.errors[0]


def test_retirement_leaves_other_shields_alone(tmp_path, monkeypatch):
    from control import shield
    staging = tmp_path / "staging"; staging.mkdir()
    monkeypatch.setattr(shield, "STAGING", staging)
    two = STG_AP_PAYMENT.replace(
        "    bank_ref,\n", f"    null::varchar(64) as bank_ref,  {MARK} BANK_REF dropped\n"
    ).replace(
        "    paid_amount,\n", f"    cast(paid_amount as number(18,2)) as paid_amount,  {MARK} PAID_AMOUNT type changed\n")
    (staging / "stg_ap_payment.sql").write_text(two)
    r = plan_retirement("AP_PAYMENT", {"BANK_REF"})
    assert r.ok
    assert "    bank_ref," in r.staging_after
    assert f"{MARK} PAID_AMOUNT" in r.staging_after     # untouched


def test_retirement_body_closes_the_issue():
    r = Retirement(table="AP_PAYMENT", columns=["BANK_REF"], issues=[ISSUE])
    body = retire_body(r)
    assert f"Closes {ISSUE}" in body
    assert "`BANK_REF`" in body
