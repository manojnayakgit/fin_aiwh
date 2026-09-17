"""Classification tests. No warehouse required: the rules are pure functions."""
import pytest

from control.contracts import Contract, ContractColumn
from control.detect import BREAKING, LOW, MEDIUM, ObservedColumn, diff_all, diff_dataset


def col(name, type_="TEXT", nullable=False, length=32, precision=None, scale=None):
    return ContractColumn(name=name, type=type_, nullable=nullable, length=length,
                          precision=precision, scale=scale)


def obs(name, type_="TEXT", nullable=False, length=32, precision=None, scale=None, ordinal=1):
    return ObservedColumn(name=name, type=type_, nullable=nullable, length=length,
                          precision=precision, scale=scale, ordinal=ordinal)


@pytest.fixture
def contract():
    return Contract(
        dataset="RAW.AP_INVOICE", version=1, owner="o", classification="c",
        description="", primary_key=["INVOICE_ID"], freshness={},
        columns=[
            col("INVOICE_ID"),
            col("GROSS_AMOUNT", "NUMBER", False, None, 18, 2),
            col("TAX_AMOUNT", "NUMBER", True, None, 18, 2),
        ],
    )


def live(contract, **overrides):
    """The warehouse exactly matching the contract, with named columns replaced."""
    base = {
        c.name: obs(c.name, c.type, c.nullable, c.length, c.precision, c.scale, i + 1)
        for i, c in enumerate(contract.columns)
    }
    base.update(overrides)
    return base


def only(findings, change_type):
    return [f for f in findings if f.change_type == change_type]


def test_matching_warehouse_produces_no_findings(contract):
    assert diff_dataset(contract, live(contract)) == []


def test_missing_table_is_breaking(contract):
    f = diff_dataset(contract, None)
    assert len(f) == 1
    assert f[0].change_type == "DATASET_MISSING"
    assert f[0].severity == BREAKING


def test_dropped_column_is_breaking(contract):
    cols = live(contract)
    del cols["TAX_AMOUNT"]
    f = only(diff_dataset(contract, cols), "COLUMN_REMOVED")
    assert [x.severity for x in f] == [BREAKING]
    assert f[0].object_name == "TAX_AMOUNT"


def test_dropped_primary_key_explains_identity_loss(contract):
    cols = live(contract)
    del cols["INVOICE_ID"]
    f = only(diff_dataset(contract, cols), "COLUMN_REMOVED")[0]
    assert f.severity == BREAKING
    assert "identity" in f.rationale


def test_new_nullable_column_is_low(contract):
    cols = live(contract)
    cols["APPROVER_ID"] = obs("APPROVER_ID", nullable=True, ordinal=9)
    f = only(diff_dataset(contract, cols), "COLUMN_ADDED")[0]
    assert f.severity == LOW


def test_new_not_null_column_is_medium(contract):
    cols = live(contract)
    cols["ENTITY_CODE"] = obs("ENTITY_CODE", nullable=False, ordinal=9)
    f = only(diff_dataset(contract, cols), "COLUMN_ADDED")[0]
    assert f.severity == MEDIUM


def test_widening_text_is_low(contract):
    cols = live(contract, INVOICE_ID=obs("INVOICE_ID", length=64))
    f = only(diff_dataset(contract, cols), "TYPE_CHANGED")[0]
    assert f.severity == LOW
    assert f.after["type"] == "TEXT(64)"


def test_narrowing_text_is_breaking(contract):
    cols = live(contract, INVOICE_ID=obs("INVOICE_ID", length=8))
    assert only(diff_dataset(contract, cols), "TYPE_CHANGED")[0].severity == BREAKING


def test_base_type_change_is_breaking(contract):
    cols = live(contract, GROSS_AMOUNT=obs("GROSS_AMOUNT", "TEXT", False, 32))
    assert only(diff_dataset(contract, cols), "TYPE_CHANGED")[0].severity == BREAKING


def test_scale_change_on_money_is_breaking(contract):
    cols = live(contract, GROSS_AMOUNT=obs("GROSS_AMOUNT", "NUMBER", False, None, 18, 4))
    f = only(diff_dataset(contract, cols), "TYPE_CHANGED")[0]
    assert f.severity == BREAKING
    assert "precision" in f.rationale


def test_precision_widening_is_low(contract):
    cols = live(contract, GROSS_AMOUNT=obs("GROSS_AMOUNT", "NUMBER", False, None, 38, 2))
    assert only(diff_dataset(contract, cols), "TYPE_CHANGED")[0].severity == LOW


def test_relaxed_nullability_is_breaking(contract):
    cols = live(contract, INVOICE_ID=obs("INVOICE_ID", nullable=True))
    f = only(diff_dataset(contract, cols), "NULLABILITY_RELAXED")[0]
    assert f.severity == BREAKING


def test_tightened_nullability_is_medium(contract):
    cols = live(contract, TAX_AMOUNT=obs("TAX_AMOUNT", "NUMBER", False, None, 18, 2))
    f = only(diff_dataset(contract, cols), "NULLABILITY_TIGHTENED")[0]
    assert f.severity == MEDIUM


def test_uncontracted_table_is_flagged(contract):
    observed = {contract.dataset: live(contract), "RAW.AP_ACCRUAL": {"X": obs("X")}}
    f = only(diff_all([contract], observed), "DATASET_UNGOVERNED")
    assert len(f) == 1
    assert f[0].dataset_key == "RAW.AP_ACCRUAL"


def test_findings_are_ordered_worst_first(contract):
    cols = live(contract, INVOICE_ID=obs("INVOICE_ID", length=64))
    del cols["TAX_AMOUNT"]
    cols["NOTE"] = obs("NOTE", nullable=True, ordinal=9)
    sevs = [f.severity for f in diff_all([contract], {contract.dataset: cols})]
    assert sevs == sorted(sevs, key=lambda s: {BREAKING: 0, MEDIUM: 1, LOW: 2}[s])


def test_fingerprint_is_stable_and_specific(contract):
    a = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=64)))[0]
    b = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=64)))[0]
    c = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=128)))[0]
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
