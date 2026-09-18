# fin_aiwh — Knowledge Transfer

AI enabled finance warehouse for AP/AR subledger data. Contract-first
governance, an AI agent that proposes changes, a CI gate that decides if they
ship.

| | |
|---|---|
| Repo | https://github.com/manojnayakgit/fin_aiwh |
| Warehouse | Snowflake, database `FIN_AIWH` |
| Status | Working proof of concept, verified end to end on a live account |
| For | Engineers joining the project, and stakeholders funding it |

---

## 1. The problem and the approach

**Problem.** Upstream ERP teams change table shapes without telling anyone. The
monthly pack is wrong for weeks before anyone notices. The cost is the
investigation, not the fix.

**Why not schema drift tools.** They fingerprint tables and alert on change.
That answers *did something change*. Finance asks *is anything we report wrong,
and who agreed to it*.

**Approach.**

| Principle | Meaning |
|---|---|
| Contract first | Every source dataset has a YAML contract in git: columns, types, nullability, keys, owner. It is the agreement, not a description |
| Compare to contract | The warehouse is judged against the contract, never against its own past |
| Classify by consequence | Divergence is graded by what it breaks, not how unusual it looks |
| AI drafts, code decides | Claude proposes the contract change. Deterministic code verifies it. CI gates it |

**Goal.** Remove the routine half of data engineering. Safe upstream changes
are adopted with no human. Dangerous ones stop the release with a plain
language reason.

### Severity

| Level | Meaning | Action |
|---|---|---|
| `LOW` | Additive, nothing downstream is wrong | PR opened, auto merges when CI is green |
| `MEDIUM` | Needs a decision, nothing broken yet | PR opened with a recommendation, human decides |
| `BREAKING` | Something is already wrong or about to be | No PR. GitHub issue with evidence. Gate fails |

**The example for stakeholders.** A money column moves from `NUMBER(18,2)` to
`NUMBER(18,4)`. Nothing errors. dbt builds green. Every total drifts from the
subledger by a rounding margin that compounds over a million invoices. This
system says `BREAKING` and blocks the release. A fingerprint diff logs "type
changed" and moves on.

---

## 2. Architecture

```
  UPSTREAM ERP  (changes shape without warning)
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ SNOWFLAKE  FIN_AIWH                                          │
│  RAW ──► STAGING ──► MARTS          META                     │
│  8 tables  8 views   5 tables       contracts, drift events, │
│                      AP/AR aging    observed schema, run log │
│                      DSO/DPO                                 │
│            └── dbt, mart contracts enforced ──┘              │
└──────────────────────────────────────────────────────────────┘
       ▲ reads INFORMATION_SCHEMA         │ writes events
       │                                  ▼
┌──────────────────────────────────────────────────────────────┐
│ CONTROL PLANE   python -m control.cli   (outside Snowflake)  │
│  detect ─► classify ─► impact ─► agent ─► verify ─► publish  │
│                                    │        ▲                │
│                               Claude API   same rules that   │
│                               drafts       raised the event  │
└──────────────────────────────────────────────────────────────┘
       │                                          │
       ▼                                          ▼
  contracts/raw/*.yml                     GITHUB
  agreement of record                     PR ─► gate ─► merge
                                          issue for BREAKING
```

| Decision | Reason |
|---|---|
| Control plane outside Snowflake | Governance that lives in the warehouse is owned by the vendor. Contracts, rules and audit trail are portable Python and YAML |
| Model drafts, does not decide | The proposal is verified by the same rules that raised the event, then gated by CI. The AI is inside the loop, not in charge of it |
| Contracts in git | Every change is a reviewed pull request with history |

---

## 3. Tools

| Tool | Used for | Where |
|---|---|---|
| Snowflake | Warehouse and control plane tables | `FIN_AIWH.{RAW,STAGING,MARTS,META,CI}` |
| dbt-snowflake | Transformation, tests, enforced mart contracts | `dbt/` |
| Python 3.11 | Detection, classification, agent, console | `control/` |
| Claude API | Drafting contract changes. Nothing else | `control/agent.py` |
| GitHub | Contracts under review. PRs and issues are the work queue | `contracts/` |
| GitHub Actions | Release gate, scheduled operating cycle | `.github/workflows/ci.yml`, `cycle.yml` |
| pytest | 62 tests, no warehouse or API needed | `tests/` |

Model: `claude-sonnet-4-5`, override with `ANTHROPIC_MODEL`. Only the agent
calls it. Everything else is deterministic.

---

## 4. How it works

### 4.1 A contract

```yaml
dataset: RAW.AP_PAYMENT
version: 1
owner: finance-data-engineering
classification: financial-restricted
primary_key: [PAYMENT_ID]
freshness: {column: LOADED_AT, max_lag_hours: 24}
columns:
  - {name: PAYMENT_ID,  type: TEXT,   length: 32,                nullable: false}
  - {name: PAID_AMOUNT, type: NUMBER, precision: 18, scale: 2,   nullable: false}
```

Types use Snowflake's `INFORMATION_SCHEMA` vocabulary (`TEXT` not `VARCHAR`),
so comparison is a direct field match with no translation layer.
`seeds/emit.py` generates the v1 contracts and the RAW DDL from one spec.

### 4.2 Detection

`python -m control.cli detect` → `control/detect.py`

| Step | What | Detail |
|---|---|---|
| 1 Observe | One query on `INFORMATION_SCHEMA.COLUMNS` for schema `RAW` | No table scans. Returns type, length, precision, scale, nullability per column |
| 2 Snapshot | Write every column to `META.OBSERVED_SCHEMA` with a run id | Evidence for "what did it look like on the 3rd" |
| 3 Diff | For each contract: compare every declared column, then flag undeclared ones | Then any RAW table with no contract → `DATASET_UNGOVERNED` |
| 4 Classify | Each divergence gets a severity and a one sentence reason written for a finance reader | Rules table in section 5 |
| 5 Impact | Attach what it breaks downstream, from dbt lineage | Section 4.6 |
| 6 Fingerprint | `sha256(dataset, change_type, object, after_state)[:32]` | Same divergence, same id. The idempotency key |
| 7 Persist | Insert only fingerprints not already `OPEN`, `PROPOSED` or `ESCALATED`. Suppressed events still get their impact refreshed | A scheduled run never opens a duplicate issue, but an event open for a week reports what it breaks today |
| 8 Log | One row in `META.RUN_LOG`, even when nothing was found | A quiet run is still proof |

Type comparison:

| Contract → Live | Verdict |
|---|---|
| `TEXT` → `NUMBER` | BREAKING, base type |
| `TEXT(32)` → `TEXT(64)` | LOW, widened |
| `TEXT(64)` → `TEXT(32)` | BREAKING, narrowed |
| `NUMBER(18,2)` → `NUMBER(18,4)` | BREAKING, scale moved |
| `NUMBER(18,2)` → `NUMBER(38,2)` | LOW, precision widened |
| `NUMBER(38,2)` → `NUMBER(18,2)` | BREAKING, precision narrowed |

Flags: `--fail-on-breaking` exits 2 (CI depends on this). `--dataset X` scopes
both contracts and observed schema. `--dry-run` writes nothing.

### 4.3 Registration

`python -m control.cli register` → `control/register.py`

A contract file changing on disk means nothing until registered.

| Step | What |
|---|---|
| Canonicalise | Sort columns and keys, fill defaults. Formatting and comments cannot change the result |
| Hash | `sha256` of the canonical form |
| Compare | Against `META.ACTIVE_CONTRACT` for that dataset |
| Same hash | Nothing happens |
| New hash, version not bumped | Refused, dataset named |
| New hash, version bumped | New row: version, hash, full spec as JSON, git SHA |

Append only. `ACTIVE_CONTRACT` is a view picking the highest version. A March
event can still be read against the March contract.

### 4.4 Agent

`python -m control.cli agent` → `control/agent.py`

| Step | What |
|---|---|
| Bundle | Group open events by dataset. Route on the **worst** event, because you cannot adopt half a dataset |
| Route | BREAKING → issue, events `ESCALATED`. LOW or MEDIUM → draft, verify, PR, events `PROPOSED` |
| Draft | Claude receives: events as JSON, live schema as JSON, current contract, current staging model, downstream impact. Replies through a **forced tool call** with a fixed schema: `contract_yaml`, `staging_sql`, `pr_title`, `pr_body`, `reasoning`. Structured data, nothing to parse |
| Verify | Reject if: YAML fails to parse, dataset renamed, version ≠ old+1, any contracted column dropped, primary key changed, **proposal still diverges from live schema**, staging no longer reads `source('raw', ...)` |
| Publish | Fetch, branch from `origin/main` (never local HEAD), write contract and optional staging model, commit, push, `gh pr create` with `drift` + severity labels |
| Auto merge | Only if branch protection reports required status checks. Otherwise the agent says why it did not |
| Mark | Events → `PROPOSED` or `ESCALATED`, URL stored in `RESOLUTION_REF` |

The verify step re-runs `diff_dataset()`, the function that raised the event,
against the proposal. If the gap is not closed exactly, the draft is wrong by
definition. Rejected drafts are printed and skipped. Nothing reaches git.

System prompt constraints: bump version by exactly one, describe live schema
exactly, never remove a live column, keep existing descriptions, mark inferred
descriptions as inferred, invent no business rules.

### 4.5 Sync

`python -m control.cli sync` reads each `PROPOSED` or `ESCALATED` event's
stored URL and asks GitHub what happened.

| Reference | GitHub state | Event becomes |
|---|---|---|
| PR | merged | `MERGED` |
| PR | closed, not merged | `OPEN` (the drift is still there) |
| PR | open | unchanged |
| Issue | closed | `DISMISSED` |
| Issue | open | unchanged |

Without `sync`, a merged PR leaves events `PROPOSED` forever and the detector
stays silent about that dataset.

### 4.6 Impact analysis

`control/lineage.py`. Turns "BANK_REF dropped" into "BANK_REF dropped, breaks
`fct_ap_open_items`, `agg_ap_aging`, `kpi_dso_dpo`".

| Level | Source | Confidence |
|---|---|---|
| Model | dbt `manifest.json` `child_map`, walked transitively from the source | Exact. dbt derives it from compiled SQL |
| Column | Each direct child model's SQL: names the column, or `select *`, or neither | Stated on every result: "references the column", "selects *", or not affected |
| Report | dbt **exposures** in `dbt/models/marts/exposures.yml`, one per dashboard or extract that reads a mart | Exact. Declared with an owner |

Reports lead every impact summary. "BANK_REF dropped" reads as "breaks AP
Aging Pack (AP Controller), Bank Reconciliation Extract (Treasury Ops)". Add a
new consumer by adding an exposure; nothing else changes.

A column no staging model selects correctly reports no impact. A missing
manifest reports `unknown`, never "nothing affected".

Shown in: `detect` (`breaks` column, red line naming marts hit by BREAKING),
`status`, console, `META.DRIFT_EVENT.IMPACT`, `META.OPEN_DRIFT`, issue
headline, PR "Downstream impact" section, and the agent prompt.

Limits: needs `dbt parse` to have run. Column match is textual, errs toward
over reporting. A consumer not declared as an exposure is invisible.

### 4.7 dbt layer

| Layer | Models | Purpose |
|---|---|---|
| staging | 8 views | Normalise RAW: trim, upper, coalesce tax, derive net |
| marts | `fct_ap_open_items`, `fct_ar_open_items` | One row per invoice, paid vs outstanding, USD, aging bucket. **Enforced contracts** |
| marts | `agg_ap_aging`, `agg_ar_aging` | By entity and bucket |
| marts | `kpi_dso_dpo` | DSO/DPO per entity, trailing 90 days |
| exposures | 4 declared | AP Aging Pack, AR Aging Pack, Working Capital KPIs, Bank Reconciliation Extract |

22 tests. Amounts cast explicitly (`::number(18,2)`) because Snowflake widens
through arithmetic and the enforced contract would otherwise fail.

**Known limit.** Those casts mean the money-scale scenario builds green. The
mart contract protects output shape; it cannot see that input lost meaning.
Only the source contract catches that. dbt is not a second safety net here.

### 4.8 Event lifecycle

```
detect ─► OPEN ─┬─► PROPOSED ─► MERGED
                │       └──(PR closed)──► OPEN
                └─► ESCALATED ─► DISMISSED
```

Live: `OPEN`, `PROPOSED`, `ESCALATED`. The detector will not re-raise a
divergence while one exists. Finished: `MERGED`, `DISMISSED`. A reappearance
after those is new.

---

## 5. Classification rules

| Divergence | Severity | Why |
|---|---|---|
| Contracted table missing | BREAKING | Everything reading it fails |
| Table with no contract | MEDIUM | Ungoverned, cannot be modelled |
| Contracted column dropped | BREAKING | Selects fail. A dropped key destroys row identity |
| New nullable column | LOW | Contract is stale, nothing breaks |
| New NOT NULL column | MEDIUM | Readers fine, unaware writers fail |
| Base type changed | BREAKING | Every cast and comparison suspect |
| TEXT widened | LOW | Downstream sized smaller will truncate |
| TEXT narrowed | BREAKING | Permitted values no longer fit |
| NUMBER scale changed | BREAKING | Monetary precision moved |
| NUMBER precision widened | LOW | Larger values possible |
| NUMBER precision narrowed | BREAKING | Permitted values overflow |
| NOT NULL → nullable | BREAKING | Joins and aggregates assumed a value |
| Nullable → NOT NULL | MEDIUM | Loads carrying nulls start failing |

Change a severity: edit `diff_dataset()` or `_compare_type()` in
`control/detect.py`, update `tests/test_detect.py`. Nothing else reads the
rules; agent, gate and console all take severity from the event.

---

## 6. Repository and objects

```
contracts/raw/       8 contracts, the agreement of record
control/
  config.py          .env and settings
  snow.py            Snowflake connection, key pair auth
  contracts.py       parse, canonicalise, hash
  detect.py          observe, diff, classify, persist          ← core
  lineage.py         dbt manifest → what a change breaks
  register.py        publish contracts, append only
  load.py            stage + COPY seed CSVs
  agent.py           draft, verify, publish, sync
  cli.py             every command
  ui.py + static/    local console
dbt/                 13 models, 22 tests
ops/sql/             00 bootstrap, 01 META, 02 RAW, 03 impact migration
ops/scenarios/       7 drift scenarios + reset, all re-runnable
seeds/               deterministic AP/AR generator, ~54k rows
tests/               62 tests
docs/KT.md           this
docs/BUILD_LOG.md    how it was built and why, with defects found
```

| Snowflake object | Purpose |
|---|---|
| `FIN_AIWH_WH` | XSMALL, auto suspend 60s |
| `RAW` / `STAGING` / `MARTS` | Landed data / dbt views / what reports read |
| `META` | Control plane tables |
| `CI` | Ephemeral target for gate builds |
| `FIN_AIWH_ENG` | Role |
| `FIN_AIWH_SVC` | Service user, key pair only |

| META table | Holds |
|---|---|
| `CONTRACT_REGISTRY` | Every contract version, hashed, tied to git SHA. Append only |
| `OBSERVED_SCHEMA` | Live schema per run |
| `DRIFT_EVENT` | Every divergence: severity, reasoning, impact, status, resolution URL |
| `RUN_LOG` | One row per run |
| `ACTIVE_CONTRACT` (view) | Latest version per dataset |
| `OPEN_DRIFT` (view) | Open events worst first, with affected marts |

Datasets: `AP_VENDOR`, `AP_INVOICE`, `AP_INVOICE_LINE`, `AP_PAYMENT`,
`AR_CUSTOMER`, `AR_INVOICE`, `AR_RECEIPT`, `FX_RATE`. Five entities, five
currencies, USD reporting.

---

## 7. Setup from zero

### Snowflake, once

```bash
mkdir -p .secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out .secrets/fin_aiwh_rsa_key.p8 -nocrypt
openssl rsa -in .secrets/fin_aiwh_rsa_key.p8 -pubout -out .secrets/fin_aiwh_rsa_key.pub
chmod 600 .secrets/fin_aiwh_rsa_key.p8
```

Snowsight worksheet as `ACCOUNTADMIN`:

1. Run `ops/sql/00_bootstrap.sql`
2. `ALTER USER FIN_AIWH_SVC SET RSA_PUBLIC_KEY='<.pub contents, no BEGIN/END lines>';`

### Local

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
gh auth login
```

`.env`:

```
SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME
SNOWFLAKE_USER=FIN_AIWH_SVC
SNOWFLAKE_PRIVATE_KEY_PATH=.secrets/fin_aiwh_rsa_key.p8
SNOWFLAKE_ROLE=FIN_AIWH_ENG
SNOWFLAKE_WAREHOUSE=FIN_AIWH_WH
SNOWFLAKE_DATABASE=FIN_AIWH
ANTHROPIC_API_KEY=sk-ant-...
```

### Build

```bash
python -m control.cli ping
python -m control.cli apply ops/sql/01_meta_control_plane.sql
python -m control.cli apply ops/sql/02_raw_tables.sql
python seeds/generate.py
python -m control.cli load
python -m control.cli register
python -m control.cli dbt build
python -m control.cli detect
```

Expected: `ping` prints account details, `dbt build` reports `PASS=35`,
`detect` reports `warehouse matches every registered contract`. That is the
baseline. `dbt build` precedes `detect` because it writes the manifest impact
analysis needs.

### GitHub

| Where | What |
|---|---|
| Settings → Secrets → Actions | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PRIVATE_KEY` (full `.p8`, BEGIN/END included), `ANTHROPIC_API_KEY`, `AGENT_GH_TOKEN` |
| Settings → Branches → `main` | Require checks: `rule tests`, `contracts match warehouse`, `dbt build (CI schema)` |

Branch protection is not optional. `gh pr merge --auto` merges the moment a PR
is mergeable. Without required checks the gate never runs. The agent detects
this and refuses to enable auto merge.

---

## 8. Running it

### Commands

| Command | Does | Flags |
|---|---|---|
| `ping` | Verify connection | |
| `apply <file.sql>` | Run a SQL file. Refuses unqualified table DDL | |
| `load` | Stage and COPY seed CSVs into RAW | `[TABLE ...]` |
| `register` | Publish contracts. Append only | |
| `detect` | Compare, classify, persist | `--dry-run`, `--fail-on-breaking`, `--dataset X` |
| `status` | Contracts and open drift with affected marts | |
| `agent` | Open drift → PRs and issues | `--dry-run`, `--no-merge`, `--dataset X` |
| `sync` | GitHub outcomes → event status | `--dry-run` |
| `resolve` | Close events by hand | `--all`, `--status`, `--ref` |
| `dbt <args>` | dbt with the control plane's connection | any dbt args |

### Operating cycle

```bash
python -m control.cli sync
python -m control.cli register
python -m control.cli detect
python -m control.cli agent
```

Order matters. Reconcile what finished, make merged contracts the record,
compare, act.

**Scheduled.** `.github/workflows/cycle.yml` runs this four times a day and on
demand (Actions → cycle → Run workflow, with a dry run option). One extra
secret:

| Secret | Why |
|---|---|
| `ANTHROPIC_API_KEY` | the agent drafts in CI |
| `AGENT_GH_TOKEN` | fine-grained PAT, this repo, contents + pull requests + issues write. GitHub does not run workflows for events caused by `GITHUB_TOKEN`, so without this the gate never runs on agent PRs and auto merge is refused. The run warns if it is missing |

### Console

```bash
python -m control.ui
```

http://127.0.0.1:8765. Fire scenarios, run commands, watch events. Every
button runs the same CLI command and streams its output. Allowlisted actions,
loopback only.

### Four click demo

| Click | Result |
|---|---|
| `detect` | Clean |
| `04 money scale changed` | Nothing errors anywhere |
| `detect` | Two BREAKING events, plain language reasons, marts named |
| `agent` | PR for what is safe, issue for what is not |

### Scenarios

| File | Change | Verdict |
|---|---|---|
| `01_additive_column` | Nullable column added | COLUMN_ADDED / LOW |
| `02_required_column_added` | NOT NULL column added | COLUMN_ADDED / MEDIUM |
| `03_type_widened` | VARCHAR 64 → 128 | TYPE_CHANGED / LOW |
| `04_money_scale_changed` | NUMBER (18,2) → (18,4) | TYPE_CHANGED / BREAKING |
| `05_column_dropped` | Contracted column removed | COLUMN_REMOVED / BREAKING |
| `06_nullability_relaxed` | NOT NULL dropped | NULLABILITY_RELAXED / BREAKING |
| `07_new_ungoverned_source` | New table, no contract | DATASET_UNGOVERNED / MEDIUM |
| `99_reset` | Rebuild RAW to v1 | then `load`, `resolve --all` |

All verified live. All safe to run twice.

```bash
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect --fail-on-breaking
```

### Tests

```bash
python -m pytest tests -q
```

---

## 9. Release gate

`.github/workflows/ci.yml`, on every PR:

| Job | Question | How |
|---|---|---|
| `rule tests` | Do the rules still hold | `pytest tests` |
| `contracts match warehouse` | Do the contracts **this PR changes** match live | `detect --dry-run --fail-on-breaking --dataset ...` |
| `dbt build (CI schema)` | Does dbt build with contracts enforced | `dbt build -t ci` into `FIN_AIWH.CI` |

| Design point | Why |
|---|---|
| Reads only | Persisting events is the scheduled detector's job. A gate that wrote would double count |
| Scoped to changed contracts | Diffs against base, reads `dataset` from each changed file. A correct PR must not fail on unrelated drift |
| CI schema | PR builds never touch STAGING or MARTS |

---

## 10. State of play

### Working, verified live

| Area | State |
|---|---|
| Detection | 8 contracts, all 7 scenarios classified correctly |
| dbt | 13 models, 22 tests, contracts enforced, `PASS=35` |
| Agent | Drafts, verifies, opens PRs and issues, routes correctly |
| Full cycle | drift → agent PR → merged → register → dataset clean at v2 → sync marks MERGED |
| Impact | On every event, in every PR and issue |
| Console, sync, scheduled cycle, 63 tests | Done |

### Not done

| Item | Effect |
|---|---|
| Branch protection not configured | Agent refuses auto merge, LOW path needs a human click |
| Gate never observed running | Workflow and secrets exist, no run watched |
| Jira handoff | Deferred |

### Known limits

| Limit | Note |
|---|---|
| dbt does not catch upstream precision loss | By design. Only the source contract sees it |
| CI reuses `FIN_AIWH_SVC` | Production would use a separate CI user and narrower role |
| Synthetic seed data | Demo scale, not TB scale |
| Column lineage is textual | Errs toward over reporting |
| Undeclared consumers are invisible | Every report must be an exposure |

---

## 11. Roadmap

| Priority | Item | Why |
|---|---|---|
| 1 | Branch protection, watch one gated PR go green | Makes the LOW path autonomous |
| later | Jira handoff for MEDIUM | Out of scope for the PoC, kept open |
| 3 | Agent remediates BREAKING | Compatibility view, backfill, staged contract with deprecation window |
| 4 | Extend the shape | Same propose → verify → gate pattern for new source onboarding, test generation, backfill planning |

**Replacing Claude.** The agent is the only hosted model call, behind one
function with a structured interface. Swapping to a self-hosted model is
contained. Verification does not trust the draft either way.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not connect to Snowflake backend` | Check `SNOWFLAKE_ACCOUNT` is `ORGNAME-ACCOUNTNAME` and the host is reachable |
| `refusing to apply: unqualified table names` | Use `FIN_AIWH.<SCHEMA>.<TABLE>`. Bare names land in the session default schema |
| `contract content changed but version is still N` | Bump `version` in the YAML |
| `auto merge NOT enabled` | Configure branch protection, section 7 |
| `working tree is not clean` | Commit or stash. Agent branches from `origin/main` |
| Old contract version in `status` | Run `register` after a merge |
| `already being worked on N` | Expected. Dedupe is working |
| Impact shows `no manifest` | `dbt/target/` is gitignored. Run `dbt parse` |
| `IMPACT` column missing | Run `apply ops/sql/03_event_impact.sql` |
| dbt contract error on a fact model | A cast changed shape. Contracts in `marts.yml` are enforced deliberately |
| Pasted commands fail with `unrecognized arguments: #` | Trailing comments are passed as arguments. Paste commands without them |
