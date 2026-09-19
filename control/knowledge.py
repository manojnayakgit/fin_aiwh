"""Knowledge: definitions the agent should cite instead of infer.

Every column description the agent has written so far says "inferred from
column name, needs review". A finance function has the real answers: the ERP
data dictionary, the chart of accounts, close procedures, accounting policies.
This module retrieves them from RAGFlow so the model can cite a document
instead of guessing.

Same two rules as memory.py. Retrieval only feeds the draft; verify() never
sees it. Optional; with no RAGFLOW_URL nothing changes.

Configuration (.env):
  RAGFLOW_URL          http://localhost:9380
  RAGFLOW_API_KEY      from the RAGFlow UI
  RAGFLOW_DATASET_IDS  comma separated dataset ids to search
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def enabled() -> bool:
    return bool(os.getenv("RAGFLOW_URL") and os.getenv("RAGFLOW_API_KEY") and os.getenv("RAGFLOW_DATASET_IDS"))


@dataclass(frozen=True)
class Definition:
    term: str
    text: str
    source: str        # document name, what the description will cite

    def line(self) -> str:
        return f"- {self.term}: {self.text.strip()}  (source: {self.source})"


def _client():
    from ragflow_sdk import RAGFlow
    return RAGFlow(api_key=os.environ["RAGFLOW_API_KEY"], base_url=os.environ["RAGFLOW_URL"])


def _dataset_ids() -> list[str]:
    return [x.strip() for x in os.environ["RAGFLOW_DATASET_IDS"].split(",") if x.strip()]


def definitions(table: str, columns: list[str], per_term: int = 1,
                threshold: float = 0.3) -> tuple[list[Definition], str | None]:
    """Best chunk per column name. Empty list when nothing clears the threshold."""
    if not enabled():
        return [], None
    try:
        rag = _client()
        ids = _dataset_ids()
        out: list[Definition] = []
        for col in columns:
            chunks = rag.retrieve(dataset_ids=ids, question=f"{table} {col} definition meaning",
                                  page_size=per_term, similarity_threshold=threshold)
            for c in chunks[:per_term]:
                text = " ".join(str(c.content).split())
                out.append(Definition(term=col, text=text[:400], source=c.document_name))
        return out, None
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e).splitlines()[0]}"


def upload(paths: list[str], dataset_name: str = "fin_aiwh_reference") -> tuple[str, int]:
    """Create or reuse a dataset, upload the files, start parsing. Returns (dataset_id, n)."""
    rag = _client()
    existing = [d for d in rag.list_datasets(name=dataset_name)] if hasattr(rag, "list_datasets") else []
    ds = existing[0] if existing else rag.create_dataset(name=dataset_name)
    docs = [{"display_name": os.path.basename(p), "blob": open(p, "rb").read()} for p in paths]
    uploaded = ds.upload_documents(docs)
    ds.async_parse_documents([d.id for d in uploaded])
    return ds.id, len(uploaded)
