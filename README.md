# fin_aiwh

An AI enabled finance warehouse for AP and AR subledger data, built contract first.

**New here? Read [docs/KT.md](docs/KT.md).** It covers what this is, the
architecture, setup from zero, every command, and what is and is not done.
[docs/SCENARIOS.md](docs/SCENARIOS.md) is the evidence register: every failure
mode, how it was proven, what the agent did. [docs/BUILD_LOG.md](docs/BUILD_LOG.md)
is the chronological record of how it was built and why.
[docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md) is the rebuild:
every stage with the code that landed, the commands run, and every PR and
issue, generated from the repository itself.

The warehouse is Snowflake. The transformation layer is dbt. The part that
matters is neither of those: it is a control plane that sits outside both and
holds the agreement about what every dataset is allowed to look like.

## The idea in one paragraph

Most schema drift tooling fingerprints your tables, notices when a fingerprint
changes, and tells you something changed. That is a diff with a notification
attached, and it has been sold for a decade. It answers the wrong question.
The question a finance function actually has is not "did something change" but
"is anything we report now wrong, and who agreed to it". So here, every source
dataset has a contract in version control. The warehouse is compared against the
contract, not against its own past. Divergence is classified by what it breaks,
and the classification decides whether a change can be adopted automatically,
needs a decision, or has to stop a release.

## Severity, and what each level means

| Level | Meaning | What happens |
|---|---|---|
| `LOW` | Additive. Nothing downstream is wrong. | Agent proposes a contract bump, CI merges it. |
| `MEDIUM` | Needs a decision. Nothing is broken yet. | Agent opens a PR with a recommendation, a human decides. |
| `BREAKING` | Something is already wrong or about to be. | Release gate fails. No silent adoption, ever. |

The interesting cases are the quiet ones. A monetary column moving from
`NUMBER(18,2)` to `NUMBER(18,4)` throws no error anywhere. It just makes every
total disagree with the subledger by a rounding margin that compounds. That is
`BREAKING` here, and a fingerprint diff would have called it "type changed".

## Layout

```
contracts/raw/      one YAML contract per source dataset, the agreement of record
control/            the control plane: connection, contracts, registration, detection
ops/sql/            account bootstrap, META control plane DDL, RAW layer DDL
ops/scenarios/      drift scenarios you can fire at the warehouse on demand
seeds/              deterministic AP/AR seed data generator
dbt/                staging views, marts (AP/AR open items, aging, DSO/DPO), enforced contracts
tests/              classification rules, tested without a warehouse
```

## Control plane tables

| Table | Holds |
|---|---|
| `META.CONTRACT_REGISTRY` | Every registered contract version, hashed, tied to a git SHA. Append only. |
| `META.OBSERVED_SCHEMA` | What the warehouse actually looked like on each run. |
| `META.DRIFT_EVENT` | Every divergence, classified, with the reasoning kept. |
| `META.RUN_LOG` | Every detector execution, so a quiet run is still evidence. |

Registration never overwrites. A drift event raised last month can still be read
against the contract that was in force when it was raised.

## Setup

One time, in a Snowflake worksheet as `ACCOUNTADMIN`:

1. `ops/sql/00_bootstrap.sql` (warehouse, database, schemas, role, service user)
2. the generated key SQL in `.secrets/` (attaches the public key to that user)

Then locally:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in SNOWFLAKE_ACCOUNT

python -m control.cli ping
python -m control.cli apply ops/sql/01_meta_control_plane.sql
python -m control.cli apply ops/sql/02_raw_tables.sql
python seeds/generate.py
python -m control.cli load
python -m control.cli register
python -m control.cli detect
```

A clean run reports zero divergences. That is the baseline.

Then build the transformation layer:

```bash
python -m control.cli dbt build
```

`dbt build` runs models and tests together. Mart contracts in `dbt/models/marts/marts.yml`
are enforced, so a model whose output shape drifts from its declaration does not build.

## Firing a scenario

```bash
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect --fail-on-breaking
```

`detect` exits `2` when breaking drift is present, which is what CI gates on.

Note what dbt does with the same scenario: it builds green. The fact models
cast amounts to `number(18,2)`, so the mart contract holds while the input has
quietly lost meaning. A contract on the output cannot see that. Only the
contract on the input can, which is the whole argument for the control plane.

## The console

```bash
python -m control.ui          # http://127.0.0.1:8765
```

Fires scenarios, runs the detector and the agent, shows drift events and
contracts live. Every button runs the same CLI command you would type, and
streams its output.

## Tests

```bash
python -m pytest tests -q
```

The classification rules are pure functions and need no warehouse, so the logic
that decides whether a release is blocked is testable in a second.

## The agent

```bash
python -m control.cli agent --dry-run     # draft and verify, change nothing
python -m control.cli agent               # PRs for what is safe, issues for what is not
```

Claude drafts the contract change. The code decides whether it is right: the
proposal is re-run through the detector's own rules and rejected if it does not
close the gap exactly, bumps the version wrongly, or drops a column. LOW auto
merges once the gate is green, MEDIUM waits for a reviewer, BREAKING never gets
a PR at all.

Auto merge is only enabled when branch protection on the base branch actually
requires status checks. Without that, GitHub merges as soon as a PR is
mergeable, and the gate is decoration.

## Not yet built

Jira handoff for MEDIUM events, and marking PROPOSED events MERGED when their
pull request lands.
