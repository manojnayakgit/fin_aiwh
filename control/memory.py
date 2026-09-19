"""Memory: what happened to this dataset before.

The agent judges every drift as if it were the first. It is not. A column that
was dropped and restored once, a NOT NULL adoption that a writer rejected, a
shield that lived for a month: that history changes what a sensible proposal
looks like, and it is already recorded in META and on GitHub. This module puts
it where the agent can ask for it, in a Graphiti temporal graph on Neo4j.

Two rules from the rest of the system apply unchanged:

  Nothing is invented.  Ingest is deterministic: every fact is built from a
  DRIFT_EVENT row by code, through Graphiti's add_triplet. No model extracts
  anything. The graph holds what META holds, in a form that can be asked
  "what happened to BANK_REF before".

  Optional, and honest when absent.  Nothing here is required. With no
  GRAPHITI_URI the agent runs exactly as it did. If the graph is configured
  and unreachable, the agent drafts without history and says so.

Configuration (.env):
  GRAPHITI_URI         bolt://localhost:7687
  GRAPHITI_USER        neo4j
  GRAPHITI_PASSWORD    ...
  EMBED_BASE_URL       an OpenAI-compatible embeddings endpoint, e.g. Ollama
                       http://localhost:11434/v1
  EMBED_MODEL          nomic-embed-text
  EMBED_API_KEY        any non-empty string for Ollama
Graphiti wants three clients: an LLM (Anthropic here), an embedder (Ollama
through the OpenAI-compatible endpoint) and a cross-encoder reranker (a no-op
that keeps search order). None of them is OpenAI, so no OpenAI key.
add_triplet does not call the LLM for extraction.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

GROUP = "fin_aiwh"
NS = uuid.UUID("6f1b6b2e-7c3c-4d3a-9c1a-0000aa1a0001")   # namespace for deterministic ids


def enabled() -> bool:
    return bool(os.getenv("GRAPHITI_URI"))


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "|".join(parts)))


# --------------------------------------------------------------------------
# facts, built deterministically from event rows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    subject: str            # RAW.AP_PAYMENT.BANK_REF or RAW.AP_ACCRUAL
    subject_kind: str       # Column or Dataset
    object: str             # issue #3, PR #5, contract v2, "no reference"
    text: str               # the sentence the agent will read
    valid_at: datetime
    invalid_at: datetime | None
    key: str                # deterministic identity for idempotent ingest


def _ref_label(url: str | None) -> str:
    if not url:
        return "no reference"
    tail = url.rstrip("/").split("/")
    if len(tail) >= 2 and tail[-2] in ("pull", "issues"):
        return ("PR #" if tail[-2] == "pull" else "issue #") + tail[-1]
    return url


def fact_from_event(e: dict) -> Fact:
    """One fact per event row. The status is part of the fact, so a row that
    moves from ESCALATED to DISMISSED yields a second fact with its own key
    and the first one is closed by invalid_at."""
    dataset = e["DATASET_KEY"]
    col = e.get("OBJECT_NAME")
    subject = f"{dataset}.{col}" if col and col != "*" else dataset
    kind = "Column" if col and col != "*" else "Dataset"
    ref = _ref_label(e.get("RESOLUTION_REF"))
    status = e["STATUS"]
    detected = e["DETECTED_AT"]
    resolved = e.get("RESOLVED_AT")
    if isinstance(detected, str):
        detected = datetime.fromisoformat(detected)
    if isinstance(resolved, str):
        resolved = datetime.fromisoformat(resolved)
    detected = detected.replace(tzinfo=timezone.utc) if detected.tzinfo is None else detected
    if resolved is not None and resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)

    outcome = {
        "OPEN": "open, awaiting triage",
        "PROPOSED": f"proposed for adoption in {ref}",
        "ESCALATED": f"escalated as {ref}, no contract change",
        "MERGED": f"adopted: {ref} merged and the contract updated",
        "DISMISSED": f"dismissed: {ref} closed" if ref != "no reference" else "dismissed by hand",
    }.get(status, status)
    text = (f"{subject}: {e['CHANGE_TYPE']} ({e['SEVERITY']}) detected "
            f"{detected:%Y-%m-%d}; {outcome}. {e.get('RATIONALE') or ''}").strip()
    return Fact(subject=subject, subject_kind=kind, object=ref, text=text,
                valid_at=detected, invalid_at=resolved,
                key=_uuid(e["EVENT_ID"], status))


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

def _client():
    # Graphiti reports version, OS, provider choices to PostHog by default.
    # Nothing sensitive, by its own documentation, but a governance layer that
    # exists so control is not held by a vendor does not phone one either.
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.anthropic_client import AnthropicClient
    from graphiti_core.llm_client.config import LLMConfig

    class KeepOrder(CrossEncoderClient):
        """Graphiti's default reranker is an OpenAI client, the third place it
        wants an OpenAI key. For a graph of a few hundred facts, hybrid search
        order is good enough; this keeps it and needs no key."""
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            n = max(len(passages), 1)
            return [(p, 1.0 - i / n) for i, p in enumerate(passages)]

    llm = AnthropicClient(config=LLMConfig(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")))
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
        api_key=os.getenv("EMBED_API_KEY", "ollama"),
        base_url=os.getenv("EMBED_BASE_URL", "http://localhost:11434/v1"),
        embedding_model=os.getenv("EMBED_MODEL", "nomic-embed-text")))
    return Graphiti(os.environ["GRAPHITI_URI"], os.getenv("GRAPHITI_USER", "neo4j"),
                    os.environ["GRAPHITI_PASSWORD"], llm_client=llm, embedder=embedder,
                    cross_encoder=KeepOrder())


async def _ingest(facts: list[Fact]) -> int:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    g = _client()
    try:
        await g.build_indices_and_constraints()
        n = 0
        now = datetime.now(timezone.utc)
        for f in facts:
            src = EntityNode(uuid=_uuid("node", f.subject), name=f.subject,
                             group_id=GROUP, labels=["Entity", f.subject_kind], created_at=now)
            dst = EntityNode(uuid=_uuid("node", f.object), name=f.object,
                             group_id=GROUP, labels=["Entity", "Reference"], created_at=now)
            edge = EntityEdge(uuid=f.key, source_node_uuid=src.uuid, target_node_uuid=dst.uuid,
                              name="DRIFT_OUTCOME", fact=f.text, group_id=GROUP,
                              created_at=now, valid_at=f.valid_at, invalid_at=f.invalid_at,
                              episodes=[])
            await g.add_triplet(src, edge, dst)
            n += 1
        return n
    finally:
        await g.close()


# Retrieval is a lookup, not a similarity contest. Ingest writes deterministic
# node names, so "what happened to this column" has an exact answer. Semantic
# search was tried first and returned a neighbouring column's history while
# missing the one asked for, which is the worst possible outcome: an agent
# arguing confidently from the wrong case. Embeddings still run at ingest,
# because Graphiti's model wants them; they are simply not how this is read.
HISTORY_CYPHER = """
MATCH (s:Entity)-[e:RELATES_TO]->(:Entity)
WHERE e.group_id = $group AND (s.name = $subject OR s.name STARTS WITH $prefix)
RETURN e.fact AS fact, e.valid_at AS valid_at, e.invalid_at AS invalid_at
ORDER BY e.valid_at DESC, e.fact
LIMIT $limit
"""


def _line(fact: str, valid_at, invalid_at) -> str:
    def d(x):
        return x.to_native().strftime("%Y-%m-%d") if hasattr(x, "to_native") else (
            x.strftime("%Y-%m-%d") if x else "?")
    span = d(valid_at) + (f" to {d(invalid_at)}" if invalid_at else "")
    return f"[{span}] {fact}"


async def _history(subject: str, limit: int) -> list[str]:
    """Every fact about this dataset or column, newest first.

    Asking for a dataset includes its columns: RAW.AP_PAYMENT returns the
    dataset's own events and BANK_REF's. Asking for a column returns only it.
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        os.environ["GRAPHITI_URI"],
        auth=(os.getenv("GRAPHITI_USER", "neo4j"), os.environ["GRAPHITI_PASSWORD"]))
    try:
        async with driver.session() as s:
            res = await s.run(HISTORY_CYPHER, group=GROUP, subject=subject,
                              prefix=f"{subject}.", limit=limit)
            return [_line(r["fact"], r["valid_at"], r["invalid_at"]) async for r in res]
    finally:
        await driver.close()


# --------------------------------------------------------------------------
# what the rest of the system calls
# --------------------------------------------------------------------------

def _import_problem(e: ImportError) -> str:
    """Only blame a missing install when graphiti itself is missing. An import
    failing inside the package (a renamed module, a missing extra) is a
    different problem and its own message is the useful one."""
    if getattr(e, "name", "") == "graphiti_core" or "No module named 'graphiti_core'" in str(e):
        return "graphiti-core is not installed: pip install -r requirements-knowledge.txt"
    return f"ImportError inside graphiti-core: {e}"


def remember(events: list[dict]) -> tuple[int, str | None]:
    """Push every event row into the graph. Idempotent. Returns (count, error)."""
    if not enabled():
        return 0, None
    facts = [fact_from_event(e) for e in events]
    try:
        return asyncio.run(_ingest(facts)), None
    except ImportError as e:
        return 0, _import_problem(e)
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {str(e).splitlines()[0]}"


def history(dataset: str, column: str | None = None, limit: int = 8) -> tuple[list[str], str | None]:
    """Lines the agent can read, worst case an empty list and why."""
    if not enabled():
        return [], None
    subject = f"{dataset}.{column}" if column else dataset
    try:
        return asyncio.run(_history(subject, limit)), None
    except ImportError as e:
        return [], _import_problem(e)
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e).splitlines()[0]}"


def all_events_sql() -> str:
    return """
        SELECT EVENT_ID, DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME, RATIONALE,
               STATUS, RESOLUTION_REF, DETECTED_AT, RESOLVED_AT
        FROM FIN_AIWH.META.DRIFT_EVENT
        ORDER BY DETECTED_AT
    """
