"""The drift agent.

Reads open drift events and turns them into pull requests a reviewer can
merge, or into escalations a reviewer must handle. The model drafts; the code
decides. Every proposal is verified deterministically against the live schema
before anything touches git, so a wrong draft is discarded, not merged.

Routing, by the worst event in a dataset:
  LOW       draft a contract bump, open a PR, enable auto merge
  MEDIUM    draft a contract bump, open a PR, wait for a human
  BREAKING  no PR. Open an issue with the evidence and mark the events ESCALATED.
"""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT
from .contracts import CONTRACT_DIR, Contract, load_contracts, parse_contract
from .detect import BREAKING, LOW, MEDIUM, ObservedColumn, diff_dataset
from .lineage import Lineage
from .snow import execute, query

SEV_ORDER = {LOW: 0, MEDIUM: 1, BREAKING: 2}
CONTRACT_TOOL = "propose_contract_change"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

@dataclass
class Bundle:
    """Every open event for one dataset, plus what the agent needs to reason."""
    dataset_key: str
    events: list[dict]
    contract: Contract | None
    observed: dict[str, ObservedColumn]
    lineage: Lineage | None = None

    def impact_for(self, event: dict):
        """What this one event breaks downstream."""
        if self.lineage is None:
            return None
        col = event.get("OBJECT_NAME") if event.get("CHANGE_TYPE") not in (
            "DATASET_MISSING", "DATASET_UNGOVERNED") else None
        return self.lineage.impact(self.dataset_key, col)

    def dataset_impact(self):
        """Everything downstream of the dataset, regardless of column."""
        return self.lineage.impact(self.dataset_key) if self.lineage else None

    @property
    def worst(self) -> str:
        return max((e["SEVERITY"] for e in self.events), key=SEV_ORDER.get)

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[1]

    @property
    def event_ids(self) -> list[str]:
        return [e["EVENT_ID"] for e in self.events]


@dataclass
class Proposal:
    bundle: Bundle
    contract_yaml: str
    pr_title: str
    pr_body: str
    reasoning: str
    staging_sql: str | None = None
    contract: Contract | None = None          # parsed, set by verify()
    errors: list[str] = field(default_factory=list)
    merge_note: str = "awaiting review"

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def contract_path(self) -> Path:
        return CONTRACT_DIR / "raw" / f"{self.bundle.table.lower()}.yml"

    @property
    def staging_path(self) -> Path:
        return ROOT / "dbt" / "models" / "staging" / f"stg_{self.bundle.table.lower()}.sql"

    @property
    def branch(self) -> str:
        v = self.contract.version if self.contract else "x"
        return f"drift/{self.bundle.table.lower()}-v{v}"


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def open_events(conn) -> list[dict]:
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, DETECTED_AT
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS = 'OPEN'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def bundle_events(
    events: list[dict],
    contracts: list[Contract],
    observed: dict[str, dict[str, ObservedColumn]],
    lineage: Lineage | None = None,
) -> list[Bundle]:
    by_key = {c.dataset: c for c in contracts}
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(e["DATASET_KEY"], []).append(e)
    return [
        Bundle(
            dataset_key=k,
            events=v,
            contract=by_key.get(k),
            observed=observed.get(k, {}),
            lineage=lineage,
        )
        for k, v in sorted(groups.items())
    ]


# --------------------------------------------------------------------------
# drafting (the only place the model is involved)
# --------------------------------------------------------------------------

TOOL_SCHEMA = {
    "name": CONTRACT_TOOL,
    "description": "Return the updated contract and, only if needed, an updated staging model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contract_yaml": {
                "type": "string",
                "description": "Complete contract file content. Same format as the current one.",
            },
            "staging_sql": {
                "type": ["string", "null"],
                "description": "Complete updated dbt staging model, or null if no change is needed.",
            },
            "pr_title": {"type": "string", "description": "Under 70 characters, imperative."},
            "pr_body": {
                "type": "string",
                "description": "Markdown. What changed upstream, what this PR adopts, what a "
                               "reviewer should check. Plain language for a finance reviewer.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences on why these choices, for the audit log.",
            },
        },
        "required": ["contract_yaml", "staging_sql", "pr_title", "pr_body", "reasoning"],
    },
}

SYSTEM = """You maintain data contracts for a finance data warehouse. A contract is the
agreement of record for the shape of a source dataset. Upstream changed a dataset
without telling anyone; the detector has classified the divergence. Your job is to
draft the contract change that adopts what is safe to adopt.

Rules you must follow exactly:
- Bump `version` by exactly one. For a dataset with no contract, version is 1.
- The new contract must describe the live schema exactly: every live column present,
  with the live type, length or precision/scale, and nullability. Nothing else.
- Never remove a column that exists in the live schema.
- Keep every existing description. Write a plausible finance description for a new
  column from its name and type. Say so in the description if you are inferring.
- Keep owner, classification, primary_key and freshness unchanged unless the change
  makes them wrong. For a new dataset use owner `unassigned-needs-review`.
- Only return staging_sql when a new column should flow through to staging. Keep the
  model's existing structure and add the column in the same style.
- Do not invent business rules. If you are unsure, adopt the column plainly and say
  so in pr_body so a reviewer can decide.
"""


def _observed_json(observed: dict[str, ObservedColumn]) -> str:
    rows = [
        {
            "name": o.name, "type": o.type, "nullable": o.nullable,
            "length": o.length, "precision": o.precision, "scale": o.scale,
            "ordinal": o.ordinal,
        }
        for o in sorted(observed.values(), key=lambda x: x.ordinal)
    ]
    return json.dumps(rows, indent=2)


def _events_json(events: list[dict]) -> str:
    keep = ("CHANGE_TYPE", "SEVERITY", "OBJECT_NAME", "BEFORE_STATE", "AFTER_STATE", "RATIONALE")
    return json.dumps([{k: e.get(k) for k in keep} for e in events], indent=2, default=str)


def build_prompt(bundle: Bundle, example_contract: str) -> str:
    current = (
        bundle.contract.source_path.read_text()
        if bundle.contract and bundle.contract.source_path
        else None
    )
    staging = None
    p = ROOT / "dbt" / "models" / "staging" / f"stg_{bundle.table.lower()}.sql"
    if p.exists():
        staging = p.read_text()

    parts = [f"Dataset: {bundle.dataset_key}", "", "Drift events:", _events_json(bundle.events), ""]

    impacts = [(e, bundle.impact_for(e)) for e in bundle.events]
    lines = [f"- {e['OBJECT_NAME'] or bundle.dataset_key}: {i.summary()}"
             + (f" ({', '.join(i.marts)})" if i and i.marts else "")
             for e, i in impacts if i and not i.empty]
    if lines:
        parts += ["Downstream impact of these changes, from dbt lineage:", *lines, ""]
    parts += ["Live schema from INFORMATION_SCHEMA:", _observed_json(bundle.observed), ""]
    if current:
        parts += ["Current contract:", "```yaml", current, "```", ""]
    else:
        parts += [
            "There is no contract for this dataset. Draft version 1 in the same "
            "format as this example:", "```yaml", example_contract, "```", "",
        ]
    if staging:
        parts += ["Current staging model:", "```sql", staging, "```", ""]
    parts.append(f"Call {CONTRACT_TOOL} with your proposal.")
    return "\n".join(parts)


def draft(bundle: Bundle, client=None, model: str | None = None) -> Proposal:
    """Ask the model for a proposal. Returns an unverified Proposal."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (CONTRACT_DIR / "raw" / "ap_invoice.yml").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": CONTRACT_TOOL},
        messages=[{"role": "user", "content": build_prompt(bundle, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    d = block.input
    return Proposal(
        bundle=bundle,
        contract_yaml=d["contract_yaml"],
        staging_sql=d.get("staging_sql") or None,
        pr_title=d["pr_title"],
        pr_body=d["pr_body"],
        reasoning=d["reasoning"],
    )


# --------------------------------------------------------------------------
# verification (deterministic, no model)
# --------------------------------------------------------------------------

def verify(proposal: Proposal) -> Proposal:
    """Reject anything the model got wrong. Populates proposal.errors."""
    b = proposal.bundle
    errs: list[str] = []
    tmp = ROOT / ".agent_tmp.yml"
    try:
        tmp.write_text(proposal.contract_yaml)
        new = parse_contract(tmp)
    except Exception as e:  # noqa: BLE001
        proposal.errors = [f"contract does not parse: {e}"]
        return proposal
    finally:
        if tmp.exists():
            tmp.unlink()

    if new.dataset != b.dataset_key:
        errs.append(f"dataset is {new.dataset}, expected {b.dataset_key}")

    expected_version = (b.contract.version + 1) if b.contract else 1
    if new.version != expected_version:
        errs.append(f"version is {new.version}, expected {expected_version}")

    if b.contract:
        old_names = {c.name for c in b.contract.columns}
        dropped = old_names - {c.name for c in new.columns}
        if dropped:
            errs.append(f"proposal drops contracted columns {sorted(dropped)}")
        if new.primary_key != b.contract.primary_key:
            errs.append("primary key changed")

    residual = diff_dataset(new, b.observed)
    for f in residual:
        errs.append(f"still diverges from live: {f.change_type} {f.object_name or ''}".strip())

    if proposal.staging_sql is not None and "source('raw'" not in proposal.staging_sql:
        errs.append("staging model no longer reads from source('raw', ...)")

    proposal.contract = new
    proposal.errors = errs
    return proposal


# --------------------------------------------------------------------------
# publishing (git + gh, runs on the machine that owns the repo)
# --------------------------------------------------------------------------

def _run(cmd: list[str], **kw) -> str:
    """Run a command, and on failure say what it actually said.

    CalledProcessError prints the command and the exit code but not the output,
    so a failing `gh pr create` used to produce a traceback with no reason in it.
    """
    try:
        return subprocess.check_output(cmd, cwd=ROOT, text=True,
                                       stderr=subprocess.STDOUT, **kw).strip()
    except subprocess.CalledProcessError as e:
        out = (e.output or "").strip()
        raise SystemExit(f"{' '.join(cmd[:3])} failed (exit {e.returncode}):\n{out}") from None


def _start_branch(branch: str, base: str):
    """Branch from the remote base, discarding any leftover from a failed run.

    Always from origin, never from local HEAD: otherwise an unpushed local commit
    is swept into the PR. A branch left behind by a run that died after the push
    is deleted first, so a retry is not blocked by its own wreckage.
    """
    _run(["git", "fetch", "-q", "origin", base])
    subprocess.run(["git", "branch", "-q", "-D", branch], cwd=ROOT,
                   capture_output=True, text=True)
    _run(["git", "checkout", "-q", "-b", branch, f"origin/{base}"])


def _push(branch: str):
    # --force-with-lease only ever overwrites a branch from an earlier failed run
    # of this same agent: if a PR existed, the caller returned before reaching here.
    _run(["git", "push", "-q", "--force-with-lease", "-u", "origin", branch])


LABELS = {
    "drift": ("0E8A16", "raised by the drift agent"),
    "low": ("C5DEF5", "additive, safe to auto merge"),
    "medium": ("FBCA04", "needs a decision"),
    "breaking": ("B60205", "release gate fails until resolved"),
}


def _ensure_labels():
    for name, (colour, desc) in LABELS.items():
        subprocess.run(
            ["gh", "label", "create", name, "--color", colour, "--description", desc, "--force"],
            cwd=ROOT, capture_output=True, text=True,
        )


def _ensure_clean_tree():
    if _run(["git", "status", "--porcelain"]):
        raise SystemExit("working tree is not clean; commit or stash before running the agent")


def _ensure_pushed(base: str):
    """Refuse to open a PR against a base that is missing your local commits.

    The agent branches from origin/<base> so unpushed local work is never swept
    into a PR. The other half of that rule: if local <base> is ahead of the
    remote, every file you have changed locally but not pushed shows up in the
    PR diff as a deletion or a revert. The PR is not wrong, it is just built on
    a base nobody else can see yet.
    """
    _run(["git", "fetch", "-q", "origin", base])
    ahead = _run(["git", "rev-list", "--count", f"origin/{base}..{base}"])
    if ahead != "0":
        raise SystemExit(
            f"local '{base}' is {ahead} commit(s) ahead of origin/{base}. The agent "
            f"branches from origin, so the PR would read as reverting them.\n"
            f"Run: git push"
        )


def required_checks(base: str) -> list[str]:
    """Contexts GitHub will actually wait for before an auto merge completes.

    Empty means auto merge is not a gate: GitHub merges as soon as the PR is
    mergeable, whether or not the workflow ever ran.
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{base}/protection"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("required_status_checks", {}).get("contexts", []) or []


def existing_pr(branch: str) -> str | None:
    """A previous run may have opened the PR and failed afterwards. Reuse it."""
    out = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
         "--jq", ".[0].url"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    return out or None


def publish(proposal: Proposal, auto_merge: bool) -> str:
    """Write files, branch, commit, push, open PR. Returns the PR url."""
    assert proposal.ok and proposal.contract
    _ensure_clean_tree()
    _ensure_labels()
    base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    branch = proposal.branch
    existing = existing_pr(branch)
    if existing:
        return existing

    # Branch from the remote base, never from local HEAD. Otherwise any local
    # commit not yet pushed is swept into the PR, and a contract change arrives
    # carrying unrelated work.
    try:
        _start_branch(branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        files = [str(proposal.contract_path.relative_to(ROOT))]
        if proposal.staging_sql is not None:
            proposal.staging_path.write_text(proposal.staging_sql)
            files.append(str(proposal.staging_path.relative_to(ROOT)))
        _run(["git", "add", *files])
        impact = proposal.bundle.dataset_impact()
        impact_block = (
            f"\n## Downstream impact\n\n{impact.markdown()}\n"
            if impact and not impact.empty else ""
        )
        body = (
            f"{proposal.pr_body}\n{impact_block}\n---\n"
            f"Agent reasoning: {proposal.reasoning}\n\n"
            f"Drift events: {', '.join(proposal.bundle.event_ids)}\n"
        )
        _run(["git", "commit", "-q", "-m", proposal.pr_title, "-m", body])
        _push(branch)
        labels = ["drift", proposal.bundle.worst.lower()]
        url = _run([
            "gh", "pr", "create", "--title", proposal.pr_title, "--body", body,
            "--base", base, "--head", branch, *sum((["--label", l] for l in labels), []),
        ]).splitlines()[-1]
        if auto_merge and proposal.bundle.worst == LOW:
            if required_checks(base):
                _run(["gh", "pr", "merge", url, "--auto", "--squash", "--delete-branch"])
                proposal.merge_note = "auto merge on"
            else:
                proposal.merge_note = (
                    "auto merge NOT enabled: branch protection on "
                    f"'{base}' requires no status checks, so GitHub would merge "
                    "without waiting for the gate"
                )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


def escalate(bundle: Bundle) -> str:
    """BREAKING gets an issue, never a PR. Returns the issue url."""
    _ensure_labels()
    imps = [bundle.impact_for(e) for e in bundle.events]
    reports = sorted({r["label"] for i in imps if i for r in i.reports})
    marts = sorted({m for i in imps if i for m in i.marts})
    hit = [f"**{r}**" for r in reports] or [f"`{m}`" for m in marts]
    headline = (
        f"Breaking drift on `{bundle.dataset_key}`."
        + (f" This affects {', '.join(hit)}." if hit else "")
        + " The release gate will fail until this is resolved."
    )
    lines = [
        headline, "",
        "| severity | change | object | breaks | why |",
        "|---|---|---|---|---|",
    ]
    for e in bundle.events:
        imp = bundle.impact_for(e)
        lines.append(
            f"| {e['SEVERITY']} | {e['CHANGE_TYPE']} | {e['OBJECT_NAME'] or '-'} "
            f"| {imp.summary() if imp else '-'} | {e['RATIONALE']} |"
        )
    detail = bundle.dataset_impact()
    if detail and not detail.empty:
        lines += ["", "<details><summary>Full downstream impact</summary>", "",
                  detail.markdown(), "", "</details>"]
    lines += [
        "",
        "Options: revert the upstream change, or agree a new contract version through a reviewed PR.",
        "",
        f"Drift events: {', '.join(bundle.event_ids)}",
    ]
    return _run([
        "gh", "issue", "create",
        "--title", f"Breaking drift: {bundle.dataset_key}",
        "--body", "\n".join(lines),
        "--label", "drift", "--label", "breaking",
    ]).splitlines()[-1]


def mark(conn, event_ids: list[str], status: str, ref: str):
    params = {"st": status, "ref": ref}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    execute(
        conn,
        "UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, RESOLUTION_REF = %(ref)s "
        "WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )


# --------------------------------------------------------------------------
# reconciliation: GitHub is the source of truth for what happened to the work
# --------------------------------------------------------------------------

def _gh_json(args: list[str]) -> dict | None:
    r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def github_outcome(url: str) -> tuple[str, str] | None:
    """What became of the pull request or issue behind an event.

    Returns (new_status, human reason), or None when nothing has changed yet or
    the reference cannot be read.
    """
    if "/pull/" in url:
        d = _gh_json(["pr", "view", url, "--json", "state,mergedAt"])
        if not d:
            return None
        if d.get("mergedAt"):
            return "MERGED", "pull request merged"
        if d.get("state") == "CLOSED":
            return "OPEN", "pull request closed without merging, so the drift is unresolved"
        return None
    if "/issues/" in url:
        d = _gh_json(["issue", "view", url, "--json", "state"])
        if not d:
            return None
        if d.get("state") == "CLOSED":
            return "DISMISSED", "issue closed"
        return None
    return None


def pending_events(conn) -> list[dict]:
    return query(
        conn,
        "SELECT EVENT_ID, DATASET_KEY, OBJECT_NAME, SEVERITY, STATUS, RESOLUTION_REF "
        "FROM FIN_AIWH.META.DRIFT_EVENT "
        "WHERE STATUS IN ('PROPOSED', 'ESCALATED') AND RESOLUTION_REF IS NOT NULL "
        "ORDER BY DATASET_KEY, OBJECT_NAME",
    )


def set_status(conn, event_ids: list[str], status: str):
    params = {"st": status}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    resolved = "RESOLVED_AT = SYSDATE(), " if status in ("MERGED", "DISMISSED") else ""
    execute(
        conn,
        f"UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, {resolved}"
        "RESOLUTION_REF = RESOLUTION_REF WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )


# --------------------------------------------------------------------------
# shields: a PR that keeps the reports correct while upstream is fixed
# --------------------------------------------------------------------------

def breaking_events(conn) -> list[dict]:
    """Escalated events too, so a shield can follow an issue opened last run."""
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS, RESOLUTION_REF
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS IN ('OPEN', 'ESCALATED') AND SEVERITY = 'BREAKING'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def publish_shield(sh, issue_url: str | None, impact_md: str | None, base: str | None = None) -> str:
    """Branch from origin, write the staging model and any tests, open a PR. Never auto merges."""
    from .shield import pr_body, pr_title
    assert sh.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "shield", "--color", "5319E7",
                    "--description", "restores contracted shape over breaking drift", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(sh.branch)
    if existing:
        return existing
    try:
        _start_branch(sh.branch, base)
        sh.staging_path.write_text(sh.staging_after)
        files = [str(sh.staging_path.relative_to(ROOT))]
        for path, sql in sh.test_files().items():
            path.write_text(sql)
            files.append(str(path.relative_to(ROOT)))
        _run(["git", "add", *files])
        body = pr_body(sh, issue_url, impact_md)
        _run(["git", "commit", "-q", "-m", pr_title(sh), "-m", body])
        _push(sh.branch)
        url = _run([
            "gh", "pr", "create", "--title", pr_title(sh), "--body", body,
            "--base", base, "--head", sh.branch,
            "--label", "drift", "--label", "breaking", "--label", "shield",
        ]).splitlines()[-1]
        if issue_url:
            subprocess.run(
                ["gh", "issue", "comment", issue_url, "--body",
                 f"Shield proposed: {url}\n\nKeeps the reports correct while this is fixed. "
                 f"Remove it when this issue closes."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


# --------------------------------------------------------------------------
# onboarding a new source
#
# A contract alone leaves the table unusable: not declared as a dbt source, no
# staging model, no tests. Onboarding produces all four artifacts in one PR.
# The contract and the staging model are drafted by the model; the source
# entry, the tests and every check on the result are deterministic code.
# --------------------------------------------------------------------------

ONBOARD_TOOL = "propose_staging_model"

ONBOARD_TOOL_SCHEMA = {
    "name": ONBOARD_TOOL,
    "description": "Return a dbt staging model over a newly contracted raw table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "staging_sql": {
                "type": "string",
                "description": "Complete dbt model file. One select over the raw source, "
                               "one output column per contracted column, same names.",
            },
            "notes": {
                "type": "string",
                "description": "Two or three sentences for the reviewer: what this table "
                               "appears to hold, which mart it might belong to and why you "
                               "did not wire it, anything you had to guess.",
            },
        },
        "required": ["staging_sql", "notes"],
    },
}

ONBOARD_SYSTEM = """You write dbt staging models for a finance data warehouse. A new raw
table has just been put under contract. Write the staging model that makes it usable.

The staging layer normalises, it does not interpret. Rules you must follow exactly:
- Output exactly one column per column in the contract, with the contract's own name
  in lower case. No extra columns, no derived or calculated columns, none dropped.
  A reviewer adds business logic later; inventing it here hides it from review.
- Read from {{ source('raw', 'TABLE') }} and nothing else. No joins, no ref().
- Never use select *. The column list is what a reviewer reads.
- Clean only where the column's own type and meaning justify it: upper() on codes,
  statuses and currencies, trim() on free text, nullif(trim(x), '') where an empty
  string is really a missing value. Leave amounts, dates, ids and flags untouched:
  casting or rounding money in staging is a business decision.
- Do not wire the table into any mart. Say in notes where you think it belongs.
"""


def build_onboard_prompt(contract_yaml: str, observed: dict[str, ObservedColumn],
                         table: str, example_model: str) -> str:
    return "\n".join([
        f"Raw table: RAW.{table}", "",
        "Its contract, merged in this same pull request:", "```yaml", contract_yaml, "```", "",
        "Live schema from INFORMATION_SCHEMA:", _observed_json(observed), "",
        "An existing staging model in this project, for style:", "```sql", example_model, "```", "",
        f"Call {ONBOARD_TOOL} with the staging model for this table.",
    ])


def draft_staging(contract_yaml: str, observed: dict[str, ObservedColumn], table: str,
                  client=None, model: str | None = None) -> tuple[str, str]:
    """Ask the model for the staging SQL. Returns (staging_sql, notes), unverified."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (ROOT / "dbt" / "models" / "staging" / "stg_ap_vendor.sql").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=3000,
        system=ONBOARD_SYSTEM,
        tools=[ONBOARD_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": ONBOARD_TOOL},
        messages=[{"role": "user",
                   "content": build_onboard_prompt(contract_yaml, observed, table, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    return block.input["staging_sql"], block.input.get("notes", "")


def publish_onboarding(proposal: Proposal, ob, impact_md: str | None,
                       base: str | None = None) -> str:
    """Contract, source entry, staging model and tests in one PR. Never auto merges."""
    from .onboard import SCHEMA, SOURCES, pr_body, pr_title
    assert proposal.ok and proposal.contract and ob.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "onboard", "--color", "0E8A16",
                    "--description", "brings an ungoverned table under contract", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(ob.branch)
    if existing:
        return existing
    try:
        _start_branch(ob.branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        ob.staging_path.write_text(ob.staging_sql)
        SOURCES.write_text(ob.sources_after)
        SCHEMA.write_text(ob.schema_after)
        files = [str(p.relative_to(ROOT)) for p in
                 (proposal.contract_path, ob.staging_path, SOURCES, SCHEMA)]
        _run(["git", "add", *files])
        title = pr_title(ob)
        body = (f"{pr_body(ob, proposal.contract, impact_md)}\n\n---\n"
                f"Agent reasoning: {proposal.reasoning}\n\n"
                f"Drift events: {', '.join(proposal.bundle.event_ids)}\n")
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(ob.branch)
        return _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", ob.branch,
            "--label", "drift", "--label", "medium", "--label", "onboard",
        ]).splitlines()[-1]
    finally:
        _run(["git", "checkout", "-q", base])


# --------------------------------------------------------------------------
# retiring a shield
#
# A shield is temporary by definition. When the live schema matches the contract
# again, the shield is serving a substitute value where the real one is now
# available. This opens the PR that takes it out. The PR body carries
# "Closes <issue>", so merging it closes the tracking issue, and the next sync
# moves the escalated event to DISMISSED through the existing lifecycle. No new
# state was added for this.
# --------------------------------------------------------------------------

def publish_retirement(r, base: str | None = None) -> str:
    from .shield import retire_body, retire_title
    assert r.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "retire", "--color", "BFD4F2",
                    "--description", "removes a shield whose drift is repaired", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(r.branch)
    if existing:
        return existing
    try:
        _start_branch(r.branch, base)
        r.staging_path.write_text(r.staging_after)
        _run(["git", "add", str(r.staging_path.relative_to(ROOT))])
        for t in r.drop_tests:
            _run(["git", "rm", "-q", str(t.relative_to(ROOT))])
        title, body = retire_title(r), retire_body(r)
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(r.branch)
        url = _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", r.branch,
            "--label", "drift", "--label", "retire",
        ]).splitlines()[-1]
        for issue in r.issues:
            subprocess.run(
                ["gh", "issue", "comment", issue, "--body",
                 f"Upstream is repaired and the detector no longer raises this. "
                 f"Retirement proposed: {url}. Merging it closes this issue."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])
