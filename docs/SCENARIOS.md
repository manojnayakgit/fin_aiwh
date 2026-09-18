# fin_aiwh — Scenario Register

Every failure mode this system claims to handle, what was done to prove it, and
what the AI did when it met one. Companion to `docs/KT.md`, which explains how
the machinery works. This document is the evidence.

Read it as a test report. Each scenario states the change, the verdict the
detector is supposed to reach, how that was validated at two levels, and the
artifact that proves it happened.

---

## Status legend

| Status | Meaning |
|---|---|
| **Passed** | Verdict verified by unit test *and* fired live against Snowflake, with the resulting PR or issue on GitHub |
| **Rule only** | Classification rule unit tested, no live scenario script exists to fire it |
| **Built, not merged** | Code and tests complete, verified by dry run, no live artifact yet |
| **Not started** | Designed or on the roadmap, no code |

---

## 1. At a glance

| # | Scenario | Change | Verdict | Status | Artifact |
|---|---|---|---|---|---|
| 01 | Additive column | `AP_INVOICE.APPROVER_ID` added, nullable | `COLUMN_ADDED` / LOW | **Passed** | PR #2, merged `2a2de28` |
| 02 | Required column added | `AR_INVOICE.REVENUE_STREAM` added NOT NULL | `COLUMN_ADDED` / MEDIUM | **Passed** | first swept into issue #4 with the bundle; after repair, adopt PR on `drift/ar_invoice-v2` |
| 03 | Type widened | `AP_INVOICE.INVOICE_NUMBER` VARCHAR 64 → 128 | `TYPE_CHANGED` / LOW | **Passed** | PR #2, merged `2a2de28` |
| 04 | Money scale changed | `AP_INVOICE` amounts NUMBER(18,2) → (18,4) | `TYPE_CHANGED` / BREAKING | **Passed** | no PR by design, BREAKING never auto-adopts |
| 05 | Column dropped | `AP_PAYMENT.BANK_REF` removed | `COLUMN_REMOVED` / BREAKING | **Passed** | issue #3, shield PR #5, merged `72485a8` |
| 06 | Nullability relaxed | `AR_INVOICE.STATUS` NOT NULL dropped | `NULLABILITY_RELAXED` / BREAKING | **Passed** | issue #4, shield PR #6, merged `3a6c350` |
| 07 | New ungoverned source | `RAW.AP_ACCRUAL` created, no contract | `DATASET_UNGOVERNED` / MEDIUM | **Passed** | PR #1 closed, **PR #7 merged** `99760b0` |
| 08 | Upstream repaired | `BANK_REF` restored, `STATUS` NOT NULL again | no drift; 2 shields stale | **Passed** | PR #8 merged `de7e079`, PR #9 merged `1756b39` |
| 09 | Duplicate key | one `AP_INVOICE` row duplicated | `DUPLICATE_KEY` / BREAKING | **Built, not merged** | issue expected, schema detection sees nothing |
| 10 | Stale source | `AR_RECEIPT.LOADED_AT` set 3 days back | `STALE` / MEDIUM | **Built, not merged** | issue expected |
| 11 | Content repaired | 09 and 10 fixed at source | breaches clear | **Built, not merged** | agent closes both issues |
| — | Contracted table missing | table dropped entirely | `DATASET_MISSING` / BREAKING | **Rule only** | `test_missing_table_is_breaking` |
| — | Type narrowed | VARCHAR 128 → 64 | `TYPE_CHANGED` / BREAKING | **Rule only** | `test_narrowing_text_is_breaking` |
| — | Nullability tightened | nullable → NOT NULL | `NULLABILITY_TIGHTENED` / MEDIUM | **Rule only** | `test_tightened_nullability_is_medium` |

11 scenario scripts, all re-runnable. Eight fired live and resolved end to end.
Three content scenarios are built and await their live run. 3 further rules
covered by unit test with no live script.

---

## 2. How each scenario was validated

Three independent layers. A scenario is only marked **Passed** when the first
two agree and the third produced a real artifact.

| Layer | What it proves | Where |
|---|---|---|
| **Unit** | The classification rule is right, against a synthetic live schema built in memory. No Snowflake, no model, runs in ~2s | `tests/test_detect.py`, 20 tests |
| **Live** | Real DDL on the real warehouse produces the same verdict through `INFORMATION_SCHEMA` | `ops/scenarios/*.sql` + `python -m control.cli detect` |
| **Artifact** | The agent did the right thing with the verdict, and a human could see it | PR / issue on GitHub |

The unit layer matters because the live layer is slow and destructive. The live
layer matters because a rule that is right in a fixture and wrong against
Snowflake's actual type reporting is worthless. `TEXT` vs `VARCHAR` is exactly
that trap, and contracts are written in Snowflake's reporting vocabulary so the
comparison is a direct field match.

Full command sequence for any scenario:

```
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect
python -m control.cli agent
python -m control.cli sync
```

Reset afterwards:

```
python -m control.cli apply ops/scenarios/99_reset.sql
python -m control.cli load
python -m control.cli resolve --all
```

---

## 3. Scenario detail

### 01 — Additive column · **Passed**

**Change.** The ERP upgrade added `APPROVER_ID VARCHAR(32)` to `AP_INVOICE`.
Nobody told the data team.

**Why LOW.** Nothing downstream selects a column it does not know about. The
contract is stale, that is all. Safe to adopt without a human deciding.

**Validated.** `test_new_nullable_column_is_low` at unit level. Live: DDL
applied, `detect` raised one `COLUMN_ADDED` / LOW event.

**What the AI did.** Drafted contract v2 carrying `APPROVER_ID` with an inferred
description, and marked the description as inferred, as the system prompt
requires. Added `approver_id` to `stg_ap_invoice.sql` in the existing style.
Opened PR #2. Verification re-ran `diff_dataset()` against the proposal and
found no residual divergence, so the draft was allowed through.

**Merged.** `2a2de28`. After `register`, the dataset was clean at v2.

---

### 02 — Required column added · **Passed**

**Change.** `AR_INVOICE.REVENUE_STREAM VARCHAR(16)`, backfilled, then set NOT
NULL.

**Why MEDIUM.** Readers are unaffected. Any writer that does not know about the
column now fails on insert. A human should decide whether the pipeline can
supply it, so it does not auto-merge.

**Validated.** `test_new_not_null_column_is_medium` at unit level. Live: DDL
applied, `detect` raised `COLUMN_ADDED` / MEDIUM.

**What the AI did.** Nothing on its own. The event was escalated as part of the
`AR_INVOICE` bundle, because scenario 06 put a BREAKING event on the same
dataset and the agent routes on the **worst** event in a bundle. You cannot
adopt half a dataset. This is visible in `sync`:

```
RAW.AR_INVOICE.REVENUE_STREAM   ESCALATED still
RAW.AR_INVOICE.STATUS           ESCALATED still
```

**Then, on its own.** Once scenario 08 repaired the BREAKING drift on the same
table and issue #4 closed, the detector re-raised `REVENUE_STREAM` as a fresh
event on a now-clean dataset, and the agent took the MEDIUM path in isolation:
contract v2 with the column as `TEXT(16)`, `nullable: false`, description marked
inferred, and `upper(revenue_stream)` appended to the staging model in the
existing style. Branch `drift/ar_invoice-v2`. Not auto-merged, as MEDIUM never
is.

---

### 03 — Type widened · **Passed**

**Change.** `AP_INVOICE.INVOICE_NUMBER` VARCHAR(64) → VARCHAR(128).

**Why LOW.** Every existing value still fits. The risk is downstream: any column
still sized to 64 will truncate silently. That is a stale-contract problem, not
a break.

**Validated.** `test_widening_text_is_low` at unit level. Live: raised
`TYPE_CHANGED` / LOW.

**What the AI did.** Bundled it with scenario 01, since both events were on
`AP_INVOICE`. One PR, both adopted: *"Adopt APPROVER_ID and widen
INVOICE_NUMBER to TEXT(128)"*. Bundling by dataset is the reason a reviewer sees
one coherent change instead of two competing branches on the same contract.

---

### 08 — Upstream repaired · **Passed**

**Change.** The source team puts `BANK_REF` back on `AP_PAYMENT` and restores
NOT NULL on `AR_INVOICE.STATUS`. Nothing else is touched: `AP_ACCRUAL` is now
contracted and stays, and `REVENUE_STREAM` is a MEDIUM the project should adopt
rather than push back upstream.

**Why it matters.** Scenarios 05 and 06 prove the system can keep reports
correct while a source is broken. This proves it can stand down again. A shield
that outlives its drift serves a substitute value where the real one is now
available, which is a quiet error of exactly the kind the whole system exists to
prevent.

**Validated so far.** Ten unit tests on the inverse: a null shield restores the
bare column, a cast shield restores the original expression, a pass-through
shield loses its header and its guard test, an unrecognised line is refused, a
column with no shield is refused, and a neighbouring shield is left alone. Run
against the two live shielded models in the repo, both round-trip to their exact
pre-shield SQL, and the planner finds issues #3 and #4 from the markers.

**What the system does.** `agent` checks every installed shield against the
live schema on every run, before it looks at events. For each stale shield it
opens a `retire/<table>` PR whose body says `Closes <issue>`, and comments on
the issue. Merging closes the issue; the next `sync` dismisses the event.

**Fired live.** `detect` reported both shields stale and the warehouse matching
every contract. `agent` opened PR #8 (`retire/ap_payment`) and PR #9
(`retire/ar_invoice`, also deleting the guard test), and commented on issues #3
and #4.

**What broke.** The same run then died. After retiring, the agent moved to its
"already escalated" pass, found the AP_PAYMENT event still marked ESCALATED in
the database, and tried to shield it again. The shield was already on `main`,
so the rewrite produced an identical file and `git commit` had nothing to
commit. Two defects, both fixed and unit tested:

→ the escalated pass trusted the event table; it now consults the live schema,
the same source retirement uses

→ shields never checked whether they were already installed, so any rerun after
a shield merged would have hit the same wall. That bug predates scenario 08.

**Merged.** PR #8 `de7e079` and PR #9 `1756b39`. Both staging models are back
to their plain selects, the guard test is gone, and the `Closes` lines closed
issues #3 and #4 on merge. The BREAKING path is now proven in both directions:
break → escalate → shield → repair → retire → close.

---

### 09, 10, 11 — Content breaches · **Built, not merged**

The first eight scenarios change the **shape** of a table. These change its
**contents** and leave the shape alone, so schema detection is blind to all
three by design. Only the content checks see them.

| # | Change | Verdict | Why that severity |
|---|---|---|---|
| 09 | One invoice row duplicated | `DUPLICATE_KEY` / BREAKING | every join on `INVOICE_ID` fans out; the aging pack double counts one invoice quietly |
| 10 | AR receipts stop loading, 72h behind | `STALE` / MEDIUM | every number stays plausible and drifts from true; a human decides how stale is too stale |
| 11 | Both repaired | breaches clear | the agent closes both issues and dismisses both events on its next run |

**Validated so far.** Fourteen unit tests on the derivation and verdict rules:
a primary key becomes a duplicate check, a composite key is one check over all
its columns, every NOT NULL column gets a null check and nullable ones do not,
the freshness block becomes a stale check in hours, explicit expectations are
honoured and unknown metrics ignored, a value over `max` or under `min` is a
finding with the value in the rationale, a NULL measurement is a finding and
not a pass, and the fingerprint is stable while a breach persists so a week of
the same breach is one event.

The account was probed first. `SNOWFLAKE.CORE.NULL_COUNT` returned `0` on a
live table, so DMFs are available. `SNOWFLAKE.CORE.FRESHNESS` refused
`TIMESTAMP_NTZ`, which is every `LOADED_AT` in RAW, so freshness is a custom
DMF in `ops/20_quality.sql`.

**What the AI does.** Nothing. No contract change makes bad data good, so
content breaches never reach the drafting path. They become an issue, and the
issue closes itself when the measurement is back inside the contract.

**Not yet done.** `ops/20_quality.sql` as ACCOUNTADMIN, then the three
scenarios live.

---

### 04 — Money scale changed · **Passed**

**Change.** `GROSS_AMOUNT` and `TAX_AMOUNT` from NUMBER(18,2) to NUMBER(18,4).

**Why BREAKING, and why this one matters most.** Nothing errors. No job fails.
No alert fires. Every total simply stops agreeing with the subledger, by
fractions, forever. This is the scenario that justifies the whole project: dbt
tests do not catch it, because dbt sees a valid number. Only a contract that
declared scale 2 can see that scale is now 4.

**Validated.** `test_scale_change_on_money_is_breaking` at unit level. Live:
raised two `TYPE_CHANGED` / BREAKING events, one per monetary column, and
nothing else.

**Scenario design note.** Snowflake will not rescale a NUMBER in place, so the
script rebuilds the table. The column list is written out in full deliberately:
a bare `CREATE TABLE AS SELECT` drops every NOT NULL constraint, which the
detector would then correctly report as ten additional breaks. The first draft
of this script did exactly that. The detector was right and the scenario was
wrong.

**What the AI did.** Escalated. No contract PR, by design. A BREAKING event is
never drafted into an adoption, because adopting it would silently ratify the
precision loss. The contract stays at the old scale, the drift stays open, and a
human decides.

---

### 05 — Column dropped · **Passed**

**Change.** `AP_PAYMENT.BANK_REF` dropped.

**Why BREAKING.** The bank reconciliation extract joins on it. Selects fail
outright.

**Validated.** `test_dropped_column_is_breaking` at unit level, plus
`test_dropped_primary_key_explains_identity_loss` for the harsher case where the
dropped column is a key. Live: raised `COLUMN_REMOVED` / BREAKING.

**What the AI did, in two steps.**

→ **Escalated.** Opened issue #3, attaching the downstream impact from dbt
lineage: which models, which marts, which exposures. Events moved to
`ESCALATED`. No contract change proposed.

→ **Shielded.** Then proposed PR #5, a deterministic edit to
`stg_ap_payment.sql` replacing the column with `null::varchar(64) as bank_ref`,
carrying a `-- shield:` comment pointing at issue #3.

The shield is written by code, not by the model. A wrong transformation on a
finance mart costs more than a model's fluency is worth.

**Proof it was needed.** Before the shield, `dbt build` failed with
`invalid identifier 'BANK_REF'` and skipped 13 downstream models. The gate was
telling the truth: main could not build against the warehouse as it stood.
After PR #5 and PR #6 merged, `dbt build` reported `PASS=36 WARN=0 ERROR=0
SKIP=0 NO-OP=4 TOTAL=40`, with the drift still open and both issues still open.

**Merged.** `72485a8`. This is also the PR on which the release gate ran green
for the first time, on work the system wrote itself: rule tests, contract check,
dbt build, three checks passed.

---

### 06 — Nullability relaxed · **Passed**

**Change.** `AR_INVOICE.STATUS` lost NOT NULL.

**Why BREAKING.** Every aggregate that assumed a status exists now has a silent
bucket of nulls. Aging buckets stop summing to the total.

**Validated.** `test_relaxed_nullability_is_breaking` at unit level. Live:
raised `NULLABILITY_RELAXED` / BREAKING.

**What the AI did.** Escalated as issue #4, then proposed shield PR #6. The
shield here is different in kind: it passes the column through unchanged and
adds a singular test that fails on any null.

That is the honest answer. There is no value to substitute for a missing
status, so the shield does not invent one. It makes the build fail loudly
instead of letting a null flow into a report quietly.

**Merged.** `3a6c350`.

**Rough edge found here.** PR #6 failed the gate on PR #5's column, because its
branch predated that merge and `dbt build` is project-wide. Merging main into
the branch cleared it. Two fixes are on the roadmap: require branches to be up
to date, or open one shield PR covering every unbuildable dataset at once.

---

### 07 — New ungoverned source · **Passed**

**Change.** The close automation team created `RAW.AP_ACCRUAL`, 7 columns, 400
rows, no contract, no review.

**Why MEDIUM.** Nothing breaks. The table simply cannot be modelled or trusted
until someone either writes a contract for it or rejects it.

**Validated.** `test_uncontracted_table_is_flagged` at unit level. Live: raised
`DATASET_UNGOVERNED` / MEDIUM.

**What the AI did, first attempt.** Drafted a v1 contract and opened PR #1,
*"Create contract for RAW.AP_ACCRUAL (version 1)"*.

**Why that was not good enough.** Merging PR #1 would have changed nothing
usable. The table was still not declared as a dbt source, still had no staging
model, still had no tests. The contract described a table the project could
still not read. PR #1 was closed unmerged and the capability rebuilt.

**What the AI does now.** Two forced tool calls, then four artifacts in one PR:

| Artifact | Written by | Why that side |
|---|---|---|
| `contracts/raw/ap_accrual.yml` v1 | model | descriptions and the key need reading the column names |
| entry in `sources.yml` | code | one line at the existing indent, no judgement |
| `stg_ap_accrual.sql` | model | which codes to `upper`, what to `trim`, what is really missing |
| tests in `staging.yml` | code | the contract already states the key and nullability |

**Verified by dry run.** The drafted model:

```sql
select
    accrual_id,
    upper(entity_code)   as entity_code,
    gl_account,
    period,
    accrual_amount,
    upper(currency_code) as currency_code,
    loaded_at
from {{ source('raw', 'AP_ACCRUAL') }}
```

It uppercased the two standardised code columns, left GL account, period and
amounts alone, and declined to wire a mart while saying where it thought the
table belonged. That last part is the designed boundary: where a new dataset
sits in the reporting layer has accounting consequences, so the PR says what the
agent thinks and stops.

**Merged.** `99760b0`, PR #7. All four artifacts are on `main`: the v1
contract, the `sources.yml` entry, `stg_ap_accrual.sql`, and seven tested columns
in `staging.yml`.

Getting there cost four defects, none of them in the model's output. Three were
in the publish path and one was in the test suite, all described in section 4.
The merged staging model uppercases the three code columns, including
`GL_ACCOUNT` which the dry run had left alone, and leaves period, amount and
timestamp untouched.

---

## 4. Where the AI got it wrong

A scenario register that only records successes is a brochure. Every one of
these was caught by deterministic code or by the gate, never by the model
noticing its own mistake.

| What the model did | Caught by | Outcome |
|---|---|---|
| Wrote `source('raw', 'ap_accrual')` in lower case | added check after the first live onboarding run | dbt resolves source names case-sensitively, so this parses and then fails to compile. Spelling is mechanical, so it is now rewritten to the canonical form rather than bounced back |
| Would have been free to drop a contracted column | `test_proposal_that_drops_a_column_is_rejected` | rejected before git |
| Would have been free to skip the column that raised the event | `test_proposal_that_ignores_the_new_column_is_rejected` | rejected. `verify()` re-runs `diff_dataset()`, the same function that raised the event, against the proposal. If the gap is not closed exactly, the draft is wrong by definition |
| Would have been free to bump the version wrongly | `test_unbumped_version_is_rejected`, `test_over_bumped_version_is_rejected` | rejected |
| Would have been free to point the staging model at another table | `test_staging_that_abandons_the_source_is_rejected` | rejected |

The design point: the model drafts, deterministic code decides. Verification
does not trust the draft, and would not trust a different model either.

**Defects found in the system's own code, by testing it:**

→ The column parser split the select list on every comma, so `nullif(trim(x), '')`
read as two columns. Now tracks bracket depth.

→ It stripped comments after splitting, so the prose comma inside a shield's
`-- shield: ... restored as NULL, see <issue>` swallowed the next column.
Comments are stripped first now.

→ An early verifier looked for the literal string `source('raw', 'AP_ACCRUAL')`,
so a model that omitted the space was rejected for reading the wrong table. True
about the string, false about the SQL.

→ `_run()` raised `CalledProcessError`, which prints the command and the exit
code but not the output. A failing `gh pr create` produced a 40-line traceback
that did not contain the reason. Failures now carry what the command said.

→ The agent refused a dirty working tree but not an unpushed one. Branching from
`origin/base` is right, and is what keeps unpushed local work out of a PR. The
missing half: when local `main` was three commits ahead of origin, the onboarding
branch was built on a base that lacked them, and the PR diff read as deleting
`docs/SCENARIOS.md` and reverting `control/onboard.py`. The agent now refuses to
publish until the base is pushed.

→ A publish that died after `git push` left its branch behind, so the retry
failed on `git checkout -b`. Branches are now recreated from `origin/base` and
force-pushed with a lease, which only ever overwrites the wreckage of an earlier
run of the same agent.

→ The agent's escalated pass re-shielded a column whose shield was already
merged, producing an identical file and an empty commit. It consulted the event
table, which says what was true when the event was raised, instead of the live
schema, which says what is true now. Both the "already installed" and the
"no longer diverges" cases are now skipped, with a reason printed.

→ `test_all_contracts_parse` asserted `len(contracts) == 8`. The first
successful onboarding PR added a ninth and failed the gate. A test that breaks
whenever the product succeeds is measuring the wrong property; it now derives
the count from the directory listing. The same fix exposed an owner check that
compared against the literal `"unassigned"` while the agent writes
`"unassigned-needs-review"`, so an ownerless dataset had been passing.

---

## 5. Current live state

As of the last `sync`, four events open or escalated:

| Dataset | Column | Status | Scenario |
|---|---|---|---|
| `RAW.AP_ACCRUAL` | (dataset) | PROPOSED → MERGED after `sync` | 07, PR #7 merged |
| `RAW.AP_PAYMENT` | `BANK_REF` | ESCALATED → DISMISSED after `sync` | 05 repaired by 08, issue #3 closed |
| `RAW.AR_INVOICE` | `REVENUE_STREAM` | PROPOSED | 02, adopt PR open on `drift/ar_invoice-v2` |
| `RAW.AR_INVOICE` | `STATUS` | ESCALATED → DISMISSED after `sync` | 06 repaired by 08, issue #4 closed |

Escalated drift stays open on purpose. A shield keeps the reports correct while
the source is fixed, and hides nothing from the control plane.

---

## 6. Not started

| Item | Status | Why it is not done |
|---|---|---|
| **Staged contract change (deprecation window)** | Designed, on hold | v+1 marks a shielded column deprecated, v+2 removes it, retiring the shield on a schedule instead of by hand. Held deliberately after the shields proved out |
| **Backfill planning** | Not started | Rewriting historical finance data automatically was ruled out of scope at the start, and should stay out |
| **Jira handoff for MEDIUM** | Not started | No Jira on this PoC. The MEDIUM path stops at a PR a human reviews |
| **Freshness enforcement** | Not started | Contracts declare a `freshness` block. Nothing reads it. A stale table that still has the right shape passes detection today |
| **Branch protection on `main`** | Not configured | Until it is, the agent refuses to enable auto-merge, and correctly says why. The LOW path needs one human click that it should not need |
| **One shield PR for all breaking datasets** | Not started | Two shields opened separately both fail the gate until the first merges, because the build is project-wide |
| **Live scenario for `DATASET_MISSING`** | Rule only | Dropping a contracted table live is destructive to the demo. The rule is unit tested |

---

## 7. Test inventory

134 tests, no Snowflake connection required, ~2s.

| File | Tests | Covers |
|---|---|---|
| `tests/test_detect.py` | 20 | every classification rule, fingerprint stability, dedupe against live statuses |
| `tests/test_onboard.py` | 27 | source entry, test generation, SQL column parsing, every `verify()` rejection |
| `tests/test_agent.py` | 26 | proposal verification, bundling, branch protection probe, GitHub outcome reconciliation, publish guards |
| `tests/test_lineage.py` | 13 | model and column lineage from the dbt manifest, confidence levels |
| `tests/test_shield.py` | 22 | shield planning, refusal to guess, staleness, retirement inverse and its refusals, shield idempotency |
| `tests/test_ui.py` | 6 | console action allowlist |
| `tests/test_quality.py` | 14 | check derivation from the contract, breach verdicts, fingerprint stability, one statement per table |
| `tests/test_contracts.py` | 6 | parsing, unique dataset keys, ownership, canonicalisation, hashing |

```
python -m pytest tests/ -q
```
