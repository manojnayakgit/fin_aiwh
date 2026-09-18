"""Content checks are derived from the contract and nothing else."""
import pytest

from control.contracts import Contract, ContractColumn, load_contracts
from control.detect import BREAKING, MEDIUM
from control.quality import (
    FRESHNESS_DMF, QUALITY_TYPES, Check, check_all, desired, diff_quality, measure,
)


def contract(**kw) -> Contract:
    base = dict(
        dataset="RAW.AP_INVOICE", version=2, owner="fin", classification="internal",
        description="", primary_key=["INVOICE_ID"],
        freshness={"column": "LOADED_AT", "max_lag_hours": 24},
        columns=[
            ContractColumn("INVOICE_ID", "TEXT", False, length=32),
            ContractColumn("GROSS_AMOUNT", "NUMBER", False, precision=18, scale=2),
            ContractColumn("TAX_AMOUNT", "NUMBER", True, precision=18, scale=2),
            ContractColumn("LOADED_AT", "TIMESTAMP_NTZ", False),
        ],
        raw={},
    )
    base.update(kw)
    return Contract(**base)


# ------------------------------------------------------------------ derivation

def test_primary_key_becomes_a_duplicate_check():
    dup = [c for c in desired(contract()) if c.change_type == "DUPLICATE_KEY"]
    assert len(dup) == 1
    assert dup[0].columns == ("INVOICE_ID",)
    assert dup[0].max == 0 and dup[0].severity == BREAKING


def test_a_composite_key_is_one_check_over_all_its_columns():
    c = contract(primary_key=["INVOICE_ID", "LOADED_AT"])
    dup = next(x for x in desired(c) if x.change_type == "DUPLICATE_KEY")
    assert dup.columns == ("INVOICE_ID", "LOADED_AT")
    assert "INVOICE_ID, LOADED_AT" in dup.sql()


def test_every_not_null_column_gets_a_null_check_and_nullable_ones_do_not():
    nulls = {c.columns[0] for c in desired(contract()) if c.change_type == "NULL_IN_REQUIRED"}
    assert nulls == {"INVOICE_ID", "GROSS_AMOUNT", "LOADED_AT"}
    assert "TAX_AMOUNT" not in nulls


def test_freshness_block_becomes_a_stale_check_in_hours():
    st = next(c for c in desired(contract()) if c.change_type == "STALE")
    assert st.dmf == FRESHNESS_DMF
    assert st.columns == ("LOADED_AT",) and st.max == 24 and st.severity == MEDIUM


def test_no_freshness_block_means_no_stale_check():
    assert not [c for c in desired(contract(freshness={})) if c.change_type == "STALE"]


def test_explicit_expectations_are_honoured_and_unknown_metrics_ignored():
    c = contract(raw={"expectations": [
        {"column": "GROSS_AMOUNT", "metric": "MIN", "min": 0, "severity": "BREAKING",
         "description": "no negative invoices"},
        {"metric": "ROW_COUNT", "min": 1},
        {"column": "X", "metric": "MADE_UP", "max": 1},
    ]})
    exp = [x for x in desired(c) if x.change_type == "EXPECTATION_BREACHED"]
    assert [(x.dmf, x.columns, x.min, x.severity) for x in exp] == [
        ("SNOWFLAKE.CORE.MIN", ("GROSS_AMOUNT",), 0, BREAKING),
        ("SNOWFLAKE.CORE.ROW_COUNT", (), 1, MEDIUM),
    ]


def test_every_shipped_contract_yields_only_derivable_checks():
    for k in load_contracts():
        for c in desired(k):
            assert c.change_type in QUALITY_TYPES
            assert c.severity in (BREAKING, MEDIUM)


# ------------------------------------------------------------------ verdicts

def _check(**kw) -> Check:
    base = dict(dataset_key="RAW.AP_INVOICE", change_type="NULL_IN_REQUIRED",
                dmf="SNOWFLAKE.CORE.NULL_COUNT", columns=("GROSS_AMOUNT",),
                min=None, max=0, severity=BREAKING, why="contracted NOT NULL")
    base.update(kw)
    return Check(**base)


def test_within_threshold_is_not_a_finding():
    assert diff_quality([], {_check(): 0.0}) == []


def test_over_max_is_a_finding_with_the_value_in_the_rationale():
    f = diff_quality([], {_check(): 3.0})
    assert len(f) == 1
    assert f[0].change_type == "NULL_IN_REQUIRED" and f[0].severity == BREAKING
    assert "= 3" in f[0].rationale and "at most 0" in f[0].rationale


def test_under_min_is_a_finding():
    f = diff_quality([], {_check(change_type="EXPECTATION_BREACHED", min=1, max=None): 0.0})
    assert "at least 1" in f[0].rationale


def test_null_measurement_is_a_finding_not_a_pass():
    assert diff_quality([], {_check(): None})[0].rationale.startswith("measurement returned NULL")


def test_fingerprint_is_stable_while_the_breach_persists():
    """Same breach, different measured value, one event."""
    a = diff_quality([], {_check(): 3.0})[0]
    b = diff_quality([], {_check(): 7.0})[0]
    assert a.fingerprint() == b.fingerprint()


def test_findings_are_worst_first():
    stale = _check(change_type="STALE", dmf=FRESHNESS_DMF, columns=("LOADED_AT",), max=24, severity=MEDIUM)
    f = diff_quality([], {stale: 30.0, _check(): 1.0})
    assert [x.severity for x in f] == [BREAKING, MEDIUM]


# ------------------------------------------------------------------ measuring

def test_measure_issues_one_statement_per_table(monkeypatch):
    from control import quality
    seen = []
    def fake_query(conn, sql, *a, **k):
        seen.append(sql)
        n = sql.count(" AS M")
        return [{f"M{i}": 0 for i in range(n)}]
    monkeypatch.setattr(quality, "query", fake_query)
    checks = desired(contract()) + desired(contract(dataset="RAW.AR_INVOICE"))
    out = measure(None, checks)
    assert len(seen) == 2
    assert all(v == 0.0 for v in out.values())
    assert "SNOWFLAKE.CORE.DUPLICATE_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE)" in seen[0]
