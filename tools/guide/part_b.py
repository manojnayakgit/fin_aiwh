from lib import *

S = []
S.append(r"""
# Part B. Automation

## Stage 13. The release gate

**Commits:** `b3eb4b3` (first workflow, arrived inside PR #2), `202b695` (scoped to changed contracts), `f2c1d21` (informational when nothing changed), `a9512af` (`--no-quality`)

A contract change is a pull request. The gate answers three questions on
every PR and blocks the merge if any answer is no.

| Job | Question | How |
|---|---|---|
| `rule tests` | do the classification rules still hold | `pytest tests` |
| `contracts match warehouse` | do the contracts **this PR changes** match the live warehouse | `detect --dry-run --fail-on-breaking --no-quality --dataset ...` |
| `dbt build (CI schema)` | does dbt build with mart contracts enforced | `dbt build -t ci` into `FIN_AIWH.CI` |

| Design point | Why |
|---|---|
| Reads only | persisting events is the scheduled detector's job; a gate that wrote would double count |
| Scoped to changed contracts | diffs against base, reads `dataset` from each changed file. A correct PR must not fail on unrelated drift |
| Informational when nothing changed | the gate judges the change, never the warehouse. Every run was red for a day because of this |
| Schema only | a duplicate key in RAW is real, but this PR did not cause it |
| CI schema | PR builds never touch STAGING or MARTS |

**Secrets** (repo Settings, Secrets, Actions): `SNOWFLAKE_ACCOUNT`,
`SNOWFLAKE_PRIVATE_KEY` (full `.p8` content), `ANTHROPIC_API_KEY`,
`AGENT_GH_TOKEN` (a PAT: events caused by the built-in `GITHUB_TOKEN` do not
trigger workflows, so a PR the agent opens under it would never be gated).

""")
S.append(file(".github/workflows/ci.yml", title="`.github/workflows/ci.yml` (final form)"))
S.append(r"""
The gate was first observed green on PR #5, a shield pull request the system
wrote itself: three checks passed.

---

## Stage 14. The drift agent

**Commit:** `b3eb4b3` (arrived inside PR #2, see the bug below), `3c5c0bf` (branch from origin, auto merge guard)

**What it does.** Reads OPEN events, groups them by dataset, routes by the
worst event in the group. You cannot adopt half a dataset.

| Worst | Action |
|---|---|
| LOW | model drafts a contract bump, code verifies, PR opened, auto merge once the gate is green |
| MEDIUM | same draft and verify, PR opened, waits for a human |
| BREAKING | no PR. GitHub issue with the evidence, events marked ESCALATED |

**The model drafts, the code decides.** Claude receives the events, the live
schema, the current contract and the staging model, and returns a proposal
through a forced tool call. There is no free text to parse. Then `verify()`
rejects the proposal, without asking the model again, if any of these hold:

→ the YAML does not parse
→ the dataset name changed
→ version is not exactly old + 1 (or 1 for a new dataset)
→ any contracted column was dropped
→ the primary key changed
→ the proposal still diverges from the live schema: `verify()` re-runs `diff_dataset()`, the function that raised the event, on the proposal
→ the staging model no longer reads from `source('raw', ...)`

""")
S.append(defs("control/agent.py", ["Bundle", "Proposal", "open_events", "bundle_events", "TOOL_SCHEMA", "SYSTEM", "build_prompt", "draft", "verify"]))
S.append(r"""
**Publishing.** Branch from `origin/<base>`, never local HEAD. Write the
contract and, if the model supplied one, the staging model. Commit, push, open
the PR with `drift` and severity labels. Enable auto merge only for LOW and
only if branch protection reports required status checks; otherwise say why
not. The helpers `_run`, `_start_branch`, `_push` and `_ensure_pushed` are
shown at Stage 22, where the failures that shaped them happened.

""")
S.append(defs("control/agent.py", ["LABELS", "_ensure_labels", "_ensure_clean_tree", "required_checks", "existing_pr", "publish", "escalate", "mark"]))
S.append(defs("control/cli.py", ["cmd_agent"], title="`control/cli.py` `cmd_agent` (final form: onboarding, retirement and content passes were added later and are shown at their stages)"))
S.append(r"""
**Configuration in `.env`.**

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

Requires `gh` authenticated on the machine, push rights, a clean tree.

""")
S.append(sh("""
python -m control.cli agent --dry-run
python -m control.cli agent
python -m control.cli agent --no-merge
python -m control.cli agent --dataset RAW.AP_INVOICE
""", "Commands"))
S.append(r"""
**First live run.** Six open events across four datasets, routed correctly:

| Dataset | Worst | Action |
|---|---|---|
| RAW.AP_ACCRUAL | MEDIUM | **PR #1**, contract v1, awaiting review (later closed unmerged, superseded by onboarding in Stage 22) |
| RAW.AP_INVOICE | LOW | **PR #2**, contract v2 plus staging model, auto merge |
| RAW.AP_PAYMENT | BREAKING | **issue #3** |
| RAW.AR_INVOICE | BREAKING | **issue #4** (one MEDIUM and one BREAKING event; the whole dataset escalated) |

**Three bugs only a live run would find.**

*`mark()` mixed `%` formatting with driver parameters.* `"... IN (%s)" % ",".join(...)`
collided with the connector's own `%(name)s` placeholders and raised
`TypeError: format requires a mapping`, after the PR had already been created.
Every id is now a bound parameter, and `publish()` reuses an open PR on the
branch so a re-run after a crash does not fail on an existing branch.

*The agent branched from local HEAD.* Four unpushed commits were swept into
PR #2. A contract change arrived carrying 815 lines of unrelated work, and
squash merging it put all of that on `main` under the title "Adopt
APPROVER_ID". That is why commit `b3eb4b3` contains the workflow, the agent and
its tests. The agent now fetches and branches from `origin/<base>`.

*Auto merge is not a gate without branch protection.* `gh pr merge --auto`
merges as soon as the PR is mergeable. With no protection on `main`, PR #2
merged without the workflow ever gating it. The agent now reads
`repos/{owner}/{repo}/branches/<base>/protection` and enables auto merge only
when required status check contexts exist.

**Merged:** PR #2 as `b3eb4b3`. After `register` and `detect`, `RAW.AP_INVOICE`
was clean at v2. That is the whole thesis in one run.

""")
S.append(sh("""
python -m control.cli register
python -m control.cli detect
""", "Commands: after merging a contract PR"))
S.append(r"""
Registration is not automatic and should not be. A contract file changing on
disk means nothing until it is registered.

---

## Stage 15. The gate judges only what the PR changes

**Commit:** `202b695`

The `contracts` job originally ran `detect` across the whole warehouse. PR #2
adopted a LOW change on `AP_INVOICE` and would have failed on unrelated
BREAKING drift in `AP_PAYMENT`. `detect` grew `--dataset` (repeatable); the
job diffs the PR against its base, reads the `dataset` key out of each changed
contract file, and scopes the run. Scoped runs also filter the observed
schema, so an unrelated ungoverned table does not surface on someone else's PR.

""")
S.append(sh("python -m control.cli detect --dataset RAW.AP_INVOICE --dry-run --fail-on-breaking", "Commands"))
S.append(r"""
---

## Stage 16. The console

**Commit:** `d904338`, refined at `ab7ceb4`

One page served on `127.0.0.1:8765` that fires scenarios, runs the control
plane and shows events, contracts and runs as they change.

| Design point | Why |
|---|---|
| Runs the CLI as subprocesses | there is no second implementation to drift from the first; what the page shows is what the terminal shows |
| Allowlist | the page can run only the named actions, and a scenario button can name only a file that exists in `ops/scenarios/` |
| Loopback only | it runs commands against a live warehouse and a live GitHub token |
| Open events by default | thirty DISMISSED rows had buried two ESCALATED and three PROPOSED |

""")
S.append(defs("control/ui.py", ["ALLOWED", "scenarios", "start_job", "read_state"]))
S.append(sh("python -m control.ui", "Commands"))
S.append(r"""
Four click demo: `detect` (clean), `04 money scale changed`, `detect` (two
BREAKING with reasoning), `agent` (a PR for what is safe, an issue for what is
not).

---

## Stage 17. Do not raise it twice

**Commit:** `b3dbff2`

The loop-closing run reported `new 4`. Two of those were already escalated to
issues #3 and #4, and one already had PR #1 open. They were raised again as
fresh events because deduplication only looked at `STATUS = 'OPEN'`. Left
alone, a scheduled detector would open a duplicate issue every run for as long
as the breaking change existed.

| Status | Meaning | Re-raise? |
|---|---|---|
| OPEN | waiting for triage | no |
| PROPOSED | a pull request is open for it | no |
| ESCALATED | an issue is open for it | no |
| DISMISSED | someone decided no action | yes, a new occurrence |
| MERGED | the contract was updated | yes, a new occurrence |

""")
S.append(defs("control/detect.py", ["ACTIVE_STATUSES", "active_fingerprints", "event_id", "persist"], title="`control/detect.py` `ACTIVE_STATUSES`, `active_fingerprints`, `event_id`, `persist` (final form; `event_id` and `FINGERPRINT` arrived in Stage 27)"))
S.append(r"""
`detect` now also reports what it stayed quiet about:

```
run 20260917T212712-893e161a  scanned 9 datasets  found 4 divergences
  new 1  already being worked on 3
```

---

## Stage 18. Closing the lifecycle: sync

**Commit:** `db19ce4`

Deduplication stopped the detector shouting about work in flight. Nothing yet
told the control plane when that work finished. `sync` reads the outcome from
GitHub, where the decision happened, and writes it back.

| Reference | GitHub state | Event becomes |
|---|---|---|
| pull request | merged | MERGED |
| pull request | closed, not merged | OPEN, the drift is still there |
| pull request | open | unchanged |
| issue | closed | DISMISSED |
| issue | open | unchanged |

""")
S.append(defs("control/agent.py", ["_gh_json", "github_outcome", "pending_events", "set_status"]))
S.append(defs("control/cli.py", ["cmd_sync"]))
S.append(sh("""
python -m control.cli sync --dry-run
python -m control.cli sync
python -m control.cli register
""", "Commands"))
S.append(r"""
**The operating order:** `sync → register → detect → agent`. Reconcile what
finished, make merged contracts the agreement of record, compare, act on what
is left.

---

## Stage 19. Impact analysis

**Commits:** `fd120e0` (lineage), `f46edb2` (refresh on suppressed events), `cc2b461` (exposures)

A drift event said what changed. It did not say what it broke. `lineage.py`
reads dbt's `manifest.json`: `child_map` for exact model lineage, SQL text
matching for column lineage (a heuristic with stated confidence), and dbt
**exposures** for which report a finance user opens. Every event, PR and
issue now leads with the report.

| Confidence | Meaning |
|---|---|
| `exact` | the column is named in the model's SQL |
| `wildcard` | the model selects `*`, so it carries the column |
| `inherited` | downstream of a model that does |

""")
S.append(file("ops/sql/03_event_impact.sql"))
S.append(file("control/lineage.py"))
S.append(file("dbt/models/marts/exposures.yml"))
S.append(defs("control/detect.py", ["attach_impact"]))
S.append(r"""
Impact on a suppressed event is refreshed on every run: an event open for a
week should say what it breaks today, not what it broke when raised.

---

## Stage 20. The scheduled operating cycle

**Commit:** `22832df`

`sync → register → detect → agent`, every six hours, with `workflow_dispatch`
and a `dry_run` input. Uses `AGENT_GH_TOKEN` because events caused by
`GITHUB_TOKEN` do not trigger other workflows, so a PR opened under it would
never be gated. First dry run completed clean.

""")
S.append(file(".github/workflows/cycle.yml"))
S.append(r"""
---

## Stage 21. Shields: keeping reports correct over breaking drift

**Commits:** `f735dfd` (shields), `f2c1d21` (gate fix), PR #5 `b887684`, PR #6 `93c81fb`, `4fe73cb` (recorded)

An issue tells people something is wrong. It does not make the aging pack
right again. A shield does, while the source is fixed. Fully deterministic:
the transformations are mechanical, and a wrong one on a finance mart costs
more than a model's fluency is worth.

| Breaking change | Shield in the staging model | Honest about |
|---|---|---|
| COLUMN_REMOVED | `null::<contracted type> as col` | values are gone until upstream restores them |
| TYPE_CHANGED | `cast(<original expression> as <contracted type>) as col` | rounding or truncation |
| NULLABILITY_RELAXED | pass through, plus a singular test that fails on any null | nothing filled in, the build fails so a human sees it |
| DATASET_MISSING | none | nothing to shield |

| Rule | Why |
|---|---|
| Edits only the select line for that column | the original expression is preserved inside the cast |
| Refuses if the line cannot be found | never guesses at SQL |
| Contract unchanged, drift stays open, issue stays open | a shield hides nothing from the control plane |
| Never auto merges | labelled `shield` and `breaking`, a human merges |
| Every shield line carries `-- shield: ... see <issue>` | `detect` warns when a shield's drift is gone |

""")
S.append(defs("control/shield.py", ["MARK", "SHIELDABLE", "Patch", "Shield", "ddl_type", "plan", "_LINE", "_column_of", "apply", "build", "installed", "stale", "pr_title", "pr_body"]))
S.append(defs("control/agent.py", ["breaking_events", "publish_shield"]))
S.append(defs("control/cli.py", ["_live_active", "_shield"], title="`control/cli.py` `_live_active`, `_shield` (final form; the idempotency checks arrived in Stage 24)"))
S.append(r"""
**Live.** `dbt build` on `main` was failing: `invalid identifier 'BANK_REF'`,
13 downstream models skipped. The gate was telling the truth. The agent
proposed **PR #5** (`stg_ap_payment`: `null::varchar(64) as bank_ref`) and
**PR #6** (`stg_ar_invoice`: header comment plus
`dbt/tests/shield_ar_invoice_status_not_null.sql`).

""")
S.append(file("dbt/models/staging/stg_ap_payment.sql", rev="b887684", title="`stg_ap_payment.sql` as merged in PR #5"))
S.append(file("dbt/tests/shield_ar_invoice_status_not_null.sql", rev="93c81fb", title="`dbt/tests/shield_ar_invoice_status_not_null.sql` as merged in PR #6"))
S.append(r"""
PR #5 was the first time the gate ran green on work the system wrote: rule
tests, contracts, dbt build. PR #6 failed the gate on PR #5's column, because
its branch predated that merge and the build is project wide; merging `main`
into the branch cleared it. After both merged:

```
Done. PASS=36 WARN=0 ERROR=0 SKIP=0 NO-OP=4 TOTAL=40
```

with the drift still open and both issues still open. 75 tests at this point.

---
""")
