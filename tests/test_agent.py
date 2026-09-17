"""The agent's verification must reject a wrong draft regardless of how
confident the model was. No API, no warehouse."""
import pytest

from control.agent import Bundle, Proposal, bundle_events, build_prompt, verify
from control.contracts import load_contracts
from control.detect import ObservedColumn


def obs(name, type_="TEXT", nullable=False, length=32, precision=None, scale=None, ordinal=1):
    return ObservedColumn(name=name, type=type_, nullable=nullable, length=length,
                          precision=precision, scale=scale, ordinal=ordinal)


@pytest.fixture
def ap_payment():
    return next(c for c in load_contracts() if c.dataset == "RAW.AP_PAYMENT")


@pytest.fixture
def live_with_new_column(ap_payment):
    """AP_PAYMENT exactly as contracted, plus an undeclared nullable column."""
    cols = {
        c.name: obs(c.name, c.type, c.nullable, c.length, c.precision, c.scale, i + 1)
        for i, c in enumerate(ap_payment.columns)
    }
    cols["CLEARING_HOUSE"] = obs("CLEARING_HOUSE", nullable=True, length=16, ordinal=99)
    return cols


@pytest.fixture
def bundle(ap_payment, live_with_new_column):
    return Bundle(
        dataset_key="RAW.AP_PAYMENT",
        events=[{
            "EVENT_ID": "abc", "DATASET_KEY": "RAW.AP_PAYMENT", "CHANGE_TYPE": "COLUMN_ADDED",
            "SEVERITY": "LOW", "OBJECT_NAME": "CLEARING_HOUSE", "BEFORE_STATE": None,
            "AFTER_STATE": {"type": "TEXT(16)", "nullable": True}, "RATIONALE": "x",
        }],
        contract=ap_payment,
        observed=live_with_new_column,
    )


def good_yaml(ap_payment, version=2, extra=True):
    text = ap_payment.source_path.read_text().replace("version: 1", f"version: {version}")
    if extra:
        text += (
            "  - name: CLEARING_HOUSE\n"
            "    type: TEXT\n"
            "    length: 16\n"
            "    nullable: true\n"
            "    description: Clearing house code. Inferred from the column name.\n"
        )
    return text


def proposal(bundle, yaml_text, staging=None):
    return Proposal(bundle=bundle, contract_yaml=yaml_text, staging_sql=staging,
                    pr_title="t", pr_body="b", reasoning="r")


def test_good_proposal_passes(bundle, ap_payment):
    p = verify(proposal(bundle, good_yaml(ap_payment)))
    assert p.ok, p.errors
    assert p.contract.version == 2
    assert p.branch == "drift/ap_payment-v2"


def test_unbumped_version_is_rejected(bundle, ap_payment):
    p = verify(proposal(bundle, good_yaml(ap_payment, version=1)))
    assert any("version is 1" in e for e in p.errors)


def test_over_bumped_version_is_rejected(bundle, ap_payment):
    p = verify(proposal(bundle, good_yaml(ap_payment, version=5)))
    assert any("expected 2" in e for e in p.errors)


def test_proposal_that_ignores_the_new_column_is_rejected(bundle, ap_payment):
    p = verify(proposal(bundle, good_yaml(ap_payment, extra=False)))
    assert any("COLUMN_ADDED CLEARING_HOUSE" in e for e in p.errors)


def test_proposal_that_drops_a_column_is_rejected(bundle, ap_payment):
    text = good_yaml(ap_payment)
    text = text.replace("  - name: BANK_REF\n    type: TEXT\n    length: 64\n    nullable: true\n"
                        "    description: Bank reference for reconciliation.\n", "")
    p = verify(proposal(bundle, text))
    assert any("drops contracted columns ['BANK_REF']" in e for e in p.errors)


def test_garbage_yaml_is_rejected(bundle):
    p = verify(proposal(bundle, "dataset: [unclosed"))
    assert p.errors and "does not parse" in p.errors[0]


def test_staging_that_abandons_the_source_is_rejected(bundle, ap_payment):
    p = verify(proposal(bundle, good_yaml(ap_payment), staging="select 1"))
    assert any("source('raw'" in e for e in p.errors)


def test_new_dataset_expects_version_one(live_with_new_column):
    b = Bundle(dataset_key="RAW.AP_ACCRUAL", events=[{
        "EVENT_ID": "z", "DATASET_KEY": "RAW.AP_ACCRUAL", "CHANGE_TYPE": "DATASET_UNGOVERNED",
        "SEVERITY": "MEDIUM", "OBJECT_NAME": None, "BEFORE_STATE": None,
        "AFTER_STATE": {"columns": 1}, "RATIONALE": "x",
    }], contract=None, observed={"ACCRUAL_ID": obs("ACCRUAL_ID")})
    text = ("dataset: RAW.AP_ACCRUAL\nversion: 2\nowner: unassigned-needs-review\n"
            "primary_key: [ACCRUAL_ID]\ncolumns:\n  - name: ACCRUAL_ID\n    type: TEXT\n"
            "    length: 32\n    nullable: false\n")
    p = verify(proposal(b, text))
    assert any("expected 1" in e for e in p.errors)
    p = verify(proposal(b, text.replace("version: 2", "version: 1")))
    assert p.ok, p.errors


def test_bundles_group_by_dataset_and_pick_worst(ap_payment, live_with_new_column):
    events = [
        {"EVENT_ID": "1", "DATASET_KEY": "RAW.AP_PAYMENT", "SEVERITY": "LOW"},
        {"EVENT_ID": "2", "DATASET_KEY": "RAW.AP_PAYMENT", "SEVERITY": "MEDIUM"},
        {"EVENT_ID": "3", "DATASET_KEY": "RAW.AP_INVOICE", "SEVERITY": "BREAKING"},
    ]
    bs = bundle_events(events, [ap_payment], {"RAW.AP_PAYMENT": live_with_new_column})
    assert [b.dataset_key for b in bs] == ["RAW.AP_INVOICE", "RAW.AP_PAYMENT"]
    assert bs[0].worst == "BREAKING" and bs[0].contract is None
    assert bs[1].worst == "MEDIUM" and bs[1].event_ids == ["1", "2"]


def test_prompt_carries_contract_events_and_live_schema(bundle):
    text = build_prompt(bundle, "example: yes")
    assert "RAW.AP_PAYMENT" in text
    assert "CLEARING_HOUSE" in text
    assert "Current contract:" in text
    assert "Current staging model:" in text
    assert "COLUMN_ADDED" in text


def test_mark_binds_every_event_id_as_a_parameter():
    """Regression: the IN list must be driver parameters, not string formatting."""
    from control import agent

    captured = {}

    def fake_execute(conn, sql, params):
        captured["sql"], captured["params"] = sql, params

    original = agent.execute
    agent.execute = fake_execute
    try:
        agent.mark(None, ["a1", "b2"], "PROPOSED", "https://x/pr/1")
    finally:
        agent.execute = original

    assert "%(e0)s,%(e1)s" in captured["sql"]
    assert "%s" not in captured["sql"].replace("%(", "")
    assert captured["params"] == {"st": "PROPOSED", "ref": "https://x/pr/1", "e0": "a1", "e1": "b2"}
