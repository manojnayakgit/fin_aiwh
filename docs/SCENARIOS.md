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
| 02 | Required column added | `AR_INVOICE.REVENUE_STREAM` added NOT NULL | `COLUMN_ADDED` / MEDIUM | **Passed** | escalated with the AR_INVOICE bundle, issue #4 |
| 03 | Type widened | `AP_INVOICE.INVOICE_NUMBER` VARCHAR 64 → 128 | `TYPE_CHANGED` / LOW | **Passed** | PR #2, merged `2a2de28` |
| 04 | Money scale changed | `AP_INVOICE` amounts NUMBER(18,2) → (18,4) | `TYPE_CHANGED` / BREAKING | **Passed** | no PR by design, BREAKING never auto-adopts |
| 05 | Column dropped | `AP_PAYMENT.BANK_REF` removed | `COLUMN_REMOVED` / BREAKING | **Passed** | issue #3, shield PR #5, merged `72485a8` |
| 06 | Nullability relaxed | `AR_INVOICE.STATUS` NOT NULL dropped | `NULLABILITY_RELAXED` / BREAKING | **Passed** | issue #4, shield PR #6, merged `3a6c350` |
| 07 | New ungoverned source | `RAW.AP_ACCRUAL` created, no contract | `DATASET_UNGOVERNED` / MEDIUM | **Passed** (detection) / **Built, not merged** (onboarding) | PR #1 closed, **PR #7** open |
| — | Contracted table missing | table dropped entirely | `DATASET_MISSING` / BREAKING | **Rule only** | `test_missing_table_is_breaking` |
| — | Type narrowed | VARCHAR 128 → 64 | `TYPE_CHANGED` / BREAKING | **Rule only** | `test_narrowing_text_is_breaking` |
| — | Nullability tightened | nullable → NOT NULL | `NULLABILITY_TIGHTENED` / MEDIUM | **Rule only** | `test_tightened_nullability_is_medium` |

7 scenario scripts, all re-runnable, all fired live. 3 further rules covered by
unit test with no live script.

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

**Note.** The MEDIUM path in isolation has been proven by scenario 01's sibling
LOW path and by unit tests of bundling (`test_bundles_group_by_dataset_and_pick_worst`).
A MEDIUM event alone on a clean dataset has not been fired live.

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

### 07 — New ungoverned source · **Passed** (detection) / **Built, not merged** (onboarding)

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

**Status.** Live. **PR #7**, *"Onboard RAW.AP_ACCRUAL: contract, source,
staging model and tests"*, opened by the agent with all four artifacts.

The first publish attempt failed and exposed three defects in the publish path,
all fixed and described in section 4. The gate then failed the PR itself on a
test that asserted a hard contract count, which is a test that breaks whenever
onboarding succeeds. Also fixed. Awaiting review and merge.

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
| `RAW.AP_ACCRUAL` | (dataset) | OPEN, 2 events | 07, awaiting the onboarding PR |
| `RAW.AP_PAYMENT` | `BANK_REF` | ESCALATED | 05, issue #3 open, shielded |
| `RAW.AR_INVOICE` | `REVENUE_STREAM` | ESCALATED | 02, with the bundle |
| `RAW.AR_INVOICE` | `STATUS` | ESCALATED | 06, issue #4 open, shielded |

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
| **MEDIUM alone on a clean dataset** | Rule only | Every live MEDIUM so far has shared a dataset with a BREAKING event, so bundling routed it to escalation |

---

## 7. Test inventory

107 tests, no Snowflake connection required, ~2s.

| File | Tests | Covers |
|---|---|---|
| `tests/test_detect.py` | 20 | every classification rule, fingerprint stability, dedupe against live statuses |
| `tests/test_onboard.py` | 27 | source entry, test generation, SQL column parsing, every `verify()` rejection |
| `tests/test_agent.py` | 26 | proposal verification, bundling, branch protection probe, GitHub outcome reconciliation, publish guards |
| `tests/test_lineage.py` | 13 | model and column lineage from the dbt manifest, confidence levels |
| `tests/test_shield.py` | 9 | shield planning, refusal to guess, staleness detection |
| `tests/test_ui.py` | 6 | console action allowlist |
| `tests/test_contracts.py` | 6 | parsing, unique dataset keys, ownership, canonicalisation, hashing |

```
python -m pytest tests/ -q
```
