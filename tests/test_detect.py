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


# --------------------------------------------------------------------------
# persistence: a divergence already being worked on must not be raised twice
# --------------------------------------------------------------------------

class FakeConn:
    """Records what persist() writes, and answers the fingerprint query."""
    def __init__(self, active_ids):
        self.active_ids = active_ids
        self.inserted = []


def _wire(monkeypatch, conn):
    from control import detect as d
    # The live rows now carry both: the fingerprint (what) and a per-raise id.
    monkeypatch.setattr(d, "query", lambda c, sql, p=None: [
        {"FINGERPRINT": i, "EVENT_ID": f"live-{i[:8]}"} for i in c.active_ids])
    monkeypatch.setattr(d, "execute", lambda c, sql, p=None: c.inserted.append(p))


def _one_finding(contract):
    from control.detect import diff_dataset
    cols = {
        c.name: ObservedColumn(c.name, c.type, c.nullable, i + 1, c.length,
                               c.precision, c.scale)
        for i, c in enumerate(contract.columns)
    }
    cols["NEW_COL"] = obs("NEW_COL", nullable=True, ordinal=9)
    return diff_dataset(contract, cols)


def test_a_finding_with_no_active_event_is_written(monkeypatch, contract):
    from control.detect import persist
    conn = FakeConn(active_ids=[])
    _wire(monkeypatch, conn)
    written, suppressed = persist(conn, "run1", _one_finding(contract), [contract])
    assert (written, suppressed) == (1, 0)
    assert conn.inserted[0]["change_type"] == "COLUMN_ADDED"


def test_a_finding_already_escalated_is_not_raised_again(monkeypatch, contract):
    """Regression: an issue is already open for this, do not open another."""
    from control.detect import persist
    findings = _one_finding(contract)
    conn = FakeConn(active_ids=[findings[0].fingerprint()])
    _wire(monkeypatch, conn)
    written, suppressed = persist(conn, "run2", findings, [contract])
    assert (written, suppressed) == (0, 1)
    assert conn.inserted == []


def test_active_statuses_cover_every_live_workflow_state():
    from control.detect import ACTIVE_STATUSES
    assert set(ACTIVE_STATUSES) == {"OPEN", "PROPOSED", "ESCALATED"}
    assert "DISMISSED" not in ACTIVE_STATUSES and "MERGED" not in ACTIVE_STATUSES


def test_a_suppressed_finding_still_gets_its_impact_refreshed(monkeypatch, contract):
    """An event open for a week must report what it breaks today."""
    from control.detect import persist
    from control.lineage import Impact

    findings = _one_finding(contract)
    findings[0].impact = Impact("RAW.AP_INVOICE", "NEW_COL", marts=["fct_ap_open_items"])
    conn = FakeConn(active_ids=[findings[0].fingerprint()])
    _wire(monkeypatch, conn)

    written, suppressed = persist(conn, "run3", findings, [contract])
    assert (written, suppressed) == (0, 1)
    assert len(conn.inserted) == 1, "expected exactly one statement: the impact refresh"
    stmt = conn.inserted[0]
    # the refresh targets the live row's own id, never the fingerprint
    assert stmt["event_id"] == f"live-{findings[0].fingerprint()[:8]}"
    assert "fct_ap_open_items" in stmt["impact"]



def test_re_raising_a_dismissed_divergence_gets_a_new_event_id(monkeypatch, contract):
    """Regression: EVENT_ID used to be the fingerprint, so a divergence dismissed
    and later re-raised produced two rows with one id, and every status update
    moved both. Identity is per raise; the fingerprint is what repeats."""
    from control.detect import event_id, persist
    findings = _one_finding(contract)
    fp = findings[0].fingerprint()
    conn = FakeConn(active_ids=[])          # the old one is DISMISSED, so not active
    _wire(monkeypatch, conn)
    persist(conn, "run-a", findings, [contract])
    persist(conn, "run-b", findings, [contract])
    ids = [r["event_id"] for r in conn.inserted]
    assert ids[0] != ids[1]
    assert all(r["fingerprint"] == fp for r in conn.inserted)
    assert ids[0] == event_id(fp, "run-a")


def test_refreshing_impact_targets_the_live_row_not_the_fingerprint(monkeypatch, contract):
    from control.detect import persist
    from control.lineage import Impact
    findings = _one_finding(contract)
    findings[0].impact = Impact("RAW.AP_INVOICE", "NEW_COL", marts=["m"])
    fp = findings[0].fingerprint()
    conn = FakeConn(active_ids=[fp])
    _wire(monkeypatch, conn)
    persist(conn, "run-c", findings, [contract])
    assert conn.inserted[0]["event_id"] == f"live-{fp[:8]}"
