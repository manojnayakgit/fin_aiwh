# fin_aiwh

An AI enabled finance warehouse for AP and AR subledger data, built contract first.

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

## Tests

```bash
python -m pytest tests -q
```

The classification rules are pure functions and need no warehouse, so the logic
that decides whether a release is blocked is testable in a second.

## Not yet built

Agent authoring of dbt changes and pull requests, the GitHub Actions gate, and
the Jira handoff. The control plane is the foundation those sit on.
