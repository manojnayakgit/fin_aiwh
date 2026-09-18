# fin_aiwh build log

Chronological record of how this was built, including the defects found along
the way and why each decision was made. Appended, never reordered.

**For handover, read `docs/KT.md` instead.** This file is the reasoning behind
it, kept for anyone who needs to know why something is the way it is.

Conventions:
→ commands are run from the repo root, inside the venv, unless stated
→ SQL marked "worksheet" is run in Snowsight as ACCOUNTADMIN
→ everything else is run through `python -m control.cli`

---

## Step 0. What this is and why it is shaped this way

**What:** an AI enabled finance warehouse for AP and AR subledger data on
Snowflake and dbt, with a control plane outside both that holds the agreement
about what every dataset is allowed to look like.

**Why contract first:** most drift tooling fingerprints tables and reports
that a fingerprint changed. That answers "did something change", which nobody
in finance is asking. The question is "is anything we report now wrong, and
who agreed to it". So every source dataset has a contract in git, the
warehouse is compared against the contract rather than its own past, and
divergence is classified by what it breaks.

**Why the control plane is Python, not Snowflake native:** so the governance
layer is not owned by the warehouse vendor. Snowflake can be swapped, the
contracts and the rules that judge them cannot be held hostage.

**Severity, decided once, applied everywhere:**

| Level | Meaning | Consequence |
|---|---|---|
| LOW | Additive, nothing downstream is wrong | Adopt automatically, contract bumps on merge |
| MEDIUM | Needs a decision, nothing broken yet | Agent opens a PR with a recommendation, human decides |
| BREAKING | Something is already wrong or about to be | Release gate fails, never silently adopted |

---

## Step 1. Snowflake account bootstrap

**Why:** one warehouse, one database, five schemas with one job each, one role,
one service user that authenticates with a key pair and has no password. This
is how an MNC would run it, and it means nothing sensitive ever sits in a
config file.

| Object | Purpose |
|---|---|
| `FIN_AIWH_WH` | XSMALL compute, suspends after 60s idle |
| `FIN_AIWH.RAW` | landed source data, governed by contracts |
| `FIN_AIWH.STAGING` | dbt staging views |
| `FIN_AIWH.MARTS` | dbt marts, what reports read |
| `FIN_AIWH.META` | control plane tables |
| `FIN_AIWH.CI` | ephemeral target for CI builds |
| `FIN_AIWH_ENG` | role with full rights on the database |
| `FIN_AIWH_SVC` | service user, key pair only |

**Commands (worksheet):**
```sql
-- ops/sql/00_bootstrap.sql   creates everything above
-- .secrets/00_bootstrap_key.sql   attaches this machine's public key
```

Note: `.secrets/` starts with a dot so Finder hides it. `Cmd+Shift+.` shows it.

---

## Step 2. Key pair generation

**Why:** password auth is disabled on the service user. The private key stays
on the machine that runs the control plane. Only the public key goes to
Snowflake.

**Commands (local, once):**
```bash
mkdir -p .secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out .secrets/fin_aiwh_rsa_key.p8 -nocrypt
openssl rsa -in .secrets/fin_aiwh_rsa_key.p8 -pubout -out .secrets/fin_aiwh_rsa_key.pub
chmod 600 .secrets/fin_aiwh_rsa_key.p8
```

`.secrets/` is gitignored. The public key body (without BEGIN/END lines) goes
into `ALTER USER FIN_AIWH_SVC SET RSA_PUBLIC_KEY='...'`.

---

## Step 3. Repo layout

```
contracts/raw/      one YAML per source dataset, the agreement of record
control/            the control plane package
ops/sql/            bootstrap, META DDL, RAW DDL
ops/scenarios/      drift scenarios to fire on demand
seeds/              deterministic seed data
dbt/                transformation layer
tests/              rule tests, no warehouse needed
docs/               this file
```

**Why `control/` and not `platform/`:** `platform` is a Python stdlib module.
Naming a package after it shadows the import.

**Why `.venv` is not in the repo folder:** the Cowork bridge cannot delete
files, and package installs need to. The venv lives outside. Locally, a venv
inside the repo is fine (`.venv/` is gitignored).

---

## Step 4. Python toolchain

**Commands (local):**
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill SNOWFLAKE_ACCOUNT, keep the rest
```

`.env` holds account identifier, user, key path, role, warehouse, database.
It is gitignored. Both the control plane and dbt read it, so there is one
place connection settings live.

**Verify:**
```bash
python -m control.cli ping
```
Prints account, user, role, warehouse, database, Snowflake version.

---

## Step 5. Control plane tables (META)

**Why each table exists:**

| Table | Holds | Why |
|---|---|---|
| `CONTRACT_REGISTRY` | every registered contract version, hashed, tied to a git SHA | append only, so any past drift event can be read against the contract in force at the time |
| `OBSERVED_SCHEMA` | what the warehouse looked like on each run | evidence, not just conclusions |
| `DRIFT_EVENT` | every divergence, classified, with the reasoning stored | the agent and the humans work from this |
| `RUN_LOG` | one row per detector run | a quiet run is still evidence something was checked |

Views: `ACTIVE_CONTRACT` (latest version per dataset), `OPEN_DRIFT` (what
needs attention, worst first).

**Command:**
```bash
python -m control.cli apply ops/sql/01_meta_control_plane.sql
```

---

## Step 6. RAW layer and contracts, generated from one spec

**Why one spec:** `seeds/_spec.py` describes the eight datasets once. Both the
RAW DDL and the v1 contracts are emitted from it, so on day one they match
exactly. After that they diverge on purpose: DDL is changed by upstream, the
contract only by a reviewed PR. The gap is what the detector reports.

**Datasets:** `AP_VENDOR`, `AP_INVOICE`, `AP_INVOICE_LINE`, `AP_PAYMENT`,
`AR_CUSTOMER`, `AR_INVOICE`, `AR_RECEIPT`, `FX_RATE`. 70 columns total.

**Contract shape:** dataset, version, owner, classification, primary key,
freshness SLA, and per column: type (as Snowflake's INFORMATION_SCHEMA reports
it), length or precision/scale, nullable, description.

**Commands:**
```bash
python seeds/emit.py                                   # regenerates DDL + contracts (only at bootstrap)
python -m control.cli apply ops/sql/02_raw_tables.sql  # creates the 8 RAW tables
```

---

## Step 7. Seed data

**Why deterministic:** seed 20260918, same data every run, so a drift
scenario is reproducible. Data is deliberately imperfect the way finance data
is: partial payments, disputes, missing cost centres, a few near duplicate
vendor names.

**Volumes:** 220 vendors, 310 customers, 8,000 AP invoices, ~20k lines,
~5.6k payments, 9,000 AR invoices, ~6k receipts, ~4.9k FX rates. Five legal
entities, five currencies, USD reporting.

**Commands:**
```bash
python seeds/generate.py      # writes seeds/out/*.csv (gitignored)
python -m control.cli load    # PUT to internal stage, COPY INTO each RAW table
```

Load truncates first, so it is safe to re-run.

---

## Step 8. Register contracts

**Why:** the control plane only judges against what has been published.
Registration hashes the canonical form of each contract (column order and
defaults normalised) and inserts a version row. Re-registering an unchanged
contract does nothing. Changing content without bumping `version` is refused.

**Command:**
```bash
python -m control.cli register
```

---

## Step 9. Detect

**What it does:**
1. reads live column metadata from `INFORMATION_SCHEMA.COLUMNS` for RAW
2. snapshots it into `OBSERVED_SCHEMA`
3. diffs against every active contract
4. classifies each divergence
5. writes new events to `DRIFT_EVENT`, skipping any already OPEN with the same fingerprint
6. logs the run

**Classification rules:**

| Divergence | Severity | Why |
|---|---|---|
| contracted table missing | BREAKING | everything reading it fails |
| table with no contract | MEDIUM | ungoverned, cannot be modelled safely |
| contracted column dropped | BREAKING | anything selecting it fails; PK loss also kills row identity |
| new nullable column | LOW | nothing breaks, contract is just stale |
| new NOT NULL column | MEDIUM | readers fine, writers unaware of it fail |
| base type changed | BREAKING | every cast and comparison suspect |
| TEXT widened | LOW | values still fit, downstream sized columns may truncate |
| TEXT narrowed | BREAKING | permitted values no longer fit |
| NUMBER scale changed | BREAKING | monetary precision moved, totals disagree |
| NUMBER precision widened | LOW | larger values now possible |
| NUMBER precision narrowed | BREAKING | permitted values overflow |
| NOT NULL relaxed to nullable | BREAKING | downstream joins and aggregates assumed a value |
| nullable tightened to NOT NULL | MEDIUM | readers fine, loads carrying nulls fail |

**Commands:**
```bash
python -m control.cli detect                      # report and persist
python -m control.cli detect --dry-run            # report only
python -m control.cli detect --fail-on-breaking   # exit 2 on BREAKING, for CI
python -m control.cli status                      # active contracts + open events
```

First run against a fresh load: `warehouse matches every registered contract`.
That is the baseline.

---

## Step 10. Rule tests

**Why:** the rules that decide whether a release is blocked are pure
functions. They are tested without a warehouse, in under a second.

**Command:**
```bash
python -m pytest tests -q      # 21 tests
```

---

## Step 11. dbt layer

**Why explicit casts everywhere in the facts:** mart contracts are enforced.
dbt compares the model's output types to `marts.yml` and refuses to build on
mismatch. Snowflake widens numeric types through arithmetic, so without casts
the declared `number(18,2)` would arrive as `number(38,2)` and fail.

**Models:**

| Layer | Model | Purpose |
|---|---|---|
| staging | `stg_*` (8 views) | normalise RAW: trim, upper, coalesce tax, net amount |
| staging | `stg_fx_rate` | closing rate to USD per day, plus USD→USD identity so joins never drop USD |
| marts | `fct_ap_open_items` | one row per AP invoice, paid vs outstanding, USD, aging bucket |
| marts | `fct_ar_open_items` | same for AR |
| marts | `agg_ap_aging`, `agg_ar_aging` | by entity and bucket |
| marts | `kpi_dso_dpo` | DSO and DPO per entity, count back over trailing 90 days |

**Tests:** 22 generic (unique, not_null, accepted_values, relationships) plus
two singular: no duplicate FX rate per day, no invoice missing an FX rate.

**Why the `generate_schema_name` macro:** dbt's default would create
`STAGING_STAGING`. The macro uses the configured schema as is, except under
the `ci` target where everything lands in `CI`.

**Why `profiles.yml` is committed:** it contains only `env_var()` references.
The CLI loads `.env` and hands dbt an absolute key path.

**Commands:**
```bash
python -m control.cli dbt build          # models + tests, dev target
python -m control.cli dbt build -t ci    # same, into the CI schema
python -m control.cli dbt test           # tests only
```

---

## Step 12. Drift scenarios

**Why:** each file changes RAW the way an upstream team would, without
telling anyone. The detector's job is to notice and judge.

| File | Change | Expected verdict |
|---|---|---|
| `01_additive_column` | nullable column added to AP_INVOICE | COLUMN_ADDED / LOW |
| `02_required_column_added` | NOT NULL column added to AR_INVOICE | COLUMN_ADDED / MEDIUM |
| `03_type_widened` | INVOICE_NUMBER 64 → 128 | TYPE_CHANGED / LOW |
| `04_money_scale_changed` | GROSS_AMOUNT and TAX_AMOUNT (18,2) → (18,4) | TYPE_CHANGED / BREAKING ×2 |
| `05_column_dropped` | AP_PAYMENT.BANK_REF dropped | COLUMN_REMOVED / BREAKING |
| `06_nullability_relaxed` | AR_INVOICE.STATUS allows null | NULLABILITY_RELAXED / BREAKING |
| `07_new_ungoverned_source` | AP_ACCRUAL appears with no contract | DATASET_UNGOVERNED / MEDIUM |
| `99_reset` | rebuild RAW to v1 | then `load` and `resolve --all` |

**Commands:**
```bash
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect --fail-on-breaking      # exit 2
python -m control.cli dbt build                      # fct_ap_open_items fails its contract
python -m control.cli status

# undo
python -m control.cli apply ops/scenarios/99_reset.sql
python -m control.cli load
python -m control.cli resolve --all
```

**Lesson from the first run of 04:** the original scenario used a bare
`CREATE OR REPLACE TABLE ... AS SELECT`. Snowflake drops every `NOT NULL`
constraint on a CTAS. The detector correctly raised ten `NULLABILITY_RELAXED`
breaks on top of the two intended ones. The scenario now declares the column
list explicitly. The behaviour is worth remembering: "just rebuild the table"
is a change with consequences, and this is exactly the kind the control plane
exists to catch.

---

## Step 13. Closing events

**Why:** an event stays OPEN until someone decides. `resolve` marks it
DISMISSED (the warehouse was reverted, or the change was rejected) or MERGED
(the contract was updated by a PR). `--ref` stores the PR or ticket.

**Commands:**
```bash
python -m control.cli resolve --all                          # dismiss everything open
python -m control.cli resolve <event_id> --status MERGED --ref https://github.com/.../pull/12
```

---

## Step 14. First full run, and what the dbt gate did not catch

**Result:** `dbt build` on the live account: PASS=35 WARN=0 ERROR=0. All 13
models built into STAGING and MARTS, all 22 tests passed.

**With scenario 04 applied, dbt still passed.** This was expected to fail and
did not. The fact models cast every amount to `number(18,2)` so the mart
contract holds. The cast rounds the (18,4) input silently. The mart looks
perfect, and every total is off by up to half a cent per invoice.

**Why this matters more than a failure would have:** a downstream contract
protects the shape of the output. It cannot know the input lost meaning. Only
a contract on the input can. The detector raised `TYPE_CHANGED / BREAKING` on
`GROSS_AMOUNT` and `TAX_AMOUNT` before any model ran; dbt never saw a problem.
Two gates, and only the upstream one had the information to say no.

The README's earlier claim that both gates catch scenario 04 is corrected.

---

## Step 15. Reset that did not reset, and the guard that came out of it

**What happened:** `99_reset.sql` was built by copying the RAW DDL and
stripping its `USE SCHEMA RAW` line. The eight `CREATE OR REPLACE TABLE`
statements then ran unqualified and landed in the session's default schema,
which for the service user is `META`. RAW was untouched, `load` reloaded into
the still-broken `AP_INVOICE`, and `detect` reported the same 13 breaks.

**Rule that follows:** every table name in every SQL file is three part,
`FIN_AIWH.<SCHEMA>.<TABLE>`. No file depends on `USE` state.

**Enforcement:** `apply` now scans the file (comments stripped) for any
`CREATE|ALTER|DROP TABLE` with fewer than three name parts and refuses to run
it. Cheaper than finding eight stray tables next month.

**Cleanup:** `99_reset.sql` also drops the eight empty copies from `META`.
Safe to repeat.

**Commands:**
```bash
python -m control.cli apply ops/scenarios/99_reset.sql   # now genuinely rebuilds RAW
python -m control.cli load
python -m control.cli resolve --all
python -m control.cli detect                             # clean
```

---

## Step 16. All scenarios verified on the live account

Reset to v1, clean baseline confirmed, then scenarios 01, 02, 03, 05, 06, 07
applied in one pass and detected together.

| Scenario | Object | Verdict | Expected |
|---|---|---|---|
| 05 | AP_PAYMENT.BANK_REF | COLUMN_REMOVED / BREAKING | yes |
| 06 | AR_INVOICE.STATUS | NULLABILITY_RELAXED / BREAKING | yes |
| 07 | AP_ACCRUAL | DATASET_UNGOVERNED / MEDIUM | yes |
| 02 | AR_INVOICE.REVENUE_STREAM | COLUMN_ADDED / MEDIUM | yes |
| 01 | AP_INVOICE.APPROVER_ID | COLUMN_ADDED / LOW | yes |
| 03 | AP_INVOICE.INVOICE_NUMBER | TYPE_CHANGED / LOW | yes |

Six divergences, six correct verdicts, ordered worst first, each with its
reasoning stored in `META.DRIFT_EVENT`. Scenario 04 was verified separately
in step 14.

**Day one scope closed:** foundation, contracts, detection, dbt layer, all
live and verified against the real account.

---

# Day 2

## Step 17. The release gate (GitHub Actions)

**Why:** a contract change is a pull request. The gate answers two questions
on every PR, and blocks the merge if either answer is no.

| Job | Question | How |
|---|---|---|
| `rules` | do the classification rules still hold | `pytest tests` |
| `contracts` | do the contracts in this PR match the live warehouse | `detect --dry-run --fail-on-breaking` |
| `build` | does dbt build with mart contracts enforced | `dbt build -t ci` into `FIN_AIWH.CI` |

**Why `--dry-run`:** the gate reads, it never writes to META. Persisting events
is the detector's job on its schedule. A gate that wrote would double count.

**Why a CI schema:** `dbt build -t ci` lands everything in `FIN_AIWH.CI`
through the `generate_schema_name` macro. PR builds never touch STAGING or
MARTS.

**Secrets (GitHub → repo → Settings → Secrets → Actions):**

| Secret | Value |
|---|---|
| `SNOWFLAKE_ACCOUNT` | `JBOEYJF-CUB76064` |
| `SNOWFLAKE_PRIVATE_KEY` | full content of `.secrets/fin_aiwh_rsa_key.p8`, BEGIN/END lines included |

For a real MNC the CI user would be a separate service user with its own key
and a narrower role. The PoC reuses `FIN_AIWH_SVC`.

**File:** `.github/workflows/ci.yml`

---

## Step 18. The drift agent

**What it does:** reads OPEN events, groups them by dataset, and routes by the
worst event in the group.

| Worst | Action |
|---|---|
| LOW | model drafts a contract bump, code verifies it, PR opened, auto merge on once the gate is green |
| MEDIUM | same draft and verify, PR opened, waits for a human |
| BREAKING | no PR. GitHub issue with the evidence, events marked ESCALATED |

**The model drafts, the code decides.** This is the design point. Claude is
given the current contract, the events, the live schema and the staging model,
and returns a proposal through a forced tool call (structured output, no
free text to parse). Then `verify()` rejects the proposal if any of these are
true, without asking the model again:

→ the YAML does not parse
→ dataset name changed
→ version is not exactly old + 1 (or 1 for a new dataset)
→ any contracted column was dropped
→ primary key changed
→ the proposed contract still diverges from the live schema (re-runs the detector's own `diff_dataset` on it)
→ the staging model no longer reads from `source('raw', ...)`

A rejected proposal is printed and skipped. Nothing reaches git.

**Why forced tool use:** the response is a JSON object matching a schema the
code owns. No markdown fences, no "here is your contract", nothing to strip.

**Why re-running `diff_dataset` on the proposal is the real guard:** the
agent's output is judged by the same rules that raised the event. If the
proposal does not close the gap completely, it is wrong by definition.

**Files:** `control/agent.py`, `agent` command in `control/cli.py`,
`tests/test_agent.py` (10 tests, model mocked out entirely).

**Configuration in `.env`:**
```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5     # optional
```

**Requires on the machine running it:** `gh` authenticated (`gh auth login`),
push rights to the repo, clean working tree.

**Commands:**
```bash
python -m control.cli agent --dry-run            # draft + verify, print, touch nothing
python -m control.cli agent                      # PRs, issues, auto merge for LOW
python -m control.cli agent --no-merge           # PRs but never auto merge
python -m control.cli agent --dataset RAW.AP_INVOICE
```

**Event lifecycle:** OPEN → PROPOSED (PR url stored) → MERGED, or
OPEN → ESCALATED (issue url stored), or OPEN → DISMISSED via `resolve`.

---

## Step 19. First live agent run, and two bugs it exposed

**The run worked.** Six open events across four datasets, routed correctly:

| Dataset | Worst | Action taken |
|---|---|---|
| RAW.AP_ACCRUAL | MEDIUM | PR #1, contract v1, awaiting review |
| RAW.AP_INVOICE | LOW | PR #2, contract v2 + staging model, auto merge |
| RAW.AP_PAYMENT | BREAKING | issue #3 |
| RAW.AR_INVOICE | BREAKING | issue #4 |

`AR_INVOICE` had one MEDIUM and one BREAKING event. The whole dataset
escalated, because routing takes the worst event in the group. Correct: you
cannot adopt half a dataset.

Two defects surfaced that only a live run would find.

### Bug 1: `mark()` mixed string formatting with driver parameters

`"... WHERE EVENT_ID IN (%s)" % ",".join(...)` collided with the connector's
own `%(name)s` placeholders and raised `TypeError: format requires a mapping`,
*after* the PR had already been created. Every id is now a bound parameter.
`publish()` also checks for an open PR on the branch first, so a re-run after
a crash reuses it instead of failing on an existing branch.

### Bug 2: the agent branched from local HEAD

`git checkout -b` from whatever was checked out swept four unpushed commits
into PR #2. A contract change arrived carrying 815 lines of unrelated work,
and squash merging it put all of it on main under the title "Adopt
APPROVER_ID". The agent now fetches and branches from `origin/<base>`, so a
drift PR contains exactly the contract file and, if needed, one staging model.

### Bug 3: auto merge is not a gate without branch protection

`gh pr merge --auto` merges as soon as the PR is mergeable. With no branch
protection on `main`, no status check is required, so PR #2 merged without the
workflow ever gating it. The agent now reads
`repos/{owner}/{repo}/branches/<base>/protection` and only enables auto merge
when required status check contexts exist. Otherwise it opens the PR and says
plainly why it did not enable auto merge.

**To make the gate real, branch protection must require these contexts on `main`:**
`rule tests`, `contracts match warehouse`, `dbt build (CI schema)`.

The wider point: an agent that opens pull requests is only as safe as the
branch it targets. The review gate lives in the repository's settings, not in
the agent's code, and an agent that assumes otherwise is writing to main.

---

## Step 20. The gate judges only what the PR changes

The `contracts` job originally ran `detect` across the whole warehouse. PR #2
adopts a LOW change on `AP_INVOICE` and would have failed on unrelated
BREAKING drift in `AP_PAYMENT`.

`detect` now takes `--dataset` (repeatable). The job diffs the PR against its
base, reads the `dataset` key out of each changed contract file, and scopes
the run to those. A PR that changes no contracts is still checked against all
of them.

Scoped runs also filter the observed schema, so an unrelated ungoverned table
does not surface as a finding on someone else's PR.

```bash
python -m control.cli detect --dataset RAW.AP_INVOICE --dry-run --fail-on-breaking
```

---

## Step 21. The console

**What:** one page served locally that fires scenarios, runs the control plane,
and shows drift events, contracts and runs as they change.

**Why it runs the CLI rather than calling the code:** every button spawns the
same `python -m control.cli ...` command a person would type, and streams its
stdout. There is no second implementation to drift out of sync with the first,
and what the page shows is exactly what the terminal would show. A demo that
quietly does something different from the documented commands is worse than no
demo.

**Why an allowlist:** the page can only run the eight commands named in
`ALLOWED`, and a scenario button can only name a file that already exists in
`ops/scenarios/`. The browser cannot ask the server to run an arbitrary path.

**Why it binds to 127.0.0.1:** it runs commands against a live warehouse and a
live GitHub token. It is not exposed on the network.

**Layout:** left column fires things (scenarios, control plane actions, reset),
right column is the console output followed by drift events, active contracts
and the run log. Severity counts sit in the header. State refreshes every 15
seconds while idle and immediately after any job finishes.

**Commands:**
```bash
python -m control.ui          # then open http://127.0.0.1:8765
```

**Tests:** `tests/test_ui.py` covers the page rendering, all eight scenarios
being offered with descriptions, state being served, an action outside the
allowlist being refused, a scenario name that tries to escape the folder being
refused, and a job streaming to completion with its exit code. No Snowflake,
no model.

**A demo path that tells the whole story in four clicks:**

1. `detect` → clean, the warehouse matches every contract
2. `04 money scale changed` → the quiet one
3. `detect` → two BREAKING events, with the reasoning in plain language
4. `agent` → a PR for what is safe, an issue for what is not

---

## Step 22. What the first console session exposed

**Scenarios were not re-runnable.** `apply 01_additive_column` failed with
`column 'APPROVER_ID' already exists`. Fine for a script you run once by hand,
wrong for a button. Every scenario is now idempotent: `ADD COLUMN IF NOT
EXISTS`, `DROP COLUMN IF EXISTS`, and the two that rebuild a table already
used `CREATE OR REPLACE`. Firing one twice is a no-op.

**The event table drowned the signal.** Thirty-plus rows, nearly all
`DISMISSED` from earlier runs, with two `ESCALATED` and three `PROPOSED` buried
among them. The console now shows open events by default and puts
`n closed hidden` in the section header as a toggle.

**The table overflowed sideways** because the `why` column is a full sentence.
Fixed columns plus wrapping now, so the reasoning stays readable without a
horizontal scrollbar.

**Contracts still read v1 in the registry** while `contracts/raw/ap_invoice.yml`
is v2 on disk, because the agent's PR merged but `register` has not run since.
That is correct: registration is a deliberate act, not a side effect of a file
changing. Run `register` after merging a contract PR, or the detector keeps
judging against the old version. A scheduled detector should register first.

---

## Step 23. The loop closed, and the duplicate it exposed

**The full cycle worked.** After merging the agent's PR and running
`register` then `detect`, `RAW.AP_INVOICE` disappeared from the drift list
entirely. Upstream changed it, the detector classified the change, the agent
proposed a contract, a human merged it, registration made it the agreement of
record, and the warehouse now matches. That is the whole thesis in one run.

```bash
python -m control.cli register    # the merged contract becomes v2 of record
python -m control.cli detect      # AP_INVOICE no longer diverges
```

**Registration is not automatic, and should not be.** A contract file changing
on disk means nothing until it is registered. Merge a contract PR, then run
`register`, or the detector keeps judging against the old version. A scheduled
detector registers first.

### The duplicate

That run reported `new 4`. Two of those were already escalated to issues #3
and #4, and one already had PR #1 open. They were raised again as fresh
events, because deduplication only looked at `STATUS = 'OPEN'`.

Left alone, a detector on a schedule would open a duplicate issue every single
run, for as long as the breaking change existed. The noise would bury the
signal within a day, which is how alerting systems get muted and then ignored.

**Fix:** deduplicate against every live workflow state.

| Status | Meaning | Re-raise? |
|---|---|---|
| `OPEN` | waiting for triage | no |
| `PROPOSED` | a pull request is open for it | no |
| `ESCALATED` | an issue is open for it | no |
| `DISMISSED` | someone decided no action | yes, it is a new occurrence |
| `MERGED` | the contract was updated | yes, it is a new occurrence |

`detect` now also reports what it stayed quiet about:

```
run 20260917T212712-893e161a  scanned 9 datasets  found 4 divergences
  new 1  already being worked on 3
```

The general rule: anything that runs on a schedule and creates work items
needs an idempotency key and a definition of "already handled". The
fingerprint was the key; the missing half was the definition.

---

## Step 24. Closing the lifecycle: sync

Deduplication stopped the detector shouting about work already in flight. The
other half was missing: nothing told the control plane when that work finished.
A merged pull request left its events `PROPOSED` forever, which meant the
detector would stay silent about that dataset permanently, even if the drift
came back.

`sync` reads the outcome from GitHub, which is where the decision actually
happened, and writes it back.

| Reference | GitHub state | Event becomes | Why |
|---|---|---|---|
| pull request | merged | `MERGED` | the contract was adopted |
| pull request | closed, not merged | `OPEN` | someone rejected the fix, the drift is still there |
| pull request | open | unchanged | still in review |
| issue | closed | `DISMISSED` | a human decided it is handled |
| issue | open | unchanged | still being worked |

The closed-without-merging case is the one worth thinking about. Rejecting a
proposed contract does not make the divergence go away. The event goes back to
`OPEN` so it is triaged again, rather than quietly disappearing because a
pull request was closed.

`RESOLVED_AT` is stamped only on the terminal states. Reopening does not fake a
resolution time.

**Commands:**
```bash
python -m control.cli sync --dry-run   # what GitHub says happened
python -m control.cli sync             # write it back
python -m control.cli register         # a MERGED contract is not in force until this runs
```

**The operating order, for a scheduled run:**

```
sync  →  register  →  detect  →  agent
```

Reconcile what finished, make merged contracts the agreement of record, compare
the warehouse against them, then act on what is left. Any other order either
acts on stale contracts or re-raises work already done.

Seven tests cover the mapping, with `gh` mocked out.

---

## Not yet built

→ observe the GitHub Actions gate actually running (never yet seen green or red)
→ branch protection on main, without which auto merge is decoration
→ run the detector on a schedule, in the sync → register → detect → agent order
→ Jira handoff for MEDIUM events


---

## Gate and shields proven live

The release gate ran green for the first time on a pull request the system
wrote itself: `Shield AP_PAYMENT`, three checks passed.

Getting there exposed two things.

**The gate was judging the warehouse, not the change.** A push touching no
contract files fell back to scanning every contract, and failed because the
warehouse had breaking drift that had nothing to do with the push. Every run
since the secrets were added was red for this reason. Fixed: a change with no
contract files runs detect informationally and cannot fail on it.

**`dbt build` then failed for the right reason.** `stg_ap_payment` still
selected `BANK_REF`, which upstream had dropped, so the model could not compile
and 13 downstream models were skipped. That is the gate telling the truth: main
could not build against the warehouse as it stood.

The agent's shields fixed exactly that. `null::varchar(64) as bank_ref` for the
dropped column, a pass-through plus a failing test for the relaxed nullability.
After both merged, `dbt build` reported `PASS=36 ERROR=0`, with the drift still
open and both issues still open.

**One rough edge.** The second shield PR failed the gate on the first shield's
column, because its branch predated that merge and the build is project wide.
Merging main into the branch cleared it. Two fixes on the roadmap: require
branches to be up to date, or open a single shield PR covering every
unbuildable dataset at once.

---

## Onboarding: a contract was never enough

An ungoverned table used to end in a v1 contract PR and nothing else. Merging
it changed nothing anyone could use: the table was still not a dbt source, had
no staging model and no tests. The contract described a table the project still
could not read.

Onboarding finishes the job. One PR carries the contract, the `sources.yml`
entry, `stg_<table>.sql` and the tests the contract already justifies.

The split is the interesting part. Two things need reading and judgement, and a
model does them: the contract descriptions, and which columns deserve `upper`,
`trim` or `nullif(trim(x), '')`. Two things are mechanical, and code does them:
the source entry is one line at the existing indent, and the tests come
straight out of the contract's primary key and nullability — `unique, not_null`
on the key, `not_null` on anything declared not nullable. Nothing invented.

Then code checks the model's SQL before it reaches git: the output column set
must equal the contract exactly, every key column must be present, it must read
`source('raw', '<TABLE>')`, and `select *` is refused. A derived column is a
rejection, not a bonus — business logic added at onboarding time is business
logic nobody reviewed.

**No mart is wired up, deliberately.** Where a new dataset belongs in the
reporting layer has accounting consequences. The PR says where the agent thinks
it belongs and stops there.

**Two defects found by writing the tests.** The column parser split the select
list on every comma, so `nullif(trim(x), '')` looked like two columns; it now
tracks bracket depth. And it stripped comments per item *after* splitting, so
the prose comma in a shield's `-- shield: … restored as NULL, see <issue>`
swallowed the next column. Comments are stripped first now. A regression test
walks every pass-through staging model in the repo and asserts it still carries
its whole contract.
