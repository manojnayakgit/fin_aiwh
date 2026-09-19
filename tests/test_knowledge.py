"""Memory and knowledge feed the draft. They never decide, and they fail soft."""
from datetime import datetime, timezone

import pytest

from control import agent, knowledge, memory

ROW = {
    "EVENT_ID": "abc123", "DATASET_KEY": "RAW.AP_PAYMENT", "CHANGE_TYPE": "COLUMN_REMOVED",
    "SEVERITY": "BREAKING", "OBJECT_NAME": "BANK_REF", "RATIONALE": "a contracted column disappeared",
    "STATUS": "ESCALATED", "RESOLUTION_REF": "https://github.com/manojnayakgit/fin_aiwh/issues/3",
    "DETECTED_AT": datetime(2026, 9, 17, 20, 0), "RESOLVED_AT": None,
}


# ------------------------------------------------------------------- memory

def test_a_fact_is_built_from_the_row_and_nothing_else():
    f = memory.fact_from_event(ROW)
    assert f.subject == "RAW.AP_PAYMENT.BANK_REF" and f.subject_kind == "Column"
    assert f.object == "issue #3"
    assert "COLUMN_REMOVED (BREAKING) detected 2026-09-17; escalated as issue #3" in f.text
    assert f.valid_at.tzinfo is not None and f.invalid_at is None


def test_the_same_row_and_status_always_yield_the_same_key():
    """Ingest is idempotent: re-running sync must not multiply facts."""
    assert memory.fact_from_event(ROW).key == memory.fact_from_event(dict(ROW)).key


def test_a_later_status_is_a_new_fact_with_the_old_one_closed():
    later = dict(ROW, STATUS="DISMISSED", RESOLVED_AT=datetime(2026, 9, 18, 12, 0))
    a, b = memory.fact_from_event(ROW), memory.fact_from_event(later)
    assert a.key != b.key
    assert b.invalid_at is not None and "dismissed: issue #3 closed" in b.text


def test_a_dataset_level_event_names_the_dataset():
    f = memory.fact_from_event(dict(ROW, OBJECT_NAME=None, CHANGE_TYPE="DATASET_UNGOVERNED",
                                   RESOLUTION_REF="https://github.com/x/y/pull/7", STATUS="PROPOSED"))
    assert f.subject == "RAW.AP_PAYMENT" and f.subject_kind == "Dataset" and f.object == "PR #7"


def test_memory_is_a_no_op_when_not_configured(monkeypatch):
    monkeypatch.delenv("GRAPHITI_URI", raising=False)
    assert memory.remember([ROW]) == (0, None)
    assert memory.history("RAW.AP_PAYMENT", "BANK_REF") == ([], None)


def test_memory_fails_soft_when_the_graph_is_unreachable(monkeypatch):
    monkeypatch.setenv("GRAPHITI_URI", "bolt://nowhere:7687")
    async def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(memory, "_ingest", boom)
    monkeypatch.setattr(memory, "_history", boom)
    n, err = memory.remember([ROW])
    assert n == 0 and "ConnectionError" in err
    lines, err = memory.history("RAW.AP_PAYMENT", "BANK_REF")
    assert lines == [] and "ConnectionError" in err


# ---------------------------------------------------------------- knowledge

def test_knowledge_is_a_no_op_when_not_configured(monkeypatch):
    for k in ("RAGFLOW_URL", "RAGFLOW_API_KEY", "RAGFLOW_DATASET_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert knowledge.definitions("AP_ACCRUAL", ["GL_ACCOUNT"]) == ([], None)


def test_definitions_cite_the_document(monkeypatch):
    monkeypatch.setenv("RAGFLOW_URL", "http://x"); monkeypatch.setenv("RAGFLOW_API_KEY", "k")
    monkeypatch.setenv("RAGFLOW_DATASET_IDS", "ds1, ds2")

    class Chunk:
        def __init__(self, content, name): self.content, self.document_name = content, name

    class Rag:
        def retrieve(self, dataset_ids, question, page_size, similarity_threshold):
            assert dataset_ids == ["ds1", "ds2"] and "GL_ACCOUNT" in question
            return [Chunk("General ledger account\n  in the chart of accounts.", "SAP_FI_data_dictionary.pdf")]

    monkeypatch.setattr(knowledge, "_client", lambda: Rag())
    defs, err = knowledge.definitions("AP_ACCRUAL", ["GL_ACCOUNT"])
    assert err is None and len(defs) == 1
    assert defs[0].line() == "- GL_ACCOUNT: General ledger account in the chart of accounts.  (source: SAP_FI_data_dictionary.pdf)"


def test_knowledge_fails_soft(monkeypatch):
    monkeypatch.setenv("RAGFLOW_URL", "http://x"); monkeypatch.setenv("RAGFLOW_API_KEY", "k")
    monkeypatch.setenv("RAGFLOW_DATASET_IDS", "ds1")
    monkeypatch.setattr(knowledge, "_client", lambda: (_ for _ in ()).throw(TimeoutError("slow")))
    defs, err = knowledge.definitions("AP_ACCRUAL", ["GL_ACCOUNT"])
    assert defs == [] and "TimeoutError" in err


# ---------------------------------------------------------------- the prompt

def test_prompt_carries_history_and_definitions_when_available(monkeypatch):
    monkeypatch.setattr(agent.memory, "history", lambda d, c=None, limit=8: (["[2026-09-17] BANK_REF dropped before"], None))
    monkeypatch.setattr(agent.knowledge, "definitions", lambda t, cols, **k: ([knowledge.Definition("BANK_REF", "Bank reference from the payment run", "SAP_FI.pdf")], None))
    lines, notes = agent.context_sections("RAW.AP_PAYMENT", "AP_PAYMENT", ["BANK_REF"], ["BANK_REF"])
    text = "\n".join(lines)
    assert "History of this dataset" in text and "BANK_REF dropped before" in text
    assert "Reference definitions" in text and "(source: SAP_FI.pdf)" in text
    assert notes == []


def test_prompt_is_unchanged_and_the_gap_is_named_when_stores_are_down(monkeypatch):
    monkeypatch.setattr(agent.memory, "history", lambda d, c=None, limit=8: ([], "ConnectionError: refused"))
    monkeypatch.setattr(agent.knowledge, "definitions", lambda t, cols, **k: ([], None))
    lines, notes = agent.context_sections("RAW.AP_PAYMENT", "AP_PAYMENT", ["BANK_REF"], ["BANK_REF"])
    assert lines == []
    assert notes == ["history unavailable: ConnectionError: refused"]


def test_prompt_is_unchanged_when_nothing_is_configured(monkeypatch):
    for k in ("GRAPHITI_URI", "RAGFLOW_URL", "RAGFLOW_API_KEY", "RAGFLOW_DATASET_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert agent.context_sections("RAW.AP_PAYMENT", "AP_PAYMENT", ["BANK_REF"], ["BANK_REF"]) == ([], [])
