"""The agent's verification must reject a wrong draft regardless of how
confident the model was. No API, no warehouse."""
import json

import pytest

from control import agent
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


class FakeRun:
    """Stand in for subprocess.run when probing branch protection."""
    def __init__(self, returncode, stdout=""):
        self.returncode, self.stdout = returncode, stdout


def test_no_branch_protection_means_no_required_checks(monkeypatch):
    from control import agent
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: FakeRun(1, ""))
    assert agent.required_checks("main") == []


def test_protection_without_status_checks_is_not_a_gate(monkeypatch):
    from control import agent
    body = '{"required_pull_request_reviews": {}}'
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: FakeRun(0, body))
    assert agent.required_checks("main") == []


def test_required_contexts_are_returned(monkeypatch):
    from control import agent
    body = '{"required_status_checks": {"contexts": ["rule tests", "dbt build (CI schema)"]}}'
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: FakeRun(0, body))
    assert agent.required_checks("main") == ["rule tests", "dbt build (CI schema)"]


def test_unparseable_protection_response_is_not_a_gate(monkeypatch):
    from control import agent
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: FakeRun(0, "<html>"))
    assert agent.required_checks("main") == []


# --------------------------------------------------------------------------
# reconciliation with GitHub
# --------------------------------------------------------------------------

PR = "https://github.com/o/r/pull/2"
ISSUE = "https://github.com/o/r/issues/3"


def _gh(monkeypatch, payload, rc=0):
    from control import agent
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: FakeRun(rc, json.dumps(payload) if payload else ""))


def test_a_merged_pr_closes_the_event(monkeypatch):
    from control import agent
    _gh(monkeypatch, {"state": "MERGED", "mergedAt": "2026-09-18T10:00:00Z"})
    assert agent.github_outcome(PR) == ("MERGED", "pull request merged")


def test_an_open_pr_changes_nothing(monkeypatch):
    from control import agent
    _gh(monkeypatch, {"state": "OPEN", "mergedAt": None})
    assert agent.github_outcome(PR) is None


def test_a_pr_closed_without_merging_reopens_the_event(monkeypatch):
    """The drift is still there. Someone rejected the fix, not the problem."""
    from control import agent
    _gh(monkeypatch, {"state": "CLOSED", "mergedAt": None})
    status, why = agent.github_outcome(PR)
    assert status == "OPEN"
    assert "unresolved" in why


def test_a_closed_issue_dismisses_the_event(monkeypatch):
    from control import agent
    _gh(monkeypatch, {"state": "CLOSED"})
    assert agent.github_outcome(ISSUE) == ("DISMISSED", "issue closed")


def test_an_open_issue_changes_nothing(monkeypatch):
    from control import agent
    _gh(monkeypatch, {"state": "OPEN"})
    assert agent.github_outcome(ISSUE) is None


def test_an_unreadable_reference_changes_nothing(monkeypatch):
    from control import agent
    _gh(monkeypatch, None, rc=1)
    assert agent.github_outcome(PR) is None
    assert agent.github_outcome("https://example.com/whatever") is None


def test_set_status_binds_ids_and_stamps_resolution(monkeypatch):
    from control import agent
    captured = {}
    monkeypatch.setattr(agent, "execute",
                        lambda c, sql, p: captured.update(sql=sql, params=p))
    agent.set_status(None, ["a", "b"], "MERGED")
    assert "%(e0)s,%(e1)s" in captured["sql"]
    assert "RESOLVED_AT = SYSDATE()" in captured["sql"]
    assert captured["params"] == {"st": "MERGED", "e0": "a", "e1": "b"}

    agent.set_status(None, ["c"], "OPEN")
    assert "RESOLVED_AT" not in captured["sql"], "reopening must not stamp a resolution time"


# --------------------------------------------------------------- publish guards

def test_a_failing_command_reports_what_it_said(monkeypatch):
    """A traceback with no error message in it is not a bug report."""
    import subprocess as sp

    def boom(*a, **k):
        raise sp.CalledProcessError(1, ["gh", "pr", "create"], output="could not add label: 'onboard'")

    monkeypatch.setattr(agent.subprocess, "check_output", boom)
    with pytest.raises(SystemExit) as e:
        agent._run(["gh", "pr", "create", "--title", "x"])
    assert "could not add label" in str(e.value)
    assert "exit 1" in str(e.value)


def test_unpushed_local_commits_block_publishing(monkeypatch):
    """Branching from origin is only safe if origin has your work."""
    monkeypatch.setattr(agent, "_run", lambda cmd, **k: "3" if "rev-list" in cmd else "")
    with pytest.raises(SystemExit) as e:
        agent._ensure_pushed("main")
    assert "3 commit(s) ahead" in str(e.value)
    assert "git push" in str(e.value)


def test_a_pushed_base_publishes(monkeypatch):
    monkeypatch.setattr(agent, "_run", lambda cmd, **k: "0" if "rev-list" in cmd else "")
    agent._ensure_pushed("main")


def test_a_branch_left_by_a_failed_run_is_recreated(monkeypatch):
    """A run that dies after the push must not be blocked by its own wreckage."""
    calls = []
    monkeypatch.setattr(agent, "_run", lambda cmd, **k: calls.append(cmd) or "")
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})())
    agent._start_branch("onboard/ap_accrual", "main")
    assert ["git", "branch", "-q", "-D", "onboard/ap_accrual"] in calls
    assert ["git", "checkout", "-q", "-b", "onboard/ap_accrual", "origin/main"] in calls
