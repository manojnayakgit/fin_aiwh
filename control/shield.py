"""Compatibility shields for breaking drift.

A breaking change upstream leaves every downstream report wrong until someone
fixes the source. A shield is a change to the staging model that restores the
contracted shape over the broken source, so the marts keep building and the
aging pack stays correct, while the issue stays open and the drift stays
visible.

Everything here is deterministic. The transformations are mechanical, and a
wrong one on a finance mart costs more than a model's fluency is worth.

What a shield can and cannot do:

| Change               | Shield                                              | Honest about |
|----------------------|-----------------------------------------------------|--------------|
| COLUMN_REMOVED       | NULL cast to the contracted type, same alias        | the values are gone |
| TYPE_CHANGED         | CAST back to the contracted type, same expression   | rounding, if any |
| NULLABILITY_RELAXED  | pass through, plus a test that fails on any null    | nothing is hidden |
| DATASET_MISSING      | nothing. There is no source to shield               | |

A shield never edits a contract, never touches RAW, and never auto merges.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .contracts import Contract, ContractColumn

MARK = "-- shield:"
STAGING = ROOT / "dbt" / "models" / "staging"
TESTS = ROOT / "dbt" / "tests"

SHIELDABLE = {"COLUMN_REMOVED", "TYPE_CHANGED", "NULLABILITY_RELAXED"}


@dataclass
class Patch:
    column: str
    change_type: str
    expression: str | None          # new select expression, None for pass-through
    test_sql: str | None            # a singular test to add, if any
    note: str                       # the comment left in the model
    honest: str                     # what the shield does not fix


@dataclass
class Shield:
    dataset_key: str
    table: str
    patches: list[Patch] = field(default_factory=list)
    unshieldable: list[str] = field(default_factory=list)   # "COLUMN.CHANGE: why"
    staging_before: str = ""
    staging_after: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.patches) and not self.errors

    @property
    def staging_path(self) -> Path:
        return STAGING / f"stg_{self.table.lower()}.sql"

    @property
    def branch(self) -> str:
        return f"shield/{self.table.lower()}"

    def test_files(self) -> dict[Path, str]:
        out = {}
        for p in self.patches:
            if p.test_sql:
                out[TESTS / f"shield_{self.table.lower()}_{p.column.lower()}_not_null.sql"] = p.test_sql
        return out


# --------------------------------------------------------------------------
# types
# --------------------------------------------------------------------------

def ddl_type(c: ContractColumn) -> str:
    """Contract type back to something CAST accepts."""
    if c.type == "TEXT":
        return f"varchar({c.length})" if c.length else "varchar"
    if c.type == "NUMBER":
        return f"number({c.precision},{c.scale})" if c.precision is not None else "number"
    return c.type.lower()


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    key = events[0]["DATASET_KEY"]
    table = key.split(".")[-1]
    sh = Shield(dataset_key=key, table=table)
    ref = f"see {issue_url}" if issue_url else "see the open drift issue"

    if contract is None:
        sh.errors.append("no contract for this dataset, nothing to restore to")
        return sh

    for e in events:
        ct, col = e["CHANGE_TYPE"], e.get("OBJECT_NAME")
        if ct not in SHIELDABLE or not col:
            sh.unshieldable.append(f"{col or '*'}.{ct}: cannot be shielded in staging")
            continue
        cc = contract.column(col)
        if cc is None:
            sh.unshieldable.append(f"{col}.{ct}: column not in the contract")
            continue
        t = ddl_type(cc)
        lower = col.lower()

        if ct == "COLUMN_REMOVED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"null::{t}",
                test_sql=None,
                note=f"{MARK} {col} dropped upstream, restored as NULL, {ref}",
                honest=f"{col} carries no values until upstream restores it",
            ))
        elif ct == "TYPE_CHANGED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"cast({{expr}} as {t})",
                test_sql=None,
                note=f"{MARK} {col} type changed upstream, cast back to {t}, {ref}",
                honest=f"{col} is cast to {t}; a wider or more precise upstream value is rounded or truncated",
            ))
        elif ct == "NULLABILITY_RELAXED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=None,
                test_sql=(
                    f"-- shield test: {col} is contracted NOT NULL but upstream relaxed it, {ref}\n"
                    f"-- Fails the build the moment a null arrives, so it cannot flow into a report unseen.\n"
                    f"select {lower}\nfrom {{{{ ref('stg_{table.lower()}') }}}}\nwhere {lower} is null\n"
                ),
                note=f"{MARK} {col} may now be null upstream, guarded by a test, {ref}",
                honest=f"nulls in {col} are not filled in; the build fails so a human sees them",
            ))
    return sh


# --------------------------------------------------------------------------
# applying to the staging model
# --------------------------------------------------------------------------

_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<expr>.+?)(?:\s+as\s+(?P<alias>\w+))?(?P<tail>\s*,?\s*(?:--.*)?)$",
    re.IGNORECASE,
)


def _column_of(line: str) -> tuple[str | None, str | None]:
    """(column name this select line produces, the expression), or (None, None)."""
    s = line.strip()
    if not s or s.lower().startswith(("select", "from", "where", "with", "union", "--", "{{", "}}")):
        return None, None
    m = _LINE.match(line)
    if not m:
        return None, None
    expr, alias = m.group("expr").strip(), m.group("alias")
    name = alias or (expr if re.fullmatch(r"\w+", expr) else None)
    return (name.upper() if name else None), expr


def apply(shield: Shield, sql: str) -> Shield:
    """Rewrite the select lines the patches name. Refuse anything it cannot find."""
    shield.staging_before = sql
    lines = sql.splitlines()
    todo = {p.column.upper(): p for p in shield.patches if p.expression is not None}
    done = set()

    for i, line in enumerate(lines):
        name, expr = _column_of(line)
        if not name or name not in todo:
            continue
        p = todo[name]
        m = _LINE.match(line)
        indent, tail = m.group("indent"), m.group("tail") or ""
        comma = "," if "," in tail else ""
        new_expr = p.expression.replace("{expr}", expr)
        lines[i] = f"{indent}{new_expr} as {p.column.lower()}{comma}  {p.note}"
        done.add(name)

    missing = set(todo) - done
    for name in sorted(missing):
        shield.errors.append(f"{name}: select line not found in the staging model, refusing to guess")

    # pass-through patches only need the note somewhere visible
    header = [f"{p.note}" for p in shield.patches if p.expression is None]
    if header and not shield.errors:
        lines = header + [""] + lines if not lines[0].startswith(MARK) else header + lines

    shield.staging_after = "\n".join(lines) + ("\n" if sql.endswith("\n") else "")
    if "source('raw'" not in shield.staging_after:
        shield.errors.append("staging model no longer reads from source('raw', ...)")
    return shield


def build(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    sh = plan(events, contract, issue_url)
    if not sh.patches:
        return sh
    if not sh.staging_path.exists():
        sh.errors.append(f"no staging model at {sh.staging_path.name}")
        return sh
    return apply(sh, sh.staging_path.read_text())


# --------------------------------------------------------------------------
# stale shields: the upstream got fixed, the shield is still there
# --------------------------------------------------------------------------

def installed() -> list[tuple[str, str, str]]:
    """(table, column, note) for every shield marker in every staging model."""
    out = []
    for p in sorted(STAGING.glob("stg_*.sql")):
        table = p.stem.removeprefix("stg_").upper()
        for line in p.read_text().splitlines():
            if MARK in line:
                note = line[line.index(MARK) + len(MARK):].strip()
                col = note.split(" ", 1)[0].upper()
                out.append((table, col, note))
    return out


def stale(active_columns: set[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Shields whose column no longer diverges. Safe to remove."""
    return [(t, c, n) for t, c, n in installed() if (t, c) not in active_columns]


# --------------------------------------------------------------------------
# pull request text
# --------------------------------------------------------------------------

def pr_title(sh: Shield) -> str:
    return f"Shield {sh.table}: keep reports correct over breaking upstream drift"


def pr_body(sh: Shield, issue_url: str | None, impact_md: str | None) -> str:
    lines = [
        f"Upstream broke `{sh.dataset_key}`. This restores the contracted shape in "
        f"`stg_{sh.table.lower()}` so the marts keep building and the reports stay "
        f"correct while the source is fixed.",
        "",
        "**This does not fix the data.** The contract is unchanged, the drift is still "
        "open, and the issue stays open until upstream is repaired. Remove this shield "
        "when it is.",
        "",
        "| column | change | shield | what it does not fix |",
        "|---|---|---|---|",
    ]
    for p in sh.patches:
        how = p.expression.replace("{expr}", "…") if p.expression else "pass through + not_null test"
        lines.append(f"| `{p.column}` | {p.change_type} | `{how}` | {p.honest} |")
    if sh.unshieldable:
        lines += ["", "**Not shielded**", *[f"- {u}" for u in sh.unshieldable]]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    if issue_url:
        lines += ["", f"Tracking issue: {issue_url}"]
    return "\n".join(lines)
