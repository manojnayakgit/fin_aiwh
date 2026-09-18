from lib import *

S = []
S.append(r"""
# fin_aiwh Implementation Guide

Every stage of the build, in the order it happened, with the code that landed
at each one, the commands that were run, and the pull requests and issues the
system produced along the way.

This is a reconstruction guide. Anyone with a Snowflake account and this
document should be able to rebuild the system stage by stage and reach the
same state. It is not the handover (that is `docs/KT.md`), the evidence
register (`docs/SCENARIOS.md`) or the diary (`docs/BUILD_LOG.md`). It is the
build itself.

**How the code is shown.** Every SQL file, YAML file, workflow and scenario is
reproduced in full. Python modules are shown by the functions and classes that
define the stage, pulled verbatim from git at the commit that introduced them.
Appendix B has the complete final source of every module. Nothing here was
retyped: the document is generated from the repository.

**Conventions.** `Commands` blocks are what was typed on the machine with the
repo. `Snowsight` blocks were run in a Snowflake worksheet as `ACCOUNTADMIN`.
Commit hashes refer to `https://github.com/manojnayakgit/fin_aiwh`.

---

## Contents

| Part | Stages | What lands |
|---|---|---|
| A. Foundation | 1 to 12 | account, control plane, contracts, detection, dbt, scenarios |
| B. Automation | 13 to 21 | release gate, drift agent, console, sync, impact, schedule, shields |
| C. Onboarding and retirement | 22 to 24 | ungoverned tables, authorship, taking shields back out |
| D. Content governance | 25 to 27 | DMF checks, load and identity fixes, content scenarios |
| E. Operating reference | | every command, every PR and issue, test inventory |
| Appendix A | | all scenario scripts |
| Appendix B | | complete source of the control plane |

---

# Part A. Foundation

## Stage 1. Snowflake account and key pair

**Commit:** `55135f0` Contract first control plane for the AP/AR finance warehouse

**What.** One warehouse, one database, five schemas with one job each, one
role, one service user that authenticates with a key pair and has no password.

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

**Why.** Nothing sensitive sits in a config file. The private key stays on
the machine running the control plane; only the public key goes to Snowflake.

""")
S.append(file("ops/sql/00_bootstrap.sql", title="Snowsight: `ops/sql/00_bootstrap.sql`"))
S.append(sh("""
mkdir -p .secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out .secrets/fin_aiwh_rsa_key.p8 -nocrypt
openssl rsa -in .secrets/fin_aiwh_rsa_key.p8 -pubout -out .secrets/fin_aiwh_rsa_key.pub
chmod 600 .secrets/fin_aiwh_rsa_key.p8
""", "Commands: key pair, once"))
S.append(sql("""
-- .secrets/00_bootstrap_key.sql   (generated, gitignored)
-- the public key body without its BEGIN/END lines
ALTER USER FIN_AIWH_SVC SET RSA_PUBLIC_KEY='MIIBIjANBg...';
""", "Snowsight: attach the public key"))
S.append(r"""
`.secrets/` is gitignored. Account identifier used throughout: `JBOEYJF-CUB76064`.

---

## Stage 2. Repository, toolchain and connection

**Commit:** `55135f0`

**Layout.**

```
contracts/raw/      one YAML per source dataset, the agreement of record
control/            the control plane package
ops/sql/            bootstrap, META DDL, RAW DDL
ops/scenarios/      drift scenarios to fire on demand
seeds/              deterministic seed data generator
dbt/                transformation layer
tests/              rule tests, no warehouse needed
docs/               KT, SCENARIOS, BUILD_LOG, this guide
```

**Why `control/` and not `platform/`.** `platform` is a Python stdlib module.
The package was first named `platform/` and shadowed the import. Renamed.

""")
S.append(file("requirements.txt"))
S.append(file(".env.example"))
S.append(sh("""
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m control.cli ping
""", "Commands"))
S.append(r"""
`ping` prints account, user, role, warehouse, database and Snowflake version.

**Connection.** Key pair only. The session timezone is pinned to UTC (added in
Stage 25, shown here in its final form) so every `TIMESTAMP_NTZ` the CLI writes
and every clock it compares against agree.

""")
S.append(file("control/config.py"))
S.append(file("control/snow.py"))
S.append(r"""
---

## Stage 3. Control plane tables

**Commit:** `55135f0`, extended by `fd120e0` (IMPACT column) and `81bc8db` (FINGERPRINT column)

| Table | Holds | Why |
|---|---|---|
| `CONTRACT_REGISTRY` | every registered contract version, hashed, tied to a git SHA | append only, so a past event can be read against the contract in force at the time |
| `OBSERVED_SCHEMA` | what the warehouse looked like on each run | evidence, not just conclusions |
| `DRIFT_EVENT` | every divergence, classified, with the reasoning stored | what the agent and the humans work from |
| `RUN_LOG` | one row per detector run | a quiet run is still evidence something was checked |

Views: `ACTIVE_CONTRACT` (latest version per dataset), `OPEN_DRIFT` (what needs
attention, worst first).

""")
S.append(file("ops/sql/01_meta_control_plane.sql", title="Snowsight or `apply`: `ops/sql/01_meta_control_plane.sql` (final form)"))
S.append(sh("python -m control.cli apply ops/sql/01_meta_control_plane.sql", "Commands"))
S.append(r"""
---

## Stage 4. RAW layer and contracts from one spec

**Commit:** `55135f0`

**Why one spec.** `seeds/_spec.py` describes the eight datasets once. Both the
RAW DDL and the version 1 contracts are emitted from it, so on day one they
match exactly. After that they diverge on purpose: DDL is changed by upstream,
the contract only by a reviewed pull request. The gap is what the detector
reports.

**Datasets.** `AP_VENDOR`, `AP_INVOICE`, `AP_INVOICE_LINE`, `AP_PAYMENT`,
`AR_CUSTOMER`, `AR_INVOICE`, `AR_RECEIPT`, `FX_RATE`. 70 columns.

**Contract shape.** Types are written in Snowflake's `INFORMATION_SCHEMA`
vocabulary (`TEXT`, not `VARCHAR`) so comparison is a direct field match.

""")
S.append(file("contracts/raw/ap_invoice.yml", rev="55135f0", title="`contracts/raw/ap_invoice.yml` as generated (version 1)"))
S.append(sh("""
python seeds/emit.py
python -m control.cli apply ops/sql/02_raw_tables.sql
""", "Commands"))
S.append(r"""
One table from the generated DDL, for shape. All eight follow the same
pattern and every name is three part.

""")
_raw = show("ops/sql/02_raw_tables.sql")
_head = "CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE ("
_one = _head + _raw.split(_head, 1)[1].split(";", 1)[0] + ";"
S.append(sql(_one, "`ops/sql/02_raw_tables.sql`, one of eight"))
S.append(r"""
---

## Stage 5. Seed data and load

**Commit:** `55135f0`, `load` rewritten at `473fd7f` and `82f657d` (shown in final form)

**Why deterministic.** Seed `20260918`, same data every run, so a scenario is
reproducible. Data is deliberately imperfect the way finance data is: partial
payments, disputes, missing cost centres, near-duplicate vendor names.

**Volumes.** 220 vendors, 310 customers, 8,000 AP invoices, ~20k lines, ~5.6k
payments, 9,000 AR invoices, ~6k receipts, ~4.9k FX rates. Five legal
entities, five currencies, USD reporting.

""")
S.append(sh("""
python seeds/generate.py
python -m control.cli load
""", "Commands"))
S.append(r"""
`load` in its final form. Two things were learned the hard way in Stage 26:
it copies by named column so a table that has moved past the seed still loads,
and it refuses before truncating when it cannot succeed.

""")
S.append(file("control/load.py"))
S.append(r"""
---

## Stage 6. Register contracts

**Commit:** `55135f0`

The control plane only judges against what has been published. Registration
hashes the canonical form of each contract (column order and defaults
normalised) and inserts a version row. Re-registering an unchanged contract
does nothing. Changing content without bumping `version` is refused.

""")
S.append(file("control/register.py"))
S.append(sh("python -m control.cli register", "Commands"))
S.append(r"""
---

## Stage 7. Detect

**Commit:** `55135f0`

**What it does.**

1. reads live column metadata from `INFORMATION_SCHEMA.COLUMNS` for RAW, one query, no table scans
2. snapshots it into `OBSERVED_SCHEMA`
3. diffs against every active contract
4. classifies each divergence by consequence
5. writes new events to `DRIFT_EVENT`, skipping any already live with the same fingerprint
6. logs the run

**Classification rules.** Severity is decided once, here, and every other
component reads it from the event.

| Divergence | Severity | Why |
|---|---|---|
| contracted table missing | BREAKING | everything reading it fails |
| table with no contract | MEDIUM | ungoverned, cannot be modelled safely |
| contracted column dropped | BREAKING | selects fail; a dropped key destroys row identity |
| new nullable column | LOW | contract is stale, nothing breaks |
| new NOT NULL column | MEDIUM | readers fine, unaware writers fail |
| base type changed | BREAKING | every cast and comparison suspect |
| TEXT widened | LOW | downstream sized smaller will truncate |
| TEXT narrowed | BREAKING | permitted values no longer fit |
| NUMBER scale changed | BREAKING | monetary precision moved |
| NUMBER precision widened | LOW | larger values possible |
| NUMBER precision narrowed | BREAKING | permitted values overflow |
| NOT NULL relaxed | BREAKING | joins and aggregates assumed a value |
| nullable tightened | MEDIUM | loads carrying nulls start failing |

""")
S.append(file("control/contracts.py"))
S.append(defs("control/detect.py", ["ObservedColumn", "Finding", "OBSERVE_SQL", "fetch_observed", "_compare_type", "diff_dataset", "diff_all"]))
S.append(r"""
`persist()`, `active_fingerprints()` and `event_id()` are shown at the stages
that shaped them (Stages 17 and 27).

""")
S.append(sh("""
python -m control.cli detect
python -m control.cli detect --dry-run
python -m control.cli detect --fail-on-breaking
python -m control.cli status
""", "Commands"))
S.append(r"""
First run against a fresh load: `warehouse matches every registered contract`.
That is the baseline.

---

## Stage 8. The CLI and the DDL guard

**Commit:** `55135f0`, guard added at `efbffc6`

Every operation is a subcommand of `python -m control.cli`. Two pieces worth
showing here: `apply`, because of the guard it grew, and `main`, because it
is the map of everything the system can do.

**The guard.** `99_reset.sql` was first built by copying the RAW DDL and
stripping its `USE SCHEMA RAW` line. Eight `CREATE OR REPLACE TABLE`
statements then ran unqualified and landed in the session's default schema,
`META`. RAW was untouched and the reset silently did nothing. Rule since:
every table name in every SQL file is three part, and `apply` refuses any
`CREATE|ALTER|DROP TABLE` with fewer than three name parts.

""")
S.append(defs("control/cli.py", ["cmd_ping", "_DDL", "_unqualified_ddl", "cmd_apply", "cmd_detect", "main"]))
S.append(r"""
---

## Stage 9. Rule tests

**Commit:** `55135f0`

The rules that decide whether a release is blocked are pure functions. They
are tested without a warehouse, in under a second. 21 tests at this stage;
145 by the end.

""")
S.append(sh("python -m pytest tests -q", "Commands"))
S.append(defs("tests/test_detect.py", ["test_scale_change_on_money_is_breaking", "test_relaxed_nullability_is_breaking", "test_new_nullable_column_is_low", "test_fingerprint_is_stable_and_specific"], title="`tests/test_detect.py`, four of twenty"))
S.append(r"""
---

## Stage 10. The dbt layer

**Commit:** `6f7d837` dbt layer: AP/AR staging, open item facts, aging and DSO/DPO marts

| Layer | Model | Purpose |
|---|---|---|
| staging | `stg_*` (8 views, 9 after onboarding) | normalise RAW: trim, upper, coalesce tax, net amount |
| staging | `stg_fx_rate` | closing rate to USD per day, plus USD to USD identity so joins never drop USD |
| marts | `fct_ap_open_items` | one row per AP invoice, paid vs outstanding, USD, aging bucket |
| marts | `fct_ar_open_items` | same for AR |
| marts | `agg_ap_aging`, `agg_ar_aging` | by entity and bucket |
| marts | `kpi_dso_dpo` | DSO and DPO per entity, count back over trailing 90 days |

**Why explicit casts in the facts.** Mart contracts are enforced. Snowflake
widens numeric types through arithmetic, so without casts a declared
`number(18,2)` arrives as `number(38,2)` and the build fails.

**Why `generate_schema_name`.** dbt's default would create `STAGING_STAGING`.
The macro uses the configured schema as is, except under the `ci` target where
everything lands in `CI`.

**Why `profiles.yml` is committed.** It contains only `env_var()` references.
The CLI loads `.env` and hands dbt an absolute key path.

""")
S.append(file("dbt/dbt_project.yml"))
S.append(file("dbt/profiles.yml"))
S.append(file("dbt/macros/generate_schema_name.sql"))
S.append(file("dbt/macros/aging_bucket.sql"))
S.append(file("dbt/models/staging/sources.yml", title="`dbt/models/staging/sources.yml` (final form, includes AP_ACCRUAL from Stage 22)"))
S.append(file("dbt/models/staging/stg_ap_invoice.sql", rev="6f7d837", title="`dbt/models/staging/stg_ap_invoice.sql` as first written"))
S.append(file("dbt/models/staging/stg_fx_rate.sql"))
S.append(file("dbt/models/marts/fct_ap_open_items.sql"))
S.append(file("dbt/models/marts/kpi_dso_dpo.sql"))
S.append(file("dbt/tests/assert_fx_one_rate_per_day.sql"))
S.append(file("dbt/tests/assert_no_invoice_without_fx.sql"))
S.append(defs("control/cli.py", ["cmd_dbt"]))
S.append(sh("""
python -m control.cli dbt build
python -m control.cli dbt build -t ci
python -m control.cli dbt test
""", "Commands"))
S.append(r"""
First live build: `PASS=35 WARN=0 ERROR=0 SKIP=0 TOTAL=35`. 13 models, 22 tests.

---

## Stage 11. Drift scenarios and the first live run

**Commits:** `55135f0`, `384ef60` (04 keeps its constraints), `ab7ceb4` (idempotent), `d596f6d` (all seven verified)

Each file changes RAW the way an upstream team would, without telling anyone.
Every scenario is idempotent so a console button can fire it twice. Full
scripts are in Appendix A.

| File | Change | Verdict |
|---|---|---|
| `01_additive_column` | nullable `APPROVER_ID` added to `AP_INVOICE` | COLUMN_ADDED / LOW |
| `02_required_column_added` | NOT NULL `REVENUE_STREAM` added to `AR_INVOICE` | COLUMN_ADDED / MEDIUM |
| `03_type_widened` | `INVOICE_NUMBER` 64 to 128 | TYPE_CHANGED / LOW |
| `04_money_scale_changed` | amounts (18,2) to (18,4) | TYPE_CHANGED / BREAKING, two |
| `05_column_dropped` | `AP_PAYMENT.BANK_REF` dropped | COLUMN_REMOVED / BREAKING |
| `06_nullability_relaxed` | `AR_INVOICE.STATUS` allows null | NULLABILITY_RELAXED / BREAKING |
| `07_new_ungoverned_source` | `AP_ACCRUAL` appears, no contract | DATASET_UNGOVERNED / MEDIUM |
| `99_reset` | rebuild RAW to v1 | now stale, see Stage 26 |

""")
S.append(sh("""
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect --fail-on-breaking
python -m control.cli dbt build
python -m control.cli status
""", "Commands: the quiet one"))
S.append(r"""
**What scenario 04 taught, twice.**

The first version of the script used a bare `CREATE OR REPLACE TABLE ... AS
SELECT`. Snowflake drops every `NOT NULL` constraint on a CTAS. The detector
correctly raised ten `NULLABILITY_RELAXED` breaks on top of the two intended
ones. The scenario now declares its column list. The detector was right and
the scenario was wrong.

Then `dbt build` **passed** with scenario 04 applied. The fact models cast
every amount to `number(18,2)`, so the mart contract holds. The cast rounds
the (18,4) input silently. The mart looks perfect and every total is off by up
to half a cent per invoice. A downstream contract protects the shape of the
output; it cannot know the input lost meaning. Only the source contract can.
The detector raised `BREAKING` before any model ran. dbt never saw a problem.

**All seven verified.** Reset, clean baseline, then 01, 02, 03, 05, 06, 07
applied in one pass:

| Scenario | Object | Verdict |
|---|---|---|
| 05 | AP_PAYMENT.BANK_REF | COLUMN_REMOVED / BREAKING |
| 06 | AR_INVOICE.STATUS | NULLABILITY_RELAXED / BREAKING |
| 07 | AP_ACCRUAL | DATASET_UNGOVERNED / MEDIUM |
| 02 | AR_INVOICE.REVENUE_STREAM | COLUMN_ADDED / MEDIUM |
| 01 | AP_INVOICE.APPROVER_ID | COLUMN_ADDED / LOW |
| 03 | AP_INVOICE.INVOICE_NUMBER | TYPE_CHANGED / LOW |

Six divergences, six correct verdicts, worst first.

---

## Stage 12. Closing events by hand

**Commit:** `384ef60`

An event stays OPEN until someone decides. `resolve` marks it DISMISSED or
MERGED and stores a reference. This was the only way to close an event until
`sync` arrived in Stage 18.

""")
S.append(defs("control/cli.py", ["cmd_resolve"]))
S.append(sh("""
python -m control.cli resolve --all
python -m control.cli resolve <event_id> --status MERGED --ref https://github.com/manojnayakgit/fin_aiwh/pull/2
""", "Commands"))
S.append(r"""
**Day one closed here:** foundation, contracts, detection, dbt layer, seven
scenarios, all live and verified against the real account.

---
""")
