# fin_aiwh — Knowledge Transfer

AI enabled finance data warehouse for AP and AR subledger data.
Contract-first governance with an AI agent that proposes changes and a CI gate
that decides whether they ship.

| | |
|---|---|
| Repo | https://github.com/manojnayakgit/fin_aiwh |
| Warehouse | Snowflake, database `FIN_AIWH` |
| Status | Working proof of concept, verified end to end on a live account |
| Audience | A data engineer joining the project, and the stakeholders funding it |

---

## 1. What we are building, and why

A finance function's warehouse breaks quietly. An upstream ERP team adds a
column, widens a field, or changes a decimal place, tells nobody, and the
monthly pack is wrong for three weeks before anyone notices. The engineering
cost is not the fix, it is the investigation.

Most tools on the market fingerprint your tables and alert when a fingerprint
changes. That answers *did something change*. Nobody in finance is asking that.
They are asking **is anything we report now wrong, and who agreed to it**.

So this system works differently:

→ Every source dataset has a **contract** in version control: columns, types,
  nullability, keys, owner. It is the agreement, not a description.
→ The warehouse is compared **against the contract**, not against its own past.
→ Every divergence is classified by **what it breaks**, not by how unusual it looks.
→ An **AI agent** drafts the contract change. Deterministic code decides whether
  the draft is correct. A **CI gate** decides whether it ships.

The outcome we are aiming at is not drift alerts. It is removing the routine
half of data engineering: when upstream makes a safe change, the system adopts
it without a human; when upstream makes a dangerous one, the system stops the
release and says why in plain language.

### Severity is the whole design

| Level | Meaning | What happens |
|---|---|---|
| `LOW` | Additive. Nothing downstream is wrong. | Agent opens a PR, auto merges once CI is green. |
| `MEDIUM` | Needs a decision. Nothing is broken yet. | Agent opens a PR with a recommendation. A human decides. |
| `BREAKING` | Something is already wrong, or about to be. | No PR. A GitHub issue with evidence. Release gate fails. |

**The example to use with stakeholders.** A monetary column moves from
`NUMBER(18,2)` to `NUMBER(18,4)`. Nothing errors. No pipeline fails. dbt builds
green. Every total just stops agreeing with the subledger by a rounding margin
that compounds across a million invoices. This system classifies it `BREAKING`
and blocks the release. A fingerprint diff would have logged "type changed" and
moved on.

---

## 2. Architecture

```
  UPSTREAM ERP
       │  (lands data, changes shape without warning)
       ▼
┌──────────────────────────────────────────────────────────────┐
│ SNOWFLAKE  FIN_AIWH                                          │
│                                                              │
│  RAW ──────► STAGING ──────► MARTS        META               │
│  8 source    8 views         5 tables     control plane      │
│  tables                      AP/AR aging  contracts, events, │
│                              DSO/DPO      observed schema,   │
│                                           run log            │
│              └──── dbt, mart contracts enforced ────┘        │
└──────────────────────────────────────────────────────────────┘
       ▲                              │
       │ reads INFORMATION_SCHEMA     │ writes classified events
       │                              ▼
┌──────────────────────────────────────────────────────────────┐
│ CONTROL PLANE   python -m control.cli   (runs outside SF)    │
│                                                              │
│  detect ──► classify ──► agent ──► verify ──► PR / issue     │
│                            │         ▲                       │
│                       Claude API   same rules that           │
│                       drafts       raised the event          │
└──────────────────────────────────────────────────────────────┘
       │                                          │
       ▼                                          ▼
  contracts/raw/*.yml                     GITHUB
  the agreement of record                 PR ──► gate ──► merge
                                          issue for BREAKING
```

**Why the control plane sits outside Snowflake.** Governance that lives inside
the warehouse is owned by the warehouse vendor. Contracts, classification rules
and the audit trail are portable Python and YAML. Snowflake can be replaced
without renegotiating what the data is allowed to be.

**Why the model drafts but does not decide.** Claude proposes; deterministic
code judges the proposal against the same rules that raised the event, and a CI
gate decides whether it ships. The AI sits inside the loop, not in charge of it.
Section 3.4 has the mechanics.


---

## 3. How it works

Four mechanisms. Read this section and you can change the system safely.

### 3.1 A contract

`contracts/raw/ap_payment.yml`, trimmed:

```yaml
dataset: RAW.AP_PAYMENT
version: 1
owner: finance-data-engineering
classification: financial-restricted
primary_key:
  - PAYMENT_ID
freshness:
  column: LOADED_AT
  max_lag_hours: 24
columns:
  - name: PAYMENT_ID
    type: TEXT          # as Snowflake INFORMATION_SCHEMA reports it, not as DDL writes it
    length: 32
    nullable: false
    description: Surrogate payment key.
  - name: PAID_AMOUNT
    type: NUMBER
    precision: 18
    scale: 2
    nullable: false
```

**Why types are written the way `INFORMATION_SCHEMA` reports them.** A column
declared `VARCHAR(32)` in DDL comes back as `DATA_TYPE = 'TEXT'` with
`CHARACTER_MAXIMUM_LENGTH = 32`. Writing the contract in Snowflake's reporting
vocabulary means comparison is a direct field match with no translation layer
to get wrong. `seeds/emit.py` does this mapping once, at bootstrap, when it
generates both the RAW DDL and the v1 contracts from one spec.

Parsed by `control/contracts.py` into a `Contract` object. Parsing rejects
duplicate columns and a `primary_key` naming a column that does not exist.

### 3.2 Detection, step by step

`python -m control.cli detect` → `control/detect.py`

**Step 1. Read what the warehouse actually holds.**

```sql
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
       IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
FROM   FIN_AIWH.INFORMATION_SCHEMA.COLUMNS
WHERE  TABLE_SCHEMA = 'RAW'
ORDER  BY TABLE_NAME, ORDINAL_POSITION
```

One query, no table scans. Each row becomes an `ObservedColumn`. The result is
a dict: `{"RAW.AP_PAYMENT": {"PAYMENT_ID": ObservedColumn(...), ...}}`.

**Step 2. Snapshot it.** Every row is written to `META.OBSERVED_SCHEMA` against
a run id like `20260918T212712-893e161a`. This is the evidence. Six months from
now you can answer "what did this table look like on the 3rd" without guessing.

**Step 3. Diff, contract by contract.** `diff_all()` walks every registered
contract and calls `diff_dataset(contract, observed_columns)`:

```
for each column the contract declares:
    not present in the warehouse?        → COLUMN_REMOVED    (BREAKING)
    present: compare type                → _compare_type()   (see table below)
    present: compare nullability         → RELAXED / TIGHTENED

for each column the warehouse has that the contract does not declare:
    nullable?                            → COLUMN_ADDED      (LOW)
    NOT NULL?                            → COLUMN_ADDED      (MEDIUM)

contract exists but the table does not   → DATASET_MISSING   (BREAKING)
```

Then, after every contract is processed, any table in `RAW` with no contract at
all becomes `DATASET_UNGOVERNED` (MEDIUM).

`_compare_type()` is where the money is. Same base type is not enough:

| Comparison | Result |
|---|---|
| `TEXT` vs `NUMBER` | BREAKING, base type changed |
| `TEXT(32)` → `TEXT(64)` | LOW, widened |
| `TEXT(64)` → `TEXT(32)` | BREAKING, narrowed |
| `NUMBER(18,2)` → `NUMBER(18,4)` | BREAKING, scale moved |
| `NUMBER(18,2)` → `NUMBER(38,2)` | LOW, precision widened |
| `NUMBER(38,2)` → `NUMBER(18,2)` | BREAKING, precision narrowed |

Each divergence becomes a `Finding` carrying the before state, the after state,
the severity, and a sentence of reasoning written for a finance reader, not an
engineer. That sentence is what appears in the PR, the issue and the console.

**Step 4. Fingerprint.** Every finding gets a stable id:

```python
sha256(json([dataset_key, change_type, object_name, after_state]))[:32]
```

The same divergence always produces the same fingerprint. A different one
produces a different fingerprint. This is the idempotency key.

**Step 5. Persist, but only what is new.** Before inserting, the detector reads
every fingerprint already sitting in a live state:

```sql
SELECT EVENT_ID FROM FIN_AIWH.META.DRIFT_EVENT
WHERE STATUS IN ('OPEN', 'PROPOSED', 'ESCALATED')
```

Anything matching is skipped and counted. This is why a detector on a schedule
does not open a duplicate issue every run. The output says so explicitly:

```
run 20260918T212712-893e161a  scanned 9 datasets  found 4 divergences
  new 0  already being worked on 4
```

**Step 6. Log the run** to `META.RUN_LOG`, including runs that found nothing.
A quiet run is still proof the check happened.

**Exit code.** With `--fail-on-breaking`, the command exits `2` when any finding
is BREAKING. That is the single line CI depends on.

**Scoping.** `--dataset RAW.AP_INVOICE` limits both the contracts considered and
the observed schema, so a scoped run cannot report unrelated tables as
ungoverned. The gate uses this.

### 3.3 Registration

`python -m control.cli register` → `control/register.py`

A contract file changing on disk means nothing. Registration is what makes it
the agreement of record.

1. The contract is **canonicalised**: columns sorted by name, keys sorted,
   defaults filled in. Formatting, comments and column order cannot change the
   result.
2. `sha256` of that canonical form is the **spec hash**.
3. The hash is compared to `META.ACTIVE_CONTRACT` for that dataset.
   → same hash: nothing happens
   → different hash and `version` was not bumped: **refused**, with the
     dataset named
   → different hash and version bumped: a new row is inserted, carrying the
     version, the hash, the full parsed spec as JSON, and the current git SHA

Nothing is ever updated or deleted. `ACTIVE_CONTRACT` is a view picking the
highest version per dataset. A drift event raised in March can still be read
against the contract that was in force in March.

### 3.4 The agent

`python -m control.cli agent` → `control/agent.py`

**Step 1. Bundle.** Open events are grouped by dataset. Routing is decided by
the **worst** event in the group, because you cannot adopt half a dataset. A
dataset with one MEDIUM and one BREAKING event escalates entirely.

**Step 2. Route.**

| Worst severity | Action |
|---|---|
| BREAKING | `gh issue create` with a table of the evidence. No PR, ever. Events → `ESCALATED` |
| MEDIUM | draft → verify → PR, waits for a human. Events → `PROPOSED` |
| LOW | draft → verify → PR, auto merge if the branch is protected. Events → `PROPOSED` |

**Step 3. Draft.** Claude receives, in one message:

→ the drift events as JSON, with severities and reasoning
→ the live schema from `INFORMATION_SCHEMA` as JSON
→ the current contract file verbatim
→ the current dbt staging model, if one exists

and must reply through a **forced tool call** named `propose_contract_change`
with a fixed schema: `contract_yaml`, `staging_sql` (or null), `pr_title`,
`pr_body`, `reasoning`. Forcing the tool means the response is structured data.
There is no prose to parse, no markdown fences to strip, no "here is your
contract" preamble.

The system prompt constrains it: bump the version by exactly one, describe the
live schema exactly, never remove a live column, keep existing descriptions,
mark inferred descriptions as inferred, do not invent business rules.

**Step 4. Verify. This is the important part.** The draft is not trusted.
`verify()` rejects it if any of these hold:

→ the YAML does not parse
→ the dataset name changed
→ the version is not exactly old + 1 (or 1 for a brand new dataset)
→ any contracted column was dropped
→ the primary key changed
→ **the proposed contract still diverges from the live schema**
→ the staging model no longer reads from `source('raw', ...)`

That sixth check is the real guard. It re-runs `diff_dataset()` — the same
function that raised the event in the first place — against the proposed
contract. If the proposal does not close the gap completely, it is wrong by
definition, not by opinion. A rejected proposal is printed and skipped. Nothing
reaches git.

**Step 5. Publish.** `git fetch origin`, branch **from `origin/<base>`** (never
from local HEAD, or unpushed local commits get swept into the PR), write the
contract file and optionally the staging model, commit, push, `gh pr create`
with `drift` and severity labels.

Auto merge is enabled only when `repos/{owner}/{repo}/branches/<base>/protection`
reports required status check contexts. Without branch protection,
`gh pr merge --auto` merges as soon as a PR is mergeable, which means the gate
never runs. The agent detects this and says so rather than merging blind.

**Step 6. Mark.** Events move to `PROPOSED` or `ESCALATED` with the PR or issue
URL stored in `RESOLUTION_REF`.

### 3.5 Closing the loop: sync

`python -m control.cli sync` reads each `PROPOSED` or `ESCALATED` event's stored
URL and asks GitHub what happened.

| Reference | GitHub state | Event becomes |
|---|---|---|
| pull request | merged | `MERGED` |
| pull request | closed, not merged | `OPEN` again |
| pull request | open | unchanged |
| issue | closed | `DISMISSED` |
| issue | open | unchanged |

The closed-without-merging case matters. Rejecting a proposed contract does not
make the divergence go away, so the event returns to `OPEN` for triage instead
of vanishing.

Without `sync`, a merged PR would leave its events `PROPOSED` forever, and the
detector would stay permanently silent about that dataset.

### 3.6 The dbt layer

`RAW` → 8 staging views (trim, upper, coalesce tax, derive net amount) → 5 marts:

| Model | What |
|---|---|
| `fct_ap_open_items` | one row per AP invoice, paid vs outstanding, USD, aging bucket |
| `fct_ar_open_items` | same for AR |
| `agg_ap_aging`, `agg_ar_aging` | by entity and bucket |
| `kpi_dso_dpo` | DSO and DPO per entity, count back over trailing 90 days |

The two fact models have **enforced dbt contracts**: the output shape is
declared in `dbt/models/marts/marts.yml` and dbt refuses to build on mismatch.
Every amount is cast explicitly (`::number(18,2)`) because Snowflake widens
numeric types through arithmetic, and an uncast sum would arrive as
`number(38,2)` and fail the contract.

**A limitation worth understanding.** Those casts also mean the money-scale
scenario builds green. The mart contract protects the *output* shape; it cannot
know the *input* lost meaning. Only the source contract sees that. This is
exactly why the control plane exists, and it is worth saying out loud so nobody
assumes dbt is a second safety net for that class of change.

---

## 4. Tools, and where each is used

| Tool | Used for | Where |
|---|---|---|
| Snowflake | Warehouse, and the control plane's own tables | `FIN_AIWH.{RAW,STAGING,MARTS,META,CI}` |
| dbt (dbt-snowflake) | Transformation, tests, enforced mart contracts | `dbt/` |
| Python 3.11 | Control plane: detection, classification, agent, console | `control/` |
| Claude API | Drafting contract changes only | `control/agent.py` |
| GitHub | Contracts under review; PRs and issues are the work queue | `contracts/`, repo |
| GitHub Actions | The release gate | `.github/workflows/ci.yml` |
| pytest | Classification and agent rules, no warehouse needed | `tests/`, 52 tests |

**Model:** `claude-sonnet-4-5` by default, override with `ANTHROPIC_MODEL`.
The agent is the only component that calls it. Detection, classification,
verification, PR creation and the gate are all deterministic code.

---

## 5. Repository map

```
contracts/raw/       8 YAML contracts, one per source dataset. The agreement of record.
control/             the control plane
  config.py            .env loading, settings
  snow.py              Snowflake connection (key pair auth)
  contracts.py         parse, canonicalise, hash contracts
  detect.py            observe live schema, diff, classify, persist   ← the core
  register.py          publish contracts to META, append only
  load.py              stage and COPY seed CSVs into RAW
  agent.py             draft via Claude, verify, open PR or issue, sync with GitHub
  cli.py               every command
  ui.py + static/      the local console
dbt/                 13 models, 22 tests, mart contracts enforced
ops/sql/             00 bootstrap, 01 META DDL, 02 RAW DDL
ops/scenarios/       7 drift scenarios plus a reset, all re-runnable
seeds/               deterministic AP/AR data generator (~54k rows)
tests/               52 tests, no warehouse or API required
docs/KT.md           this document
docs/BUILD_LOG.md    how it was built and why, including defects found
```

### Snowflake objects

| Object | Purpose |
|---|---|
| `FIN_AIWH_WH` | XSMALL warehouse, auto suspend 60s |
| `FIN_AIWH.RAW` | Landed source data, contract governed |
| `FIN_AIWH.STAGING` | dbt staging views |
| `FIN_AIWH.MARTS` | dbt marts, what reports read |
| `FIN_AIWH.META` | Control plane tables |
| `FIN_AIWH.CI` | Ephemeral target for CI builds |
| `FIN_AIWH_ENG` | Role |
| `FIN_AIWH_SVC` | Service user, key pair auth, no password |

### Control plane tables (`FIN_AIWH.META`)

| Table | Holds | Note |
|---|---|---|
| `CONTRACT_REGISTRY` | Every registered contract version, hashed, tied to a git SHA | Append only. A drift event from last month can still be read against the contract in force then. |
| `OBSERVED_SCHEMA` | What the warehouse looked like on each run | Evidence, not just conclusions |
| `DRIFT_EVENT` | Every divergence, classified, with its reasoning | The work queue |
| `RUN_LOG` | One row per detector run | A quiet run is still proof something was checked |

Views: `ACTIVE_CONTRACT` (latest version per dataset), `OPEN_DRIFT` (what needs
attention, worst first).

### Source datasets

`AP_VENDOR`, `AP_INVOICE`, `AP_INVOICE_LINE`, `AP_PAYMENT`, `AR_CUSTOMER`,
`AR_INVOICE`, `AR_RECEIPT`, `FX_RATE`. Five legal entities, five currencies,
USD reporting.

---

## 6. Classification rules reference

Section 3.2 explains how these are applied. This is the full table, for when
you need to look one up or argue about a severity.

| Divergence | Severity | Why |
|---|---|---|
| Contracted table missing | BREAKING | Everything reading it fails |
| Table with no contract | MEDIUM | Ungoverned, cannot be modelled safely |
| Contracted column dropped | BREAKING | Anything selecting it fails. A dropped key also destroys row identity |
| New nullable column | LOW | Nothing breaks, the contract is just stale |
| New NOT NULL column | MEDIUM | Readers fine, writers unaware of it fail |
| Base type changed | BREAKING | Every cast and comparison is suspect |
| TEXT widened | LOW | Values still fit, downstream columns sized smaller will truncate |
| TEXT narrowed | BREAKING | Permitted values no longer fit |
| NUMBER scale changed | BREAKING | Monetary precision moved, totals disagree |
| NUMBER precision widened | LOW | Larger values now possible |
| NUMBER precision narrowed | BREAKING | Permitted values overflow |
| NOT NULL relaxed to nullable | BREAKING | Joins and aggregates assumed a value exists |
| Nullable tightened to NOT NULL | MEDIUM | Readers fine, loads carrying nulls start failing |

Rules live in `control/detect.py` and are tested in `tests/test_detect.py`
without a warehouse, so the logic that blocks a release is verifiable in a second.

**To change a severity**, edit `diff_dataset()` or `_compare_type()` in
`control/detect.py` and update the matching test. Nothing else needs touching:
the agent, the gate and the console all read severity from the event.

### Event lifecycle

```
                 ┌─────────► PROPOSED ──(PR merged)──► MERGED
   detect ──► OPEN                 ▲ │
                 │                 │ └──(PR closed unmerged)──┐
                 │                 └──────────────────────────┘
                 └─────────► ESCALATED ──(issue closed)──► DISMISSED
```

`OPEN`, `PROPOSED` and `ESCALATED` are live. The detector will not raise the
same divergence again while one of those exists, so a scheduled run does not
create duplicate issues. `MERGED` and `DISMISSED` are finished, so a
reappearance afterwards is genuinely new.

---

## 7. Setup from zero

### 7.1 Snowflake, once

Generate the key pair locally:

```bash
mkdir -p .secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out .secrets/fin_aiwh_rsa_key.p8 -nocrypt
openssl rsa -in .secrets/fin_aiwh_rsa_key.p8 -pubout -out .secrets/fin_aiwh_rsa_key.pub
chmod 600 .secrets/fin_aiwh_rsa_key.p8
```

In a Snowsight worksheet as `ACCOUNTADMIN`:

1. Run `ops/sql/00_bootstrap.sql` (warehouse, database, schemas, role, service user)
2. Attach the public key:

```sql
ALTER USER FIN_AIWH_SVC SET RSA_PUBLIC_KEY='<contents of .pub without BEGIN/END lines>';
```

### 7.2 Local environment

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill SNOWFLAKE_ACCOUNT and ANTHROPIC_API_KEY
```

`.env` (gitignored) is the single place connection settings live. Both the
control plane and dbt read it.

```
SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME
SNOWFLAKE_USER=FIN_AIWH_SVC
SNOWFLAKE_PRIVATE_KEY_PATH=.secrets/fin_aiwh_rsa_key.p8
SNOWFLAKE_ROLE=FIN_AIWH_ENG
SNOWFLAKE_WAREHOUSE=FIN_AIWH_WH
SNOWFLAKE_DATABASE=FIN_AIWH
ANTHROPIC_API_KEY=sk-ant-...
```

Also needed for the agent: `gh` installed and authenticated (`gh auth login`),
with push rights to the repo.

### 7.3 Build the warehouse

```bash
python -m control.cli ping                              # verify the connection
python -m control.cli apply ops/sql/01_meta_control_plane.sql
python -m control.cli apply ops/sql/02_raw_tables.sql
python seeds/generate.py                                # ~54k rows of AP/AR data
python -m control.cli load                              # stage and COPY into RAW
python -m control.cli register                          # publish the 8 contracts
python -m control.cli detect                            # expect: matches every contract
python -m control.cli dbt build                         # expect: PASS=35
```

A clean `detect` is the baseline. Everything after this is drift.

### 7.4 GitHub

Secrets, under Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `SNOWFLAKE_ACCOUNT` | the account identifier |
| `SNOWFLAKE_PRIVATE_KEY` | full contents of the `.p8`, BEGIN and END lines included |

Branch protection on `main`, requiring these status checks:
`rule tests`, `contracts match warehouse`, `dbt build (CI schema)`.

**This step is not optional.** `gh pr merge --auto` merges as soon as a PR is
mergeable. Without required checks, the gate never runs and auto merge is
decoration. The agent detects this and refuses to enable auto merge until it is
configured.

---

## 8. Running it

### Commands

| Command | Does |
|---|---|
| `ping` | Verify the Snowflake connection |
| `apply <file.sql>` | Run a SQL file. Refuses unqualified table DDL |
| `load` | Stage and COPY seed CSVs into RAW |
| `register` | Publish contracts to META. Append only, refuses an unbumped version |
| `detect` | Compare warehouse to contracts, classify, persist events |
| `status` | Active contracts and open drift |
| `agent` | Turn open drift into PRs and issues |
| `sync` | Read PR and issue outcomes back into the control plane |
| `resolve` | Close events by hand |
| `dbt <args>` | Run dbt with the control plane's connection settings |

Useful flags:

```bash
python -m control.cli detect --dry-run              # report only, write nothing
python -m control.cli detect --fail-on-breaking     # exit 2 on BREAKING. CI uses this
python -m control.cli detect --dataset RAW.AP_INVOICE   # scope to one dataset
python -m control.cli agent --dry-run               # draft and verify, touch nothing
python -m control.cli agent --no-merge              # PRs, but never auto merge
python -m control.cli sync --dry-run
```

### The operating cycle

```bash
python -m control.cli sync        # 1. what finished on GitHub
python -m control.cli register    # 2. merged contracts become the agreement of record
python -m control.cli detect      # 3. compare the warehouse against them
python -m control.cli agent       # 4. act on what is left
```

**The order matters.** Any other sequence either judges against stale contracts
or re-raises work already done. This is the sequence to put on a schedule.

### The console

```bash
python -m control.ui              # http://127.0.0.1:8765
```

One page: fire drift scenarios, run the control plane, watch events, contracts
and runs. Every button runs the same CLI command you would type and streams its
output, so there is no second implementation to drift out of sync. Actions are
allowlisted and it binds to loopback only.

### Demonstrating it in four clicks

1. `detect` → clean, the warehouse matches every contract
2. `04 money scale changed` → the quiet one
3. `detect` → two BREAKING events with the reasoning in plain language
4. `agent` → a PR for what is safe, an issue for what is not

### Drift scenarios

| File | Change | Expected verdict |
|---|---|---|
| `01_additive_column` | nullable column added | COLUMN_ADDED / LOW |
| `02_required_column_added` | NOT NULL column added | COLUMN_ADDED / MEDIUM |
| `03_type_widened` | VARCHAR(64) → VARCHAR(128) | TYPE_CHANGED / LOW |
| `04_money_scale_changed` | NUMBER(18,2) → NUMBER(18,4) | TYPE_CHANGED / BREAKING |
| `05_column_dropped` | contracted column removed | COLUMN_REMOVED / BREAKING |
| `06_nullability_relaxed` | NOT NULL dropped | NULLABILITY_RELAXED / BREAKING |
| `07_new_ungoverned_source` | new table, no contract | DATASET_UNGOVERNED / MEDIUM |
| `99_reset` | rebuild RAW to v1 | then `load`, then `resolve --all` |

All verified against the live account. All safe to run twice.

### Tests

```bash
python -m pytest tests -q          # 52 tests, no Snowflake, no API key
```

---

## 9. The release gate

`.github/workflows/ci.yml`, three jobs on every pull request:

| Job | Question |
|---|---|
| `rule tests` | Do the classification rules still hold? |
| `contracts match warehouse` | Do the contracts **this PR changes** match the live warehouse? |
| `dbt build (CI schema)` | Does dbt build with mart contracts enforced? |

Two design points worth knowing:

→ The gate **reads only**. It runs `detect --dry-run`. Persisting events is the
  scheduled detector's job; a gate that wrote would double count.
→ It is **scoped to the contracts the PR changes**. The job diffs against the
  base branch, reads the dataset key out of each changed contract file, and
  limits detection to those. A correct PR must not fail because of unrelated
  drift somewhere else in the warehouse.

Builds land in `FIN_AIWH.CI`, never in STAGING or MARTS.

---

## 10. State of play

### Working, verified on a live account

→ Control plane: 8 contracts registered, detection and classification correct on
  all 7 drift scenarios
→ dbt: 13 models, 22 tests, mart contracts enforced, full build green
→ Agent: drafts, is verified, opens PRs and issues, routes by severity correctly
→ Full cycle demonstrated: drift detected → agent PR → merged → registered →
  dataset clean at contract v2 → events reconciled to MERGED
→ Console, 52 tests, `sync` closing the lifecycle both ways

### Not done

→ **Branch protection is not configured.** Until it is, auto merge is disabled
  by the agent, so the LOW path needs a human to click merge.
→ **The gate has not yet been observed running.** The workflow and secrets are
  in place but no run has been watched go green or red.
→ **Nothing is scheduled.** The operating cycle is run by hand today.
→ **Jira handoff** for MEDIUM events, deliberately deferred.

### Known limits

→ dbt's own mart contracts will not catch a precision loss upstream, because the
  models cast explicitly. Only the input contract sees it. This is by design and
  is the point of the control plane, but it is worth stating so nobody assumes
  dbt is a second safety net for that class of change.
→ CI reuses `FIN_AIWH_SVC`. A production setup would give CI its own service
  user with a narrower role.
→ Seed data is synthetic. Volumes are demo scale, not several TB.

---

## 11. Where this goes next

**Near term**

1. Branch protection, then watch one full gated PR go green. This makes the
   LOW path genuinely autonomous.
2. Put the operating cycle on a schedule (`sync → register → detect → agent`).
3. Jira handoff so MEDIUM events land in the team's actual queue.

**The larger goal**

Drift adoption is the first and easiest slice. The same shape — propose,
verify deterministically, gate — extends to the rest of routine data
engineering: new source onboarding, model changes that follow a contract
change, test generation from contract constraints, backfill planning. The
value is not that an AI writes SQL. It is that every change an AI proposes is
checked by rules a human wrote, and blocked by a gate a human controls.

**On replacing Claude.** The agent is the only component calling a hosted
model, behind one function with a structured interface. Swapping it for a
self-hosted model is a contained change. Nothing else in the system knows or
cares which model drafted a proposal, because the verification does not trust
the draft either way.

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Could not connect to Snowflake backend` | Network or account identifier. Check `SNOWFLAKE_ACCOUNT` is `ORGNAME-ACCOUNTNAME`, and that the host is reachable |
| `refusing to apply: unqualified table names` | SQL uses a bare table name. Use `FIN_AIWH.<SCHEMA>.<TABLE>`. Unqualified DDL lands in the session's default schema |
| `contract content changed but version is still N` | Bump `version` in the contract file before registering |
| Agent reports `auto merge NOT enabled` | Branch protection on `main` requires no status checks. Configure it (section 7.4) |
| `working tree is not clean` from the agent | Commit or stash first. The agent branches from `origin/main` and needs a clean tree |
| Contract shows an old version in `status` | A merged contract is not in force until `register` runs |
| `detect` reports drift already handled | Expected. It shows `already being worked on N` and does not duplicate |
| dbt contract error on a fact model | A cast changed shape. Mart contracts in `dbt/models/marts/marts.yml` are enforced deliberately |
