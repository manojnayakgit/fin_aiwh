from lib import *
import subprocess

S = []
S.append(r"""
# Part D. Content governance

## Stage 25. DMF checks derived from the contract

**Commits:** `a9512af` (quality), `5265801`, `140a980`, `82f657d`, `1094da2`, `5550b8c`, `d6e91a7`, `f4c6040` (each a lesson from Snowflake)

Until now the contract governed **shape**. A table could match every column,
type and nullability while carrying a duplicate invoice key or three days of
missing loads, and the system would report it clean. The `freshness` block was
parsed, hashed and never read.

**The probe first.** DMFs need Enterprise Edition. `ops/probe_dmf.sql` in
Snowsight returned `0` for `NULL_COUNT` on a live table (DMFs available) and
refused `SNOWFLAKE.CORE.FRESHNESS` on `TIMESTAMP_NTZ`, which is every
`LOADED_AT` in RAW.

""")
S.append(file("ops/probe_dmf.sql"))
S.append(r"""
**Every check is derived from something the contract already states.**
Nothing is invented, the same rule onboarding uses for tests.

| Contract says | Check | Severity | Why |
|---|---|---|---|
| `primary_key: [A]` | `DUPLICATE_COUNT(A) == 0` | BREAKING | every join fans out, every total double counts |
| `nullable: false` | `NULL_COUNT(col) == 0` | BREAKING | what scenario 06's guard test caught, at source |
| `freshness: max_lag_hours: N` | hours behind on the column `<= N` | MEDIUM | reports correct but old; a human decides |
| `expectations:` (optional) | any system DMF, `min` or `max` | as stated | for rules the contract cannot derive |

| Design point | Why |
|---|---|
| Synchronous, not scheduled | `SELECT SNOWFLAKE.CORE.NULL_COUNT(SELECT col FROM t)` answers now. The control plane decides on a measurement it just took |
| One statement per table | all of a table's checks in one `SELECT` |
| Fingerprint carries the threshold, not the value | a breach that persists for a week is one event, not seven |
| Always an issue, never a PR | no contract change makes bad data good |
| Closes itself | `agent` re-measures on every run; a breach back inside the contract closes its issue and dismisses its event |
| Gate is schema-only (`--no-quality`) | a duplicate key in RAW is real, but this PR did not cause it |
| Tables with COLUMN_REMOVED, DATASET_MISSING or TYPE_CHANGED are skipped | a DMF on a dropped column would only fail; a relaxed column is exactly the question worth asking, so it is not skipped |

""")
S.append(file("control/quality.py"))
S.append(defs("control/agent.py", ["escalate_quality", "close_quality_issue"]))
S.append(defs("control/cli.py", ["_quality_pass"]))
S.append(r"""
**What Snowflake refused, in order.** Each of these is one commit, and the
last one gave the rule away.

| Attempt | Error | Lesson |
|---|---|---|
| `GRANT DATABASE ROLE SNOWFLAKE.DATA_QUALITY_MONITORING_VIEWER` | does not exist | it is an *application* role, and only gates Snowflake's history view, which the CLI never reads |
| custom freshness DMF calling `CURRENT_TIMESTAMP()` | body cannot refer to a non-deterministic function | a DMF returns the newest timestamp; the caller subtracts it from `SYSDATE()` |
| `NULL_COUNT` on `IS_ACTIVE` | invalid argument types `(BOOLEAN)` | |
| same, `CAST(... AS VARCHAR)` | `(VARCHAR(134217728))` | |
| same, `CAST(... AS NUMBER(1,0))` | `(NUMBER(2,0))` | a custom DMF with `BOOLEAN` declared, taking the bare column, works |
| composite key via `CONCAT_WS` into a custom `TABLE(VARCHAR)` DMF | `(VARCHAR(134217728))` | |
| same, both sides bound to `VARCHAR(4000)` | `(VARCHAR(4000))` | types matched exactly and it still refused. **A DMF argument is a column reference and nothing else.** Composite keys are counted by plain SQL |

Two custom DMFs remain, each taking a bare column.

""")
S.append(file("ops/20_quality.sql", title="Snowsight: `ops/20_quality.sql` (final form)"))
S.append(r"""
The last statement is the proof. On the live account it returned
`0  0  0  0  1.19`: no duplicate invoice ids, no null amounts, no null flags,
no duplicate FX keys, and the newest load 1.19 hours old.

Two more things this stage changed, both in Stage 2's `snow.py` and Stage 5's
`load.py` as shown: the session is pinned to UTC because `TIMESTAMP_NTZ` has
no zone, and `load` stamps `LOADED_AT` at load time because the seed's fixed
constant would have made every table read as stale a day after loading.

---

## Stage 26. What the first content run broke

**Commits:** `473fd7f` (load), `c3dcaa8` (scenario 02 completes)

**`load` emptied a table.** It truncated `AP_INVOICE` and then failed the
COPY: the seed has 12 columns, the live table 13, because `APPROVER_ID` was
adopted into v2 in Stage 14. The rule is now the same one the publish path
learned: do the check that can refuse before the step that cannot be undone.
`plan_load()` (Stage 5) names its columns and refuses, before truncating, any
table with a NOT NULL column the seed does not carry. `AR_INVOICE` is refused
today for `REVENUE_STREAM` and keeps its data.

""")
S.append(file("tests/test_load.py"))
S.append(r"""
**`99_reset.sql` is stale.** It hard-codes the version 1 shape. Running it
now would drop `AP_ACCRUAL`, which is contracted and modelled, and strip
`APPROVER_ID` from `AP_INVOICE`. Both BREAKING. It carries a warning and is
on the roadmap to be generated from the contracts.

**Scenario 02 completed on its own.** Once scenario 08 repaired the BREAKING
drift on `AR_INVOICE` and issue #4 closed, the detector re-raised
`REVENUE_STREAM` on a now-clean dataset, and the agent took the MEDIUM path in
isolation: contract v2 with the column as `TEXT(16)` NOT NULL and
`upper(revenue_stream)` appended to the staging model. Branch
`drift/ar_invoice-v2`, **PR #10**, never auto-merged.

""")
S.append(sql(subprocess.check_output(["git", "diff", "origin/main", "origin/drift/ar_invoice-v2", "--", "dbt/models/staging/stg_ar_invoice.sql"], cwd=REPO, text=True), "PR #10, the staging change (agent-drafted)"))
S.append(r"""
---

## Stage 27. One key doing two jobs, and the content scenarios

**Commits:** `81bc8db` (event identity), `ec2a538` (recorded)

**Three rows, all PROPOSED.** `sync` listed `RAW.AR_INVOICE.REVENUE_STREAM
PROPOSED still` three times. `EVENT_ID` was the fingerprint,
`sha256(dataset, change, object, after)`, deterministic so the detector can
recognise a divergence it already raised. It was also the row identity.
`REVENUE_STREAM` had been raised, escalated with the bundle, dismissed when
issue #4 closed, re-raised, proposed. Each raise inserted a row with the same
id, because Snowflake does not enforce primary keys. Each status update was
`WHERE EVENT_ID = ...`, so it moved every lifecycle at once.

Dedupe needs a key that repeats. Identity needs one that never does.

| Column | Meaning |
|---|---|
| `FINGERPRINT` | what the event is about. Repeats across lifecycles. The dedupe key |
| `EVENT_ID` | one raise: `sha256(fingerprint, run_id)`. Never repeats. What every status update targets |

""")
S.append(file("ops/sql/04_event_identity.sql", title="Snowsight: `ops/sql/04_event_identity.sql`"))
S.append(r"""
Its last statement returned zero rows on the live account: no `EVENT_ID`
shared by more than one row.

**The content scenarios.**

""")
S.append(file("ops/scenarios/09_duplicate_key.sql"))
S.append(file("ops/scenarios/10_stale_source.sql"))
S.append(file("ops/scenarios/11_content_repaired.sql"))
S.append(sh("""
python -m control.cli apply ops/scenarios/09_duplicate_key.sql
python -m control.cli apply ops/scenarios/10_stale_source.sql
python -m control.cli detect
python -m control.cli agent
python -m control.cli apply ops/scenarios/11_content_repaired.sql
python -m control.cli agent
""", "Commands"))
S.append(r"""
**Live.** Schema detection saw nothing, which is right: the shape was
untouched. The content checks saw both. `agent` opened **issue #11**
(`Data breaches contract: RAW.AP_INVOICE`) and **issue #12** (`RAW.AR_RECEIPT`),
labelled `quality`, no pull request. After scenario 11:

```
RAW.AP_INVOICE  DUPLICATE_KEY on INVOICE_ID cleared
  closed https://github.com/manojnayakgit/fin_aiwh/issues/11
RAW.AR_RECEIPT  STALE on LOADED_AT cleared
  closed https://github.com/manojnayakgit/fin_aiwh/issues/12
no open drift, nothing to do
```

Eleven scenarios. Shape and content. Every one fired against Snowflake and
resolved by the system, with no human step other than clicking Merge on a
pull request the system wrote. Content breaches did not even need that.
145 tests.

---

# Part E. Operating reference

## Every command

| Command | What |
|---|---|
| `python -m control.cli ping` | verify the Snowflake connection |
| `python -m control.cli apply <file.sql>` | run a SQL file; refuses unqualified table DDL |
| `python -m control.cli load [TABLE ...]` | seed CSVs into RAW, by column, refuses before truncating |
| `python -m control.cli register` | publish contracts to `META.CONTRACT_REGISTRY` |
| `python -m control.cli detect [--dry-run] [--fail-on-breaking] [--dataset X] [--no-quality]` | compare the warehouse to the contracts, shape and content |
| `python -m control.cli status` | contracts, open and in-flight events |
| `python -m control.cli agent [--dry-run] [--no-merge] [--dataset X]` | retire stale shields, handle content breaches, then draft, verify, PR or issue |
| `python -m control.cli sync [--dry-run]` | pull PR and issue outcomes back into the control plane |
| `python -m control.cli resolve --all` / `<event_id> --status S --ref URL` | close events by hand |
| `python -m control.cli dbt <args>` | run dbt with the control plane's connection |
| `python -m control.ui` | the console on `127.0.0.1:8765` |
| `python -m pytest tests -q` | 145 tests, no warehouse, ~2s |

**The operating order:** `sync → register → detect → agent`.

## Every pull request and issue

| # | Kind | Title | Author | Outcome |
|---|---|---|---|---|
| 1 | PR | Create contract for RAW.AP_ACCRUAL (version 1) | agent | closed unmerged; superseded by #7 |
| 2 | PR | Adopt APPROVER_ID and widen INVOICE_NUMBER to TEXT(128) | agent | merged `b3eb4b3`; swept 4 unpushed commits (bug fixed) |
| 3 | issue | Breaking drift: RAW.AP_PAYMENT | agent | closed by PR #8 |
| 4 | issue | Breaking drift: RAW.AR_INVOICE | agent | closed by PR #9 |
| 5 | PR | Shield AP_PAYMENT | agent | merged `b887684`; first green gate |
| 6 | PR | Shield AR_INVOICE | agent | merged `93c81fb`; needed `main` merged in |
| 7 | PR | Onboard RAW.AP_ACCRUAL: contract, source, staging model and tests | agent | merged `99760b0` |
| 8 | PR | Retire the AP_PAYMENT shield: upstream is repaired | agent | merged `de7e079` |
| 9 | PR | Retire the AR_INVOICE shield: upstream is repaired | agent | merged `1756b39` |
| 10 | PR | Adopt new REVENUE_STREAM column in AR_INVOICE | agent | open on `drift/ar_invoice-v2` |
| 11 | issue | Data breaches contract: RAW.AP_INVOICE | agent | opened and closed by the agent |
| 12 | issue | Data breaches contract: RAW.AR_RECEIPT | agent | opened and closed by the agent |

Every one of these was written by the system. The human contribution was
clicking Merge on 2, 5, 6, 7, 8 and 9.

## Test inventory

| File | Tests | Covers |
|---|---|---|
| `tests/test_detect.py` | 22 | every classification rule, fingerprint stability, dedupe, per-raise identity |
| `tests/test_onboard.py` | 27 | source entry, test generation, SQL column parsing, every `verify()` rejection |
| `tests/test_agent.py` | 26 | proposal verification, bundling, branch protection probe, GitHub outcomes, publish guards |
| `tests/test_shield.py` | 22 | shield planning, refusal to guess, staleness, retirement inverse, idempotency |
| `tests/test_quality.py` | 16 | check derivation, breach verdicts, fingerprint stability, one statement per table |
| `tests/test_lineage.py` | 13 | model and column lineage, confidence levels |
| `tests/test_ui.py` | 6 | console allowlist, every scenario offered |
| `tests/test_contracts.py` | 6 | parsing, unique keys, ownership, hashing |
| `tests/test_load.py` | 5 | load planner refuses before truncating |

## Rules that recur

→ Do the check that can refuse before the step that cannot be undone. (publish, load)
→ Anything that acts on a column consults the live schema and installed shields, never an event's stored status. (retirement, shields, content)
→ Dedupe needs a key that repeats; identity needs one that never does. (events)
→ A DMF argument is a column reference, never an expression. (content)
→ A test that fails whenever the product succeeds is measuring the wrong property. (contract count, scenario count)
→ The model drafts, deterministic code decides, and the verifier re-runs the same function that raised the problem.

---
""")
