"""The shipped contracts must parse, and hashing must be change sensitive."""
import pytest

from control.contracts import CONTRACT_DIR, load_contracts, parse_contract

# What the agent sets on a dataset it has just discovered. A reviewer replaces it.
ONBOARDING_OWNER = "unassigned-needs-review"


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


def test_every_contract_file_parses(contracts):
    """One contract per file, no silent drops.

    This used to assert a hard count of 8. Onboarding a new source is something
    the system is built to do, so a test that fails whenever it succeeds was
    testing the wrong thing. It now checks the shape instead: every file yields
    exactly one contract, and the core AP/AR datasets are all present.
    """
    files = sorted(CONTRACT_DIR.rglob("*.yml")) + sorted(CONTRACT_DIR.rglob("*.yaml"))
    assert len(contracts) == len(files)
    assert {c.dataset for c in contracts} >= {
        "RAW.AP_VENDOR", "RAW.AP_INVOICE", "RAW.AP_INVOICE_LINE", "RAW.AP_PAYMENT",
        "RAW.AR_CUSTOMER", "RAW.AR_INVOICE", "RAW.AR_RECEIPT", "RAW.FX_RATE",
    }


def test_dataset_keys_are_unique(contracts):
    keys = [c.dataset for c in contracts]
    assert len(keys) == len(set(keys)), "two contracts claim the same dataset"


def test_every_contract_has_a_primary_key_and_owner(contracts):
    """`unassigned` is not an owner, and neither is an empty string.

    A freshly onboarded contract is allowed to carry ONBOARDING_OWNER, because
    the agent cannot know who owns a table that landed without review. The PR
    asks a reviewer to set it. Nothing else may be ownerless.
    """
    for c in contracts:
        assert c.primary_key, f"{c.dataset} has no primary key"
        assert c.owner.strip(), f"{c.dataset} has no owner"
        assert c.owner == ONBOARDING_OWNER or not c.owner.startswith("unassigned"), (
            f"{c.dataset} owner is {c.owner!r}; use a real team or "
            f"{ONBOARDING_OWNER!r} on a brand new dataset"
        )


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
