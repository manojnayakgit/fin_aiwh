"""Onboarding: the mechanical parts are exact, and the model's part is checked."""
import pytest

from control.contracts import Contract, ContractColumn, load_contracts
from control.onboard import (
    Onboarding, add_schema, add_source, build, canonical_source, selected_columns, verify,
)
from control.onboard import tests_for as contract_tests   # not a pytest test

SOURCES = (
    "sources:\n"
    "  - name: raw\n"
    "    database: FIN_AIWH\n"
    "    schema: RAW\n"
    "    tables:\n"
    "      - name: AP_VENDOR\n"
    "      - name: AP_INVOICE\n"
)

SCHEMA = (
    "version: 2\n"
    "models:\n"
    "  - name: stg_ap_invoice\n"
    "    columns:\n"
    "      - name: invoice_id\n"
    "        data_tests: [unique, not_null]\n"
)


def contract(**kw) -> Contract:
    cols = kw.pop("columns", [
        ContractColumn("ACCRUAL_ID", "TEXT", False, length=32),
        ContractColumn("ENTITY_CODE", "TEXT", False, length=8),
        ContractColumn("AMOUNT", "NUMBER", True, precision=18, scale=2),
    ])
    base = dict(dataset="RAW.AP_ACCRUAL", version=1, owner="unassigned-needs-review",
                classification="internal", description="Accruals awaiting invoice.",
                primary_key=["ACCRUAL_ID"], freshness={}, columns=cols)
    base.update(kw)
    return Contract(**base)


GOOD_SQL = """select
    accrual_id,
    upper(entity_code) as entity_code,
    amount
from {{ source('raw', 'AP_ACCRUAL') }}
"""


def ob(sql=GOOD_SQL, table="AP_ACCRUAL") -> Onboarding:
    return Onboarding(dataset_key=f"RAW.{table}", table=table, staging_sql=sql)


# -------------------------------------------------------------------- sources

def test_source_entry_is_appended_at_the_existing_indent():
    out, err = add_source("AP_ACCRUAL", SOURCES)
    assert err is None
    assert out.splitlines()[-1] == "      - name: AP_ACCRUAL"
    assert "- name: AP_INVOICE" in out


def test_source_entry_is_not_duplicated():
    out, err = add_source("AP_INVOICE", SOURCES)
    assert err is None and out == SOURCES


def test_source_entry_needs_a_tables_block():
    _, err = add_source("AP_ACCRUAL", "sources:\n  - name: raw\n")
    assert "tables:" in err


# ---------------------------------------------------------------------- tests

def test_tests_come_only_from_the_contract():
    out = "\n".join(contract_tests(contract()))
    assert "- name: accrual_id\n        data_tests: [unique, not_null]" in out
    assert "- name: entity_code\n        data_tests: [not_null]" in out
    assert "amount" not in out          # nullable, nothing to assert


def test_schema_entry_quotes_the_description():
    out, err = add_schema(contract(description="Accruals: not yet invoiced"),
                          "stg_ap_accrual", SCHEMA)
    assert err is None
    assert '    description: "Accruals: not yet invoiced"' in out
    assert "  - name: stg_ap_accrual" in out


def test_schema_entry_is_not_duplicated():
    out, _ = add_schema(contract(), "stg_ap_invoice", SCHEMA)
    assert out == SCHEMA


def test_schema_untouched_when_the_contract_justifies_no_test():
    c = contract(primary_key=[], columns=[ContractColumn("NOTE", "TEXT", True)])
    out, _ = add_schema(c, "stg_x", SCHEMA)
    assert out == SCHEMA


# ------------------------------------------------------------------- parsing

@pytest.mark.parametrize("sql,expected", [
    ("select a, b from t", {"A", "B"}),
    ("select\n  a,\n  upper(b) as b_code\nfrom t", {"A", "B_CODE"}),
    ("select\n  -- a comment\n  a\nfrom t", {"A"}),
    ("select nullif(trim(x), '') as x from t", {"X"}),
])
def test_selected_columns(sql, expected):
    assert selected_columns(sql) == expected


# -------------------------------------------------------------- verification

def test_a_faithful_model_passes():
    assert verify(ob(), contract()).ok


def test_a_missing_contracted_column_is_rejected():
    sql = GOOD_SQL.replace("    amount\n", "")
    assert "omits contracted columns ['AMOUNT']" in " ".join(verify(ob(sql), contract()).errors)


def test_an_undeclared_column_is_rejected():
    sql = GOOD_SQL.replace("    amount\n", "    amount,\n    surprise\n")
    assert "does not declare ['SURPRISE']" in " ".join(verify(ob(sql), contract()).errors)


def test_a_dropped_primary_key_is_rejected():
    sql = GOOD_SQL.replace("    accrual_id,\n", "")
    errs = " ".join(verify(ob(sql), contract()).errors)
    assert "primary key column ACCRUAL_ID" in errs


def test_select_star_is_rejected():
    sql = "select * from {{ source('raw', 'AP_ACCRUAL') }}"
    assert "select *" in " ".join(verify(ob(sql), contract()).errors)


def test_reading_the_wrong_source_is_rejected():
    sql = GOOD_SQL.replace("AP_ACCRUAL", "AP_INVOICE")
    assert "source('raw', 'AP_ACCRUAL')" in " ".join(verify(ob(sql), contract()).errors)


def test_an_empty_model_is_rejected():
    assert "no staging model" in " ".join(verify(ob(""), contract()).errors)


# --------------------------------------------------------------------- naming

def test_names_are_derived_from_the_table():
    o = ob()
    assert o.model_name == "stg_ap_accrual"
    assert o.branch == "onboard/ap_accrual"
    assert o.staging_path.name == "stg_ap_accrual.sql"


def test_every_pass_through_staging_model_still_carries_its_contract():
    """The rule onboarding enforces must hold for what is already in the repo.

    Models that filter, union or join are deliberate transformations, not the
    pass-through shape onboarding produces, so they are out of scope here.
    """
    from control.config import ROOT
    checked = 0
    for c in load_contracts():
        path = ROOT / "dbt" / "models" / "staging" / f"stg_{c.table.lower()}.sql"
        if not path.exists():
            continue
        sql = path.read_text().lower()
        if any(k in sql for k in (" union ", "\nwhere", " join ")):
            continue
        assert {x.name.upper() for x in c.columns} <= selected_columns(sql), c.dataset
        checked += 1
    assert checked >= 5


@pytest.mark.parametrize("ref", [
    "{{ source('raw','AP_ACCRUAL') }}",
    '{{ source("raw", "AP_ACCRUAL") }}',
    "{{source( 'raw' , 'ap_accrual' )}}",
])
def test_source_reference_accepts_any_spacing_and_quotes(ref):
    sql = GOOD_SQL.replace("{{ source('raw', 'AP_ACCRUAL') }}", ref)
    assert verify(ob(sql), contract()).ok


@pytest.mark.parametrize("ref", [
    "{{ source('raw', 'ap_accrual') }}",
    '{{ source("raw", "Ap_Accrual") }}',
    "{{source( 'raw' , 'ap_accrual' )}}",
])
def test_the_source_reference_is_rewritten_to_what_dbt_will_resolve(ref):
    """dbt matches source names case-sensitively against sources.yml."""
    sql = GOOD_SQL.replace("{{ source('raw', 'AP_ACCRUAL') }}", ref)
    assert "source('raw', 'AP_ACCRUAL')" in canonical_source(sql, "AP_ACCRUAL")


def test_build_canonicalises_before_it_verifies():
    sql = GOOD_SQL.replace("'AP_ACCRUAL'", "'ap_accrual'")
    out = build(contract(), sql)
    assert out.ok, out.errors
    assert "source('raw', 'AP_ACCRUAL')" in out.staging_sql
