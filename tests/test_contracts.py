"""The shipped contracts must parse, and hashing must be change sensitive."""
import pytest

from control.contracts import load_contracts, parse_contract


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


def test_all_contracts_parse(contracts):
    assert len(contracts) == 8
    assert {c.dataset for c in contracts} >= {"RAW.AP_INVOICE", "RAW.AR_INVOICE", "RAW.FX_RATE"}


def test_every_contract_has_a_primary_key_and_owner(contracts):
    for c in contracts:
        assert c.primary_key, f"{c.dataset} has no primary key"
        assert c.owner != "unassigned", f"{c.dataset} has no owner"


def test_primary_key_columns_are_never_nullable(contracts):
    for c in contracts:
        for k in c.primary_key:
            assert c.column(k).nullable is False, f"{c.dataset}.{k} is a nullable key"


def test_hash_ignores_column_order_but_not_content(contracts):
    c = next(x for x in contracts if x.dataset == "RAW.AP_INVOICE")
    h = c.spec_hash()
    c.columns.reverse()
    assert c.spec_hash() == h
    c.columns[0].nullable = not c.columns[0].nullable
    assert c.spec_hash() != h


def test_bad_primary_key_is_rejected(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text(
        "dataset: RAW.X\nversion: 1\nprimary_key: [NOPE]\n"
        "columns:\n  - name: A\n    type: TEXT\n    nullable: false\n"
    )
    with pytest.raises(ValueError, match="unknown columns"):
        parse_contract(p)
