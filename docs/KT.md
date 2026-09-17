# fin_aiwh knowledge transfer

Sequential record of every step taken on this project: what was done, why,
and the exact commands. Read top to bottom to rebuild the whole thing from
nothing. New steps are appended, never inserted, so the numbering is the
order things actually happened.

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

## Not yet built

→ agent: reads OPEN events, proposes contract and dbt changes, opens a PR
→ GitHub Actions gate: `detect --fail-on-breaking` + `dbt build -t ci` on every PR
→ Jira handoff for MEDIUM events
→ one page UI to fire scenarios and watch events
