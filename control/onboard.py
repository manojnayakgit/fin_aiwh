"""Onboarding a new source: contract to buildable model.

An ungoverned table used to stop at a contract. A contract alone changes
nothing: the table is still not declared as a dbt source, has no staging model,
and no tests. Onboarding finishes the job in one reviewed pull request.

Deterministic where the work is mechanical, model where it is judgement:

| Artifact                    | Who       | Why |
|-----------------------------|-----------|-----|
| sources.yml entry           | code      | one line, no judgement |
| tests from the contract     | code      | the contract already states the keys and nullability |
| staging model               | model     | which codes to upper, what to trim, what to name things |
| verification of all of it   | code      | the column set must match the contract exactly |

A mart is never wired up automatically. Where a new dataset belongs in the
reporting layer is a modelling decision with accounting consequences, and the
PR says so instead of guessing.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .contracts import Contract

STAGING = ROOT / "dbt" / "models" / "staging"
SOURCES = STAGING / "sources.yml"
SCHEMA = STAGING / "staging.yml"


@dataclass
class Onboarding:
    dataset_key: str
    table: str
    staging_sql: str = ""
    sources_after: str = ""
    schema_after: str = ""
    notes: str = ""                 # the model's note on where this might belong
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def model_name(self) -> str:
        return f"stg_{self.table.lower()}"

    @property
    def staging_path(self) -> Path:
        return STAGING / f"{self.model_name}.sql"

    @property
    def branch(self) -> str:
        return f"onboard/{self.table.lower()}"


# --------------------------------------------------------------------------
# deterministic artifacts
# --------------------------------------------------------------------------

def add_source(table: str, sources_yml: str) -> tuple[str, str | None]:
    """Append the table to the raw source list. Returns (text, error)."""
    if re.search(rf"^\s*-\s*name:\s*{re.escape(table)}\s*$", sources_yml, re.MULTILINE):
        return sources_yml, None
    lines = sources_yml.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip() == "tables:"), None)
    if idx is None:
        return sources_yml, "no `tables:` block in sources.yml"
    # indentation of the first entry under tables:
    entry_indent = None
    last = idx
    for i in range(idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if entry_indent is None:
            if not stripped.startswith("- "):
                return sources_yml, "unexpected shape under `tables:`"
            entry_indent = indent
        if indent < entry_indent:
            break
        last = i
    if entry_indent is None:
        return sources_yml, "`tables:` block is empty"
    lines.insert(last + 1, f"{' ' * entry_indent}- name: {table}")
    return "\n".join(lines) + "\n", None


def tests_for(contract: Contract) -> list[str]:
    """dbt tests the contract already justifies. Nothing invented."""
    out = []
    pk = [c.upper() for c in contract.primary_key]
    for c in contract.columns:
        tests = []
        if c.name.upper() in pk:
            tests = ["unique", "not_null"]
        elif not c.nullable:
            tests = ["not_null"]
        if tests:
            out.append(f"      - name: {c.name.lower()}\n"
                       f"        data_tests: [{', '.join(tests)}]")
    return out


def add_schema(contract: Contract, model: str, schema_yml: str) -> tuple[str, str | None]:
    if re.search(rf"^\s*-\s*name:\s*{re.escape(model)}\s*$", schema_yml, re.MULTILINE):
        return schema_yml, None
    tests = tests_for(contract)
    if not tests:
        return schema_yml, None
    desc = contract.description or f"Staging over {contract.dataset}"
    block = [f"  - name: {model}",
             # JSON strings are valid YAML double quoted scalars, so a colon or a
             # hash in the contract description cannot break the file.
             f"    description: {json.dumps(desc)}",
             "    columns:", *tests]
    text = schema_yml.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    return text, None


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _split_top_level(body: str) -> list[str]:
    """Split a select list on commas that are not inside brackets.

    `nullif(trim(x), '')` and `rate::number(18,8)` both carry commas that do not
    separate columns, so a plain split would invent columns that do not exist.
    """
    out, depth, buf = [], 0, []
    for ch in body:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def selected_columns(sql: str) -> set[str]:
    """Column names a simple `select a, b as c from ...` model produces."""
    # Comments go first: a shield comment carries prose with commas in it, and
    # splitting before stripping would mistake that prose for a column.
    clean = re.sub(r"--[^\n]*", "", sql)
    m = re.search(r"\bselect\b(.*?)\bfrom\b", clean, re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    out = set()
    for item in _split_top_level(m.group(1)):
        expr = " ".join(item.split()).strip()
        if not expr:
            continue
        alias = re.search(r"\bas\s+(\w+)\s*$", expr, re.IGNORECASE)
        name = alias.group(1) if alias else (expr if re.fullmatch(r"\w+", expr) else None)
        if name:
            out.add(name.upper())
    return out


def _source_ref(table: str) -> re.Pattern:
    return re.compile(r"source\(\s*['\"]raw['\"]\s*,\s*['\"]" + re.escape(table)
                      + r"['\"]\s*\)", re.IGNORECASE)


def canonical_source(sql: str, table: str) -> str:
    """Rewrite the source reference to the one spelling dbt will resolve.

    dbt matches a source name against sources.yml case-sensitively, and the
    entries there are upper case. A model that reads source('raw', 'ap_accrual')
    parses fine and then fails to compile. Spelling is mechanical, so it is
    corrected here rather than bounced back to the model.
    """
    return _source_ref(table).sub(f"source('raw', '{table}')", sql)


def verify(ob: Onboarding, contract: Contract) -> Onboarding:
    sql = ob.staging_sql
    if not sql.strip():
        ob.errors.append("no staging model was produced")
        return ob
    if not _source_ref(ob.table).search(sql):
        ob.errors.append(f"staging model does not read from source('raw', '{ob.table}')")

    want = {c.name.upper() for c in contract.columns}
    got = selected_columns(sql)
    missing, extra = sorted(want - got), sorted(got - want)
    if missing:
        ob.errors.append(f"staging model omits contracted columns {missing}")
    if extra:
        ob.errors.append(f"staging model produces columns the contract does not declare {extra}")
    for k in contract.primary_key:
        if k.upper() not in got:
            ob.errors.append(f"primary key column {k} is not in the staging model")
    if re.search(r"\bselect\s+\*", sql, re.IGNORECASE):
        ob.errors.append("staging model uses select *, which hides the column list from review")
    return ob


def build(contract: Contract, staging_sql: str, notes: str = "") -> Onboarding:
    table = contract.table
    ob = Onboarding(dataset_key=contract.dataset, table=table,
                    staging_sql=canonical_source(staging_sql.rstrip(), table) + "\n",
                    notes=notes)
    verify(ob, contract)

    src, err = add_source(table, SOURCES.read_text())
    if err:
        ob.errors.append(f"sources.yml: {err}")
    ob.sources_after = src

    sch, err = add_schema(contract, ob.model_name, SCHEMA.read_text())
    if err:
        ob.errors.append(f"staging.yml: {err}")
    ob.schema_after = sch
    return ob


# --------------------------------------------------------------------------
# pull request text
# --------------------------------------------------------------------------

def pr_title(ob: Onboarding) -> str:
    return f"Onboard {ob.dataset_key}: contract, source, staging model and tests"


def pr_body(ob: Onboarding, contract: Contract, impact_md: str | None) -> str:
    tests = tests_for(contract)
    lines = [
        f"`{ob.dataset_key}` landed in the warehouse with no contract. This onboards it.",
        "",
        "| Artifact | What |",
        "|---|---|",
        f"| `contracts/raw/{ob.table.lower()}.yml` | v{contract.version} contract, "
        f"{len(contract.columns)} columns, key `{', '.join(contract.primary_key)}` |",
        f"| `dbt/models/staging/sources.yml` | declares `{ob.table}` as a dbt source |",
        f"| `dbt/models/staging/{ob.model_name}.sql` | staging model over every contracted column |",
        f"| `dbt/models/staging/staging.yml` | {len(tests)} column(s) tested from the contract's own keys and nullability |",
        "",
        "**Not wired into any mart.** Where this belongs in the reporting layer is a "
        "modelling decision with accounting consequences, so it is left to a reviewer.",
    ]
    if ob.notes:
        lines += ["", f"Agent note: {ob.notes}"]
    lines += ["", "**Review checklist**",
              f"- Is `{', '.join(contract.primary_key)}` really the key?",
              "- Are the inferred column descriptions right?",
              "- Should this feed a mart, and which?"]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    return "\n".join(lines)
