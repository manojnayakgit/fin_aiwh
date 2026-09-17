"""What a drift event actually breaks.

A severity says how bad a change is in principle. Impact says what it costs
here: which models stop being correct, which tests will fail, which marts a
finance user reads. That turns "BANK_REF was dropped" into "BANK_REF was
dropped, which breaks fct_ap_open_items and the AP aging report".

Lineage comes from dbt's manifest, which dbt builds from the real SQL, so the
model graph is exact. Column level lineage is not something dbt publishes, so
it is derived here by reading each model's SQL. That part is a heuristic and
says so: every column result carries a confidence.
"""
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import ROOT

MANIFEST = ROOT / "dbt" / "target" / "manifest.json"

# How sure we are that a model really uses the column
EXACT = "references the column"
WILDCARD = "selects * from the source, so it carries the column"
INHERITED = "downstream of an affected model"


@dataclass
class Impact:
    dataset_key: str
    column: str | None
    models: list[str] = field(default_factory=list)      # staging
    marts: list[str] = field(default_factory=list)       # what people read
    tests: list[str] = field(default_factory=list)
    confidence: str | None = None
    manifest_seen: bool = True

    @property
    def empty(self) -> bool:
        return not (self.models or self.marts or self.tests)

    def summary(self) -> str:
        """One line for a table cell."""
        if not self.manifest_seen:
            return "unknown (no dbt manifest)"
        if self.empty:
            return "nothing downstream"
        bits = []
        if self.marts:
            bits.append(f"{len(self.marts)} mart" + ("s" if len(self.marts) > 1 else ""))
        if self.models:
            bits.append(f"{len(self.models)} staging")
        if self.tests:
            bits.append(f"{len(self.tests)} test" + ("s" if len(self.tests) > 1 else ""))
        return ", ".join(bits)

    def as_dict(self) -> dict:
        return asdict(self)

    def markdown(self) -> str:
        """A block for a pull request or an issue body."""
        if not self.manifest_seen:
            return ("_Downstream impact unknown: no dbt manifest was available. "
                    "Run `dbt parse` and re-run detection._")
        if self.empty:
            return "_Nothing downstream depends on this yet._"
        lines = []
        if self.marts:
            lines.append("**Marts affected** (what people read)")
            lines += [f"- `{m}`" for m in self.marts]
        if self.models:
            lines.append("")
            lines.append("**Staging models affected**")
            lines += [f"- `{m}`" for m in self.models]
        if self.tests:
            lines.append("")
            lines.append("**Tests that cover this path**")
            lines += [f"- `{t}`" for t in self.tests]
        if self.confidence:
            lines.append("")
            lines.append(f"_Column attribution: {self.confidence}._")
        return "\n".join(lines)


class Lineage:
    """The dbt graph, queried from the source side."""

    def __init__(self, manifest: dict | None):
        self.manifest = manifest or {}
        self.nodes = self.manifest.get("nodes", {})
        self.sources = self.manifest.get("sources", {})
        self.child_map = self.manifest.get("child_map", {})

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Lineage":
        p = path or MANIFEST
        if not p.exists():
            return cls(None)
        try:
            return cls(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            return cls(None)

    @property
    def available(self) -> bool:
        return bool(self.nodes)

    # -- graph ------------------------------------------------------------

    def source_id(self, dataset_key: str) -> str | None:
        """RAW.AP_PAYMENT -> source.fin_aiwh.raw.AP_PAYMENT"""
        table = dataset_key.split(".")[-1].upper()
        for uid, s in self.sources.items():
            if s.get("name", "").upper() == table:
                return uid
        return None

    def children(self, uid: str) -> list[str]:
        return self.child_map.get(uid, [])

    def descendants(self, uid: str) -> set[str]:
        """Everything reachable downstream, tests included."""
        seen, stack = set(), list(self.children(uid))
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.children(n))
        return seen

    def _sql(self, uid: str) -> str:
        n = self.nodes.get(uid, {})
        return n.get("raw_code") or n.get("compiled_code") or ""

    def _name(self, uid: str) -> str:
        return self.nodes.get(uid, {}).get("name", uid.split(".")[-1])

    def _is(self, uid: str, kind: str) -> bool:
        return self.nodes.get(uid, {}).get("resource_type") == kind

    def _schema(self, uid: str) -> str:
        return (self.nodes.get(uid, {}).get("schema") or "").upper()

    # -- impact -----------------------------------------------------------

    def _uses_column(self, uid: str, column: str) -> str | None:
        """Does this model reference the column? Returns a confidence, or None."""
        sql = self._sql(uid)
        if not sql:
            return None
        if re.search(rf"\b{re.escape(column)}\b", sql, re.IGNORECASE):
            return EXACT
        if re.search(r"select\s+\*", sql, re.IGNORECASE):
            return WILDCARD
        return None

    def impact(self, dataset_key: str, column: str | None = None) -> Impact:
        if not self.available:
            return Impact(dataset_key, column, manifest_seen=False)

        src = self.source_id(dataset_key)
        if src is None:
            return Impact(dataset_key, column)

        if column is None:
            affected = self.descendants(src)
            confidence = None
        else:
            # Direct children that actually touch the column, then everything
            # downstream of those. A model that does not reference the column is
            # not affected, and neither is anything beyond it through that path.
            affected, confidence = set(), None
            for child in self.children(src):
                if not self._is(child, "model"):
                    continue
                c = self._uses_column(child, column)
                if not c:
                    continue
                confidence = confidence or c
                affected.add(child)
                affected |= self.descendants(child)
            if affected and confidence != EXACT:
                confidence = confidence or INHERITED
            # column scoped tests on the source itself
            for child in self.children(src):
                if self._is(child, "test") and \
                        (self.nodes.get(child, {}).get("column_name") or "").upper() == column.upper():
                    affected.add(child)

        models = sorted({self._name(u) for u in affected
                         if self._is(u, "model") and self._schema(u) == "STAGING"})
        marts = sorted({self._name(u) for u in affected
                        if self._is(u, "model") and self._schema(u) == "MARTS"})
        tests = sorted({self._name(u) for u in affected if self._is(u, "test")})
        return Impact(dataset_key, column, models, marts, tests, confidence)
