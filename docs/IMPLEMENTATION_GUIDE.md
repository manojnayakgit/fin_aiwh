
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

**Snowsight: `ops/sql/00_bootstrap.sql`**

```sql
-- fin_aiwh :: account bootstrap
-- Run once as ACCOUNTADMIN in a Snowflake worksheet.

USE ROLE ACCOUNTADMIN;

CREATE WAREHOUSE IF NOT EXISTS FIN_AIWH_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'fin_aiwh PoC compute';

CREATE DATABASE IF NOT EXISTS FIN_AIWH COMMENT = 'AI enabled finance warehouse PoC';

CREATE SCHEMA IF NOT EXISTS FIN_AIWH.RAW      COMMENT = 'Landed source data, contract governed';
CREATE SCHEMA IF NOT EXISTS FIN_AIWH.STAGING  COMMENT = 'dbt staging models';
CREATE SCHEMA IF NOT EXISTS FIN_AIWH.MARTS    COMMENT = 'dbt marts';
CREATE SCHEMA IF NOT EXISTS FIN_AIWH.META     COMMENT = 'Control plane: contracts, drift events, run log';
CREATE SCHEMA IF NOT EXISTS FIN_AIWH.CI       COMMENT = 'Ephemeral CI build target';

CREATE ROLE IF NOT EXISTS FIN_AIWH_ENG COMMENT = 'fin_aiwh engineering and automation role';

GRANT USAGE, OPERATE ON WAREHOUSE FIN_AIWH_WH TO ROLE FIN_AIWH_ENG;
GRANT USAGE ON DATABASE FIN_AIWH TO ROLE FIN_AIWH_ENG;
GRANT ALL ON SCHEMA FIN_AIWH.RAW     TO ROLE FIN_AIWH_ENG;
GRANT ALL ON SCHEMA FIN_AIWH.STAGING TO ROLE FIN_AIWH_ENG;
GRANT ALL ON SCHEMA FIN_AIWH.MARTS   TO ROLE FIN_AIWH_ENG;
GRANT ALL ON SCHEMA FIN_AIWH.META    TO ROLE FIN_AIWH_ENG;
GRANT ALL ON SCHEMA FIN_AIWH.CI      TO ROLE FIN_AIWH_ENG;
GRANT ALL ON FUTURE TABLES IN DATABASE FIN_AIWH TO ROLE FIN_AIWH_ENG;
GRANT ALL ON FUTURE VIEWS  IN DATABASE FIN_AIWH TO ROLE FIN_AIWH_ENG;

-- Service user with key pair auth. No password login.
CREATE USER IF NOT EXISTS FIN_AIWH_SVC
  DEFAULT_ROLE = FIN_AIWH_ENG
  DEFAULT_WAREHOUSE = FIN_AIWH_WH
  DEFAULT_NAMESPACE = FIN_AIWH.META
  TYPE = SERVICE
  COMMENT = 'fin_aiwh automation service user';

GRANT ROLE FIN_AIWH_ENG TO USER FIN_AIWH_SVC;
GRANT ROLE FIN_AIWH_ENG TO ROLE SYSADMIN;

-- Public key is injected by ops/sql/00_bootstrap_key.sql (generated locally, not committed).
```
**Commands: key pair, once**

```bash
mkdir -p .secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out .secrets/fin_aiwh_rsa_key.p8 -nocrypt
openssl rsa -in .secrets/fin_aiwh_rsa_key.p8 -pubout -out .secrets/fin_aiwh_rsa_key.pub
chmod 600 .secrets/fin_aiwh_rsa_key.p8
```
**Snowsight: attach the public key**

```sql
-- .secrets/00_bootstrap_key.sql   (generated, gitignored)
-- the public key body without its BEGIN/END lines
ALTER USER FIN_AIWH_SVC SET RSA_PUBLIC_KEY='MIIBIjANBg...';
```

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

**`requirements.txt`**

```
snowflake-connector-python==3.12.3
cryptography==43.0.1
pyyaml==6.0.2
python-dotenv==1.0.1
dbt-snowflake==1.8.4
rich==13.9.2
anthropic==0.40.0
```
**`.env.example`**

```
# Copy to .env and fill in. .env is gitignored.
SNOWFLAKE_ACCOUNT=xxxxxxx-yyyyyyy
SNOWFLAKE_USER=FIN_AIWH_SVC
SNOWFLAKE_PRIVATE_KEY_PATH=.secrets/fin_aiwh_rsa_key.p8
SNOWFLAKE_ROLE=FIN_AIWH_ENG
SNOWFLAKE_WAREHOUSE=FIN_AIWH_WH
SNOWFLAKE_DATABASE=FIN_AIWH
SNOWFLAKE_SCHEMA=META

# Drift agent
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```
**Commands**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m control.cli ping
```

`ping` prints account, user, role, warehouse, database and Snowflake version.

**Connection.** Key pair only. The session timezone is pinned to UTC (added in
Stage 25, shown here in its final form) so every `TIMESTAMP_NTZ` the CLI writes
and every clock it compares against agree.

**`control/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    account: str
    user: str
    private_key_path: Path
    private_key_passphrase: str | None
    role: str
    warehouse: str
    database: str
    schema: str

    @property
    def raw_schema(self) -> str:
        return "RAW"


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return v


def load_settings() -> Settings:
    key_path = Path(_req("SNOWFLAKE_PRIVATE_KEY_PATH"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    if not key_path.exists():
        raise SystemExit(f"private key not found at {key_path}")
    return Settings(
        account=_req("SNOWFLAKE_ACCOUNT"),
        user=_req("SNOWFLAKE_USER"),
        private_key_path=key_path,
        private_key_passphrase=os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        role=os.getenv("SNOWFLAKE_ROLE", "FIN_AIWH_ENG"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "FIN_AIWH_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "FIN_AIWH"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "META"),
    )
```
**`control/snow.py`**

```python
from contextlib import contextmanager

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from .config import Settings, load_settings


def _private_key_der(settings: Settings) -> bytes:
    pwd = (
        settings.private_key_passphrase.encode()
        if settings.private_key_passphrase
        else None
    )
    with open(settings.private_key_path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(), password=pwd, backend=default_backend()
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@contextmanager
def connect(settings: Settings | None = None, schema: str | None = None):
    s = settings or load_settings()
    conn = snowflake.connector.connect(
        account=s.account,
        user=s.user,
        private_key=_private_key_der(s),
        role=s.role,
        warehouse=s.warehouse,
        database=s.database,
        schema=schema or s.schema,
        client_session_keep_alive=False,
        # Every LOADED_AT is TIMESTAMP_NTZ, which carries no zone. Pinning the
        # session to UTC means "now" cast to NTZ, SYSDATE(), and the freshness
        # comparison all agree, whatever the account default is.
        session_parameters={"TIMEZONE": "UTC"},
    )
    try:
        yield conn
    finally:
        conn.close()


def query(conn, sql: str, params: tuple | dict | None = None) -> list[dict]:
    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def execute(conn, sql: str, params: tuple | dict | None = None) -> None:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    finally:
        cur.close()


def execute_script(conn, sql_text: str) -> int:
    """Run a multi statement SQL file. Returns the number of statements run."""
    count = 0
    for cur in conn.execute_string(sql_text):
        cur.close()
        count += 1
    return count
```

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

**Snowsight or `apply`: `ops/sql/01_meta_control_plane.sql` (final form)**

```sql
-- fin_aiwh :: control plane
-- The registry of record for what each dataset is contractually allowed to look like,
-- what it actually looks like, and every divergence between the two.

-- Every name is fully qualified so the file does not depend on session state.

-- Registered contract versions. Append only. A contract is never edited in place;
-- a change lands as a new version with a new hash, traceable to a git commit.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.CONTRACT_REGISTRY (
    CONTRACT_KEY      VARCHAR      NOT NULL,   -- e.g. RAW.AP_INVOICE
    VERSION           NUMBER       NOT NULL,
    SPEC_HASH         VARCHAR(64)  NOT NULL,   -- sha256 of the canonical YAML
    SPEC              VARIANT      NOT NULL,   -- full parsed contract
    OWNER             VARCHAR,
    CLASSIFICATION    VARCHAR,
    GIT_SHA           VARCHAR(40),
    REGISTERED_AT     TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    REGISTERED_BY     VARCHAR      NOT NULL DEFAULT CURRENT_USER(),
    IS_ACTIVE         BOOLEAN      NOT NULL DEFAULT TRUE
);

-- Point in time snapshot of what the warehouse actually holds.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.OBSERVED_SCHEMA (
    RUN_ID            VARCHAR      NOT NULL,
    OBSERVED_AT       TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    DATASET_KEY       VARCHAR      NOT NULL,
    COLUMN_NAME       VARCHAR      NOT NULL,
    ORDINAL_POSITION  NUMBER,
    DATA_TYPE         VARCHAR,
    IS_NULLABLE       BOOLEAN,
    NUMERIC_PRECISION NUMBER,
    NUMERIC_SCALE     NUMBER,
    CHARACTER_LENGTH  NUMBER
);

-- Every divergence between contract and reality, classified and triaged.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.DRIFT_EVENT (
    EVENT_ID          VARCHAR      NOT NULL,   -- unique per raise
    FINGERPRINT       VARCHAR(32),             -- what it is about; repeats across lifecycles
    RUN_ID            VARCHAR      NOT NULL,
    DETECTED_AT       TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    DATASET_KEY       VARCHAR      NOT NULL,
    CONTRACT_VERSION  NUMBER,
    CHANGE_TYPE       VARCHAR      NOT NULL,   -- COLUMN_ADDED, COLUMN_REMOVED, TYPE_CHANGED, ...
    SEVERITY          VARCHAR      NOT NULL,   -- LOW, MEDIUM, BREAKING
    OBJECT_NAME       VARCHAR,                 -- column or dataset the change applies to
    BEFORE_STATE      VARIANT,
    AFTER_STATE       VARIANT,
    RATIONALE         VARCHAR,                 -- why this severity, in plain language
    STATUS            VARCHAR      NOT NULL DEFAULT 'OPEN',  -- OPEN, PROPOSED, MERGED, DISMISSED
    RESOLVED_AT       TIMESTAMP_NTZ,
    RESOLUTION_REF    VARCHAR,                 -- PR url once the agent acts on it
    IMPACT            VARIANT                  -- what breaks downstream, from dbt lineage
);

-- One row per detector execution, so runs are auditable even when nothing drifted.
CREATE TABLE IF NOT EXISTS FIN_AIWH.META.RUN_LOG (
    RUN_ID            VARCHAR      NOT NULL,
    STARTED_AT        TIMESTAMP_NTZ NOT NULL,
    FINISHED_AT       TIMESTAMP_NTZ,
    RUN_TYPE          VARCHAR      NOT NULL,   -- REGISTER, DETECT
    DATASETS_SCANNED  NUMBER,
    EVENTS_RAISED     NUMBER,
    STATUS            VARCHAR,                 -- SUCCESS, FAILED
    DETAIL            VARIANT
);

-- Convenience view: the active contract for each dataset.
CREATE OR REPLACE VIEW FIN_AIWH.META.ACTIVE_CONTRACT AS
SELECT * EXCLUDE (RN) FROM (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY CONTRACT_KEY ORDER BY VERSION DESC) AS RN
    FROM FIN_AIWH.META.CONTRACT_REGISTRY r
    WHERE IS_ACTIVE
) WHERE RN = 1;

-- Convenience view: what needs a human or an agent right now.
CREATE OR REPLACE VIEW FIN_AIWH.META.OPEN_DRIFT AS
SELECT DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME, RATIONALE,
       IMPACT:marts AS AFFECTED_MARTS, IMPACT:models AS AFFECTED_MODELS,
       DETECTED_AT, EVENT_ID
FROM FIN_AIWH.META.DRIFT_EVENT
WHERE STATUS = 'OPEN'
ORDER BY CASE SEVERITY WHEN 'BREAKING' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, DETECTED_AT;
```
**Commands**

```bash
python -m control.cli apply ops/sql/01_meta_control_plane.sql
```

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

**`contracts/raw/ap_invoice.yml` as generated (version 1)**

```yaml
# fin_aiwh data contract
#
# This file is the agreement, not a description. The warehouse is compared
# against it on every detector run. Changing it is a reviewed pull request.

dataset: RAW.AP_INVOICE
version: 1
owner: finance-data-engineering
classification: financial-restricted
description: Accounts payable invoice header.

primary_key:
  - INVOICE_ID

freshness:
  column: LOADED_AT
  max_lag_hours: 24

# Columns the contract guarantees. A column present here and missing in the
# warehouse is a break. A column in the warehouse and missing here is
# ungoverned and must be adopted or rejected explicitly.
columns:
  - name: INVOICE_ID
    type: TEXT
    length: 32
    nullable: false
    description: Surrogate invoice key from the ERP.
  - name: VENDOR_ID
    type: TEXT
    length: 32
    nullable: false
    description: References AP_VENDOR.VENDOR_ID.
  - name: ENTITY_CODE
    type: TEXT
    length: 8
    nullable: false
    description: Legal entity booking the liability.
  - name: INVOICE_NUMBER
    type: TEXT
    length: 64
    nullable: false
    description: Vendor's own invoice number.
  - name: INVOICE_DATE
    type: DATE
    nullable: false
    description: Date on the invoice document.
  - name: DUE_DATE
    type: DATE
    nullable: false
    description: Contractual payment due date.
  - name: CURRENCY_CODE
    type: TEXT
    length: 3
    nullable: false
    description: ISO 4217 transaction currency.
  - name: GROSS_AMOUNT
    type: NUMBER
    precision: 18
    scale: 2
    nullable: false
    description: Invoice total including tax, transaction currency.
  - name: TAX_AMOUNT
    type: NUMBER
    precision: 18
    scale: 2
    nullable: true
    description: Tax portion of the gross amount.
  - name: STATUS
    type: TEXT
    length: 16
    nullable: false
    description: OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.
  - name: SOURCE_SYSTEM
    type: TEXT
    length: 32
    nullable: false
    description: Originating ERP instance.
  - name: LOADED_AT
    type: TIMESTAMP_NTZ
    nullable: false
    description: Ingestion watermark.
```
**Commands**

```bash
python seeds/emit.py
python -m control.cli apply ops/sql/02_raw_tables.sql
```

One table from the generated DDL, for shape. All eight follow the same
pattern and every name is three part.

**`ops/sql/02_raw_tables.sql`, one of eight**

```sql
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate invoice key from the ERP.',
    VENDOR_ID       VARCHAR(32)   NOT NULL  COMMENT 'References AP_VENDOR.VENDOR_ID.',
    ENTITY_CODE     VARCHAR(8)    NOT NULL  COMMENT 'Legal entity booking the liability.',
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL  COMMENT 'Vendor''s own invoice number.',
    INVOICE_DATE    DATE          NOT NULL  COMMENT 'Date on the invoice document.',
    DUE_DATE        DATE          NOT NULL  COMMENT 'Contractual payment due date.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 transaction currency.',
    GROSS_AMOUNT    NUMBER(18,2)  NOT NULL  COMMENT 'Invoice total including tax, transaction currency.',
    TAX_AMOUNT      NUMBER(18,2)   COMMENT 'Tax portion of the gross amount.',
    STATUS          VARCHAR(16)   NOT NULL  COMMENT 'OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.',
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL  COMMENT 'Originating ERP instance.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts payable invoice header.';
```

---

## Stage 5. Seed data and load

**Commit:** `55135f0`, `load` rewritten at `473fd7f` and `82f657d` (shown in final form)

**Why deterministic.** Seed `20260918`, same data every run, so a scenario is
reproducible. Data is deliberately imperfect the way finance data is: partial
payments, disputes, missing cost centres, near-duplicate vendor names.

**Volumes.** 220 vendors, 310 customers, 8,000 AP invoices, ~20k lines, ~5.6k
payments, 9,000 AR invoices, ~6k receipts, ~4.9k FX rates. Five legal
entities, five currencies, USD reporting.

**Commands**

```bash
python seeds/generate.py
python -m control.cli load
```

`load` in its final form. Two things were learned the hard way in Stage 26:
it copies by named column so a table that has moved past the seed still loads,
and it refuses before truncating when it cannot succeed.

**`control/load.py`**

```python
"""Load generated seed CSVs into RAW via an internal stage."""
import csv
from pathlib import Path

from .config import ROOT
from .snow import execute, query

SEED_OUT = ROOT / "seeds" / "out"
STAGE = "FIN_AIWH.RAW.SEED_STAGE"

FILE_FORMAT = (
    "TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
    "NULL_IF = ('') EMPTY_FIELD_AS_NULL = TRUE TRIM_SPACE = FALSE"
)


def _live_columns(conn, table: str) -> list[dict]:
    return query(
        conn,
        "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM FIN_AIWH.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'RAW' AND TABLE_NAME = %(t)s ORDER BY ORDINAL_POSITION",
        {"t": table},
    )


def plan_load(csv_columns: list[str], live: list[dict]) -> tuple[list[str], str | None]:
    """Which columns to copy, or why this table cannot be loaded from this file.

    The seed is the contract's version 1 shape. The live table may have moved
    on: an adopted column is not in the CSV and must come through as NULL. A
    NOT NULL column with no default that the CSV does not carry cannot be
    loaded at all, and that has to be known before anything is truncated.
    """
    csv_set = {c.upper() for c in csv_columns}
    live_names = {r["COLUMN_NAME"].upper() for r in live}
    unknown = sorted(csv_set - live_names)
    if unknown:
        return [], f"CSV has columns the table does not: {unknown}"
    blocked = sorted(
        r["COLUMN_NAME"] for r in live
        if r["COLUMN_NAME"].upper() not in csv_set
        and r["IS_NULLABLE"] == "NO" and r["COLUMN_DEFAULT"] is None
    )
    if blocked:
        return [], (f"table has NOT NULL columns with no default that the seed does not "
                    f"carry: {blocked}. Reset the table to its contracted shape first")
    return [c.upper() for c in csv_columns], None


def load_all(conn, tables: list[str] | None = None) -> list[tuple[str, int | str]]:
    """Load every seed CSV. Returns (table, rows) or (table, reason it was skipped)."""
    execute(conn, f"CREATE STAGE IF NOT EXISTS {STAGE} FILE_FORMAT = ({FILE_FORMAT})")
    files = sorted(SEED_OUT.glob("*.csv"))
    if not files:
        raise SystemExit("no CSVs in seeds/out/. Run: python seeds/generate.py")

    results: list[tuple[str, int | str]] = []
    for f in files:
        table = f.stem.upper()
        if tables and table not in tables:
            continue
        with f.open(newline="") as fh:
            header = next(csv.reader(fh))
        live = _live_columns(conn, table)
        if not live:
            results.append((table, "skipped: table does not exist in RAW"))
            continue
        cols, why = plan_load(header, live)
        if why:
            results.append((table, f"skipped: {why}"))
            continue

        execute(
            conn,
            f"PUT 'file://{f.as_posix()}' @{STAGE} OVERWRITE = TRUE AUTO_COMPRESS = TRUE",
        )
        execute(conn, f"TRUNCATE TABLE IF EXISTS FIN_AIWH.RAW.{table}")
        # Explicit column list, positional from the CSV. Any live column the
        # seed does not carry arrives as NULL, which plan_load has already
        # confirmed is allowed.
        selects = ", ".join(f"${i + 1}" for i in range(len(cols)))
        execute(
            conn,
            f"COPY INTO FIN_AIWH.RAW.{table} ({', '.join(cols)}) "
            f"FROM (SELECT {selects} FROM @{STAGE}/{f.name}.gz) "
            f"FILE_FORMAT = ({FILE_FORMAT}) ON_ERROR = 'ABORT_STATEMENT'",
        )
        # The seed carries a fixed LOADED_AT. A load time that never moves would
        # make every table read as stale a day after loading, so it is stamped
        # with the actual load time.
        if "LOADED_AT" in cols:
            execute(conn, f"UPDATE FIN_AIWH.RAW.{table} SET LOADED_AT = SYSDATE()::TIMESTAMP_NTZ")
        n = query(conn, f"SELECT COUNT(*) AS C FROM FIN_AIWH.RAW.{table}")[0]["C"]
        results.append((table, n))
    return results
```

---

## Stage 6. Register contracts

**Commit:** `55135f0`

The control plane only judges against what has been published. Registration
hashes the canonical form of each contract (column order and defaults
normalised) and inserts a version row. Re-registering an unchanged contract
does nothing. Changing content without bumping `version` is refused.

**`control/register.py`**

```python
"""Publishing contracts into the control plane.

Registration is append only. Editing a contract and re-registering creates a new
version row; the old one stays readable, so any drift event from last month can
still be read against the contract that was in force at the time.
"""
import json
import subprocess

from .contracts import Contract, load_contracts
from .snow import query, execute


def git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return None


def registered_hashes(conn) -> dict[str, tuple[int, str]]:
    rows = query(
        conn,
        "SELECT CONTRACT_KEY, VERSION, SPEC_HASH FROM META.ACTIVE_CONTRACT",
    )
    return {r["CONTRACT_KEY"]: (r["VERSION"], r["SPEC_HASH"]) for r in rows}


def register(conn, contracts: list[Contract] | None = None) -> dict:
    contracts = contracts if contracts is not None else load_contracts()
    existing = registered_hashes(conn)
    sha = git_sha()
    added, unchanged, updated = [], [], []

    for c in contracts:
        h = c.spec_hash()
        prev = existing.get(c.dataset)
        if prev and prev[1] == h:
            unchanged.append(c.dataset)
            continue
        if prev and c.version <= prev[0]:
            raise SystemExit(
                f"{c.dataset}: contract content changed but version is still "
                f"{c.version}. Bump the version field before registering."
            )
        execute(
            conn,
            """
            INSERT INTO META.CONTRACT_REGISTRY
              (CONTRACT_KEY, VERSION, SPEC_HASH, SPEC, OWNER, CLASSIFICATION, GIT_SHA)
            SELECT %(key)s, %(version)s, %(hash)s, TRY_PARSE_JSON(%(spec)s),
                   %(owner)s, %(classification)s, %(sha)s
            """,
            {
                "key": c.dataset,
                "version": c.version,
                "hash": h,
                "spec": json.dumps(c.canonical()),
                "owner": c.owner,
                "classification": c.classification,
                "sha": sha,
            },
        )
        (updated if prev else added).append(c.dataset)

    return {"added": added, "updated": updated, "unchanged": unchanged}
```
**Commands**

```bash
python -m control.cli register
```

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

**`control/contracts.py`**

```python
"""Loading, canonicalising and hashing data contracts."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT

CONTRACT_DIR = ROOT / "contracts"


@dataclass
class ContractColumn:
    name: str
    type: str
    nullable: bool
    description: str = ""
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def signature(self) -> str:
        """Human readable type signature, used in drift messages."""
        if self.type == "TEXT" and self.length:
            return f"TEXT({self.length})"
        if self.type == "NUMBER" and self.precision is not None:
            return f"NUMBER({self.precision},{self.scale})"
        return self.type


@dataclass
class Contract:
    dataset: str                 # e.g. RAW.AP_INVOICE
    version: int
    owner: str
    classification: str
    description: str
    primary_key: list[str]
    freshness: dict
    columns: list[ContractColumn]
    source_path: Path | None = None
    raw: dict = field(default_factory=dict)

    @property
    def schema(self) -> str:
        return self.dataset.split(".")[0]

    @property
    def table(self) -> str:
        return self.dataset.split(".")[1]

    def column(self, name: str) -> ContractColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    def canonical(self) -> dict:
        """Ordering and defaults normalised, so the hash only moves on real change."""
        return {
            "dataset": self.dataset,
            "version": self.version,
            "owner": self.owner,
            "classification": self.classification,
            "primary_key": sorted(self.primary_key),
            "freshness": dict(sorted(self.freshness.items())),
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "length": c.length,
                    "precision": c.precision,
                    "scale": c.scale,
                    "nullable": c.nullable,
                }
                for c in sorted(self.columns, key=lambda c: c.name)
            ],
        }

    def spec_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def parse_contract(path: Path) -> Contract:
    data = yaml.safe_load(path.read_text())
    missing = [k for k in ("dataset", "version", "columns") if k not in data]
    if missing:
        raise ValueError(f"{path.name}: missing required keys {missing}")
    cols = [
        ContractColumn(
            name=c["name"],
            type=c["type"],
            nullable=bool(c.get("nullable", True)),
            description=c.get("description", ""),
            length=c.get("length"),
            precision=c.get("precision"),
            scale=c.get("scale"),
        )
        for c in data["columns"]
    ]
    names = [c.name for c in cols]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{path.name}: duplicate columns {sorted(dupes)}")
    pk = data.get("primary_key", [])
    unknown_pk = [k for k in pk if k not in names]
    if unknown_pk:
        raise ValueError(f"{path.name}: primary_key references unknown columns {unknown_pk}")
    return Contract(
        dataset=data["dataset"].upper(),
        version=int(data["version"]),
        owner=data.get("owner", "unassigned"),
        classification=data.get("classification", "internal"),
        description=data.get("description", ""),
        primary_key=pk,
        freshness=data.get("freshness", {}),
        columns=cols,
        source_path=path,
        raw=data,
    )


def load_contracts(directory: Path | None = None) -> list[Contract]:
    d = directory or CONTRACT_DIR
    paths = sorted(d.rglob("*.yml")) + sorted(d.rglob("*.yaml"))
    return [parse_contract(p) for p in paths]
```
**`control/detect.py` `ObservedColumn`, `Finding`, `OBSERVE_SQL`, `fetch_observed`, `_compare_type`, `diff_dataset`, `diff_all`**

```python
@dataclass
class ObservedColumn:
    name: str
    type: str
    nullable: bool
    ordinal: int
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def signature(self) -> str:
        if self.type == "TEXT" and self.length:
            return f"TEXT({self.length})"
        if self.type == "NUMBER" and self.precision is not None:
            return f"NUMBER({self.precision},{self.scale})"
        return self.type


@dataclass
class Finding:
    dataset_key: str
    change_type: str
    severity: str
    object_name: str | None
    before: dict | None
    after: dict | None
    rationale: str
    impact: Impact | None = None

    def fingerprint(self) -> str:
        blob = json.dumps(
            [self.dataset_key, self.change_type, self.object_name, self.after],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


OBSERVE_SQL = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
       IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
FROM   %(db)s.INFORMATION_SCHEMA.COLUMNS
WHERE  TABLE_SCHEMA = %%(schema)s
ORDER  BY TABLE_NAME, ORDINAL_POSITION
"""


def fetch_observed(conn, database: str, schema: str) -> dict[str, dict[str, ObservedColumn]]:
    rows = query(conn, OBSERVE_SQL % {"db": database}, {"schema": schema})
    out: dict[str, dict[str, ObservedColumn]] = {}
    for r in rows:
        key = f"{r['TABLE_SCHEMA']}.{r['TABLE_NAME']}"
        out.setdefault(key, {})[r["COLUMN_NAME"]] = ObservedColumn(
            name=r["COLUMN_NAME"],
            type=r["DATA_TYPE"],
            nullable=(r["IS_NULLABLE"] == "YES"),
            ordinal=r["ORDINAL_POSITION"],
            length=r["CHARACTER_MAXIMUM_LENGTH"],
            precision=r["NUMERIC_PRECISION"],
            scale=r["NUMERIC_SCALE"],
        )
    return out


def _compare_type(c: ContractColumn, o: ObservedColumn) -> tuple[str, str] | None:
    """Return (severity, rationale) when the type diverges, else None."""
    if c.type != o.type:
        return (
            BREAKING,
            f"base type changed from {c.type} to {o.type}; every downstream cast "
            f"and comparison on this column is now suspect",
        )

    if c.type == "TEXT" and c.length is not None and o.length is not None:
        if o.length > c.length:
            return (
                LOW,
                f"widened from {c.signature()} to {o.signature()}; existing values "
                f"still fit, but downstream columns sized to {c.length} will truncate",
            )
        if o.length < c.length:
            return (
                BREAKING,
                f"narrowed from {c.signature()} to {o.signature()}; values the "
                f"contract permits no longer fit",
            )

    if c.type == "NUMBER":
        if c.scale is not None and o.scale is not None and c.scale != o.scale:
            return (
                BREAKING,
                f"scale changed from {c.signature()} to {o.signature()}; monetary "
                f"precision moved, reconciliations and totals will disagree",
            )
        if c.precision is not None and o.precision is not None:
            if o.precision > c.precision:
                return (
                    LOW,
                    f"widened from {c.signature()} to {o.signature()}; larger values "
                    f"now possible than downstream models were sized for",
                )
            if o.precision < c.precision:
                return (
                    BREAKING,
                    f"narrowed from {c.signature()} to {o.signature()}; values the "
                    f"contract permits will overflow",
                )
    return None


def diff_dataset(contract: Contract, observed: dict[str, ObservedColumn] | None) -> list[Finding]:
    key = contract.dataset

    if observed is None:
        return [
            Finding(
                dataset_key=key,
                change_type="DATASET_MISSING",
                severity=BREAKING,
                object_name=None,
                before={"exists": True},
                after={"exists": False},
                rationale="a contracted dataset is not present in the warehouse; "
                          "every model reading it will fail",
            )
        ]

    findings: list[Finding] = []
    contract_names = {c.name for c in contract.columns}

    for c in contract.columns:
        o = observed.get(c.name)
        if o is None:
            in_pk = c.name in contract.primary_key
            findings.append(
                Finding(
                    dataset_key=key,
                    change_type="COLUMN_REMOVED",
                    severity=BREAKING,
                    object_name=c.name,
                    before={"type": c.signature(), "nullable": c.nullable},
                    after=None,
                    rationale=(
                        "a primary key column was dropped; row identity is gone and "
                        "incremental models cannot merge"
                        if in_pk
                        else "a contracted column was dropped; anything selecting it fails"
                    ),
                )
            )
            continue

        t = _compare_type(c, o)
        if t:
            sev, why = t
            findings.append(
                Finding(
                    dataset_key=key,
                    change_type="TYPE_CHANGED",
                    severity=sev,
                    object_name=c.name,
                    before={"type": c.signature(), "nullable": c.nullable},
                    after={"type": o.signature(), "nullable": o.nullable},
                    rationale=why,
                )
            )

        if c.nullable != o.nullable:
            if not c.nullable and o.nullable:
                findings.append(
                    Finding(
                        dataset_key=key,
                        change_type="NULLABILITY_RELAXED",
                        severity=BREAKING,
                        object_name=c.name,
                        before={"nullable": False},
                        after={"nullable": True},
                        rationale="the contract guarantees this column is never null and "
                                  "downstream joins and aggregates rely on that; nulls are "
                                  "now permitted",
                    )
                )
            else:
                findings.append(
                    Finding(
                        dataset_key=key,
                        change_type="NULLABILITY_TIGHTENED",
                        severity=MEDIUM,
                        object_name=c.name,
                        before={"nullable": True},
                        after={"nullable": False},
                        rationale="upstream now rejects nulls here; safe for readers, but "
                                  "loads carrying nulls will start failing",
                    )
                )

    for name, o in observed.items():
        if name in contract_names:
            continue
        findings.append(
            Finding(
                dataset_key=key,
                change_type="COLUMN_ADDED",
                severity=MEDIUM if not o.nullable else LOW,
                object_name=name,
                before=None,
                after={"type": o.signature(), "nullable": o.nullable},
                rationale=(
                    "a new NOT NULL column appeared; any writer unaware of it will fail"
                    if not o.nullable
                    else "a new nullable column appeared; nothing breaks, but it is "
                         "ungoverned until the contract adopts it"
                ),
            )
        )

    return findings


def diff_all(
    contracts: list[Contract], observed: dict[str, dict[str, ObservedColumn]]
) -> list[Finding]:
    findings: list[Finding] = []
    contracted = set()

    for c in contracts:
        contracted.add(c.dataset)
        findings.extend(diff_dataset(c, observed.get(c.dataset)))

    for key in sorted(observed):
        if key in contracted:
            continue
        findings.append(
            Finding(
                dataset_key=key,
                change_type="DATASET_UNGOVERNED",
                severity=MEDIUM,
                object_name=None,
                before=None,
                after={"columns": len(observed[key])},
                rationale="a table exists in the landing zone with no contract; it is "
                          "outside review and cannot be safely modelled",
            )
        )

    order = {BREAKING: 0, MEDIUM: 1, LOW: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.dataset_key, f.object_name or ""))
```

`persist()`, `active_fingerprints()` and `event_id()` are shown at the stages
that shaped them (Stages 17 and 27).

**Commands**

```bash
python -m control.cli detect
python -m control.cli detect --dry-run
python -m control.cli detect --fail-on-breaking
python -m control.cli status
```

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

**`control/cli.py` `cmd_ping`, `_DDL`, `_unqualified_ddl`, `cmd_apply`, `cmd_detect`, `main`**

```python
def cmd_ping(_args):
    s = load_settings()
    with connect(s) as conn:
        r = query(
            conn,
            "SELECT CURRENT_ACCOUNT() A, CURRENT_USER() U, CURRENT_ROLE() R, "
            "CURRENT_WAREHOUSE() W, CURRENT_DATABASE() D, CURRENT_VERSION() V",
        )[0]
    console.print("[green]connected[/green]")
    for k, label in [("A", "account"), ("U", "user"), ("R", "role"),
                     ("W", "warehouse"), ("D", "database"), ("V", "snowflake")]:
        console.print(f"  {label:<10} {r[k]}")
    return 0


_DDL = re.compile(
    r"\b(CREATE(?:\s+OR\s+REPLACE)?\s+(?:TRANSIENT\s+|TEMPORARY\s+)?TABLE|ALTER\s+TABLE|DROP\s+TABLE)"
    r"(?:\s+IF\s+(?:NOT\s+)?EXISTS)?\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)


def _unqualified_ddl(sql: str) -> list[str]:
    """Table DDL that names fewer than three parts depends on session state."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    return [
        f"{m.group(1)} {m.group(2)}"
        for m in _DDL.finditer(stripped)
        if m.group(2).count(".") < 2
    ]


def cmd_apply(args):
    path = ROOT / args.path
    if not path.exists():
        console.print(f"[red]no such file:[/red] {args.path}")
        return 1
    sql = path.read_text()
    bad = _unqualified_ddl(sql)
    if bad:
        console.print("[red]refusing to apply: unqualified table names in DDL[/red]")
        for b in bad:
            console.print(f"  {b}")
        console.print("[dim]use FIN_AIWH.<SCHEMA>.<TABLE> so the statement cannot land in the wrong schema[/dim]")
        return 1
    with connect() as conn:
        n = execute_script(conn, sql)
    console.print(f"[green]applied[/green] {args.path} ({n} statements)")
    return 0


def cmd_detect(args):
    started = datetime.now(timezone.utc)
    run_id = new_run_id()
    contracts = load_contracts()
    s = load_settings()

    if args.dataset:
        wanted = {d.upper() for d in args.dataset}
        contracts = [c for c in contracts if c.dataset in wanted]
        if not contracts:
            console.print(f"[red]no contracts match {sorted(wanted)}[/red]")
            return 1

    with connect(s) as conn:
        observed = fetch_observed(conn, s.database, s.raw_schema)
        if args.dataset:
            # scoped run: only judge the named datasets, never flag others as ungoverned
            observed = {k: v for k, v in observed.items() if k in wanted}
        findings = diff_all(contracts, observed)

        # Content, by the same contracts. Only tables that exist and match are
        # measured: a DMF on a column that was dropped upstream would just fail.
        content: list = []
        if not args.no_quality:
            # A DMF on a dropped column fails; on a relaxed one it is exactly
            # the question worth asking. Exclude only what cannot be measured.
            unmeasurable = {f.dataset_key for f in findings
                            if f.change_type in ("DATASET_MISSING", "COLUMN_REMOVED", "TYPE_CHANGED")}
            measurable = [c for c in contracts if c.dataset in observed and c.dataset not in unmeasurable]
            try:
                content = quality_mod.check_all(conn, measurable)
            except Exception as e:  # noqa: BLE001
                # Snowflake puts the code on line 1 and the reason on line 2.
                reason = " ".join(l.strip() for l in str(e).splitlines()[:3] if l.strip())
                console.print(f"[yellow]content checks skipped:[/yellow] {reason}")
                console.print("[dim]run ops/20_quality.sql once as ACCOUNTADMIN, or pass --no-quality[/dim]")
        findings += content

    lineage = Lineage.load()
    attach_impact(findings, lineage)

    with connect(s) as conn:
        if not args.dry_run:
            snapshot_observed(conn, run_id, observed)
            written, suppressed = persist(conn, run_id, findings, contracts)
            _log_run(conn, run_id, "DETECT", started, len(observed), written, "SUCCESS")
        else:
            written = suppressed = 0

    console.print(
        f"run [bold]{run_id}[/bold]  scanned {len(observed)} datasets  "
        f"found {len(findings)} divergences"
        + ("  [dim](dry run, nothing written)[/dim]" if args.dry_run
           else f"  new {written}"
                + (f"  [dim]already being worked on {suppressed}[/dim]" if suppressed else ""))
    )

    schema_only = [f for f in findings if f.change_type not in quality_mod.QUALITY_TYPES]
    active = {(f.dataset_key.split(".")[-1], (f.object_name or "").upper()) for f in schema_only}
    for table, col, note in shield_mod.stale(active):
        console.print(f"[yellow]stale shield:[/yellow] stg_{table.lower()} {col} no longer "
                      f"diverges, the shield can be removed  [dim]{note}[/dim]")

    if not findings:
        console.print("[green]warehouse matches every registered contract[/green]")
        return 0

    table = Table(show_lines=False, header_style="bold")
    for col in ("severity", "dataset", "change", "object", "breaks", "why"):
        table.add_column(col, overflow="fold")
    for f in findings:
        table.add_row(
            f"[{SEV_STYLE[f.severity]}]{f.severity}[/]",
            f.dataset_key, f.change_type, f.object_name or "-",
            f.impact.summary() if f.impact else "-",
            f.rationale,
        )
    console.print(table)

    if not lineage.available:
        console.print("[yellow]no dbt manifest, so downstream impact is unknown.[/yellow] "
                      "[dim]run: python -m control.cli dbt parse[/dim]")
    else:
        worst = [f for f in findings if f.severity == BREAKING and f.impact and not f.impact.empty]
        if worst:
            reports = sorted({r["label"] for f in worst for r in f.impact.reports})
            marts = sorted({m for f in worst for m in f.impact.marts})
            if reports:
                console.print(f"\n[bold red]reports affected by breaking drift:[/bold red] "
                              + ", ".join(reports))
            if marts:
                console.print(f"[red]marts:[/red] " + ", ".join(marts))

    if args.fail_on_breaking and any(f.severity == BREAKING for f in findings):
        console.print("[bold red]breaking drift present[/bold red]")
        return 2
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="fin-aiwh", description="fin_aiwh control plane")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="verify the Snowflake connection").set_defaults(fn=cmd_ping)

    a = sub.add_parser("apply", help="run a SQL file from the repo")
    a.add_argument("path")
    a.set_defaults(fn=cmd_apply)

    l = sub.add_parser("load", help="load seed CSVs into RAW")
    l.add_argument("tables", nargs="*", help="limit to these tables")
    l.set_defaults(fn=cmd_load)

    sub.add_parser("register", help="publish contracts to the control plane").set_defaults(
        fn=cmd_register
    )

    d = sub.add_parser("detect", help="compare the warehouse against registered contracts")
    d.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    d.add_argument("--fail-on-breaking", action="store_true", help="exit 2 on breaking drift")
    d.add_argument("--dataset", action="append",
                   help="limit to these datasets, e.g. RAW.AP_INVOICE (repeatable)")
    d.add_argument("--no-quality", action="store_true",
                   help="schema only, skip the content checks (the gate uses this)")
    d.set_defaults(fn=cmd_detect)

    sub.add_parser("status", help="contracts and open drift").set_defaults(fn=cmd_status)

    g = sub.add_parser("agent", help="turn open drift into PRs or escalations")
    g.add_argument("--dry-run", action="store_true", help="draft and verify, touch nothing")
    g.add_argument("--no-merge", action="store_true", help="never enable auto merge, even for LOW")
    g.add_argument("--dataset", help="only this dataset, e.g. RAW.AP_INVOICE")
    g.set_defaults(fn=cmd_agent)

    y = sub.add_parser("sync", help="reconcile event status with GitHub")
    y.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    y.set_defaults(fn=cmd_sync)

    r = sub.add_parser("resolve", help="close open drift events")
    r.add_argument("event", nargs="*", help="event ids to close")
    r.add_argument("--all", action="store_true", help="close every OPEN event")
    r.add_argument("--status", default="DISMISSED", choices=["DISMISSED", "MERGED", "PROPOSED", "ESCALATED"])
    r.add_argument("--ref", default=None, help="PR url or ticket that resolved it")
    r.set_defaults(fn=cmd_resolve)

    b = sub.add_parser("dbt", help="run dbt with the control plane's connection settings")
    b.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed to dbt")
    b.set_defaults(fn=cmd_dbt)

    args = p.parse_args(argv)
    return args.fn(args)
```

---

## Stage 9. Rule tests

**Commit:** `55135f0`

The rules that decide whether a release is blocked are pure functions. They
are tested without a warehouse, in under a second. 21 tests at this stage;
145 by the end.

**Commands**

```bash
python -m pytest tests -q
```
**`tests/test_detect.py`, four of twenty**

```python
def test_scale_change_on_money_is_breaking(contract):
    cols = live(contract, GROSS_AMOUNT=obs("GROSS_AMOUNT", "NUMBER", False, None, 18, 4))
    f = only(diff_dataset(contract, cols), "TYPE_CHANGED")[0]
    assert f.severity == BREAKING
    assert "precision" in f.rationale


def test_relaxed_nullability_is_breaking(contract):
    cols = live(contract, INVOICE_ID=obs("INVOICE_ID", nullable=True))
    f = only(diff_dataset(contract, cols), "NULLABILITY_RELAXED")[0]
    assert f.severity == BREAKING


def test_new_nullable_column_is_low(contract):
    cols = live(contract)
    cols["APPROVER_ID"] = obs("APPROVER_ID", nullable=True, ordinal=9)
    f = only(diff_dataset(contract, cols), "COLUMN_ADDED")[0]
    assert f.severity == LOW


def test_fingerprint_is_stable_and_specific(contract):
    a = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=64)))[0]
    b = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=64)))[0]
    c = diff_dataset(contract, live(contract, INVOICE_ID=obs("INVOICE_ID", length=128)))[0]
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
```

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

**`dbt/dbt_project.yml`**

```yaml
name: fin_aiwh
version: "0.1.0"
config-version: 2
profile: fin_aiwh

model-paths: ["models"]
macro-paths: ["macros"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  fin_aiwh:
    +persist_docs:
      relation: true
      columns: true
    staging:
      +schema: STAGING
      +materialized: view
    marts:
      +schema: MARTS
      +materialized: table
```
**`dbt/profiles.yml`**

```yaml
# Reads the same .env the control plane uses. Run dbt through:
#   python -m control.cli dbt <args>
# which loads .env and resolves the key path before handing off.
fin_aiwh:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      private_key_path: "{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      role: "{{ env_var('SNOWFLAKE_ROLE', 'FIN_AIWH_ENG') }}"
      warehouse: "{{ env_var('SNOWFLAKE_WAREHOUSE', 'FIN_AIWH_WH') }}"
      database: "{{ env_var('SNOWFLAKE_DATABASE', 'FIN_AIWH') }}"
      schema: STAGING
      threads: 4
    ci:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      private_key_path: "{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      role: "{{ env_var('SNOWFLAKE_ROLE', 'FIN_AIWH_ENG') }}"
      warehouse: "{{ env_var('SNOWFLAKE_WAREHOUSE', 'FIN_AIWH_WH') }}"
      database: "{{ env_var('SNOWFLAKE_DATABASE', 'FIN_AIWH') }}"
      schema: CI
      threads: 4
```
**`dbt/macros/generate_schema_name.sql`**

```sql
{#- Use the configured schema as is. dbt's default would produce STAGING_STAGING. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if target.name == 'ci' -%}
        {{ target.schema }}
    {%- elif custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
```
**`dbt/macros/aging_bucket.sql`**

```sql
{#- Standard finance aging buckets from days past due. -#}
{% macro aging_bucket(days_past_due) -%}
    case
        when {{ days_past_due }} <= 0  then 'CURRENT'
        when {{ days_past_due }} <= 30 then '1-30'
        when {{ days_past_due }} <= 60 then '31-60'
        when {{ days_past_due }} <= 90 then '61-90'
        else '90+'
    end
{%- endmacro %}
```
**`dbt/models/staging/sources.yml` (final form, includes AP_ACCRUAL from Stage 22)**

```yaml
version: 2

sources:
  - name: raw
    database: FIN_AIWH
    schema: RAW
    description: Landed source data. Shape is governed by contracts/raw/, not by this file.
    config:
      loaded_at_field: LOADED_AT
      freshness:
        warn_after: {count: 24, period: hour}
        error_after: {count: 48, period: hour}
    tables:
      - name: AP_VENDOR
      - name: AP_INVOICE
      - name: AP_INVOICE_LINE
      - name: AP_PAYMENT
      - name: AR_CUSTOMER
      - name: AR_INVOICE
      - name: AR_RECEIPT
      - name: FX_RATE
        config:
          freshness: null
      - name: AP_ACCRUAL
```
**`dbt/models/staging/stg_ap_invoice.sql` as first written**

```sql
select
    invoice_id,
    vendor_id,
    entity_code,
    invoice_number,
    invoice_date,
    due_date,
    upper(currency_code)      as currency_code,
    gross_amount,
    coalesce(tax_amount, 0)   as tax_amount,
    gross_amount - coalesce(tax_amount, 0) as net_amount,
    upper(status)             as status,
    source_system,
    loaded_at
from {{ source('raw', 'AP_INVOICE') }}
```
**`dbt/models/staging/stg_fx_rate.sql`**

```sql
-- One closing rate per currency per day into USD, the reporting currency.
select
    rate_date,
    upper(from_currency) as from_currency,
    rate::number(18,8)   as rate_to_usd
from {{ source('raw', 'FX_RATE') }}
where upper(to_currency) = 'USD'
  and upper(rate_type) = 'CLOSING'

union all

-- USD to USD is identity, so joins never lose USD invoices.
select distinct rate_date, 'USD', 1.0::number(18,8)
from {{ source('raw', 'FX_RATE') }}
```
**`dbt/models/marts/fct_ap_open_items.sql`**

```sql
-- One row per non cancelled AP invoice with what has been paid against it
-- and what remains. Reporting currency is USD at the closing rate on the invoice date.
-- Types are cast explicitly because the mart contract in marts.yml is enforced.
with inv as (
    select * from {{ ref('stg_ap_invoice') }}
    where status <> 'CANCELLED'
),
app as (
    select invoice_id,
           sum(paid_amount) as applied_amount,
           max(payment_date)  as last_applied_date
    from {{ ref('stg_ap_payment') }}
    group by invoice_id
),
fx as (
    select * from {{ ref('stg_fx_rate') }}
),
joined as (
    select
        inv.*,
        coalesce(app.applied_amount, 0)                     as applied_amount,
        inv.gross_amount - coalesce(app.applied_amount, 0)  as outstanding_amount,
        app.last_applied_date,
        fx.rate_to_usd,
        greatest(0, datediff('day', inv.due_date, current_date())) as days_past_due
    from inv
    left join app on app.invoice_id = inv.invoice_id
    left join fx  on fx.from_currency = inv.currency_code and fx.rate_date = inv.invoice_date
)
select
    invoice_id,
    vendor_id,
    entity_code,
    invoice_number,
    invoice_date,
    due_date,
    currency_code,
    status,
    gross_amount::number(18,2)                              as gross_amount,
    applied_amount::number(18,2)                            as paid_amount,
    outstanding_amount::number(18,2)                        as outstanding_amount,
    last_applied_date                                       as last_payment_date,
    rate_to_usd::number(18,8)                               as rate_to_usd,
    round(gross_amount * rate_to_usd, 2)::number(18,2)      as gross_amount_usd,
    round(outstanding_amount * rate_to_usd, 2)::number(18,2) as outstanding_amount_usd,
    days_past_due::number(9,0)                              as days_past_due,
    ({{ aging_bucket('days_past_due') }})::varchar(7)       as aging_bucket,
    loaded_at
from joined
```
**`dbt/models/marts/kpi_dso_dpo.sql`**

```sql
-- Working capital KPIs per entity, as of today.
-- DSO = AR outstanding / (AR invoiced in trailing 90 days / 90)
-- DPO = AP outstanding / (AP invoiced in trailing 90 days / 90)
-- Count back method, the common management reporting convention. Not GAAP.
with ar as (
    select
        entity_code,
        sum(outstanding_amount_usd) as ar_outstanding_usd,
        sum(case when invoice_date >= dateadd('day', -90, current_date())
                 then gross_amount_usd else 0 end) as ar_invoiced_90d_usd
    from {{ ref('fct_ar_open_items') }}
    group by 1
),
ap as (
    select
        entity_code,
        sum(outstanding_amount_usd) as ap_outstanding_usd,
        sum(case when invoice_date >= dateadd('day', -90, current_date())
                 then gross_amount_usd else 0 end) as ap_invoiced_90d_usd
    from {{ ref('fct_ap_open_items') }}
    group by 1
)
select
    coalesce(ar.entity_code, ap.entity_code)              as entity_code,
    current_date()                                        as as_of_date,
    ar.ar_outstanding_usd,
    ap.ap_outstanding_usd,
    round(ar.ar_outstanding_usd / nullif(ar.ar_invoiced_90d_usd / 90, 0), 1) as dso_days,
    round(ap.ap_outstanding_usd / nullif(ap.ap_invoiced_90d_usd / 90, 0), 1) as dpo_days
from ar
full outer join ap on ap.entity_code = ar.entity_code
```
**`dbt/tests/assert_fx_one_rate_per_day.sql`**

```sql
-- Two closing rates for the same currency on the same day would double count
-- every invoice in that currency after the join. Fails if any exist.
select rate_date, from_currency, count(*) as n
from {{ ref('stg_fx_rate') }}
group by 1, 2
having count(*) > 1
```
**`dbt/tests/assert_no_invoice_without_fx.sql`**

```sql
-- An invoice with no FX rate silently drops out of every USD total.
select invoice_id, currency_code, invoice_date
from {{ ref('fct_ap_open_items') }}
where rate_to_usd is null
union all
select invoice_id, currency_code, invoice_date
from {{ ref('fct_ar_open_items') }}
where rate_to_usd is null
```
**`control/cli.py` `cmd_dbt`**

```python
def cmd_dbt(args):
    """Run dbt with .env loaded and the key path made absolute."""
    s = load_settings()
    env = dict(os.environ)
    env.update({
        "SNOWFLAKE_ACCOUNT": s.account,
        "SNOWFLAKE_USER": s.user,
        "SNOWFLAKE_PRIVATE_KEY_PATH": str(s.private_key_path),
        "SNOWFLAKE_ROLE": s.role,
        "SNOWFLAKE_WAREHOUSE": s.warehouse,
        "SNOWFLAKE_DATABASE": s.database,
    })
    dbt_dir = ROOT / "dbt"
    cmd = ["dbt", *args.dbt_args, "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.call(cmd, env=env, cwd=dbt_dir)
```
**Commands**

```bash
python -m control.cli dbt build
python -m control.cli dbt build -t ci
python -m control.cli dbt test
```

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

**Commands: the quiet one**

```bash
python -m control.cli apply ops/scenarios/04_money_scale_changed.sql
python -m control.cli detect --fail-on-breaking
python -m control.cli dbt build
python -m control.cli status
```

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

**`control/cli.py` `cmd_resolve`**

```python
def cmd_resolve(args):
    """Close open drift events once the warehouse or the contract has been fixed."""
    if not args.all and not args.event:
        console.print("[red]pass --all or one or more event ids[/red]")
        return 1
    where = "STATUS = 'OPEN'" if args.all else "EVENT_ID IN (%s)" % ",".join(
        f"'{e}'" for e in args.event
    )
    with connect() as conn:
        n = query(conn, f"SELECT COUNT(*) AS C FROM META.DRIFT_EVENT WHERE {where}")[0]["C"]
        execute(
            conn,
            f"UPDATE META.DRIFT_EVENT SET STATUS = %(st)s, RESOLVED_AT = SYSDATE(), "
            f"RESOLUTION_REF = %(ref)s WHERE {where}",
            {"st": args.status, "ref": args.ref},
        )
    console.print(f"[green]{n} event(s) marked {args.status}[/green]")
    return 0
```
**Commands**

```bash
python -m control.cli resolve --all
python -m control.cli resolve <event_id> --status MERGED --ref https://github.com/manojnayakgit/fin_aiwh/pull/2
```

**Day one closed here:** foundation, contracts, detection, dbt layer, seven
scenarios, all live and verified against the real account.

---

# Part B. Automation

## Stage 13. The release gate

**Commits:** `b3eb4b3` (first workflow, arrived inside PR #2), `202b695` (scoped to changed contracts), `f2c1d21` (informational when nothing changed), `a9512af` (`--no-quality`)

A contract change is a pull request. The gate answers three questions on
every PR and blocks the merge if any answer is no.

| Job | Question | How |
|---|---|---|
| `rule tests` | do the classification rules still hold | `pytest tests` |
| `contracts match warehouse` | do the contracts **this PR changes** match the live warehouse | `detect --dry-run --fail-on-breaking --no-quality --dataset ...` |
| `dbt build (CI schema)` | does dbt build with mart contracts enforced | `dbt build -t ci` into `FIN_AIWH.CI` |

| Design point | Why |
|---|---|
| Reads only | persisting events is the scheduled detector's job; a gate that wrote would double count |
| Scoped to changed contracts | diffs against base, reads `dataset` from each changed file. A correct PR must not fail on unrelated drift |
| Informational when nothing changed | the gate judges the change, never the warehouse. Every run was red for a day because of this |
| Schema only | a duplicate key in RAW is real, but this PR did not cause it |
| CI schema | PR builds never touch STAGING or MARTS |

**Secrets** (repo Settings, Secrets, Actions): `SNOWFLAKE_ACCOUNT`,
`SNOWFLAKE_PRIVATE_KEY` (full `.p8` content), `ANTHROPIC_API_KEY`,
`AGENT_GH_TOKEN` (a PAT: events caused by the built-in `GITHUB_TOKEN` do not
trigger workflows, so a PR the agent opens under it would never be gated).

**`.github/workflows/ci.yml` (final form)**

```yaml
# fin_aiwh release gate
#
# Every pull request is judged twice:
#   1. Do the contracts THIS CHANGE TOUCHES match what the warehouse actually holds?
#      A PR that adopts a LOW change passes. A PR that ignores a BREAKING one fails.
#      Drift on datasets the change does not touch is the scheduled detector's
#      problem, not this change's. A change that touches no contracts gets an
#      informational detect run and cannot fail on it: the gate judges the change,
#      never the state of the warehouse.
#   2. Does the dbt project build against the CI schema with mart contracts enforced?
#
# Nothing here writes to META. Persisting drift is the scheduled detector's job,
# the gate only reads.
name: gate

on:
  pull_request:
  push:
    branches: [main]

concurrency:
  group: gate-${{ github.ref }}
  cancel-in-progress: true

env:
  SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
  SNOWFLAKE_USER: FIN_AIWH_SVC
  SNOWFLAKE_ROLE: FIN_AIWH_ENG
  SNOWFLAKE_WAREHOUSE: FIN_AIWH_WH
  SNOWFLAKE_DATABASE: FIN_AIWH
  SNOWFLAKE_SCHEMA: META
  SNOWFLAKE_PRIVATE_KEY_PATH: .secrets/ci_rsa_key.p8

jobs:
  rules:
    name: rule tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11", cache: pip}
      - run: pip install -r requirements.txt pytest
      - run: python -m pytest tests -q

  contracts:
    name: contracts match warehouse
    runs-on: ubuntu-latest
    needs: rules
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: actions/setup-python@v5
        with: {python-version: "3.11", cache: pip}
      - run: pip install -r requirements.txt
      - name: materialise private key
        run: |
          mkdir -p .secrets
          printf '%s' "${{ secrets.SNOWFLAKE_PRIVATE_KEY }}" > .secrets/ci_rsa_key.p8
          chmod 600 .secrets/ci_rsa_key.p8
      - run: python -m control.cli ping
      - run: python -m control.cli dbt parse --no-version-check
      - name: scope to contracts changed in this PR
        id: scope
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            base="origin/${{ github.base_ref }}"
          else
            base="${{ github.event.before }}"
          fi
          changed=$(git diff --name-only "$base"...HEAD -- contracts/ | grep -E '\.ya?ml$' || true)
          args=""
          for f in $changed; do
            ds=$(python -c "import yaml,sys; print(yaml.safe_load(open('$f'))['dataset'])")
            args="$args --dataset $ds"
          done
          echo "args=$args" >> "$GITHUB_OUTPUT"
          echo "scope: ${args:-all contracts}"
      - name: detect against the contracts this change touches
        run: |
          args="${{ steps.scope.outputs.args }}"
          if [ -z "$args" ]; then
            echo "::notice::no contract files changed. detect is informational; breaking drift elsewhere does not fail this change."
            python -m control.cli detect --dry-run --no-quality
          else
            python -m control.cli detect --dry-run --fail-on-breaking --no-quality $args
          fi

  build:
    name: dbt build (CI schema)
    runs-on: ubuntu-latest
    needs: contracts
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11", cache: pip}
      - run: pip install -r requirements.txt
      - name: materialise private key
        run: |
          mkdir -p .secrets
          printf '%s' "${{ secrets.SNOWFLAKE_PRIVATE_KEY }}" > .secrets/ci_rsa_key.p8
          chmod 600 .secrets/ci_rsa_key.p8
      - run: python -m control.cli dbt build -t ci --no-version-check
```

The gate was first observed green on PR #5, a shield pull request the system
wrote itself: three checks passed.

---

## Stage 14. The drift agent

**Commit:** `b3eb4b3` (arrived inside PR #2, see the bug below), `3c5c0bf` (branch from origin, auto merge guard)

**What it does.** Reads OPEN events, groups them by dataset, routes by the
worst event in the group. You cannot adopt half a dataset.

| Worst | Action |
|---|---|
| LOW | model drafts a contract bump, code verifies, PR opened, auto merge once the gate is green |
| MEDIUM | same draft and verify, PR opened, waits for a human |
| BREAKING | no PR. GitHub issue with the evidence, events marked ESCALATED |

**The model drafts, the code decides.** Claude receives the events, the live
schema, the current contract and the staging model, and returns a proposal
through a forced tool call. There is no free text to parse. Then `verify()`
rejects the proposal, without asking the model again, if any of these hold:

→ the YAML does not parse
→ the dataset name changed
→ version is not exactly old + 1 (or 1 for a new dataset)
→ any contracted column was dropped
→ the primary key changed
→ the proposal still diverges from the live schema: `verify()` re-runs `diff_dataset()`, the function that raised the event, on the proposal
→ the staging model no longer reads from `source('raw', ...)`

**`control/agent.py` `Bundle`, `Proposal`, `open_events`, `bundle_events`, `TOOL_SCHEMA`, `SYSTEM`, `build_prompt`, `draft`, `verify`**

```python
@dataclass
class Bundle:
    """Every open event for one dataset, plus what the agent needs to reason."""
    dataset_key: str
    events: list[dict]
    contract: Contract | None
    observed: dict[str, ObservedColumn]
    lineage: Lineage | None = None

    def impact_for(self, event: dict):
        """What this one event breaks downstream."""
        if self.lineage is None:
            return None
        col = event.get("OBJECT_NAME") if event.get("CHANGE_TYPE") not in (
            "DATASET_MISSING", "DATASET_UNGOVERNED") else None
        return self.lineage.impact(self.dataset_key, col)

    def dataset_impact(self):
        """Everything downstream of the dataset, regardless of column."""
        return self.lineage.impact(self.dataset_key) if self.lineage else None

    @property
    def worst(self) -> str:
        return max((e["SEVERITY"] for e in self.events), key=SEV_ORDER.get)

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[1]

    @property
    def event_ids(self) -> list[str]:
        return [e["EVENT_ID"] for e in self.events]


@dataclass
class Proposal:
    bundle: Bundle
    contract_yaml: str
    pr_title: str
    pr_body: str
    reasoning: str
    staging_sql: str | None = None
    contract: Contract | None = None          # parsed, set by verify()
    errors: list[str] = field(default_factory=list)
    merge_note: str = "awaiting review"

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def contract_path(self) -> Path:
        return CONTRACT_DIR / "raw" / f"{self.bundle.table.lower()}.yml"

    @property
    def staging_path(self) -> Path:
        return ROOT / "dbt" / "models" / "staging" / f"stg_{self.bundle.table.lower()}.sql"

    @property
    def branch(self) -> str:
        v = self.contract.version if self.contract else "x"
        return f"drift/{self.bundle.table.lower()}-v{v}"


def open_events(conn) -> list[dict]:
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, DETECTED_AT
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS = 'OPEN'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def bundle_events(
    events: list[dict],
    contracts: list[Contract],
    observed: dict[str, dict[str, ObservedColumn]],
    lineage: Lineage | None = None,
) -> list[Bundle]:
    by_key = {c.dataset: c for c in contracts}
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(e["DATASET_KEY"], []).append(e)
    return [
        Bundle(
            dataset_key=k,
            events=v,
            contract=by_key.get(k),
            observed=observed.get(k, {}),
            lineage=lineage,
        )
        for k, v in sorted(groups.items())
    ]


TOOL_SCHEMA = {
    "name": CONTRACT_TOOL,
    "description": "Return the updated contract and, only if needed, an updated staging model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contract_yaml": {
                "type": "string",
                "description": "Complete contract file content. Same format as the current one.",
            },
            "staging_sql": {
                "type": ["string", "null"],
                "description": "Complete updated dbt staging model, or null if no change is needed.",
            },
            "pr_title": {"type": "string", "description": "Under 70 characters, imperative."},
            "pr_body": {
                "type": "string",
                "description": "Markdown. What changed upstream, what this PR adopts, what a "
                               "reviewer should check. Plain language for a finance reviewer.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences on why these choices, for the audit log.",
            },
        },
        "required": ["contract_yaml", "staging_sql", "pr_title", "pr_body", "reasoning"],
    },
}


SYSTEM = """You maintain data contracts for a finance data warehouse. A contract is the
agreement of record for the shape of a source dataset. Upstream changed a dataset
without telling anyone; the detector has classified the divergence. Your job is to
draft the contract change that adopts what is safe to adopt.

Rules you must follow exactly:
- Bump `version` by exactly one. For a dataset with no contract, version is 1.
- The new contract must describe the live schema exactly: every live column present,
  with the live type, length or precision/scale, and nullability. Nothing else.
- Never remove a column that exists in the live schema.
- Keep every existing description. Write a plausible finance description for a new
  column from its name and type. Say so in the description if you are inferring.
- Keep owner, classification, primary_key and freshness unchanged unless the change
  makes them wrong. For a new dataset use owner `unassigned-needs-review`.
- Only return staging_sql when a new column should flow through to staging. Keep the
  model's existing structure and add the column in the same style.
- Do not invent business rules. If you are unsure, adopt the column plainly and say
  so in pr_body so a reviewer can decide.
"""


def build_prompt(bundle: Bundle, example_contract: str) -> str:
    current = (
        bundle.contract.source_path.read_text()
        if bundle.contract and bundle.contract.source_path
        else None
    )
    staging = None
    p = ROOT / "dbt" / "models" / "staging" / f"stg_{bundle.table.lower()}.sql"
    if p.exists():
        staging = p.read_text()

    parts = [f"Dataset: {bundle.dataset_key}", "", "Drift events:", _events_json(bundle.events), ""]

    impacts = [(e, bundle.impact_for(e)) for e in bundle.events]
    lines = [f"- {e['OBJECT_NAME'] or bundle.dataset_key}: {i.summary()}"
             + (f" ({', '.join(i.marts)})" if i and i.marts else "")
             for e, i in impacts if i and not i.empty]
    if lines:
        parts += ["Downstream impact of these changes, from dbt lineage:", *lines, ""]
    parts += ["Live schema from INFORMATION_SCHEMA:", _observed_json(bundle.observed), ""]
    if current:
        parts += ["Current contract:", "```yaml", current, "```", ""]
    else:
        parts += [
            "There is no contract for this dataset. Draft version 1 in the same "
            "format as this example:", "```yaml", example_contract, "```", "",
        ]
    if staging:
        parts += ["Current staging model:", "```sql", staging, "```", ""]
    parts.append(f"Call {CONTRACT_TOOL} with your proposal.")
    return "\n".join(parts)


def draft(bundle: Bundle, client=None, model: str | None = None) -> Proposal:
    """Ask the model for a proposal. Returns an unverified Proposal."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (CONTRACT_DIR / "raw" / "ap_invoice.yml").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": CONTRACT_TOOL},
        messages=[{"role": "user", "content": build_prompt(bundle, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    d = block.input
    return Proposal(
        bundle=bundle,
        contract_yaml=d["contract_yaml"],
        staging_sql=d.get("staging_sql") or None,
        pr_title=d["pr_title"],
        pr_body=d["pr_body"],
        reasoning=d["reasoning"],
    )


def verify(proposal: Proposal) -> Proposal:
    """Reject anything the model got wrong. Populates proposal.errors."""
    b = proposal.bundle
    errs: list[str] = []
    tmp = ROOT / ".agent_tmp.yml"
    try:
        tmp.write_text(proposal.contract_yaml)
        new = parse_contract(tmp)
    except Exception as e:  # noqa: BLE001
        proposal.errors = [f"contract does not parse: {e}"]
        return proposal
    finally:
        if tmp.exists():
            tmp.unlink()

    if new.dataset != b.dataset_key:
        errs.append(f"dataset is {new.dataset}, expected {b.dataset_key}")

    expected_version = (b.contract.version + 1) if b.contract else 1
    if new.version != expected_version:
        errs.append(f"version is {new.version}, expected {expected_version}")

    if b.contract:
        old_names = {c.name for c in b.contract.columns}
        dropped = old_names - {c.name for c in new.columns}
        if dropped:
            errs.append(f"proposal drops contracted columns {sorted(dropped)}")
        if new.primary_key != b.contract.primary_key:
            errs.append("primary key changed")

    residual = diff_dataset(new, b.observed)
    for f in residual:
        errs.append(f"still diverges from live: {f.change_type} {f.object_name or ''}".strip())

    if proposal.staging_sql is not None and "source('raw'" not in proposal.staging_sql:
        errs.append("staging model no longer reads from source('raw', ...)")

    proposal.contract = new
    proposal.errors = errs
    return proposal
```

**Publishing.** Branch from `origin/<base>`, never local HEAD. Write the
contract and, if the model supplied one, the staging model. Commit, push, open
the PR with `drift` and severity labels. Enable auto merge only for LOW and
only if branch protection reports required status checks; otherwise say why
not. The helpers `_run`, `_start_branch`, `_push` and `_ensure_pushed` are
shown at Stage 22, where the failures that shaped them happened.

**`control/agent.py` `LABELS`, `_ensure_labels`, `_ensure_clean_tree`, `required_checks`, `existing_pr`, `publish`, `escalate`, `mark`**

```python
LABELS = {
    "drift": ("0E8A16", "raised by the drift agent"),
    "low": ("C5DEF5", "additive, safe to auto merge"),
    "medium": ("FBCA04", "needs a decision"),
    "breaking": ("B60205", "release gate fails until resolved"),
}


def _ensure_labels():
    for name, (colour, desc) in LABELS.items():
        subprocess.run(
            ["gh", "label", "create", name, "--color", colour, "--description", desc, "--force"],
            cwd=ROOT, capture_output=True, text=True,
        )


def _ensure_clean_tree():
    if _run(["git", "status", "--porcelain"]):
        raise SystemExit("working tree is not clean; commit or stash before running the agent")


def required_checks(base: str) -> list[str]:
    """Contexts GitHub will actually wait for before an auto merge completes.

    Empty means auto merge is not a gate: GitHub merges as soon as the PR is
    mergeable, whether or not the workflow ever ran.
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{base}/protection"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("required_status_checks", {}).get("contexts", []) or []


def existing_pr(branch: str) -> str | None:
    """A previous run may have opened the PR and failed afterwards. Reuse it."""
    out = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
         "--jq", ".[0].url"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    return out or None


def publish(proposal: Proposal, auto_merge: bool) -> str:
    """Write files, branch, commit, push, open PR. Returns the PR url."""
    assert proposal.ok and proposal.contract
    _ensure_clean_tree()
    _ensure_labels()
    base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    branch = proposal.branch
    existing = existing_pr(branch)
    if existing:
        return existing

    # Branch from the remote base, never from local HEAD. Otherwise any local
    # commit not yet pushed is swept into the PR, and a contract change arrives
    # carrying unrelated work.
    try:
        _start_branch(branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        files = [str(proposal.contract_path.relative_to(ROOT))]
        if proposal.staging_sql is not None:
            proposal.staging_path.write_text(proposal.staging_sql)
            files.append(str(proposal.staging_path.relative_to(ROOT)))
        _run(["git", "add", *files])
        impact = proposal.bundle.dataset_impact()
        impact_block = (
            f"\n## Downstream impact\n\n{impact.markdown()}\n"
            if impact and not impact.empty else ""
        )
        body = (
            f"{proposal.pr_body}\n{impact_block}\n---\n"
            f"Agent reasoning: {proposal.reasoning}\n\n"
            f"Drift events: {', '.join(proposal.bundle.event_ids)}\n"
        )
        _run(["git", "commit", "-q", "-m", proposal.pr_title, "-m", body])
        _push(branch)
        labels = ["drift", proposal.bundle.worst.lower()]
        url = _run([
            "gh", "pr", "create", "--title", proposal.pr_title, "--body", body,
            "--base", base, "--head", branch, *sum((["--label", l] for l in labels), []),
        ]).splitlines()[-1]
        if auto_merge and proposal.bundle.worst == LOW:
            if required_checks(base):
                _run(["gh", "pr", "merge", url, "--auto", "--squash", "--delete-branch"])
                proposal.merge_note = "auto merge on"
            else:
                proposal.merge_note = (
                    "auto merge NOT enabled: branch protection on "
                    f"'{base}' requires no status checks, so GitHub would merge "
                    "without waiting for the gate"
                )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


def escalate(bundle: Bundle) -> str:
    """BREAKING gets an issue, never a PR. Returns the issue url."""
    _ensure_labels()
    imps = [bundle.impact_for(e) for e in bundle.events]
    reports = sorted({r["label"] for i in imps if i for r in i.reports})
    marts = sorted({m for i in imps if i for m in i.marts})
    hit = [f"**{r}**" for r in reports] or [f"`{m}`" for m in marts]
    headline = (
        f"Breaking drift on `{bundle.dataset_key}`."
        + (f" This affects {', '.join(hit)}." if hit else "")
        + " The release gate will fail until this is resolved."
    )
    lines = [
        headline, "",
        "| severity | change | object | breaks | why |",
        "|---|---|---|---|---|",
    ]
    for e in bundle.events:
        imp = bundle.impact_for(e)
        lines.append(
            f"| {e['SEVERITY']} | {e['CHANGE_TYPE']} | {e['OBJECT_NAME'] or '-'} "
            f"| {imp.summary() if imp else '-'} | {e['RATIONALE']} |"
        )
    detail = bundle.dataset_impact()
    if detail and not detail.empty:
        lines += ["", "<details><summary>Full downstream impact</summary>", "",
                  detail.markdown(), "", "</details>"]
    lines += [
        "",
        "Options: revert the upstream change, or agree a new contract version through a reviewed PR.",
        "",
        f"Drift events: {', '.join(bundle.event_ids)}",
    ]
    return _run([
        "gh", "issue", "create",
        "--title", f"Breaking drift: {bundle.dataset_key}",
        "--body", "\n".join(lines),
        "--label", "drift", "--label", "breaking",
    ]).splitlines()[-1]


def mark(conn, event_ids: list[str], status: str, ref: str):
    params = {"st": status, "ref": ref}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    execute(
        conn,
        "UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, RESOLUTION_REF = %(ref)s "
        "WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )
```
**`control/cli.py` `cmd_agent` (final form: onboarding, retirement and content passes were added later and are shown at their stages)**

```python
def cmd_agent(args):
    s = load_settings()
    contracts = load_contracts()
    with connect(s) as conn:
        events = open_events(conn)
        observed = fetch_observed(conn, s.database, s.raw_schema)
        # Content breaches are handled on their own: no contract change fixes
        # bad data, so they never reach the drafting path below.
        events = [e for e in events if e["CHANGE_TYPE"] not in quality_mod.QUALITY_TYPES]
        try:
            quality_rc = _quality_pass(conn, s, contracts, observed, args.dataset, args.dry_run)
        except Exception as e:  # noqa: BLE001
            quality_rc = 0
            reason = " ".join(l.strip() for l in str(e).splitlines()[:3] if l.strip())
            console.print(f"[yellow]content pass skipped:[/yellow] {reason}")
    bundles = bundle_events(events, contracts, observed, Lineage.load())
    if args.dataset:
        bundles = [b for b in bundles if b.dataset_key == args.dataset.upper()]

    # Shields whose drift has been repaired come out first. This does not depend
    # on any open event, so it runs even when there is nothing else to do.
    active = _live_active(contracts, observed)
    retired_rc = _retire_stale(active, args.dataset, args.dry_run)

    if not bundles and args.dry_run:
        console.print("[green]no open drift, nothing to do[/green]")
        return retired_rc or quality_rc
    if bundles:
        console.print(f"{len(events)} open event(s) across {len(bundles)} dataset(s)\n")
    rc = 0

    # Breaking drift escalated on an earlier run has an issue but may have no
    # shield yet. Offer one now, once, without re-escalating.
    if not args.dry_run:
        with connect(s) as conn:
            prior = [e for e in breaking_events(conn) if e["STATUS"] == "ESCALATED"]
        if not bundles and not prior:
            console.print("[green]no open drift, nothing to do[/green]")
            return retired_rc or quality_rc
        open_keys = {b.dataset_key for b in bundles}
        for pb in bundle_events(prior, contracts, observed, Lineage.load()):
            if pb.dataset_key in open_keys:
                continue
            if args.dataset and pb.dataset_key != args.dataset.upper():
                continue
            issue = next((e["RESOLUTION_REF"] for e in pb.events if e.get("RESOLUTION_REF")), None)
            console.print(f"[bold]{pb.dataset_key}[/bold]  already escalated  {issue or ''}")
            _shield(pb, issue, s, active)
            console.print("")
    for b in bundles:
        di = b.dataset_impact()
        breaks = f"  [dim]touches {di.summary()}[/dim]" if di and not di.empty else ""
        console.print(f"[bold]{b.dataset_key}[/bold]  worst [{SEV_STYLE[b.worst]}]{b.worst}[/]  "
                      f"{len(b.events)} event(s){breaks}")

        if b.worst == BREAKING:
            if args.dry_run:
                console.print("  → would escalate (issue) and propose a shield PR\n")
                continue
            url = escalate(b)
            with connect(s) as conn:
                mark(conn, b.event_ids, "ESCALATED", url)
            console.print(f"  [red]escalated[/red] {url}")
            _shield(b, url, s, active)
            console.print("")
            continue

        p = verify(draft(b))
        if not p.ok:
            rc = 1
            console.print("  [red]proposal rejected by verification:[/red]")
            for e in p.errors:
                console.print(f"    {e}")
            console.print("")
            continue

        # No contract at all means the table is not modelled either. Onboarding
        # ships the contract together with everything needed to build on it.
        if b.contract is None:
            url = _onboard(b, p, args.dry_run)
            if url is None:
                rc = 1
                console.print("")
                continue
            if url is DRY:
                console.print("")
                continue
            with connect(s) as conn:
                mark(conn, b.event_ids, "PROPOSED", url)
            console.print(f"  [green]onboarding PR[/green] {url}")
            console.print("  [dim]review required, never auto merged[/dim]\n")
            continue

        console.print(f"  proposal: [bold]{p.pr_title}[/bold]  → contract v{p.contract.version}"
                      + ("  + staging model" if p.staging_sql else ""))
        console.print(f"  [dim]{p.reasoning}[/dim]")
        if args.dry_run:
            console.print("  [dim]dry run, printing contract:[/dim]")
            console.print(p.contract_yaml)
            if p.staging_sql:
                console.print(p.staging_sql)
            console.print("")
            continue

        url = publish(p, auto_merge=not args.no_merge)
        with connect(s) as conn:
            mark(conn, b.event_ids, "PROPOSED", url)
        style = "yellow" if "NOT enabled" in p.merge_note else "dim"
        console.print(f"  [green]PR[/green] {url}")
        console.print(f"  [{style}]{p.merge_note}[/]\n")
    return rc or retired_rc or quality_rc
```

**Configuration in `.env`.**

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

Requires `gh` authenticated on the machine, push rights, a clean tree.

**Commands**

```bash
python -m control.cli agent --dry-run
python -m control.cli agent
python -m control.cli agent --no-merge
python -m control.cli agent --dataset RAW.AP_INVOICE
```

**First live run.** Six open events across four datasets, routed correctly:

| Dataset | Worst | Action |
|---|---|---|
| RAW.AP_ACCRUAL | MEDIUM | **PR #1**, contract v1, awaiting review (later closed unmerged, superseded by onboarding in Stage 22) |
| RAW.AP_INVOICE | LOW | **PR #2**, contract v2 plus staging model, auto merge |
| RAW.AP_PAYMENT | BREAKING | **issue #3** |
| RAW.AR_INVOICE | BREAKING | **issue #4** (one MEDIUM and one BREAKING event; the whole dataset escalated) |

**Three bugs only a live run would find.**

*`mark()` mixed `%` formatting with driver parameters.* `"... IN (%s)" % ",".join(...)`
collided with the connector's own `%(name)s` placeholders and raised
`TypeError: format requires a mapping`, after the PR had already been created.
Every id is now a bound parameter, and `publish()` reuses an open PR on the
branch so a re-run after a crash does not fail on an existing branch.

*The agent branched from local HEAD.* Four unpushed commits were swept into
PR #2. A contract change arrived carrying 815 lines of unrelated work, and
squash merging it put all of that on `main` under the title "Adopt
APPROVER_ID". That is why commit `b3eb4b3` contains the workflow, the agent and
its tests. The agent now fetches and branches from `origin/<base>`.

*Auto merge is not a gate without branch protection.* `gh pr merge --auto`
merges as soon as the PR is mergeable. With no protection on `main`, PR #2
merged without the workflow ever gating it. The agent now reads
`repos/{owner}/{repo}/branches/<base>/protection` and enables auto merge only
when required status check contexts exist.

**Merged:** PR #2 as `b3eb4b3`. After `register` and `detect`, `RAW.AP_INVOICE`
was clean at v2. That is the whole thesis in one run.

**Commands: after merging a contract PR**

```bash
python -m control.cli register
python -m control.cli detect
```

Registration is not automatic and should not be. A contract file changing on
disk means nothing until it is registered.

---

## Stage 15. The gate judges only what the PR changes

**Commit:** `202b695`

The `contracts` job originally ran `detect` across the whole warehouse. PR #2
adopted a LOW change on `AP_INVOICE` and would have failed on unrelated
BREAKING drift in `AP_PAYMENT`. `detect` grew `--dataset` (repeatable); the
job diffs the PR against its base, reads the `dataset` key out of each changed
contract file, and scopes the run. Scoped runs also filter the observed
schema, so an unrelated ungoverned table does not surface on someone else's PR.

**Commands**

```bash
python -m control.cli detect --dataset RAW.AP_INVOICE --dry-run --fail-on-breaking
```

---

## Stage 16. The console

**Commit:** `d904338`, refined at `ab7ceb4`

One page served on `127.0.0.1:8765` that fires scenarios, runs the control
plane and shows events, contracts and runs as they change.

| Design point | Why |
|---|---|
| Runs the CLI as subprocesses | there is no second implementation to drift from the first; what the page shows is what the terminal shows |
| Allowlist | the page can run only the named actions, and a scenario button can name only a file that exists in `ops/scenarios/` |
| Loopback only | it runs commands against a live warehouse and a live GitHub token |
| Open events by default | thirty DISMISSED rows had buried two ESCALATED and three PROPOSED |

**`control/ui.py` `ALLOWED`, `scenarios`, `start_job`, `read_state`**

```python
ALLOWED = {
    "detect":       ["detect"],
    "detect-strict": ["detect", "--fail-on-breaking"],
    "register":     ["register"],
    "load":         ["load"],
    "dbt-build":    ["dbt", "build"],
    "dbt-parse":    ["dbt", "parse"],
    "agent-dry":    ["agent", "--dry-run"],
    "agent":        ["agent"],
    "sync":         ["sync"],
    "resolve-all":  ["resolve", "--all"],
}


def scenarios() -> list[dict]:
    out = []
    for p in sorted((ROOT / "ops" / "scenarios").glob("*.sql")):
        first = ""
        for line in p.read_text().splitlines():
            if line.startswith("-- Scenario:"):
                first = line.removeprefix("-- Scenario:").strip()
                break
            if line.startswith("-- Undo"):
                first = "Rebuild RAW to the version 1 shape."
                break
        out.append({"file": p.name, "key": p.stem, "title": first or p.stem})
    return out


def start_job(args: list[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {"lines": [], "done": False, "rc": None, "cmd": " ".join(args)}

    def run():
        cmd = [sys.executable, "-u", "-m", "control.cli", *args]
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**__import__("os").environ, "COLUMNS": "150"},
        )
        for line in proc.stdout:
            with LOCK:
                JOBS[job_id]["lines"].append(line.rstrip("\n"))
        proc.wait()
        with LOCK:
            JOBS[job_id]["done"] = True
            JOBS[job_id]["rc"] = proc.returncode

    threading.Thread(target=run, daemon=True).start()
    return job_id


def read_state() -> dict:
    s = load_settings()
    with connect(s) as conn:
        events = query(
            conn,
            """
            SELECT EVENT_ID, DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME,
                   RATIONALE, STATUS, RESOLUTION_REF, IMPACT,
                   TO_VARCHAR(DETECTED_AT, 'YYYY-MM-DD HH24:MI') AS DETECTED
            FROM FIN_AIWH.META.DRIFT_EVENT
            ORDER BY CASE STATUS WHEN 'OPEN' THEN 0 ELSE 1 END,
                     CASE SEVERITY WHEN 'BREAKING' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                     DETECTED_AT DESC
            LIMIT 60
            """,
        )
        contracts = query(
            conn,
            "SELECT CONTRACT_KEY, VERSION, OWNER, "
            "TO_VARCHAR(REGISTERED_AT, 'YYYY-MM-DD HH24:MI') AS REGISTERED "
            "FROM FIN_AIWH.META.ACTIVE_CONTRACT ORDER BY CONTRACT_KEY",
        )
        runs = query(
            conn,
            "SELECT RUN_ID, RUN_TYPE, DATASETS_SCANNED, EVENTS_RAISED, STATUS, "
            "TO_VARCHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STARTED "
            "FROM FIN_AIWH.META.RUN_LOG ORDER BY STARTED_AT DESC LIMIT 8",
        )
    return {
        "events": events,
        "contracts": contracts,
        "runs": runs,
        "files": [
            {"dataset": c.dataset, "version": c.version, "path": str(c.source_path.name)}
            for c in load_contracts()
        ],
    }
```
**Commands**

```bash
python -m control.ui
```

Four click demo: `detect` (clean), `04 money scale changed`, `detect` (two
BREAKING with reasoning), `agent` (a PR for what is safe, an issue for what is
not).

---

## Stage 17. Do not raise it twice

**Commit:** `b3dbff2`

The loop-closing run reported `new 4`. Two of those were already escalated to
issues #3 and #4, and one already had PR #1 open. They were raised again as
fresh events because deduplication only looked at `STATUS = 'OPEN'`. Left
alone, a scheduled detector would open a duplicate issue every run for as long
as the breaking change existed.

| Status | Meaning | Re-raise? |
|---|---|---|
| OPEN | waiting for triage | no |
| PROPOSED | a pull request is open for it | no |
| ESCALATED | an issue is open for it | no |
| DISMISSED | someone decided no action | yes, a new occurrence |
| MERGED | the contract was updated | yes, a new occurrence |

**`control/detect.py` `ACTIVE_STATUSES`, `active_fingerprints`, `event_id`, `persist` (final form; `event_id` and `FINGERPRINT` arrived in Stage 27)**

```python
ACTIVE_STATUSES = ("OPEN", "PROPOSED", "ESCALATED")


def active_fingerprints(conn) -> dict[str, str]:
    """fingerprint -> EVENT_ID for every event still in a live state."""
    rows = query(
        conn,
        "SELECT FINGERPRINT, EVENT_ID FROM FIN_AIWH.META.DRIFT_EVENT WHERE STATUS IN (%s)"
        % ",".join(f"'{s}'" for s in ACTIVE_STATUSES),
    )
    return {r["FINGERPRINT"]: r["EVENT_ID"] for r in rows}


def event_id(fingerprint: str, run_id: str) -> str:
    """Unique per raise. The fingerprint says what; the run says when."""
    return hashlib.sha256(f"{fingerprint}:{run_id}".encode()).hexdigest()[:32]


def persist(conn, run_id: str, findings: list[Finding], contracts: list[Contract]) -> tuple[int, int]:
    """Write new findings. Re-detecting the same divergence does not duplicate it.

    Returns (written, suppressed) so a run can say how much it deliberately
    stayed quiet about.
    """
    versions = {c.dataset: c.version for c in contracts}
    already = active_fingerprints(conn)
    suppressed = 0
    written = 0
    for f in findings:
        fp = f.fingerprint()
        if fp in already:
            # Do not raise it again, but do keep its impact current: lineage
            # changes as the dbt project changes, and an event open for a week
            # should say what it breaks today, not what it broke when raised.
            if f.impact is not None:
                execute(
                    conn,
                    "UPDATE FIN_AIWH.META.DRIFT_EVENT SET IMPACT = TRY_PARSE_JSON(%(impact)s) "
                    "WHERE EVENT_ID = %(event_id)s",
                    {"impact": json.dumps(f.impact.as_dict()), "event_id": already[fp]},
                )
            suppressed += 1
            continue
        execute(
            conn,
            """
            INSERT INTO META.DRIFT_EVENT
              (EVENT_ID, FINGERPRINT, RUN_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE,
               SEVERITY, OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS,
               IMPACT)
            SELECT %(event_id)s, %(fingerprint)s, %(run_id)s, %(dataset)s, %(version)s, %(change_type)s,
                   %(severity)s, %(object_name)s,
                   TRY_PARSE_JSON(%(before)s), TRY_PARSE_JSON(%(after)s),
                   %(rationale)s, 'OPEN', TRY_PARSE_JSON(%(impact)s)
            """,
            {
                "event_id": event_id(fp, run_id),
                "fingerprint": fp,
                "run_id": run_id,
                "dataset": f.dataset_key,
                "version": versions.get(f.dataset_key),
                "change_type": f.change_type,
                "severity": f.severity,
                "object_name": f.object_name,
                "before": json.dumps(f.before) if f.before is not None else None,
                "after": json.dumps(f.after) if f.after is not None else None,
                "rationale": f.rationale,
                "impact": json.dumps(f.impact.as_dict()) if f.impact else None,
            },
        )
        written += 1
    return written, suppressed
```

`detect` now also reports what it stayed quiet about:

```
run 20260917T212712-893e161a  scanned 9 datasets  found 4 divergences
  new 1  already being worked on 3
```

---

## Stage 18. Closing the lifecycle: sync

**Commit:** `db19ce4`

Deduplication stopped the detector shouting about work in flight. Nothing yet
told the control plane when that work finished. `sync` reads the outcome from
GitHub, where the decision happened, and writes it back.

| Reference | GitHub state | Event becomes |
|---|---|---|
| pull request | merged | MERGED |
| pull request | closed, not merged | OPEN, the drift is still there |
| pull request | open | unchanged |
| issue | closed | DISMISSED |
| issue | open | unchanged |

**`control/agent.py` `_gh_json`, `github_outcome`, `pending_events`, `set_status`**

```python
def _gh_json(args: list[str]) -> dict | None:
    r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def github_outcome(url: str) -> tuple[str, str] | None:
    """What became of the pull request or issue behind an event.

    Returns (new_status, human reason), or None when nothing has changed yet or
    the reference cannot be read.
    """
    if "/pull/" in url:
        d = _gh_json(["pr", "view", url, "--json", "state,mergedAt"])
        if not d:
            return None
        if d.get("mergedAt"):
            return "MERGED", "pull request merged"
        if d.get("state") == "CLOSED":
            return "OPEN", "pull request closed without merging, so the drift is unresolved"
        return None
    if "/issues/" in url:
        d = _gh_json(["issue", "view", url, "--json", "state"])
        if not d:
            return None
        if d.get("state") == "CLOSED":
            return "DISMISSED", "issue closed"
        return None
    return None


def pending_events(conn) -> list[dict]:
    return query(
        conn,
        "SELECT EVENT_ID, DATASET_KEY, OBJECT_NAME, SEVERITY, STATUS, RESOLUTION_REF "
        "FROM FIN_AIWH.META.DRIFT_EVENT "
        "WHERE STATUS IN ('PROPOSED', 'ESCALATED') AND RESOLUTION_REF IS NOT NULL "
        "ORDER BY DATASET_KEY, OBJECT_NAME",
    )


def set_status(conn, event_ids: list[str], status: str):
    params = {"st": status}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    resolved = "RESOLVED_AT = SYSDATE(), " if status in ("MERGED", "DISMISSED") else ""
    execute(
        conn,
        f"UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, {resolved}"
        "RESOLUTION_REF = RESOLUTION_REF WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )
```
**`control/cli.py` `cmd_sync`**

```python
def cmd_sync(args):
    """Bring event status back in line with what happened on GitHub."""
    with connect() as conn:
        pend = pending_events(conn)
    if not pend:
        console.print("[green]nothing proposed or escalated, nothing to reconcile[/green]")
        return 0

    console.print(f"checking {len(pend)} event(s) against GitHub\n")
    moves: dict[str, list[str]] = {}
    for e in pend:
        outcome = github_outcome(e["RESOLUTION_REF"])
        label = f"{e['DATASET_KEY']}.{e['OBJECT_NAME'] or '*'}"
        if not outcome:
            console.print(f"  [dim]{label:<34} {e['STATUS']} still[/dim]")
            continue
        new, why = outcome
        moves.setdefault(new, []).append(e["EVENT_ID"])
        console.print(f"  {label:<34} [green]{e['STATUS']} → {new}[/green]  [dim]{why}[/dim]")

    if not moves:
        console.print("\n[dim]nothing to change[/dim]")
        return 0
    if args.dry_run:
        console.print("\n[dim]dry run, nothing written[/dim]")
        return 0

    with connect() as conn:
        for status, ids in moves.items():
            set_status(conn, ids, status)
    total = sum(len(v) for v in moves.values())
    console.print(f"\n[green]{total} event(s) updated[/green]")
    console.print("[dim]a MERGED contract is not in force until you run: register[/dim]")
    return 0
```
**Commands**

```bash
python -m control.cli sync --dry-run
python -m control.cli sync
python -m control.cli register
```

**The operating order:** `sync → register → detect → agent`. Reconcile what
finished, make merged contracts the agreement of record, compare, act on what
is left.

---

## Stage 19. Impact analysis

**Commits:** `fd120e0` (lineage), `f46edb2` (refresh on suppressed events), `cc2b461` (exposures)

A drift event said what changed. It did not say what it broke. `lineage.py`
reads dbt's `manifest.json`: `child_map` for exact model lineage, SQL text
matching for column lineage (a heuristic with stated confidence), and dbt
**exposures** for which report a finance user opens. Every event, PR and
issue now leads with the report.

| Confidence | Meaning |
|---|---|
| `exact` | the column is named in the model's SQL |
| `wildcard` | the model selects `*`, so it carries the column |
| `inherited` | downstream of a model that does |

**`ops/sql/03_event_impact.sql`**

```sql
-- Adds downstream impact to drift events.
-- Safe to re-run. New installs get this from 01_meta_control_plane.sql.
ALTER TABLE FIN_AIWH.META.DRIFT_EVENT ADD COLUMN IF NOT EXISTS IMPACT VARIANT;

CREATE OR REPLACE VIEW FIN_AIWH.META.OPEN_DRIFT AS
SELECT DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME, RATIONALE,
       IMPACT:marts        AS AFFECTED_MARTS,
       IMPACT:models       AS AFFECTED_MODELS,
       DETECTED_AT, EVENT_ID
FROM FIN_AIWH.META.DRIFT_EVENT
WHERE STATUS = 'OPEN'
ORDER BY CASE SEVERITY WHEN 'BREAKING' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, DETECTED_AT;
```
**`control/lineage.py`**

```python
"""What a drift event actually breaks.

A severity says how bad a change is in principle. Impact says what it costs
here: which models stop being correct, which tests will fail, which marts a
finance user reads. That turns "BANK_REF was dropped" into "BANK_REF was
dropped, which breaks fct_ap_open_items and the AP aging report".

Lineage comes from dbt's manifest, which dbt builds from the real SQL, so the
model graph is exact. Column level lineage is not something dbt publishes, so
it is derived here by reading each model's SQL. That part is a heuristic and
says so: every column result carries a confidence.
"""
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import ROOT

MANIFEST = ROOT / "dbt" / "target" / "manifest.json"

# How sure we are that a model really uses the column
EXACT = "references the column"
WILDCARD = "selects * from the source, so it carries the column"
INHERITED = "downstream of an affected model"


@dataclass
class Impact:
    dataset_key: str
    column: str | None
    models: list[str] = field(default_factory=list)      # staging
    marts: list[str] = field(default_factory=list)       # what people read
    tests: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)    # dbt exposures: what people open
    confidence: str | None = None
    manifest_seen: bool = True

    @property
    def empty(self) -> bool:
        return not (self.models or self.marts or self.tests or self.reports)

    def summary(self) -> str:
        """One line for a table cell."""
        if not self.manifest_seen:
            return "unknown (no dbt manifest)"
        if self.empty:
            return "nothing downstream"
        bits = []
        if self.reports:
            bits.append(f"{len(self.reports)} report" + ("s" if len(self.reports) > 1 else ""))
        if self.marts:
            bits.append(f"{len(self.marts)} mart" + ("s" if len(self.marts) > 1 else ""))
        if self.models:
            bits.append(f"{len(self.models)} staging")
        if self.tests:
            bits.append(f"{len(self.tests)} test" + ("s" if len(self.tests) > 1 else ""))
        return ", ".join(bits)

    def as_dict(self) -> dict:
        return asdict(self)

    def markdown(self) -> str:
        """A block for a pull request or an issue body."""
        if not self.manifest_seen:
            return ("_Downstream impact unknown: no dbt manifest was available. "
                    "Run `dbt parse` and re-run detection._")
        if self.empty:
            return "_Nothing downstream depends on this yet._"
        lines = []
        if self.reports:
            lines.append("**Reports affected** (what people open)")
            for r in self.reports:
                who = f" ({r['owner']})" if r.get("owner") else ""
                lines.append(f"- {r['label']}{who}")
            lines.append("")
        if self.marts:
            lines.append("**Marts affected** (what people read)")
            lines += [f"- `{m}`" for m in self.marts]
        if self.models:
            lines.append("")
            lines.append("**Staging models affected**")
            lines += [f"- `{m}`" for m in self.models]
        if self.tests:
            lines.append("")
            lines.append("**Tests that cover this path**")
            lines += [f"- `{t}`" for t in self.tests]
        if self.confidence:
            lines.append("")
            lines.append(f"_Column attribution: {self.confidence}._")
        return "\n".join(lines)


class Lineage:
    """The dbt graph, queried from the source side."""

    def __init__(self, manifest: dict | None):
        self.manifest = manifest or {}
        self.nodes = self.manifest.get("nodes", {})
        self.sources = self.manifest.get("sources", {})
        self.exposures = self.manifest.get("exposures", {})
        self.child_map = self.manifest.get("child_map", {})

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Lineage":
        p = path or MANIFEST
        if not p.exists():
            return cls(None)
        try:
            return cls(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            return cls(None)

    @property
    def available(self) -> bool:
        return bool(self.nodes)

    # -- graph ------------------------------------------------------------

    def source_id(self, dataset_key: str) -> str | None:
        """RAW.AP_PAYMENT -> source.fin_aiwh.raw.AP_PAYMENT"""
        table = dataset_key.split(".")[-1].upper()
        for uid, s in self.sources.items():
            if s.get("name", "").upper() == table:
                return uid
        return None

    def children(self, uid: str) -> list[str]:
        return self.child_map.get(uid, [])

    def descendants(self, uid: str) -> set[str]:
        """Everything reachable downstream, tests included."""
        seen, stack = set(), list(self.children(uid))
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.children(n))
        return seen

    def _sql(self, uid: str) -> str:
        n = self.nodes.get(uid, {})
        return n.get("raw_code") or n.get("compiled_code") or ""

    def _name(self, uid: str) -> str:
        return self.nodes.get(uid, {}).get("name", uid.split(".")[-1])

    def _is(self, uid: str, kind: str) -> bool:
        if kind == "exposure":
            return uid in self.exposures
        return self.nodes.get(uid, {}).get("resource_type") == kind

    def _report(self, uid: str) -> dict:
        e = self.exposures.get(uid, {})
        owner = e.get("owner") or {}
        return {
            "name": e.get("name", uid.split(".")[-1]),
            "label": e.get("label") or e.get("name", uid.split(".")[-1]),
            "type": e.get("type"),
            "owner": owner.get("name") or owner.get("email"),
        }

    def _schema(self, uid: str) -> str:
        return (self.nodes.get(uid, {}).get("schema") or "").upper()

    # -- impact -----------------------------------------------------------

    def _uses_column(self, uid: str, column: str) -> str | None:
        """Does this model reference the column? Returns a confidence, or None."""
        sql = self._sql(uid)
        if not sql:
            return None
        if re.search(rf"\b{re.escape(column)}\b", sql, re.IGNORECASE):
            return EXACT
        if re.search(r"select\s+\*", sql, re.IGNORECASE):
            return WILDCARD
        return None

    def impact(self, dataset_key: str, column: str | None = None) -> Impact:
        if not self.available:
            return Impact(dataset_key, column, manifest_seen=False)

        src = self.source_id(dataset_key)
        if src is None:
            return Impact(dataset_key, column)

        if column is None:
            affected = self.descendants(src)
            confidence = None
        else:
            # Direct children that actually touch the column, then everything
            # downstream of those. A model that does not reference the column is
            # not affected, and neither is anything beyond it through that path.
            affected, confidence = set(), None
            for child in self.children(src):
                if not self._is(child, "model"):
                    continue
                c = self._uses_column(child, column)
                if not c:
                    continue
                confidence = confidence or c
                affected.add(child)
                affected |= self.descendants(child)
            if affected and confidence != EXACT:
                confidence = confidence or INHERITED
            # column scoped tests on the source itself
            for child in self.children(src):
                if self._is(child, "test") and \
                        (self.nodes.get(child, {}).get("column_name") or "").upper() == column.upper():
                    affected.add(child)

        models = sorted({self._name(u) for u in affected
                         if self._is(u, "model") and self._schema(u) == "STAGING"})
        marts = sorted({self._name(u) for u in affected
                        if self._is(u, "model") and self._schema(u) == "MARTS"})
        tests = sorted({self._name(u) for u in affected if self._is(u, "test")})
        reports = sorted(
            (self._report(u) for u in affected if self._is(u, "exposure")),
            key=lambda r: r["label"],
        )
        return Impact(dataset_key, column, models, marts, tests, reports, confidence)
```
**`dbt/models/marts/exposures.yml`**

```yaml
version: 2

# What people actually open. Declaring these means impact analysis can say
# "this breaks the AP aging pack" rather than "this breaks agg_ap_aging".
# Add one entry per report, dashboard or extract that reads a mart.
exposures:
  - name: ap_aging_pack
    label: AP Aging Pack
    type: dashboard
    maturity: high
    owner: {name: AP Controller, email: ap-controller@example.com}
    description: Month end AP aging by entity and bucket, reviewed at close.
    depends_on:
      - ref('agg_ap_aging')
      - ref('fct_ap_open_items')

  - name: ar_aging_pack
    label: AR Aging Pack
    type: dashboard
    maturity: high
    owner: {name: AR Controller, email: ar-controller@example.com}
    description: Month end AR aging by entity and bucket, reviewed at close.
    depends_on:
      - ref('agg_ar_aging')
      - ref('fct_ar_open_items')

  - name: working_capital_kpis
    label: Working Capital KPIs
    type: dashboard
    maturity: high
    owner: {name: FP&A Lead, email: fpa@example.com}
    description: DSO and DPO per entity on the CFO dashboard.
    depends_on:
      - ref('kpi_dso_dpo')

  - name: bank_reconciliation_extract
    label: Bank Reconciliation Extract
    type: application
    maturity: medium
    owner: {name: Treasury Ops, email: treasury@example.com}
    description: Nightly extract of payments with bank references for reconciliation.
    depends_on:
      - ref('fct_ap_open_items')
```
**`control/detect.py` `attach_impact`**

```python
def attach_impact(findings: list[Finding], lineage: Lineage | None = None) -> list[Finding]:
    """Work out what each finding breaks. Cheap: the graph is already in memory."""
    lg = lineage or Lineage.load()
    for f in findings:
        # A dataset level change affects everything downstream of the dataset.
        # A column level change affects only the paths that carry that column.
        col = f.object_name if f.change_type not in (
            "DATASET_MISSING", "DATASET_UNGOVERNED") else None
        f.impact = lg.impact(f.dataset_key, col)
    return findings
```

Impact on a suppressed event is refreshed on every run: an event open for a
week should say what it breaks today, not what it broke when raised.

---

## Stage 20. The scheduled operating cycle

**Commit:** `22832df`

`sync → register → detect → agent`, every six hours, with `workflow_dispatch`
and a `dry_run` input. Uses `AGENT_GH_TOKEN` because events caused by
`GITHUB_TOKEN` do not trigger other workflows, so a PR opened under it would
never be gated. First dry run completed clean.

**`.github/workflows/cycle.yml`**

```yaml
# fin_aiwh operating cycle
#
# The self-managing part. On a schedule, with nobody watching:
#   sync      what finished on GitHub becomes event state
#   register  merged contracts become the agreement of record
#   detect    the warehouse is compared against them
#   agent     safe changes become PRs, dangerous ones become issues
#
# Order matters. Any other sequence judges against stale contracts or
# re-raises work already done.
#
# Token: agent PRs must trigger the gate, and GitHub does not run workflows
# for events caused by GITHUB_TOKEN. So the agent uses AGENT_GH_TOKEN, a
# fine-grained PAT with contents, pull-requests and issues write on this repo.
# Without it the cycle still runs, PRs still open, but the gate stays silent
# on them and auto merge is refused.
name: cycle

on:
  schedule:
    - cron: "17 */6 * * *"      # four times a day, off the hour to avoid the rush
  workflow_dispatch:
    inputs:
      dry_run:
        description: "detect and agent in dry run, change nothing"
        type: boolean
        default: false

concurrency:
  group: cycle
  cancel-in-progress: false      # never kill a run mid-PR

permissions:
  contents: write
  pull-requests: write
  issues: write

env:
  SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
  SNOWFLAKE_USER: FIN_AIWH_SVC
  SNOWFLAKE_ROLE: FIN_AIWH_ENG
  SNOWFLAKE_WAREHOUSE: FIN_AIWH_WH
  SNOWFLAKE_DATABASE: FIN_AIWH
  SNOWFLAKE_SCHEMA: META
  SNOWFLAKE_PRIVATE_KEY_PATH: .secrets/ci_rsa_key.p8
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  GH_TOKEN: ${{ secrets.AGENT_GH_TOKEN || github.token }}

jobs:
  cycle:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          token: ${{ secrets.AGENT_GH_TOKEN || github.token }}
      - uses: actions/setup-python@v5
        with: {python-version: "3.11", cache: pip}
      - run: pip install -r requirements.txt

      - name: materialise private key
        run: |
          mkdir -p .secrets
          printf '%s' "${{ secrets.SNOWFLAKE_PRIVATE_KEY }}" > .secrets/ci_rsa_key.p8
          chmod 600 .secrets/ci_rsa_key.p8

      - name: git identity for agent commits
        run: |
          git config user.name  "fin-aiwh agent"
          git config user.email "fin-aiwh-agent@users.noreply.github.com"

      - name: token check
        run: |
          if [ -z "${{ secrets.AGENT_GH_TOKEN }}" ]; then
            echo "::warning::AGENT_GH_TOKEN not set. PRs opened by this run will not trigger the gate."
          fi

      - run: python -m control.cli ping
      - run: python -m control.cli dbt parse --no-version-check
      - run: python -m control.cli sync
      - run: python -m control.cli register

      - name: detect
        run: python -m control.cli detect ${{ inputs.dry_run && '--dry-run' || '' }}

      - name: agent
        run: python -m control.cli agent ${{ inputs.dry_run && '--dry-run' || '' }}

      - name: status
        if: always()
        run: python -m control.cli status
```

---

## Stage 21. Shields: keeping reports correct over breaking drift

**Commits:** `f735dfd` (shields), `f2c1d21` (gate fix), PR #5 `b887684`, PR #6 `93c81fb`, `4fe73cb` (recorded)

An issue tells people something is wrong. It does not make the aging pack
right again. A shield does, while the source is fixed. Fully deterministic:
the transformations are mechanical, and a wrong one on a finance mart costs
more than a model's fluency is worth.

| Breaking change | Shield in the staging model | Honest about |
|---|---|---|
| COLUMN_REMOVED | `null::<contracted type> as col` | values are gone until upstream restores them |
| TYPE_CHANGED | `cast(<original expression> as <contracted type>) as col` | rounding or truncation |
| NULLABILITY_RELAXED | pass through, plus a singular test that fails on any null | nothing filled in, the build fails so a human sees it |
| DATASET_MISSING | none | nothing to shield |

| Rule | Why |
|---|---|
| Edits only the select line for that column | the original expression is preserved inside the cast |
| Refuses if the line cannot be found | never guesses at SQL |
| Contract unchanged, drift stays open, issue stays open | a shield hides nothing from the control plane |
| Never auto merges | labelled `shield` and `breaking`, a human merges |
| Every shield line carries `-- shield: ... see <issue>` | `detect` warns when a shield's drift is gone |

**`control/shield.py` `MARK`, `SHIELDABLE`, `Patch`, `Shield`, `ddl_type`, `plan`, `_LINE`, `_column_of`, `apply`, `build`, `installed`, `stale`, `pr_title`, `pr_body`**

```python
MARK = "-- shield:"


SHIELDABLE = {"COLUMN_REMOVED", "TYPE_CHANGED", "NULLABILITY_RELAXED"}


@dataclass
class Patch:
    column: str
    change_type: str
    expression: str | None          # new select expression, None for pass-through
    test_sql: str | None            # a singular test to add, if any
    note: str                       # the comment left in the model
    honest: str                     # what the shield does not fix


@dataclass
class Shield:
    dataset_key: str
    table: str
    patches: list[Patch] = field(default_factory=list)
    unshieldable: list[str] = field(default_factory=list)   # "COLUMN.CHANGE: why"
    staging_before: str = ""
    staging_after: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.patches) and not self.errors

    @property
    def staging_path(self) -> Path:
        return STAGING / f"stg_{self.table.lower()}.sql"

    @property
    def branch(self) -> str:
        return f"shield/{self.table.lower()}"

    def test_files(self) -> dict[Path, str]:
        out = {}
        for p in self.patches:
            if p.test_sql:
                out[TESTS / f"shield_{self.table.lower()}_{p.column.lower()}_not_null.sql"] = p.test_sql
        return out


def ddl_type(c: ContractColumn) -> str:
    """Contract type back to something CAST accepts."""
    if c.type == "TEXT":
        return f"varchar({c.length})" if c.length else "varchar"
    if c.type == "NUMBER":
        return f"number({c.precision},{c.scale})" if c.precision is not None else "number"
    return c.type.lower()


def plan(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    key = events[0]["DATASET_KEY"]
    table = key.split(".")[-1]
    sh = Shield(dataset_key=key, table=table)
    ref = f"see {issue_url}" if issue_url else "see the open drift issue"

    if contract is None:
        sh.errors.append("no contract for this dataset, nothing to restore to")
        return sh

    for e in events:
        ct, col = e["CHANGE_TYPE"], e.get("OBJECT_NAME")
        if ct not in SHIELDABLE or not col:
            sh.unshieldable.append(f"{col or '*'}.{ct}: cannot be shielded in staging")
            continue
        cc = contract.column(col)
        if cc is None:
            sh.unshieldable.append(f"{col}.{ct}: column not in the contract")
            continue
        t = ddl_type(cc)
        lower = col.lower()

        if ct == "COLUMN_REMOVED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"null::{t}",
                test_sql=None,
                note=f"{MARK} {col} dropped upstream, restored as NULL, {ref}",
                honest=f"{col} carries no values until upstream restores it",
            ))
        elif ct == "TYPE_CHANGED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"cast({{expr}} as {t})",
                test_sql=None,
                note=f"{MARK} {col} type changed upstream, cast back to {t}, {ref}",
                honest=f"{col} is cast to {t}; a wider or more precise upstream value is rounded or truncated",
            ))
        elif ct == "NULLABILITY_RELAXED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=None,
                test_sql=(
                    f"-- shield test: {col} is contracted NOT NULL but upstream relaxed it, {ref}\n"
                    f"-- Fails the build the moment a null arrives, so it cannot flow into a report unseen.\n"
                    f"select {lower}\nfrom {{{{ ref('stg_{table.lower()}') }}}}\nwhere {lower} is null\n"
                ),
                note=f"{MARK} {col} may now be null upstream, guarded by a test, {ref}",
                honest=f"nulls in {col} are not filled in; the build fails so a human sees them",
            ))
    return sh


_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<expr>.+?)(?:\s+as\s+(?P<alias>\w+))?(?P<tail>\s*,?\s*(?:--.*)?)$",
    re.IGNORECASE,
)


def _column_of(line: str) -> tuple[str | None, str | None]:
    """(column name this select line produces, the expression), or (None, None)."""
    s = line.strip()
    if not s or s.lower().startswith(("select", "from", "where", "with", "union", "--", "{{", "}}")):
        return None, None
    m = _LINE.match(line)
    if not m:
        return None, None
    expr, alias = m.group("expr").strip(), m.group("alias")
    name = alias or (expr if re.fullmatch(r"\w+", expr) else None)
    return (name.upper() if name else None), expr


def apply(shield: Shield, sql: str) -> Shield:
    """Rewrite the select lines the patches name. Refuse anything it cannot find."""
    shield.staging_before = sql
    lines = sql.splitlines()
    todo = {p.column.upper(): p for p in shield.patches if p.expression is not None}
    done = set()

    for i, line in enumerate(lines):
        name, expr = _column_of(line)
        if not name or name not in todo:
            continue
        p = todo[name]
        m = _LINE.match(line)
        indent, tail = m.group("indent"), m.group("tail") or ""
        comma = "," if "," in tail else ""
        new_expr = p.expression.replace("{expr}", expr)
        lines[i] = f"{indent}{new_expr} as {p.column.lower()}{comma}  {p.note}"
        done.add(name)

    missing = set(todo) - done
    for name in sorted(missing):
        shield.errors.append(f"{name}: select line not found in the staging model, refusing to guess")

    # pass-through patches only need the note somewhere visible
    header = [f"{p.note}" for p in shield.patches if p.expression is None]
    if header and not shield.errors:
        lines = header + [""] + lines if not lines[0].startswith(MARK) else header + lines

    shield.staging_after = "\n".join(lines) + ("\n" if sql.endswith("\n") else "")
    if "source('raw'" not in shield.staging_after:
        shield.errors.append("staging model no longer reads from source('raw', ...)")
    return shield


def build(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    sh = plan(events, contract, issue_url)
    if not sh.patches:
        return sh
    if not sh.staging_path.exists():
        sh.errors.append(f"no staging model at {sh.staging_path.name}")
        return sh
    return apply(sh, sh.staging_path.read_text())


def installed() -> list[tuple[str, str, str]]:
    """(table, column, note) for every shield marker in every staging model."""
    out = []
    for p in sorted(STAGING.glob("stg_*.sql")):
        table = p.stem.removeprefix("stg_").upper()
        for line in p.read_text().splitlines():
            if MARK in line:
                note = line[line.index(MARK) + len(MARK):].strip()
                col = note.split(" ", 1)[0].upper()
                out.append((table, col, note))
    return out


def stale(active_columns: set[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Shields whose column no longer diverges. Safe to remove."""
    return [(t, c, n) for t, c, n in installed() if (t, c) not in active_columns]


def pr_title(sh: Shield) -> str:
    return f"Shield {sh.table}: keep reports correct over breaking upstream drift"


def pr_body(sh: Shield, issue_url: str | None, impact_md: str | None) -> str:
    lines = [
        f"Upstream broke `{sh.dataset_key}`. This restores the contracted shape in "
        f"`stg_{sh.table.lower()}` so the marts keep building and the reports stay "
        f"correct while the source is fixed.",
        "",
        "**This does not fix the data.** The contract is unchanged, the drift is still "
        "open, and the issue stays open until upstream is repaired. Remove this shield "
        "when it is.",
        "",
        "| column | change | shield | what it does not fix |",
        "|---|---|---|---|",
    ]
    for p in sh.patches:
        how = p.expression.replace("{expr}", "…") if p.expression else "pass through + not_null test"
        lines.append(f"| `{p.column}` | {p.change_type} | `{how}` | {p.honest} |")
    if sh.unshieldable:
        lines += ["", "**Not shielded**", *[f"- {u}" for u in sh.unshieldable]]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    if issue_url:
        lines += ["", f"Tracking issue: {issue_url}"]
    return "\n".join(lines)
```
**`control/agent.py` `breaking_events`, `publish_shield`**

```python
def breaking_events(conn) -> list[dict]:
    """Escalated events too, so a shield can follow an issue opened last run."""
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS, RESOLUTION_REF
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS IN ('OPEN', 'ESCALATED') AND SEVERITY = 'BREAKING'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def publish_shield(sh, issue_url: str | None, impact_md: str | None, base: str | None = None) -> str:
    """Branch from origin, write the staging model and any tests, open a PR. Never auto merges."""
    from .shield import pr_body, pr_title
    assert sh.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "shield", "--color", "5319E7",
                    "--description", "restores contracted shape over breaking drift", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(sh.branch)
    if existing:
        return existing
    try:
        _start_branch(sh.branch, base)
        sh.staging_path.write_text(sh.staging_after)
        files = [str(sh.staging_path.relative_to(ROOT))]
        for path, sql in sh.test_files().items():
            path.write_text(sql)
            files.append(str(path.relative_to(ROOT)))
        _run(["git", "add", *files])
        body = pr_body(sh, issue_url, impact_md)
        _run(["git", "commit", "-q", "-m", pr_title(sh), "-m", body])
        _push(sh.branch)
        url = _run([
            "gh", "pr", "create", "--title", pr_title(sh), "--body", body,
            "--base", base, "--head", sh.branch,
            "--label", "drift", "--label", "breaking", "--label", "shield",
        ]).splitlines()[-1]
        if issue_url:
            subprocess.run(
                ["gh", "issue", "comment", issue_url, "--body",
                 f"Shield proposed: {url}\n\nKeeps the reports correct while this is fixed. "
                 f"Remove it when this issue closes."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])
```
**`control/cli.py` `_live_active`, `_shield` (final form; the idempotency checks arrived in Stage 24)**

```python
def _live_active(contracts, observed) -> set[tuple[str, str]]:
    """(table, column) pairs that diverge right now, from the live schema.

    The event table records what was true when an event was raised. The live
    schema is what is true now. Anything that acts on a column consults this,
    never the event's own status.
    """
    return {(f.dataset_key.split(".")[-1], (f.object_name or "").upper())
            for f in diff_all(contracts, observed)}


def _shield(b, issue_url, settings, active: set[tuple[str, str]]):
    """Try to keep the reports correct while upstream is fixed.

    Two things are skipped, and both are idempotency, not policy. A column that
    no longer diverges live belongs to retirement, not to a fresh shield. A
    column whose shield is already installed on the base branch would produce
    an identical file and an empty commit.
    """
    installed = {(tb, c) for tb, c, _ in shield_mod.installed()}
    breaking = []
    for e in b.events:
        if e["SEVERITY"] != "BREAKING":
            continue
        key = (b.table, (e.get("OBJECT_NAME") or "").upper())
        if key not in active:
            console.print(f"  [dim]{key[1] or b.table}: no longer diverges live, "
                          f"retirement handles it[/dim]")
            continue
        if key in installed:
            console.print(f"  [dim]{key[1]}: already shielded on the base branch[/dim]")
            continue
        breaking.append(e)
    if not breaking:
        return
    sh = shield_mod.build(breaking, b.contract, issue_url)
    if not sh.patches:
        if sh.unshieldable or sh.errors:
            console.print("  [dim]no shield possible: "
                          + "; ".join(sh.unshieldable + sh.errors) + "[/dim]")
        return
    if not sh.ok:
        console.print("  [yellow]shield refused:[/yellow] " + "; ".join(sh.errors))
        return
    di = b.dataset_impact()
    url = publish_shield(sh, issue_url, di.markdown() if di and not di.empty else None)
    cols = ", ".join(p.column for p in sh.patches)
    console.print(f"  [magenta]shield PR[/magenta] {url}  [dim]{cols}[/dim]")
    for u in sh.unshieldable:
        console.print(f"  [dim]not shielded: {u}[/dim]")
```

**Live.** `dbt build` on `main` was failing: `invalid identifier 'BANK_REF'`,
13 downstream models skipped. The gate was telling the truth. The agent
proposed **PR #5** (`stg_ap_payment`: `null::varchar(64) as bank_ref`) and
**PR #6** (`stg_ar_invoice`: header comment plus
`dbt/tests/shield_ar_invoice_status_not_null.sql`).

**`stg_ap_payment.sql` as merged in PR #5**

```sql
select
    payment_id,
    invoice_id,
    payment_date,
    upper(currency_code)   as currency_code,
    paid_amount,
    upper(payment_method)  as payment_method,
    null::varchar(64) as bank_ref,  -- shield: BANK_REF dropped upstream, restored as NULL, see https://github.com/manojnayakgit/fin_aiwh/issues/3
    loaded_at
from {{ source('raw', 'AP_PAYMENT') }}
```
**`dbt/tests/shield_ar_invoice_status_not_null.sql` as merged in PR #6**

```sql
-- shield test: STATUS is contracted NOT NULL but upstream relaxed it, see https://github.com/manojnayakgit/fin_aiwh/issues/4
-- Fails the build the moment a null arrives, so it cannot flow into a report unseen.
select status
from {{ ref('stg_ar_invoice') }}
where status is null
```

PR #5 was the first time the gate ran green on work the system wrote: rule
tests, contracts, dbt build. PR #6 failed the gate on PR #5's column, because
its branch predated that merge and the build is project wide; merging `main`
into the branch cleared it. After both merged:

```
Done. PASS=36 WARN=0 ERROR=0 SKIP=0 NO-OP=4 TOTAL=40
```

with the drift still open and both issues still open. 75 tests at this point.

---

# Part C. Onboarding and retirement

## Stage 22. Onboarding an ungoverned table

**Commits:** `beb2952` (onboarding), `0989861` and `c1d72ce` (source reference fixes), `ce6b330` (publish guards), `5411ced` (contract tests), PR #7 `99760b0`, `1d6f633` (recorded)

An ungoverned table used to end in a v1 contract PR (that was PR #1). Merging
it changed nothing anyone could use: the table was still not a dbt source,
had no staging model and no tests. Onboarding finishes the job in one PR.

| Artifact | Who writes it | Why that side |
|---|---|---|
| `contracts/raw/<table>.yml` v1 | model | descriptions and the key need reading the column names |
| entry in `sources.yml` | code | one line at the existing indent, no judgement |
| `stg_<table>.sql` | model | which codes to `upper`, what to `trim`, what is really missing |
| tests in `staging.yml` | code | the contract already states the key and nullability |

Two forced tool calls, one after the other: `propose_contract_change` for the
contract, then `propose_staging_model` with the contract and live schema in
front of it and `stg_ap_vendor.sql` as the style example. Then code checks the
model's work before anything is pushed:

| Check | Rejects |
|---|---|
| column set equals the contract exactly | a dropped column, or a derived one nobody asked for |
| every primary key column present | a model that cannot be joined |
| reads `source('raw', '<TABLE>')` | a model pointed at the wrong table |
| no `select *` | a column list a reviewer cannot read |

One thing is corrected rather than rejected: dbt resolves a source name
case-sensitively against `sources.yml`, so `source('raw', 'ap_accrual')`
parses and then fails to compile. Spelling is mechanical, so it is rewritten.

**No mart is wired up.** Where a new dataset belongs in the reporting layer
has accounting consequences. The PR says where the agent thinks it belongs
and stops.

**`control/onboard.py`**

```python
"""Onboarding a new source: contract to buildable model.

An ungoverned table used to stop at a contract. A contract alone changes
nothing: the table is still not declared as a dbt source, has no staging model,
and no tests. Onboarding finishes the job in one reviewed pull request.

Deterministic where the work is mechanical, model where it is judgement:

| Artifact                    | Who       | Why |
|-----------------------------|-----------|-----|
| sources.yml entry           | code      | one line, no judgement |
| tests from the contract     | code      | the contract already states the keys and nullability |
| staging model               | model     | which codes to upper, what to trim, what to name things |
| verification of all of it   | code      | the column set must match the contract exactly |

A mart is never wired up automatically. Where a new dataset belongs in the
reporting layer is a modelling decision with accounting consequences, and the
PR says so instead of guessing.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .contracts import Contract

STAGING = ROOT / "dbt" / "models" / "staging"
SOURCES = STAGING / "sources.yml"
SCHEMA = STAGING / "staging.yml"


@dataclass
class Onboarding:
    dataset_key: str
    table: str
    staging_sql: str = ""
    sources_after: str = ""
    schema_after: str = ""
    notes: str = ""                 # the model's note on where this might belong
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def model_name(self) -> str:
        return f"stg_{self.table.lower()}"

    @property
    def staging_path(self) -> Path:
        return STAGING / f"{self.model_name}.sql"

    @property
    def branch(self) -> str:
        return f"onboard/{self.table.lower()}"


# --------------------------------------------------------------------------
# deterministic artifacts
# --------------------------------------------------------------------------

def add_source(table: str, sources_yml: str) -> tuple[str, str | None]:
    """Append the table to the raw source list. Returns (text, error)."""
    if re.search(rf"^\s*-\s*name:\s*{re.escape(table)}\s*$", sources_yml, re.MULTILINE):
        return sources_yml, None
    lines = sources_yml.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip() == "tables:"), None)
    if idx is None:
        return sources_yml, "no `tables:` block in sources.yml"
    # indentation of the first entry under tables:
    entry_indent = None
    last = idx
    for i in range(idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if entry_indent is None:
            if not stripped.startswith("- "):
                return sources_yml, "unexpected shape under `tables:`"
            entry_indent = indent
        if indent < entry_indent:
            break
        last = i
    if entry_indent is None:
        return sources_yml, "`tables:` block is empty"
    lines.insert(last + 1, f"{' ' * entry_indent}- name: {table}")
    return "\n".join(lines) + "\n", None


def tests_for(contract: Contract) -> list[str]:
    """dbt tests the contract already justifies. Nothing invented."""
    out = []
    pk = [c.upper() for c in contract.primary_key]
    for c in contract.columns:
        tests = []
        if c.name.upper() in pk:
            tests = ["unique", "not_null"]
        elif not c.nullable:
            tests = ["not_null"]
        if tests:
            out.append(f"      - name: {c.name.lower()}\n"
                       f"        data_tests: [{', '.join(tests)}]")
    return out


def add_schema(contract: Contract, model: str, schema_yml: str) -> tuple[str, str | None]:
    if re.search(rf"^\s*-\s*name:\s*{re.escape(model)}\s*$", schema_yml, re.MULTILINE):
        return schema_yml, None
    tests = tests_for(contract)
    if not tests:
        return schema_yml, None
    desc = contract.description or f"Staging over {contract.dataset}"
    block = [f"  - name: {model}",
             # JSON strings are valid YAML double quoted scalars, so a colon or a
             # hash in the contract description cannot break the file.
             f"    description: {json.dumps(desc)}",
             "    columns:", *tests]
    text = schema_yml.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    return text, None


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _split_top_level(body: str) -> list[str]:
    """Split a select list on commas that are not inside brackets.

    `nullif(trim(x), '')` and `rate::number(18,8)` both carry commas that do not
    separate columns, so a plain split would invent columns that do not exist.
    """
    out, depth, buf = [], 0, []
    for ch in body:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def selected_columns(sql: str) -> set[str]:
    """Column names a simple `select a, b as c from ...` model produces."""
    # Comments go first: a shield comment carries prose with commas in it, and
    # splitting before stripping would mistake that prose for a column.
    clean = re.sub(r"--[^\n]*", "", sql)
    m = re.search(r"\bselect\b(.*?)\bfrom\b", clean, re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    out = set()
    for item in _split_top_level(m.group(1)):
        expr = " ".join(item.split()).strip()
        if not expr:
            continue
        alias = re.search(r"\bas\s+(\w+)\s*$", expr, re.IGNORECASE)
        name = alias.group(1) if alias else (expr if re.fullmatch(r"\w+", expr) else None)
        if name:
            out.add(name.upper())
    return out


def _source_ref(table: str) -> re.Pattern:
    return re.compile(r"source\(\s*['\"]raw['\"]\s*,\s*['\"]" + re.escape(table)
                      + r"['\"]\s*\)", re.IGNORECASE)


def canonical_source(sql: str, table: str) -> str:
    """Rewrite the source reference to the one spelling dbt will resolve.

    dbt matches a source name against sources.yml case-sensitively, and the
    entries there are upper case. A model that reads source('raw', 'ap_accrual')
    parses fine and then fails to compile. Spelling is mechanical, so it is
    corrected here rather than bounced back to the model.
    """
    return _source_ref(table).sub(f"source('raw', '{table}')", sql)


def verify(ob: Onboarding, contract: Contract) -> Onboarding:
    sql = ob.staging_sql
    if not sql.strip():
        ob.errors.append("no staging model was produced")
        return ob
    if not _source_ref(ob.table).search(sql):
        ob.errors.append(f"staging model does not read from source('raw', '{ob.table}')")

    want = {c.name.upper() for c in contract.columns}
    got = selected_columns(sql)
    missing, extra = sorted(want - got), sorted(got - want)
    if missing:
        ob.errors.append(f"staging model omits contracted columns {missing}")
    if extra:
        ob.errors.append(f"staging model produces columns the contract does not declare {extra}")
    for k in contract.primary_key:
        if k.upper() not in got:
            ob.errors.append(f"primary key column {k} is not in the staging model")
    if re.search(r"\bselect\s+\*", sql, re.IGNORECASE):
        ob.errors.append("staging model uses select *, which hides the column list from review")
    return ob


def build(contract: Contract, staging_sql: str, notes: str = "") -> Onboarding:
    table = contract.table
    ob = Onboarding(dataset_key=contract.dataset, table=table,
                    staging_sql=canonical_source(staging_sql.rstrip(), table) + "\n",
                    notes=notes)
    verify(ob, contract)

    src, err = add_source(table, SOURCES.read_text())
    if err:
        ob.errors.append(f"sources.yml: {err}")
    ob.sources_after = src

    sch, err = add_schema(contract, ob.model_name, SCHEMA.read_text())
    if err:
        ob.errors.append(f"staging.yml: {err}")
    ob.schema_after = sch
    return ob


# --------------------------------------------------------------------------
# pull request text
# --------------------------------------------------------------------------

def pr_title(ob: Onboarding) -> str:
    return f"Onboard {ob.dataset_key}: contract, source, staging model and tests"


def pr_body(ob: Onboarding, contract: Contract, impact_md: str | None) -> str:
    tests = tests_for(contract)
    lines = [
        f"`{ob.dataset_key}` landed in the warehouse with no contract. This onboards it.",
        "",
        "| Artifact | What |",
        "|---|---|",
        f"| `contracts/raw/{ob.table.lower()}.yml` | v{contract.version} contract, "
        f"{len(contract.columns)} columns, key `{', '.join(contract.primary_key)}` |",
        f"| `dbt/models/staging/sources.yml` | declares `{ob.table}` as a dbt source |",
        f"| `dbt/models/staging/{ob.model_name}.sql` | staging model over every contracted column |",
        f"| `dbt/models/staging/staging.yml` | {len(tests)} column(s) tested from the contract's own keys and nullability |",
        "",
        "**Not wired into any mart.** Where this belongs in the reporting layer is a "
        "modelling decision with accounting consequences, so it is left to a reviewer.",
    ]
    if ob.notes:
        lines += ["", f"Agent note: {ob.notes}"]
    lines += ["", "**Review checklist**",
              f"- Is `{', '.join(contract.primary_key)}` really the key?",
              "- Are the inferred column descriptions right?",
              "- Should this feed a mart, and which?"]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    return "\n".join(lines)
```
**`control/agent.py` `ONBOARD_TOOL`, `ONBOARD_TOOL_SCHEMA`, `ONBOARD_SYSTEM`, `build_onboard_prompt`, `draft_staging`, `publish_onboarding`**

```python
ONBOARD_TOOL = "propose_staging_model"


ONBOARD_TOOL_SCHEMA = {
    "name": ONBOARD_TOOL,
    "description": "Return a dbt staging model over a newly contracted raw table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "staging_sql": {
                "type": "string",
                "description": "Complete dbt model file. One select over the raw source, "
                               "one output column per contracted column, same names.",
            },
            "notes": {
                "type": "string",
                "description": "Two or three sentences for the reviewer: what this table "
                               "appears to hold, which mart it might belong to and why you "
                               "did not wire it, anything you had to guess.",
            },
        },
        "required": ["staging_sql", "notes"],
    },
}


ONBOARD_SYSTEM = """You write dbt staging models for a finance data warehouse. A new raw
table has just been put under contract. Write the staging model that makes it usable.

The staging layer normalises, it does not interpret. Rules you must follow exactly:
- Output exactly one column per column in the contract, with the contract's own name
  in lower case. No extra columns, no derived or calculated columns, none dropped.
  A reviewer adds business logic later; inventing it here hides it from review.
- Read from {{ source('raw', 'TABLE') }} and nothing else. No joins, no ref().
- Never use select *. The column list is what a reviewer reads.
- Clean only where the column's own type and meaning justify it: upper() on codes,
  statuses and currencies, trim() on free text, nullif(trim(x), '') where an empty
  string is really a missing value. Leave amounts, dates, ids and flags untouched:
  casting or rounding money in staging is a business decision.
- Do not wire the table into any mart. Say in notes where you think it belongs.
"""


def build_onboard_prompt(contract_yaml: str, observed: dict[str, ObservedColumn],
                         table: str, example_model: str) -> str:
    return "\n".join([
        f"Raw table: RAW.{table}", "",
        "Its contract, merged in this same pull request:", "```yaml", contract_yaml, "```", "",
        "Live schema from INFORMATION_SCHEMA:", _observed_json(observed), "",
        "An existing staging model in this project, for style:", "```sql", example_model, "```", "",
        f"Call {ONBOARD_TOOL} with the staging model for this table.",
    ])


def draft_staging(contract_yaml: str, observed: dict[str, ObservedColumn], table: str,
                  client=None, model: str | None = None) -> tuple[str, str]:
    """Ask the model for the staging SQL. Returns (staging_sql, notes), unverified."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (ROOT / "dbt" / "models" / "staging" / "stg_ap_vendor.sql").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=3000,
        system=ONBOARD_SYSTEM,
        tools=[ONBOARD_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": ONBOARD_TOOL},
        messages=[{"role": "user",
                   "content": build_onboard_prompt(contract_yaml, observed, table, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    return block.input["staging_sql"], block.input.get("notes", "")


def publish_onboarding(proposal: Proposal, ob, impact_md: str | None,
                       base: str | None = None) -> str:
    """Contract, source entry, staging model and tests in one PR. Never auto merges."""
    from .onboard import SCHEMA, SOURCES, pr_body, pr_title
    assert proposal.ok and proposal.contract and ob.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "onboard", "--color", "0E8A16",
                    "--description", "brings an ungoverned table under contract", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(ob.branch)
    if existing:
        return existing
    try:
        _start_branch(ob.branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        ob.staging_path.write_text(ob.staging_sql)
        SOURCES.write_text(ob.sources_after)
        SCHEMA.write_text(ob.schema_after)
        files = [str(p.relative_to(ROOT)) for p in
                 (proposal.contract_path, ob.staging_path, SOURCES, SCHEMA)]
        _run(["git", "add", *files])
        title = pr_title(ob)
        body = (f"{pr_body(ob, proposal.contract, impact_md)}\n\n---\n"
                f"Agent reasoning: {proposal.reasoning}\n\n"
                f"Drift events: {', '.join(proposal.bundle.event_ids)}\n")
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(ob.branch)
        return _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", ob.branch,
            "--label", "drift", "--label", "medium", "--label", "onboard",
        ]).splitlines()[-1]
    finally:
        _run(["git", "checkout", "-q", base])
```
**`control/cli.py` `DRY`, `_onboard`**

```python
DRY = "dry-run"


def _onboard(b, p, dry_run: bool):
    """An ungoverned table needs more than a contract to be usable.

    The contract says what the table is. The source entry, the staging model and
    the tests are what let anything read it. All four land in one PR, and the
    column list of the staging model is checked against the contract before the
    PR is opened.
    """
    sql, notes = draft_staging(p.contract_yaml, b.observed, b.table)
    ob = onboard_mod.build(p.contract, sql, notes)
    if not ob.ok:
        console.print("  [red]staging model rejected by verification:[/red]")
        for e in ob.errors:
            console.print(f"    {e}")
        console.print("  [dim]rejected model:[/dim]")
        console.print(ob.staging_sql)
        return None
    console.print(f"  onboarding: contract v{p.contract.version}, source entry, "
                  f"[bold]{ob.model_name}[/bold], "
                  f"{len(onboard_mod.tests_for(p.contract))} tested column(s)")
    console.print(f"  [dim]{notes}[/dim]")
    if dry_run:
        console.print("  [dim]dry run, printing staging model:[/dim]")
        console.print(ob.staging_sql)
        return DRY
    di = b.dataset_impact()
    return publish_onboarding(p, ob, di.markdown() if di and not di.empty else None)
```
**Commands**

```bash
gh pr close 1
python -m control.cli sync
python -m control.cli agent --dataset RAW.AP_ACCRUAL --dry-run
python -m control.cli agent --dataset RAW.AP_ACCRUAL
```

PR #1 had to be closed first: the onboarding PR carries the contract itself.
`sync` saw the closed PR and moved the event back to OPEN.

**The dry run.** The model drafted a staging model that uppercased the two
code columns and left GL account, period, amounts and timestamp alone, and
declined to wire a mart while saying where it thought the table belonged.

**The first live publish failed, and exposed three defects in the publish
path**, none of them about the model:

*The traceback had no error in it.* `_run` used `check_output`, and
`CalledProcessError` prints the command and exit code but not the output.
Forty lines that did not contain the reason.

*The branch was built on the wrong base.* Its parent was `975860d`;
`origin/main` was three commits later. The last three commits were local only
when the agent ran, so the PR diff read as deleting `docs/SCENARIOS.md`.
Branching from `origin/main` is correct and stays; the missing half was
refusing to publish when local `main` is ahead of the remote.

*A dead run blocked its own retry.* The push succeeded, `gh` failed, the
branch stayed, and `git checkout -b` failed next time.

**`control/agent.py` `_run`, `_start_branch`, `_push`, `_ensure_pushed`**

```python
def _run(cmd: list[str], **kw) -> str:
    """Run a command, and on failure say what it actually said.

    CalledProcessError prints the command and the exit code but not the output,
    so a failing `gh pr create` used to produce a traceback with no reason in it.
    """
    try:
        return subprocess.check_output(cmd, cwd=ROOT, text=True,
                                       stderr=subprocess.STDOUT, **kw).strip()
    except subprocess.CalledProcessError as e:
        out = (e.output or "").strip()
        raise SystemExit(f"{' '.join(cmd[:3])} failed (exit {e.returncode}):\n{out}") from None


def _start_branch(branch: str, base: str):
    """Branch from the remote base, discarding any leftover from a failed run.

    Always from origin, never from local HEAD: otherwise an unpushed local commit
    is swept into the PR. A branch left behind by a run that died after the push
    is deleted first, so a retry is not blocked by its own wreckage.
    """
    _run(["git", "fetch", "-q", "origin", base])
    subprocess.run(["git", "branch", "-q", "-D", branch], cwd=ROOT,
                   capture_output=True, text=True)
    _run(["git", "checkout", "-q", "-b", branch, f"origin/{base}"])


def _push(branch: str):
    # --force-with-lease only ever overwrites a branch from an earlier failed run
    # of this same agent: if a PR existed, the caller returned before reaching here.
    _run(["git", "push", "-q", "--force-with-lease", "-u", "origin", branch])


def _ensure_pushed(base: str):
    """Refuse to open a PR against a base that is missing your local commits.

    The agent branches from origin/<base> so unpushed local work is never swept
    into a PR. The other half of that rule: if local <base> is ahead of the
    remote, every file you have changed locally but not pushed shows up in the
    PR diff as a deletion or a revert. The PR is not wrong, it is just built on
    a base nobody else can see yet.
    """
    _run(["git", "fetch", "-q", "origin", base])
    ahead = _run(["git", "rev-list", "--count", f"origin/{base}..{base}"])
    if ahead != "0":
        raise SystemExit(
            f"local '{base}' is {ahead} commit(s) ahead of origin/{base}. The agent "
            f"branches from origin, so the PR would read as reverting them.\n"
            f"Run: git push"
        )
```

**Then the gate failed PR #7 on a test.** `test_all_contracts_parse` asserted
`len(contracts) == 8`. The PR added a ninth. A test that fails whenever the
product succeeds is measuring the wrong property. It now derives the count
from the contract directory. The same fix exposed an owner check comparing
against the literal `"unassigned"` while the agent writes
`"unassigned-needs-review"`.

**`tests/test_contracts.py`**

```python
"""The shipped contracts must parse, and hashing must be change sensitive."""
import pytest

from control.contracts import CONTRACT_DIR, load_contracts, parse_contract

# What the agent sets on a dataset it has just discovered. A reviewer replaces it.
ONBOARDING_OWNER = "unassigned-needs-review"


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


def test_every_contract_file_parses(contracts):
    """One contract per file, no silent drops.

    This used to assert a hard count of 8. Onboarding a new source is something
    the system is built to do, so a test that fails whenever it succeeds was
    testing the wrong thing. It now checks the shape instead: every file yields
    exactly one contract, and the core AP/AR datasets are all present.
    """
    files = sorted(CONTRACT_DIR.rglob("*.yml")) + sorted(CONTRACT_DIR.rglob("*.yaml"))
    assert len(contracts) == len(files)
    assert {c.dataset for c in contracts} >= {
        "RAW.AP_VENDOR", "RAW.AP_INVOICE", "RAW.AP_INVOICE_LINE", "RAW.AP_PAYMENT",
        "RAW.AR_CUSTOMER", "RAW.AR_INVOICE", "RAW.AR_RECEIPT", "RAW.FX_RATE",
    }


def test_dataset_keys_are_unique(contracts):
    keys = [c.dataset for c in contracts]
    assert len(keys) == len(set(keys)), "two contracts claim the same dataset"


def test_every_contract_has_a_primary_key_and_owner(contracts):
    """`unassigned` is not an owner, and neither is an empty string.

    A freshly onboarded contract is allowed to carry ONBOARDING_OWNER, because
    the agent cannot know who owns a table that landed without review. The PR
    asks a reviewer to set it. Nothing else may be ownerless.
    """
    for c in contracts:
        assert c.primary_key, f"{c.dataset} has no primary key"
        assert c.owner.strip(), f"{c.dataset} has no owner"
        assert c.owner == ONBOARDING_OWNER or not c.owner.startswith("unassigned"), (
            f"{c.dataset} owner is {c.owner!r}; use a real team or "
            f"{ONBOARDING_OWNER!r} on a brand new dataset"
        )


def test_primary_key_columns_are_never_nullable(contracts):
    for c in contracts:
        for k in c.primary_key:
            assert c.column(k).nullable is False, f"{c.dataset}.{k} is a nullable key"


def test_hash_ignores_column_order_but_not_content(contracts):
    c = next(x for x in contracts if x.dataset == "RAW.AP_INVOICE")
    h = c.spec_hash()
    c.columns.reverse()
    assert c.spec_hash() == h
    c.columns[0].nullable = not c.columns[0].nullable
    assert c.spec_hash() != h


def test_bad_primary_key_is_rejected(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text(
        "dataset: RAW.X\nversion: 1\nprimary_key: [NOPE]\n"
        "columns:\n  - name: A\n    type: TEXT\n    nullable: false\n"
    )
    with pytest.raises(ValueError, match="unknown columns"):
        parse_contract(p)
```
**Commands: bring the PR branch up to date**

```bash
git checkout onboard/ap_accrual
git merge origin/main -m "Merge main for the contract test fix"
git push
git checkout main
```

**Merged:** PR #7 `Onboard RAW.AP_ACCRUAL: contract, source, staging model and
tests` as `99760b0`. All four artifacts on `main`.

**`contracts/raw/ap_accrual.yml` as merged in PR #7 (agent-drafted)**

```yaml
# fin_aiwh data contract
#
# This file is the agreement, not a description. The warehouse is compared
# against it on every detector run. Changing it is a reviewed pull request.

dataset: RAW.AP_ACCRUAL
version: 1
owner: unassigned-needs-review
classification: financial-restricted
description: Accounts payable accrual entries for period-end adjustments.

primary_key:
  - ACCRUAL_ID

freshness:
  column: LOADED_AT
  max_lag_hours: 24

# Columns the contract guarantees. A column present here and missing in the
# warehouse is a break. A column in the warehouse and missing here is
# ungoverned and must be adopted or rejected explicitly.
columns:
  - name: ACCRUAL_ID
    type: TEXT
    length: 32
    nullable: false
    description: Unique identifier for the accrual entry (inferred from column name).
  - name: ENTITY_CODE
    type: TEXT
    length: 8
    nullable: false
    description: Legal entity code for which the accrual is recorded (inferred from column name).
  - name: GL_ACCOUNT
    type: TEXT
    length: 16
    nullable: false
    description: General ledger account code for the accrual (inferred from column name).
  - name: PERIOD
    type: TEXT
    length: 7
    nullable: false
    description: Accounting period for the accrual, typically YYYY-MM format (inferred from column name).
  - name: ACCRUAL_AMOUNT
    type: NUMBER
    precision: 18
    scale: 2
    nullable: false
    description: Monetary value of the accrual entry (inferred from column name).
  - name: CURRENCY_CODE
    type: TEXT
    length: 3
    nullable: false
    description: ISO 4217 currency code for the accrual amount (inferred from column name).
  - name: LOADED_AT
    type: TIMESTAMP_NTZ
    nullable: false
    description: Ingestion watermark.
```
**`dbt/models/staging/stg_ap_accrual.sql` as merged in PR #7 (agent-drafted)**

```sql
select
    accrual_id,
    upper(entity_code)   as entity_code,
    upper(gl_account)    as gl_account,
    period,
    accrual_amount,
    upper(currency_code) as currency_code,
    loaded_at
from {{ source('raw', 'AP_ACCRUAL') }}
```
**Commands: after the merge**

```bash
git pull
python -m control.cli register
python -m control.cli sync
python -m control.cli detect
python -m control.cli dbt build
```

---

## Stage 23. History rewritten to one author

**Commit:** `d0ac356` (recorded)

Commits had accumulated across three identities, plus GitHub as committer on
the PR merges, plus `Co-authored-by` trailers naming both a second personal
account and the model. GitHub counts each as a contributor. All 38 commits at
the time were rewritten to one identity, trailers stripped, the tree verified
byte-identical to the pre-rewrite tip. A pre-rewrite bundle was kept at
`.git/backup-preRewrite.bundle`.

The `@users.noreply.github.com` form is used on purpose: GitHub attributes a
commit to an account by email, that address is derived from the account, so
attribution cannot miss and no personal address is published.

**Commands**

```bash
git bundle create .git/backup-preRewrite.bundle --all
git config user.name  manojnayakgit
git config user.email 39649907+manojnayakgit@users.noreply.github.com

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f \
  --env-filter '
    export GIT_AUTHOR_NAME=manojnayakgit
    export GIT_AUTHOR_EMAIL=39649907+manojnayakgit@users.noreply.github.com
    export GIT_COMMITTER_NAME=manojnayakgit
    export GIT_COMMITTER_EMAIL=39649907+manojnayakgit@users.noreply.github.com
  ' \
  --msg-filter 'python3 .git/msgfilter.py' \
  -- main drift/ap_accrual-v1 onboard/ap_accrual shield/ap_payment shield/ar_invoice

git push --force origin main drift/ap_accrual-v1 onboard/ap_accrual shield/ap_payment shield/ar_invoice
gh auth refresh -s workflow
```

`msgfilter.py` dropped any line matching `Co-authored-by:`, `Claude-Session:`
or `Generated with [Claude Code]` and trailing blank lines. Known consequence,
recorded: merged PRs #2, #5, #6 point at commit SHAs that no longer exist on
`main`. The `workflow` scope was needed afterwards because the token predated
the first edit to a workflow file.

---

## Stage 24. Retiring a shield

**Commits:** `6d78d1d` (retirement, scenario 08), PR #8 `de7e079`, PR #9 `1756b39`, `9ed0ac3` (idempotency), `cdc04dd` and `0b4e690` (recorded)

A shield is temporary by definition. Every `agent` run first checks every
installed shield against the **live schema**, not the event table, because
an escalated event can outlive the divergence it recorded.

| Step | What |
|---|---|
| Find | `installed()` reads every `-- shield:` marker; `stale()` keeps those whose column no longer diverges |
| Invert | `retire_line()` restores the select line from the shield's own text: `null::T as col` to `col`, `cast(expr as T) as col` to `expr as col`. A pass-through shield loses its header comment and its guard test file |
| Refuse | any line that does not match a shape `apply()` wrote |
| Publish | branch `retire/<table>`, label `retire`, body says `Closes <issue>` |
| Close | merging closes the issue on GitHub; the next `sync` moves the event to DISMISSED. No new state was added |

**`ops/scenarios/08_upstream_fixed.sql`**

```sql
-- Scenario: the source team repairs both breaking changes.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: the two BREAKING events stop being raised, and `detect` reports the
-- two installed shields as stale. Nothing here touches AP_ACCRUAL, which is now
-- contracted, and nothing reverts REVENUE_STREAM, which is a MEDIUM the project
-- should adopt rather than push back upstream.
--
-- This is the other half of scenarios 05 and 06. A shield is a temporary
-- measure by definition, so the system has to be able to take one back out.

-- 05 reversed: the dropped column returns. The values do not. The shield said
-- exactly that, and re-adding the column as nullable is the honest repair:
-- the shape is restored, the history is still missing.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT
  ADD COLUMN IF NOT EXISTS BANK_REF VARCHAR(64)
  COMMENT 'Bank reference, restored by the source team after being dropped';

-- 06 reversed: the guarantee comes back. This fails if any null slipped in
-- while the guarantee was gone, which is the correct outcome: the source team
-- must clean those rows before it can promise NOT NULL again.
UPDATE FIN_AIWH.RAW.AR_INVOICE SET STATUS = 'OPEN' WHERE STATUS IS NULL;
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS SET NOT NULL;
```
**`control/shield.py` `_NULL_SHIELD`, `_CAST_SHIELD`, `_ISSUE`, `Retirement`, `retire_line`, `plan_retirement`, `retire_title`, `retire_body`**

```python
_NULL_SHIELD = re.compile(
    r"^(?P<indent>\s*)null::[\w(),\s]+\s+as\s+(?P<col>\w+)(?P<comma>,?)\s*" + re.escape(MARK),
    re.IGNORECASE)


_CAST_SHIELD = re.compile(
    r"^(?P<indent>\s*)cast\(\s*(?P<expr>.+?)\s+as\s+[\w(),\s]+\)\s+as\s+(?P<col>\w+)(?P<comma>,?)\s*"
    + re.escape(MARK), re.IGNORECASE)


_ISSUE = re.compile(r"see (https://\S+)")


@dataclass
class Retirement:
    table: str
    columns: list[str] = field(default_factory=list)
    staging_before: str = ""
    staging_after: str = ""
    drop_tests: list[Path] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.columns or self.drop_tests) and not self.errors

    @property
    def staging_path(self) -> Path:
        return STAGING / f"stg_{self.table.lower()}.sql"

    @property
    def branch(self) -> str:
        return f"retire/{self.table.lower()}"


def retire_line(line: str) -> str | None:
    """A shielded select line restored to what it was. None if the shape is unknown."""
    m = _NULL_SHIELD.match(line)
    if m:
        return f"{m.group('indent')}{m.group('col').lower()}{m.group('comma')}"
    m = _CAST_SHIELD.match(line)
    if m:
        expr, col = m.group("expr").strip(), m.group("col")
        body = col.lower() if expr.lower() == col.lower() else f"{expr} as {col.lower()}"
        return f"{m.group('indent')}{body}{m.group('comma')}"
    return None


def plan_retirement(table: str, columns: set[str]) -> Retirement:
    """Remove the shields for these columns from one staging model."""
    r = Retirement(table=table)
    path = r.staging_path
    if not path.exists():
        r.errors.append(f"no staging model at {path.name}")
        return r

    sql = path.read_text()
    r.staging_before = sql
    out, wanted = [], {c.upper() for c in columns}
    seen = set()

    for line in sql.splitlines():
        if MARK not in line:
            out.append(line)
            continue
        note = line[line.index(MARK) + len(MARK):].strip()
        col = note.split(" ", 1)[0].upper()
        if col not in wanted:
            out.append(line)
            continue
        seen.add(col)
        found = _ISSUE.search(note)
        if found and found.group(1) not in r.issues:
            r.issues.append(found.group(1))

        if line.strip().startswith(MARK):
            # a pass-through shield: the note is the whole line, and its guard
            # is a singular test file that goes with it
            t = TESTS / f"shield_{table.lower()}_{col.lower()}_not_null.sql"
            if t.exists():
                r.drop_tests.append(t)
            r.columns.append(col)
            continue

        restored = retire_line(line)
        if restored is None:
            r.errors.append(
                f"{col}: shield line does not match a shape this can safely undo, "
                f"refusing to guess: {line.strip()}")
            out.append(line)
            continue
        out.append(restored)
        r.columns.append(col)

    for missing in sorted(wanted - seen):
        r.errors.append(f"{missing}: no shield marker found in {path.name}")

    text = "\n".join(out)
    # a pass-through shield leaves its blank separator line behind
    while text.startswith("\n"):
        text = text[1:]
    r.staging_after = text + ("\n" if sql.endswith("\n") else "")
    if "source('raw'" not in r.staging_after:
        r.errors.append("staging model no longer reads from source('raw', ...)")
    return r


def retire_title(r: Retirement) -> str:
    return f"Retire the {r.table} shield: upstream is repaired"


def retire_body(r: Retirement) -> str:
    lines = [
        f"`RAW.{r.table}` matches its contract again, so the shield in "
        f"`stg_{r.table.lower()}` has nothing left to do. Leaving it would keep "
        f"serving the shield's value instead of the real one.",
        "",
        "| column | shield removed | now reads |",
        "|---|---|---|",
    ]
    for c in r.columns:
        lines.append(f"| `{c}` | yes | the live column |")
    if r.drop_tests:
        lines += ["", "**Guard tests removed**", *[f"- `{p.name}`" for p in r.drop_tests]]
    lines += [
        "",
        "The detector confirmed the drift is gone before this was opened. If it "
        "reappears, the next cycle raises it again and proposes a fresh shield.",
    ]
    for url in r.issues:
        lines += ["", f"Closes {url}"]
    return "\n".join(lines)
```
**`control/agent.py` `publish_retirement`**

```python
def publish_retirement(r, base: str | None = None) -> str:
    from .shield import retire_body, retire_title
    assert r.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "retire", "--color", "BFD4F2",
                    "--description", "removes a shield whose drift is repaired", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(r.branch)
    if existing:
        return existing
    try:
        _start_branch(r.branch, base)
        r.staging_path.write_text(r.staging_after)
        _run(["git", "add", str(r.staging_path.relative_to(ROOT))])
        for t in r.drop_tests:
            _run(["git", "rm", "-q", str(t.relative_to(ROOT))])
        title, body = retire_title(r), retire_body(r)
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(r.branch)
        url = _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", r.branch,
            "--label", "drift", "--label", "retire",
        ]).splitlines()[-1]
        for issue in r.issues:
            subprocess.run(
                ["gh", "issue", "comment", issue, "--body",
                 f"Upstream is repaired and the detector no longer raises this. "
                 f"Retirement proposed: {url}. Merging it closes this issue."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])
```
**`control/cli.py` `_retire_stale`**

```python
def _retire_stale(active: set[tuple[str, str]], dataset: str | None, dry_run: bool) -> int:
    """Open a retirement PR for every shield whose drift is gone.

    Judged against the live schema, not the event table. An escalated event can
    outlive the divergence it recorded; the shield's own column is the truth.
    """
    by_table: dict[str, set[str]] = {}
    for table, col, _ in shield_mod.stale(active):
        if dataset and f"RAW.{table}" != dataset.upper():
            continue
        by_table.setdefault(table, set()).add(col)
    if not by_table:
        return 0
    rc = 0
    for table, cols in sorted(by_table.items()):
        r = shield_mod.plan_retirement(table, cols)
        console.print(f"[bold]RAW.{table}[/bold]  shield stale  {', '.join(sorted(cols))}")
        if not r.ok:
            rc = 1
            console.print("  [yellow]retirement refused:[/yellow] " + "; ".join(r.errors))
            continue
        if dry_run:
            console.print("  → would open a retirement PR and close " + ", ".join(r.issues))
            console.print("  [dim]dry run, printing restored model:[/dim]")
            console.print(r.staging_after)
            continue
        url = publish_retirement(r)
        console.print(f"  [cyan]retirement PR[/cyan] {url}")
        if r.drop_tests:
            console.print(f"  [dim]removes {', '.join(p.name for p in r.drop_tests)}[/dim]")
        console.print("")
    return rc
```
**Commands**

```bash
python -m control.cli apply ops/scenarios/08_upstream_fixed.sql
python -m control.cli detect
python -m control.cli agent --dry-run
python -m control.cli agent
```

**Live.**

```
RAW.AP_PAYMENT  shield stale  BANK_REF
  retirement PR https://github.com/manojnayakgit/fin_aiwh/pull/8

RAW.AR_INVOICE  shield stale  STATUS
  retirement PR https://github.com/manojnayakgit/fin_aiwh/pull/9
  removes shield_ar_invoice_status_not_null.sql
```

**Then the same run died.** After retiring, the agent moved to its "already
escalated" pass, found the AP_PAYMENT event still ESCALATED in the database,
and tried to shield it again. The shield was already on `main`, so the rewrite
produced an identical file and `git commit` had nothing to commit. Two
defects, one older than scenario 08:

→ the escalated pass trusted the event table; it now consults the live schema, the same source retirement uses
→ shields never checked whether they were already installed, so any rerun after a shield merged would have failed the same way

One rule fixes both, visible in `_shield` at Stage 21: anything that acts on
a column consults the live schema and the installed shields, never an event's
stored status.

**Merged:** PR #8 `de7e079`, PR #9 `1756b39`. Both staging models back to
plain selects, the guard test gone, issues #3 and #4 closed by the `Closes`
lines.

**Commands: close the loop**

```bash
git pull
python -m control.cli sync
python -m control.cli dbt build
python -m control.cli agent
```

`sync` dismissed both events, `dbt build` passed with one fewer test, and the
final `agent` run, the rerun that used to crash, reported nothing to do.

**Eight scenarios, every path proven live**, no manual step other than
clicking Merge on a pull request the system wrote. 120 tests.

---

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

**`ops/probe_dmf.sql`**

```sql
-- Can this account use Data Metric Functions at all?
-- DMFs are an Enterprise Edition feature. Run this in Snowsight as ACCOUNTADMIN.
-- If step 2 errors, DMFs are not available and the quality layer needs the
-- fallback design instead.

-- 1. what edition and region are we on
SELECT CURRENT_VERSION() AS version, CURRENT_ACCOUNT() AS account, CURRENT_REGION() AS region;

-- 2. can we call a system DMF directly
SELECT SNOWFLAKE.CORE.NULL_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE) AS null_ids;

-- 3. can we call the freshness DMF, which is what the contract block needs
SELECT SNOWFLAKE.CORE.FRESHNESS(SELECT LOADED_AT FROM FIN_AIWH.RAW.AP_INVOICE) AS seconds_stale;

-- 4. can the service role be granted the privilege the scheduled checks need
SHOW GRANTS TO ROLE FIN_AIWH_SVC;
```

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

**`control/quality.py`**

```python
"""Content governance: what is in the columns, not only their shape.

Schema drift compares INFORMATION_SCHEMA to the contract. This compares the
data to the contract, using Snowflake Data Metric Functions as the measuring
instrument. Every check is derived from something the contract already states.
Nothing is invented:

| Contract says                | Check                              | Severity |
|------------------------------|------------------------------------|----------|
| primary_key: [A, B]          | DUPLICATE_COUNT on (A, B) == 0     | BREAKING |
| nullable: false              | NULL_COUNT on col == 0             | BREAKING |
| freshness: max_lag_hours: N  | hours since max(column) <= N       | MEDIUM   |
| expectations: (optional)     | any system DMF with min or max     | as stated |

Severity by consequence, the same rule as schema drift. A duplicate key makes
every join fan out and every total double count. A null in a contracted NOT
NULL column is exactly what scenario 06's guard test caught, caught at source.
Stale data leaves every report correct but old, which a human should decide
about rather than the system.

Two ways the DMFs are used, and the split matters:

  Synchronous  `SELECT SNOWFLAKE.CORE.NULL_COUNT(SELECT col FROM t)` runs now
               and answers now. This is what `detect` uses. The control plane
               decides on a measurement it just took, not one Snowflake took
               on its own schedule.

  Attached     `ALTER TABLE t ADD DATA METRIC FUNCTION ...` gives Snowflake
               side history in DATA_QUALITY_MONITORING_RESULTS for audit and
               for people who never run this CLI. `quality attach` reconciles
               the attachments to the contracts; nothing else depends on them.

Freshness uses a custom DMF (ops/20_quality.sql) because the system one refuses
TIMESTAMP_NTZ, which is every LOADED_AT in RAW. A DMF body must be
deterministic, so it cannot read the clock: it returns the newest timestamp and
the calling statement subtracts it from SYSDATE(). The session is pinned to UTC
so the NTZ values and the clock agree.
"""
import json
from dataclasses import dataclass

from .contracts import Contract
from .detect import BREAKING, MEDIUM, LOW, Finding
from .snow import execute, query

DMF_SCHEMA = "FIN_AIWH.META"
FRESHNESS_DMF = f"{DMF_SCHEMA}.NEWEST_EPOCH_NTZ"

SYSTEM_DMFS = {
    "NULL_COUNT", "NULL_PERCENT", "DUPLICATE_COUNT", "UNIQUE_COUNT",
    "ROW_COUNT", "BLANK_COUNT", "BLANK_PERCENT", "AVG", "MIN", "MAX", "STDDEV",
}
QUALITY_TYPES = {"DUPLICATE_KEY", "NULL_IN_REQUIRED", "STALE", "EXPECTATION_BREACHED"}


# SNOWFLAKE.CORE.NULL_COUNT refuses a BOOLEAN argument, cast or not. Columns of
# these types use a custom DMF with the type declared (ops/20_quality.sql),
# which is also attachable. Same pattern as freshness.
DMF_BY_TYPE = {"BOOLEAN": f"{DMF_SCHEMA}.NULL_COUNT_BOOL"}

# A DMF argument is a column reference and nothing else: every expression,
# cast or concatenation is refused whatever its declared type. So a composite
# key, which has no single column to name, is not measured by a DMF at all.
# It is measured by plain SQL in the same statement. Same number, no function.
PLAIN_SQL = "SQL"


@dataclass(frozen=True)
class Check:
    dataset_key: str
    change_type: str          # one of QUALITY_TYPES
    dmf: str                  # fully qualified function
    columns: tuple[str, ...]  # () for table level
    min: float | None
    max: float | None
    severity: str
    why: str                  # what the contract said that justifies this

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[-1]

    @property
    def object_name(self) -> str:
        return ",".join(self.columns) if self.columns else "*"

    @property
    def attachable(self) -> bool:
        return self.dmf != PLAIN_SQL

    @property
    def label(self) -> str:
        name = "DUPLICATE_COUNT" if self.dmf == PLAIN_SQL else self.dmf.rsplit(".", 1)[-1]
        return f"{name}({self.object_name})"

    def sql(self) -> str:
        if self.dmf == PLAIN_SQL:
            keys = ", ".join(self.columns)
            return (f"(SELECT COUNT(*) - COUNT(DISTINCT {keys}) "
                    f"FROM FIN_AIWH.{self.dataset_key})")
        cols = ", ".join(self.columns) if self.columns else "*"
        call = f"{self.dmf}(SELECT {cols} FROM FIN_AIWH.{self.dataset_key})"
        if self.change_type == "STALE":
            # The DMF returns the newest load as epoch seconds (a DMF body may
            # not read the clock). Hours behind is computed here, against
            # SYSDATE() in the same statement, so one clock is used throughout.
            return f"(DATE_PART(EPOCH_SECOND, SYSDATE()) - {call}) / 3600"
        return call

    def breached(self, value: float | None) -> str | None:
        """A reason, or None if the value is within the contract."""
        if value is None:
            return "measurement returned NULL"
        if self.max is not None and value > self.max:
            return f"{self.label} = {value:g}, contract allows at most {self.max:g}"
        if self.min is not None and value < self.min:
            return f"{self.label} = {value:g}, contract requires at least {self.min:g}"
        return None


# --------------------------------------------------------------------------
# what the contract implies
# --------------------------------------------------------------------------

def desired(contract: Contract) -> list[Check]:
    out: list[Check] = []
    key = contract.dataset

    if contract.primary_key:
        out.append(Check(
            dataset_key=key, change_type="DUPLICATE_KEY",
            dmf=PLAIN_SQL if len(contract.primary_key) > 1 else "SNOWFLAKE.CORE.DUPLICATE_COUNT",
            columns=tuple(contract.primary_key), min=None, max=0, severity=BREAKING,
            why=f"primary_key is {contract.primary_key}; a duplicate makes every join fan out",
        ))

    for c in contract.columns:
        if not c.nullable:
            out.append(Check(
                dataset_key=key, change_type="NULL_IN_REQUIRED",
                dmf=DMF_BY_TYPE.get(c.type.upper(), "SNOWFLAKE.CORE.NULL_COUNT"),
                columns=(c.name,), min=None, max=0, severity=BREAKING,
                why=f"{c.name} is contracted NOT NULL",
            ))

    f = contract.freshness or {}
    if f.get("column") and f.get("max_lag_hours") is not None:
        out.append(Check(
            dataset_key=key, change_type="STALE",
            dmf=FRESHNESS_DMF,
            columns=(f["column"],), min=None, max=float(f["max_lag_hours"]), severity=MEDIUM,
            why=f"freshness allows {f['max_lag_hours']}h behind on {f['column']}",
        ))

    for e in contract.raw.get("expectations", []) or []:
        metric = str(e.get("metric", "")).upper()
        if metric not in SYSTEM_DMFS:
            continue
        cols = e.get("columns") or ([e["column"]] if e.get("column") else [])
        sev = str(e.get("severity", MEDIUM)).upper()
        out.append(Check(
            dataset_key=key, change_type="EXPECTATION_BREACHED",
            dmf=f"SNOWFLAKE.CORE.{metric}",
            columns=tuple(cols), min=e.get("min"), max=e.get("max"),
            severity=sev if sev in (LOW, MEDIUM, BREAKING) else MEDIUM,
            why=e.get("description") or f"contract expectation on {metric}",
        ))
    return out


# --------------------------------------------------------------------------
# measuring, synchronously
# --------------------------------------------------------------------------

def measure(conn, checks: list[Check]) -> dict[Check, float | None]:
    """One statement per table. Each DMF call is a column in the result."""
    out: dict[Check, float | None] = {}
    by_table: dict[str, list[Check]] = {}
    for c in checks:
        by_table.setdefault(c.dataset_key, []).append(c)
    for key, group in by_table.items():
        cols = ", ".join(f"{c.sql()} AS M{i}" for i, c in enumerate(group))
        row = query(conn, f"SELECT {cols}")[0]
        for i, c in enumerate(group):
            v = row[f"M{i}"]
            out[c] = float(v) if v is not None else None
    return out


def diff_quality(contracts: list[Contract], measured: dict[Check, float | None]) -> list[Finding]:
    """Findings in the same shape as schema drift, so one event table holds both.

    `after` carries the threshold, not the measured value, so a breach that
    persists across runs has one stable fingerprint and one event. The value
    goes in the rationale, where a human reads it.
    """
    findings = []
    for c in measured:
        reason = c.breached(measured[c])
        if reason is None:
            continue
        findings.append(Finding(
            dataset_key=c.dataset_key,
            change_type=c.change_type,
            severity=c.severity,
            object_name=c.object_name,
            before={"contract": {"min": c.min, "max": c.max}},
            after={"check": c.label, "min": c.min, "max": c.max},
            rationale=f"{reason}. {c.why}",
        ))
    order = {BREAKING: 0, MEDIUM: 1, LOW: 2}
    findings.sort(key=lambda f: (order[f.severity], f.dataset_key, f.object_name or ""))
    return findings


def check_all(conn, contracts: list[Contract]) -> list[Finding]:
    checks = [c for k in contracts for c in desired(k)]
    return diff_quality(contracts, measure(conn, checks)) if checks else []


# --------------------------------------------------------------------------
# attaching, for Snowflake side history
# --------------------------------------------------------------------------

def attached(conn, dataset_key: str) -> set[tuple[str, tuple[str, ...]]]:
    rows = query(conn, f"""
        SELECT METRIC_DATABASE_NAME, METRIC_SCHEMA_NAME, METRIC_NAME, REF_ARGUMENTS
        FROM TABLE(FIN_AIWH.INFORMATION_SCHEMA.DATA_METRIC_FUNCTION_REFERENCES(
            REF_ENTITY_NAME => 'FIN_AIWH.{dataset_key}', REF_ENTITY_DOMAIN => 'TABLE'))
    """)
    out = set()
    for r in rows:
        fn = f"{r['METRIC_DATABASE_NAME']}.{r['METRIC_SCHEMA_NAME']}.{r['METRIC_NAME']}"
        args = r["REF_ARGUMENTS"]
        if isinstance(args, str):
            args = json.loads(args)
        names = tuple(a["name"].upper() for a in (args or []))
        out.add((fn.upper(), names))
    return out


def reconcile(conn, contract: Contract, schedule: str = "TRIGGER_ON_CHANGES") -> tuple[list[str], list[str]]:
    """Make the attached DMFs equal what the contract implies. Returns (added, dropped)."""
    want = {(c.dmf.upper(), tuple(x.upper() for x in c.columns))
            for c in desired(contract) if c.attachable}
    have = attached(conn, contract.dataset)
    table = f"FIN_AIWH.{contract.dataset}"
    added, dropped = [], []
    for fn, cols in sorted(want - have):
        execute(conn, f"ALTER TABLE {table} ADD DATA METRIC FUNCTION {fn} ON ({', '.join(cols)})")
        added.append(f"{fn}({', '.join(cols)})")
    for fn, cols in sorted(have - want):
        if not fn.startswith("SNOWFLAKE.CORE.") and not fn.startswith(DMF_SCHEMA):
            continue        # someone else's attachment, not ours to remove
        execute(conn, f"ALTER TABLE {table} DROP DATA METRIC FUNCTION {fn} ON ({', '.join(cols)})")
        dropped.append(f"{fn}({', '.join(cols)})")
    if added or dropped:
        execute(conn, f"ALTER TABLE {table} SET DATA_METRIC_SCHEDULE = '{schedule}'")
    return added, dropped
```
**`control/agent.py` `escalate_quality`, `close_quality_issue`**

```python
def escalate_quality(bundle: Bundle) -> str:
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "quality", "--color", "D93F0B",
                    "--description", "data breaches its contract", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    imps = [bundle.impact_for(e) for e in bundle.events]
    reports = sorted({r["label"] for i in imps if i for r in i.reports})
    hit = ", ".join(f"**{r}**" for r in reports)
    lines = [
        f"The data in `{bundle.dataset_key}` breaches its contract. The schema is "
        f"fine; the contents are not." + (f" Feeds {hit}." if hit else ""),
        "",
        "| severity | breach | on | measured |",
        "|---|---|---|---|",
    ]
    for e in bundle.events:
        lines.append(f"| {e['SEVERITY']} | {e['CHANGE_TYPE']} | `{e['OBJECT_NAME']}` | {e['RATIONALE']} |")
    lines += [
        "",
        "No pull request is proposed: no contract change makes bad data good. "
        "Fix the data at source. This issue closes itself on the next agent run "
        "after the measurement is back within the contract.",
        "",
        f"Drift events: {', '.join(bundle.event_ids)}",
    ]
    worst = bundle.worst.lower()
    return _run([
        "gh", "issue", "create",
        "--title", f"Data breaches contract: {bundle.dataset_key}",
        "--body", "\n".join(lines),
        "--label", "drift", "--label", "quality", "--label", worst,
    ]).splitlines()[-1]


def close_quality_issue(url: str, why: str):
    subprocess.run(["gh", "issue", "close", url, "--comment",
                    f"Measurement is back within the contract: {why}. Closed by the agent."],
                   cwd=ROOT, capture_output=True, text=True)
```
**`control/cli.py` `_quality_pass`**

```python
def _quality_pass(conn, s, contracts, observed, dataset: str | None, dry_run: bool) -> int:
    """Content breaches: open an issue for each new one, close the ones that cleared.

    Both directions consult a fresh measurement, never the event's own status.
    """
    live = quality_mod.check_all(conn, [c for c in contracts if c.dataset in observed])
    live_keys = {(f.dataset_key, f.object_name) for f in live}

    # 1. breaches that cleared: close the issue, dismiss the event
    esc = query(conn, """
        SELECT EVENT_ID, DATASET_KEY, CHANGE_TYPE, OBJECT_NAME, RESOLUTION_REF
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS = 'ESCALATED' AND CHANGE_TYPE IN (%s)
    """ % ",".join(f"'{q}'" for q in sorted(quality_mod.QUALITY_TYPES)))
    for e in esc:
        if dataset and e["DATASET_KEY"] != dataset.upper():
            continue
        if (e["DATASET_KEY"], e["OBJECT_NAME"]) in live_keys:
            continue
        console.print(f"[bold]{e['DATASET_KEY']}[/bold]  {e['CHANGE_TYPE']} on {e['OBJECT_NAME']} cleared")
        if dry_run:
            console.print(f"  → would close {e['RESOLUTION_REF']} and dismiss the event")
            continue
        if e["RESOLUTION_REF"]:
            close_quality_issue(e["RESOLUTION_REF"], f"{e['CHANGE_TYPE']} on {e['OBJECT_NAME']}")
        set_status(conn, [e["EVENT_ID"]], "DISMISSED")
        console.print(f"  [green]closed[/green] {e['RESOLUTION_REF']}")

    # 2. new breaches with an OPEN event: one issue per dataset
    opened = [e for e in open_events(conn) if e["CHANGE_TYPE"] in quality_mod.QUALITY_TYPES]
    if dataset:
        opened = [e for e in opened if e["DATASET_KEY"] == dataset.upper()]
    rc = 0
    for b in bundle_events(opened, contracts, observed, Lineage.load()):
        console.print(f"[bold]{b.dataset_key}[/bold]  data breaches contract  "
                      f"worst [{SEV_STYLE[b.worst]}]{b.worst}[/]  {len(b.events)} breach(es)")
        for e in b.events:
            console.print(f"  [dim]{e['CHANGE_TYPE']} {e['OBJECT_NAME']}: {e['RATIONALE']}[/dim]")
        if dry_run:
            console.print("  → would open an issue (never a PR)\n")
            continue
        url = escalate_quality(b)
        mark(conn, b.event_ids, "ESCALATED", url)
        console.print(f"  [red]issue[/red] {url}\n")
    return rc
```

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

**Snowsight: `ops/20_quality.sql` (final form)**

```sql
-- Content governance prerequisites. Run once as ACCOUNTADMIN.
--
-- Data Metric Functions need two grants the bootstrap did not give, and the
-- freshness check needs a custom DMF because SNOWFLAKE.CORE.FRESHNESS refuses
-- TIMESTAMP_NTZ, which is every LOADED_AT in RAW.

-- 1. Let the engineering role attach DMFs to tables it owns. Calling a
--    system DMF directly needs no grant: every role has USAGE on them.
GRANT EXECUTE DATA METRIC FUNCTION ON ACCOUNT TO ROLE FIN_AIWH_ENG;
GRANT DATABASE ROLE SNOWFLAKE.DATA_METRIC_USER TO ROLE FIN_AIWH_ENG;

-- 1b. Optional. Only needed to read Snowflake's own history view,
--     SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS, which the CLI does not
--     use: detect measures synchronously. This is an APPLICATION role.
GRANT APPLICATION ROLE SNOWFLAKE.DATA_QUALITY_MONITORING_VIEWER TO ROLE FIN_AIWH_ENG;

-- 2. Freshness. A DMF body must be deterministic, so it cannot know what
--    time it is: that is why Snowflake ships FRESHNESS as a special case.
--    This one returns the newest timestamp as epoch seconds, and the caller
--    subtracts it from now. Two consequences, both fine:
--      - detect compares against SYSDATE() in the same statement
--      - attached, Snowflake's history records the newest load time, not a lag
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.NEWEST_EPOCH_NTZ(
    arg_t TABLE(arg_c TIMESTAMP_NTZ)
)
RETURNS NUMBER
AS
$$
    SELECT DATE_PART(EPOCH_SECOND, MAX(arg_c)) FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.NEWEST_EPOCH_NTZ(TABLE(TIMESTAMP_NTZ)) TO ROLE FIN_AIWH_ENG;

-- 2b. Null count for BOOLEAN columns. SNOWFLAKE.CORE.NULL_COUNT refuses a
--     BOOLEAN argument, and refuses it cast to text or number as well. A custom
--     DMF with the type declared is the honest fix and can be attached.
CREATE OR REPLACE DATA METRIC FUNCTION FIN_AIWH.META.NULL_COUNT_BOOL(
    arg_t TABLE(arg_c BOOLEAN)
)
RETURNS NUMBER
AS
$$
    SELECT COUNT_IF(arg_c IS NULL) FROM arg_t
$$;

GRANT USAGE ON FUNCTION FIN_AIWH.META.NULL_COUNT_BOOL(TABLE(BOOLEAN)) TO ROLE FIN_AIWH_ENG;

-- 2c. Composite keys. There is no DMF for these. A DMF argument is a column
--     reference and nothing else; every expression, cast or concatenation is
--     refused whatever its declared type. A multi-column key is measured by
--     plain SQL in the same statement instead. Same number, no function.

-- 3. Prove it, as ACCOUNTADMIN, before handing to the CLI.
SELECT SNOWFLAKE.CORE.DUPLICATE_COUNT(SELECT INVOICE_ID FROM FIN_AIWH.RAW.AP_INVOICE)  AS dup_invoice_ids,
       SNOWFLAKE.CORE.NULL_COUNT(SELECT GROSS_AMOUNT FROM FIN_AIWH.RAW.AP_INVOICE)      AS null_amounts,
       FIN_AIWH.META.NULL_COUNT_BOOL(SELECT IS_ACTIVE FROM FIN_AIWH.RAW.AP_VENDOR)          AS null_flags,
       (SELECT COUNT(*) - COUNT(DISTINCT RATE_DATE, FROM_CURRENCY, TO_CURRENCY, RATE_TYPE)
        FROM FIN_AIWH.RAW.FX_RATE)                                                            AS dup_fx_keys,
       (DATE_PART(EPOCH_SECOND, SYSDATE())
          - FIN_AIWH.META.NEWEST_EPOCH_NTZ(SELECT LOADED_AT FROM FIN_AIWH.RAW.AP_INVOICE)) / 3600
                                                                                           AS hours_behind;
```

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

**`tests/test_load.py`**

```python
"""Loading must decide whether it can succeed before it truncates anything."""
from control.load import plan_load

CSV = ["INVOICE_ID", "VENDOR_ID", "GROSS_AMOUNT", "LOADED_AT"]


def live(*cols):
    """(name, nullable, default) triples as INFORMATION_SCHEMA would report them."""
    return [{"COLUMN_NAME": n, "IS_NULLABLE": "YES" if nl else "NO", "COLUMN_DEFAULT": d}
            for n, nl, d in cols]


def test_exact_match_loads_every_csv_column():
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None)))
    assert why is None and cols == CSV


def test_an_adopted_nullable_column_missing_from_the_seed_is_fine():
    """Scenario 01 adopted APPROVER_ID into v2. The v1 seed must still load."""
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("APPROVER_ID", True, None)))
    assert why is None and "APPROVER_ID" not in cols


def test_a_not_null_column_the_seed_lacks_is_refused_before_truncate():
    """Scenario 02 added REVENUE_STREAM NOT NULL. A load would leave it null and fail
    after the truncate, which is the failure that emptied AP_INVOICE live."""
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("REVENUE_STREAM", False, None)))
    assert cols == [] and "REVENUE_STREAM" in why


def test_a_not_null_column_with_a_default_is_fine():
    cols, why = plan_load(CSV, live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                    ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None),
                                    ("SOURCE", False, "'ERP'")))
    assert why is None


def test_a_csv_column_the_table_lacks_is_refused():
    cols, why = plan_load(CSV + ["GHOST"], live(("INVOICE_ID", False, None), ("VENDOR_ID", False, None),
                                                ("GROSS_AMOUNT", False, None), ("LOADED_AT", False, None)))
    assert cols == [] and "GHOST" in why
```

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

**PR #10, the staging change (agent-drafted)**

```sql
diff --git a/dbt/models/staging/stg_ar_invoice.sql b/dbt/models/staging/stg_ar_invoice.sql
index b516196..749bd91 100644
--- a/dbt/models/staging/stg_ar_invoice.sql
+++ b/dbt/models/staging/stg_ar_invoice.sql
@@ -11,5 +11,6 @@ select
     gross_amount - coalesce(tax_amount, 0) as net_amount,
     upper(status)             as status,
     source_system,
-    loaded_at
+    loaded_at,
+    upper(revenue_stream)     as revenue_stream
 from {{ source('raw', 'AR_INVOICE') }}
```

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

**Snowsight: `ops/sql/04_event_identity.sql`**

```sql
-- Separates what an event IS from what it is ABOUT.
--
-- EVENT_ID used to be the fingerprint: sha256 of (dataset, change, object,
-- after state). A deterministic key is right for "do not raise this twice
-- while it is open". It is wrong as a row identity: when a divergence is
-- dismissed and later re-raised, the new row got the same EVENT_ID as the old
-- one, Snowflake does not enforce primary keys, and every status update is
-- WHERE EVENT_ID = ..., so all lifecycles of one divergence moved together.
--
-- Now: FINGERPRINT is the dedupe key and may repeat across lifecycles.
--      EVENT_ID is unique per raise.
-- Safe to re-run.

ALTER TABLE FIN_AIWH.META.DRIFT_EVENT ADD COLUMN IF NOT EXISTS FINGERPRINT VARCHAR(32);

-- Existing rows: the old EVENT_ID was the fingerprint.
UPDATE FIN_AIWH.META.DRIFT_EVENT SET FINGERPRINT = EVENT_ID WHERE FINGERPRINT IS NULL;

-- Repair rows that share an EVENT_ID. Keep the newest as the live one, give
-- the older ones their own id and put them back to DISMISSED, which is the
-- state they were in before a later lifecycle's update swept them along.
CREATE OR REPLACE TEMPORARY TABLE FIN_AIWH.META._DUP AS
SELECT EVENT_ID, RUN_ID, DETECTED_AT,
       ROW_NUMBER() OVER (PARTITION BY EVENT_ID ORDER BY DETECTED_AT DESC) AS RN
FROM FIN_AIWH.META.DRIFT_EVENT;

UPDATE FIN_AIWH.META.DRIFT_EVENT e
SET EVENT_ID    = SUBSTR(SHA2(e.EVENT_ID || ':' || d.RUN_ID, 256), 1, 32),
    STATUS      = 'DISMISSED',
    RESOLVED_AT = COALESCE(e.RESOLVED_AT, d.DETECTED_AT)
FROM FIN_AIWH.META._DUP d
WHERE e.EVENT_ID = d.EVENT_ID AND e.RUN_ID = d.RUN_ID AND d.RN > 1;

DROP TABLE IF EXISTS FIN_AIWH.META._DUP;

-- Prove it: zero rows shared by more than one event.
SELECT EVENT_ID, COUNT(*) AS N FROM FIN_AIWH.META.DRIFT_EVENT
GROUP BY EVENT_ID HAVING COUNT(*) > 1;
```

Its last statement returned zero rows on the live account: no `EVENT_ID`
shared by more than one row.

**The content scenarios.**

**`ops/scenarios/09_duplicate_key.sql`**

```sql
-- Scenario: a duplicate primary key lands in the invoice header.
-- Re-runnable: the duplicate is a copy of one fixed row, inserted only if absent.
-- Expected: DUPLICATE_KEY / BREAKING on AP_INVOICE.INVOICE_ID. The schema is
-- untouched, so schema detection sees nothing. Only the content check does.
-- Every join on INVOICE_ID now fans out; the aging pack double counts one
-- invoice, quietly.
INSERT INTO FIN_AIWH.RAW.AP_INVOICE
SELECT * FROM FIN_AIWH.RAW.AP_INVOICE
WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)
  AND (SELECT COUNT(*) FROM FIN_AIWH.RAW.AP_INVOICE
       WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)) = 1;
```
**`ops/scenarios/10_stale_source.sql`**

```sql
-- Scenario: the AR receipts feed stops. Nothing errors, the table just stops
-- moving. Every aging number stays plausible and drifts further from true.
-- Re-runnable: setting an old timestamp older is a no-op.
-- Expected: STALE / MEDIUM on AR_RECEIPT.LOADED_AT, contract allows 24h.
UPDATE FIN_AIWH.RAW.AR_RECEIPT
SET LOADED_AT = DATEADD(DAY, -3, SYSDATE()::TIMESTAMP_NTZ)
WHERE LOADED_AT > DATEADD(DAY, -3, SYSDATE()::TIMESTAMP_NTZ);
```
**`ops/scenarios/11_content_repaired.sql`**

```sql
-- Scenario: both content breaches fixed at source.
-- Re-runnable.
-- Expected: the next `agent` run closes both issues and dismisses the events.

-- 09 reversed. Snowflake has no row id, so a duplicate that is an exact copy
-- cannot be deleted "except one" in place. Rebuild the table from its own
-- distinct rows. The schema is untouched, so no drift is raised.
CREATE OR REPLACE TEMPORARY TABLE FIN_AIWH.RAW._AP_INVOICE_DEDUP AS
SELECT DISTINCT * FROM FIN_AIWH.RAW.AP_INVOICE;
DELETE FROM FIN_AIWH.RAW.AP_INVOICE;
INSERT INTO FIN_AIWH.RAW.AP_INVOICE SELECT * FROM FIN_AIWH.RAW._AP_INVOICE_DEDUP;
DROP TABLE IF EXISTS FIN_AIWH.RAW._AP_INVOICE_DEDUP;

-- 10 reversed: the feed catches up.
UPDATE FIN_AIWH.RAW.AR_RECEIPT SET LOADED_AT = SYSDATE()::TIMESTAMP_NTZ;
```
**Commands**

```bash
python -m control.cli apply ops/scenarios/09_duplicate_key.sql
python -m control.cli apply ops/scenarios/10_stale_source.sql
python -m control.cli detect
python -m control.cli agent
python -m control.cli apply ops/scenarios/11_content_repaired.sql
python -m control.cli agent
```

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

# Appendix A. Every scenario script

All re-runnable. `99_reset` is stale and carries its own warning.

**`ops/scenarios/01_additive_column.sql`**

```sql
-- Scenario: upstream adds a nullable column nobody told us about.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: COLUMN_ADDED / LOW. Nothing downstream breaks. The contract is
-- simply out of date and should adopt the column on the next merge.
ALTER TABLE FIN_AIWH.RAW.AP_INVOICE
  ADD COLUMN IF NOT EXISTS APPROVER_ID VARCHAR(32)
  COMMENT 'Approver added by the ERP upgrade, undeclared';
```
**`ops/scenarios/02_required_column_added.sql`**

```sql
-- Scenario: upstream adds a column and makes it mandatory.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: COLUMN_ADDED / MEDIUM. Readers are fine, writers are not.
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ADD COLUMN IF NOT EXISTS REVENUE_STREAM VARCHAR(16);
UPDATE FIN_AIWH.RAW.AR_INVOICE SET REVENUE_STREAM = 'PRODUCT' WHERE REVENUE_STREAM IS NULL;
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN REVENUE_STREAM SET NOT NULL;
```
**`ops/scenarios/03_type_widened.sql`**

```sql
-- Scenario: a text column is widened upstream.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: TYPE_CHANGED / LOW. Existing values still fit, but any downstream
-- column still sized to the old width will silently truncate.
ALTER TABLE FIN_AIWH.RAW.AP_INVOICE
  ALTER COLUMN INVOICE_NUMBER SET DATA TYPE VARCHAR(128);
```
**`ops/scenarios/04_money_scale_changed.sql`**

```sql
-- Scenario: the scale on the monetary columns moves from 2 to 4 decimal places.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: TYPE_CHANGED / BREAKING on GROSS_AMOUNT and TAX_AMOUNT, nothing else.
-- This is the quiet one. Nothing errors, every total just stops agreeing with
-- the subledger.
--
-- Snowflake will not rescale a NUMBER in place, so the table is rebuilt. Note
-- the explicit column list: a bare CREATE TABLE AS SELECT drops every NOT NULL
-- constraint, which the detector would correctly report as ten extra breaks.
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL,
    VENDOR_ID       VARCHAR(32)   NOT NULL,
    ENTITY_CODE     VARCHAR(8)    NOT NULL,
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL,
    INVOICE_DATE    DATE          NOT NULL,
    DUE_DATE        DATE          NOT NULL,
    CURRENCY_CODE   VARCHAR(3)    NOT NULL,
    GROSS_AMOUNT    NUMBER(18,4)  NOT NULL,
    TAX_AMOUNT      NUMBER(18,4),
    STATUS          VARCHAR(16)   NOT NULL,
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL,
    LOADED_AT       TIMESTAMP_NTZ NOT NULL
) AS
SELECT
    INVOICE_ID, VENDOR_ID, ENTITY_CODE, INVOICE_NUMBER, INVOICE_DATE, DUE_DATE,
    CURRENCY_CODE, GROSS_AMOUNT, TAX_AMOUNT, STATUS, SOURCE_SYSTEM, LOADED_AT
FROM FIN_AIWH.RAW.AP_INVOICE;
```
**`ops/scenarios/05_column_dropped.sql`**

```sql
-- Scenario: a contracted column disappears.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: COLUMN_REMOVED / BREAKING. Bank reconciliation loses its join key.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT DROP COLUMN IF EXISTS BANK_REF;
```
**`ops/scenarios/06_nullability_relaxed.sql`**

```sql
-- Scenario: upstream stops guaranteeing a value.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: NULLABILITY_RELAXED / BREAKING. Every aggregate that assumed a
-- status exists now has a silent bucket of nulls.
-- Dropping NOT NULL on an already nullable column succeeds, so this is safe to repeat.
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS DROP NOT NULL;
```
**`ops/scenarios/07_new_ungoverned_source.sql`**

```sql
-- Scenario: a new source table lands in the warehouse with no contract.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: DATASET_UNGOVERNED / MEDIUM. It cannot be modelled until someone
-- either writes a contract for it or rejects it.
CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_ACCRUAL (
    ACCRUAL_ID     VARCHAR(32)   NOT NULL,
    ENTITY_CODE    VARCHAR(8)    NOT NULL,
    GL_ACCOUNT     VARCHAR(16)   NOT NULL,
    PERIOD         VARCHAR(7)    NOT NULL,
    ACCRUAL_AMOUNT NUMBER(18,2)  NOT NULL,
    CURRENCY_CODE  VARCHAR(3)    NOT NULL,
    LOADED_AT      TIMESTAMP_NTZ NOT NULL
) COMMENT = 'Landed by the close automation team without review';

INSERT INTO FIN_AIWH.RAW.AP_ACCRUAL
SELECT 'ACR' || SEQ8(), 'UK01', '500100', '2026-08',
       UNIFORM(1000, 90000, RANDOM())::NUMBER(18,2), 'GBP', SYSDATE()
FROM TABLE(GENERATOR(ROWCOUNT => 400));
```
**`ops/scenarios/08_upstream_fixed.sql`**

```sql
-- Scenario: the source team repairs both breaking changes.
-- Re-runnable: firing it twice is a no-op, because the console is a button.
-- Expected: the two BREAKING events stop being raised, and `detect` reports the
-- two installed shields as stale. Nothing here touches AP_ACCRUAL, which is now
-- contracted, and nothing reverts REVENUE_STREAM, which is a MEDIUM the project
-- should adopt rather than push back upstream.
--
-- This is the other half of scenarios 05 and 06. A shield is a temporary
-- measure by definition, so the system has to be able to take one back out.

-- 05 reversed: the dropped column returns. The values do not. The shield said
-- exactly that, and re-adding the column as nullable is the honest repair:
-- the shape is restored, the history is still missing.
ALTER TABLE FIN_AIWH.RAW.AP_PAYMENT
  ADD COLUMN IF NOT EXISTS BANK_REF VARCHAR(64)
  COMMENT 'Bank reference, restored by the source team after being dropped';

-- 06 reversed: the guarantee comes back. This fails if any null slipped in
-- while the guarantee was gone, which is the correct outcome: the source team
-- must clean those rows before it can promise NOT NULL again.
UPDATE FIN_AIWH.RAW.AR_INVOICE SET STATUS = 'OPEN' WHERE STATUS IS NULL;
ALTER TABLE FIN_AIWH.RAW.AR_INVOICE ALTER COLUMN STATUS SET NOT NULL;
```
**`ops/scenarios/09_duplicate_key.sql`**

```sql
-- Scenario: a duplicate primary key lands in the invoice header.
-- Re-runnable: the duplicate is a copy of one fixed row, inserted only if absent.
-- Expected: DUPLICATE_KEY / BREAKING on AP_INVOICE.INVOICE_ID. The schema is
-- untouched, so schema detection sees nothing. Only the content check does.
-- Every join on INVOICE_ID now fans out; the aging pack double counts one
-- invoice, quietly.
INSERT INTO FIN_AIWH.RAW.AP_INVOICE
SELECT * FROM FIN_AIWH.RAW.AP_INVOICE
WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)
  AND (SELECT COUNT(*) FROM FIN_AIWH.RAW.AP_INVOICE
       WHERE INVOICE_ID = (SELECT MIN(INVOICE_ID) FROM FIN_AIWH.RAW.AP_INVOICE)) = 1;
```
**`ops/scenarios/10_stale_source.sql`**

```sql
-- Scenario: the AR receipts feed stops. Nothing errors, the table just stops
-- moving. Every aging number stays plausible and drifts further from true.
-- Re-runnable: setting an old timestamp older is a no-op.
-- Expected: STALE / MEDIUM on AR_RECEIPT.LOADED_AT, contract allows 24h.
UPDATE FIN_AIWH.RAW.AR_RECEIPT
SET LOADED_AT = DATEADD(DAY, -3, SYSDATE()::TIMESTAMP_NTZ)
WHERE LOADED_AT > DATEADD(DAY, -3, SYSDATE()::TIMESTAMP_NTZ);
```
**`ops/scenarios/11_content_repaired.sql`**

```sql
-- Scenario: both content breaches fixed at source.
-- Re-runnable.
-- Expected: the next `agent` run closes both issues and dismisses the events.

-- 09 reversed. Snowflake has no row id, so a duplicate that is an exact copy
-- cannot be deleted "except one" in place. Rebuild the table from its own
-- distinct rows. The schema is untouched, so no drift is raised.
CREATE OR REPLACE TEMPORARY TABLE FIN_AIWH.RAW._AP_INVOICE_DEDUP AS
SELECT DISTINCT * FROM FIN_AIWH.RAW.AP_INVOICE;
DELETE FROM FIN_AIWH.RAW.AP_INVOICE;
INSERT INTO FIN_AIWH.RAW.AP_INVOICE SELECT * FROM FIN_AIWH.RAW._AP_INVOICE_DEDUP;
DROP TABLE IF EXISTS FIN_AIWH.RAW._AP_INVOICE_DEDUP;

-- 10 reversed: the feed catches up.
UPDATE FIN_AIWH.RAW.AR_RECEIPT SET LOADED_AT = SYSDATE()::TIMESTAMP_NTZ;
```
**`ops/scenarios/99_reset.sql`**

```sql
-- WARNING, September 2026: this file rebuilds RAW to the ORIGINAL version 1
-- shape and is now behind the contracts. AP_INVOICE is at v2 with APPROVER_ID,
-- and AP_ACCRUAL is contracted and has a staging model, so running this as-is
-- raises COLUMN_REMOVED on AP_INVOICE and DATASET_MISSING on AP_ACCRUAL, both
-- BREAKING, and dbt build fails. Roadmap: generate reset DDL from the contracts
-- so it cannot fall behind them. Until then, do not run this.
--
-- Undo every scenario. Rebuilds RAW to the version 1 shape and drops the
-- ungoverned table. Reload data afterwards:
--   python -m control.cli load
--   python -m control.cli resolve --all
DROP TABLE IF EXISTS FIN_AIWH.RAW.AP_ACCRUAL;

-- One time cleanup: an earlier version of this file ran unqualified DDL and
-- created empty copies of the RAW tables in META. Harmless to repeat.
DROP TABLE IF EXISTS FIN_AIWH.META.AP_VENDOR;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_INVOICE;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_INVOICE_LINE;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_PAYMENT;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_CUSTOMER;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_INVOICE;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_RECEIPT;
DROP TABLE IF EXISTS FIN_AIWH.META.FX_RATE;


CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_VENDOR (
    VENDOR_ID      VARCHAR(32)   NOT NULL  COMMENT 'Natural key from the ERP vendor master.',
    VENDOR_NAME    VARCHAR(200)  NOT NULL  COMMENT 'Legal name as registered.',
    COUNTRY_CODE   VARCHAR(2)     COMMENT 'ISO 3166-1 alpha-2 of the vendor''s registered address.',
    PAYMENT_TERMS  VARCHAR(16)    COMMENT 'Terms code, for example NET30.',
    TAX_ID         VARCHAR(64)    COMMENT 'Tax registration number. Restricted.',
    IS_ACTIVE      BOOLEAN       NOT NULL  COMMENT 'False once the vendor is blocked for payment.',
    CREATED_AT     TIMESTAMP_NTZ NOT NULL  COMMENT 'Vendor record creation time in the source system.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Vendor master from the source ERP.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate invoice key from the ERP.',
    VENDOR_ID       VARCHAR(32)   NOT NULL  COMMENT 'References AP_VENDOR.VENDOR_ID.',
    ENTITY_CODE     VARCHAR(8)    NOT NULL  COMMENT 'Legal entity booking the liability.',
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL  COMMENT 'Vendor''s own invoice number.',
    INVOICE_DATE    DATE          NOT NULL  COMMENT 'Date on the invoice document.',
    DUE_DATE        DATE          NOT NULL  COMMENT 'Contractual payment due date.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 transaction currency.',
    GROSS_AMOUNT    NUMBER(18,2)  NOT NULL  COMMENT 'Invoice total including tax, transaction currency.',
    TAX_AMOUNT      NUMBER(18,2)   COMMENT 'Tax portion of the gross amount.',
    STATUS          VARCHAR(16)   NOT NULL  COMMENT 'OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.',
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL  COMMENT 'Originating ERP instance.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts payable invoice header.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE_LINE (
    LINE_ID      VARCHAR(40)   NOT NULL  COMMENT 'Surrogate line key.',
    INVOICE_ID   VARCHAR(32)   NOT NULL  COMMENT 'References AP_INVOICE.INVOICE_ID.',
    LINE_NUMBER  NUMBER(9,0)   NOT NULL  COMMENT 'Line sequence within the invoice.',
    COST_CENTER  VARCHAR(16)    COMMENT 'Cost centre charged.',
    GL_ACCOUNT   VARCHAR(16)   NOT NULL  COMMENT 'GL account the line posts to.',
    LINE_AMOUNT  NUMBER(18,2)  NOT NULL  COMMENT 'Line net amount, transaction currency.',
    DESCRIPTION  VARCHAR(500)   COMMENT 'Free text line description.',
    LOADED_AT    TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts payable invoice line detail with GL coding.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_PAYMENT (
    PAYMENT_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate payment key.',
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'References AP_INVOICE.INVOICE_ID.',
    PAYMENT_DATE    DATE          NOT NULL  COMMENT 'Value date of the payment.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 payment currency.',
    PAID_AMOUNT     NUMBER(18,2)  NOT NULL  COMMENT 'Amount applied, payment currency.',
    PAYMENT_METHOD  VARCHAR(16)    COMMENT 'ACH, WIRE, CHECK, SEPA.',
    BANK_REF        VARCHAR(64)    COMMENT 'Bank reference for reconciliation.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Payments applied against AP invoices.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_CUSTOMER (
    CUSTOMER_ID    VARCHAR(32)   NOT NULL  COMMENT 'Natural key from the ERP customer master.',
    CUSTOMER_NAME  VARCHAR(200)  NOT NULL  COMMENT 'Legal name as registered.',
    COUNTRY_CODE   VARCHAR(2)     COMMENT 'ISO 3166-1 alpha-2 of the billing address.',
    CREDIT_LIMIT   NUMBER(18,2)   COMMENT 'Approved credit limit, reporting currency.',
    PAYMENT_TERMS  VARCHAR(16)    COMMENT 'Terms code, for example NET45.',
    IS_ACTIVE      BOOLEAN       NOT NULL  COMMENT 'False once the customer is on credit hold.',
    CREATED_AT     TIMESTAMP_NTZ NOT NULL  COMMENT 'Customer record creation time in the source system.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Customer master from the source ERP.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate invoice key from the ERP.',
    CUSTOMER_ID     VARCHAR(32)   NOT NULL  COMMENT 'References AR_CUSTOMER.CUSTOMER_ID.',
    ENTITY_CODE     VARCHAR(8)    NOT NULL  COMMENT 'Legal entity recognising the receivable.',
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL  COMMENT 'Our invoice number issued to the customer.',
    INVOICE_DATE    DATE          NOT NULL  COMMENT 'Date on the invoice document.',
    DUE_DATE        DATE          NOT NULL  COMMENT 'Contractual payment due date.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 transaction currency.',
    GROSS_AMOUNT    NUMBER(18,2)  NOT NULL  COMMENT 'Invoice total including tax, transaction currency.',
    TAX_AMOUNT      NUMBER(18,2)   COMMENT 'Tax portion of the gross amount.',
    STATUS          VARCHAR(16)   NOT NULL  COMMENT 'OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.',
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL  COMMENT 'Originating ERP instance.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts receivable invoice header.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_RECEIPT (
    RECEIPT_ID       VARCHAR(32)   NOT NULL  COMMENT 'Surrogate receipt key.',
    INVOICE_ID       VARCHAR(32)   NOT NULL  COMMENT 'References AR_INVOICE.INVOICE_ID.',
    RECEIPT_DATE     DATE          NOT NULL  COMMENT 'Value date of the receipt.',
    CURRENCY_CODE    VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 receipt currency.',
    RECEIVED_AMOUNT  NUMBER(18,2)  NOT NULL  COMMENT 'Amount applied, receipt currency.',
    PAYMENT_METHOD   VARCHAR(16)    COMMENT 'ACH, WIRE, CHECK, SEPA.',
    BANK_REF         VARCHAR(64)    COMMENT 'Bank reference for reconciliation.',
    LOADED_AT        TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Cash receipts applied against AR invoices.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.FX_RATE (
    RATE_DATE      DATE          NOT NULL  COMMENT 'Rate effective date.',
    FROM_CURRENCY  VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 source currency.',
    TO_CURRENCY    VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 target currency.',
    RATE           NUMBER(18,8)  NOT NULL  COMMENT 'Units of target per one unit of source.',
    RATE_TYPE      VARCHAR(16)   NOT NULL  COMMENT 'SPOT, CLOSING, AVERAGE.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Daily FX rates used to translate subledger amounts to reporting currency.';
```

# Appendix B. Complete source of the control plane

Final form at `e1ea77b`. Every module, in dependency order.

**`control/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    account: str
    user: str
    private_key_path: Path
    private_key_passphrase: str | None
    role: str
    warehouse: str
    database: str
    schema: str

    @property
    def raw_schema(self) -> str:
        return "RAW"


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return v


def load_settings() -> Settings:
    key_path = Path(_req("SNOWFLAKE_PRIVATE_KEY_PATH"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    if not key_path.exists():
        raise SystemExit(f"private key not found at {key_path}")
    return Settings(
        account=_req("SNOWFLAKE_ACCOUNT"),
        user=_req("SNOWFLAKE_USER"),
        private_key_path=key_path,
        private_key_passphrase=os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        role=os.getenv("SNOWFLAKE_ROLE", "FIN_AIWH_ENG"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "FIN_AIWH_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "FIN_AIWH"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "META"),
    )
```
**`control/snow.py`**

```python
from contextlib import contextmanager

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from .config import Settings, load_settings


def _private_key_der(settings: Settings) -> bytes:
    pwd = (
        settings.private_key_passphrase.encode()
        if settings.private_key_passphrase
        else None
    )
    with open(settings.private_key_path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(), password=pwd, backend=default_backend()
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@contextmanager
def connect(settings: Settings | None = None, schema: str | None = None):
    s = settings or load_settings()
    conn = snowflake.connector.connect(
        account=s.account,
        user=s.user,
        private_key=_private_key_der(s),
        role=s.role,
        warehouse=s.warehouse,
        database=s.database,
        schema=schema or s.schema,
        client_session_keep_alive=False,
        # Every LOADED_AT is TIMESTAMP_NTZ, which carries no zone. Pinning the
        # session to UTC means "now" cast to NTZ, SYSDATE(), and the freshness
        # comparison all agree, whatever the account default is.
        session_parameters={"TIMEZONE": "UTC"},
    )
    try:
        yield conn
    finally:
        conn.close()


def query(conn, sql: str, params: tuple | dict | None = None) -> list[dict]:
    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def execute(conn, sql: str, params: tuple | dict | None = None) -> None:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    finally:
        cur.close()


def execute_script(conn, sql_text: str) -> int:
    """Run a multi statement SQL file. Returns the number of statements run."""
    count = 0
    for cur in conn.execute_string(sql_text):
        cur.close()
        count += 1
    return count
```
**`control/contracts.py`**

```python
"""Loading, canonicalising and hashing data contracts."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT

CONTRACT_DIR = ROOT / "contracts"


@dataclass
class ContractColumn:
    name: str
    type: str
    nullable: bool
    description: str = ""
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def signature(self) -> str:
        """Human readable type signature, used in drift messages."""
        if self.type == "TEXT" and self.length:
            return f"TEXT({self.length})"
        if self.type == "NUMBER" and self.precision is not None:
            return f"NUMBER({self.precision},{self.scale})"
        return self.type


@dataclass
class Contract:
    dataset: str                 # e.g. RAW.AP_INVOICE
    version: int
    owner: str
    classification: str
    description: str
    primary_key: list[str]
    freshness: dict
    columns: list[ContractColumn]
    source_path: Path | None = None
    raw: dict = field(default_factory=dict)

    @property
    def schema(self) -> str:
        return self.dataset.split(".")[0]

    @property
    def table(self) -> str:
        return self.dataset.split(".")[1]

    def column(self, name: str) -> ContractColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    def canonical(self) -> dict:
        """Ordering and defaults normalised, so the hash only moves on real change."""
        return {
            "dataset": self.dataset,
            "version": self.version,
            "owner": self.owner,
            "classification": self.classification,
            "primary_key": sorted(self.primary_key),
            "freshness": dict(sorted(self.freshness.items())),
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "length": c.length,
                    "precision": c.precision,
                    "scale": c.scale,
                    "nullable": c.nullable,
                }
                for c in sorted(self.columns, key=lambda c: c.name)
            ],
        }

    def spec_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def parse_contract(path: Path) -> Contract:
    data = yaml.safe_load(path.read_text())
    missing = [k for k in ("dataset", "version", "columns") if k not in data]
    if missing:
        raise ValueError(f"{path.name}: missing required keys {missing}")
    cols = [
        ContractColumn(
            name=c["name"],
            type=c["type"],
            nullable=bool(c.get("nullable", True)),
            description=c.get("description", ""),
            length=c.get("length"),
            precision=c.get("precision"),
            scale=c.get("scale"),
        )
        for c in data["columns"]
    ]
    names = [c.name for c in cols]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{path.name}: duplicate columns {sorted(dupes)}")
    pk = data.get("primary_key", [])
    unknown_pk = [k for k in pk if k not in names]
    if unknown_pk:
        raise ValueError(f"{path.name}: primary_key references unknown columns {unknown_pk}")
    return Contract(
        dataset=data["dataset"].upper(),
        version=int(data["version"]),
        owner=data.get("owner", "unassigned"),
        classification=data.get("classification", "internal"),
        description=data.get("description", ""),
        primary_key=pk,
        freshness=data.get("freshness", {}),
        columns=cols,
        source_path=path,
        raw=data,
    )


def load_contracts(directory: Path | None = None) -> list[Contract]:
    d = directory or CONTRACT_DIR
    paths = sorted(d.rglob("*.yml")) + sorted(d.rglob("*.yaml"))
    return [parse_contract(p) for p in paths]
```
**`control/register.py`**

```python
"""Publishing contracts into the control plane.

Registration is append only. Editing a contract and re-registering creates a new
version row; the old one stays readable, so any drift event from last month can
still be read against the contract that was in force at the time.
"""
import json
import subprocess

from .contracts import Contract, load_contracts
from .snow import query, execute


def git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return None


def registered_hashes(conn) -> dict[str, tuple[int, str]]:
    rows = query(
        conn,
        "SELECT CONTRACT_KEY, VERSION, SPEC_HASH FROM META.ACTIVE_CONTRACT",
    )
    return {r["CONTRACT_KEY"]: (r["VERSION"], r["SPEC_HASH"]) for r in rows}


def register(conn, contracts: list[Contract] | None = None) -> dict:
    contracts = contracts if contracts is not None else load_contracts()
    existing = registered_hashes(conn)
    sha = git_sha()
    added, unchanged, updated = [], [], []

    for c in contracts:
        h = c.spec_hash()
        prev = existing.get(c.dataset)
        if prev and prev[1] == h:
            unchanged.append(c.dataset)
            continue
        if prev and c.version <= prev[0]:
            raise SystemExit(
                f"{c.dataset}: contract content changed but version is still "
                f"{c.version}. Bump the version field before registering."
            )
        execute(
            conn,
            """
            INSERT INTO META.CONTRACT_REGISTRY
              (CONTRACT_KEY, VERSION, SPEC_HASH, SPEC, OWNER, CLASSIFICATION, GIT_SHA)
            SELECT %(key)s, %(version)s, %(hash)s, TRY_PARSE_JSON(%(spec)s),
                   %(owner)s, %(classification)s, %(sha)s
            """,
            {
                "key": c.dataset,
                "version": c.version,
                "hash": h,
                "spec": json.dumps(c.canonical()),
                "owner": c.owner,
                "classification": c.classification,
                "sha": sha,
            },
        )
        (updated if prev else added).append(c.dataset)

    return {"added": added, "updated": updated, "unchanged": unchanged}
```
**`control/load.py`**

```python
"""Load generated seed CSVs into RAW via an internal stage."""
import csv
from pathlib import Path

from .config import ROOT
from .snow import execute, query

SEED_OUT = ROOT / "seeds" / "out"
STAGE = "FIN_AIWH.RAW.SEED_STAGE"

FILE_FORMAT = (
    "TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
    "NULL_IF = ('') EMPTY_FIELD_AS_NULL = TRUE TRIM_SPACE = FALSE"
)


def _live_columns(conn, table: str) -> list[dict]:
    return query(
        conn,
        "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM FIN_AIWH.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'RAW' AND TABLE_NAME = %(t)s ORDER BY ORDINAL_POSITION",
        {"t": table},
    )


def plan_load(csv_columns: list[str], live: list[dict]) -> tuple[list[str], str | None]:
    """Which columns to copy, or why this table cannot be loaded from this file.

    The seed is the contract's version 1 shape. The live table may have moved
    on: an adopted column is not in the CSV and must come through as NULL. A
    NOT NULL column with no default that the CSV does not carry cannot be
    loaded at all, and that has to be known before anything is truncated.
    """
    csv_set = {c.upper() for c in csv_columns}
    live_names = {r["COLUMN_NAME"].upper() for r in live}
    unknown = sorted(csv_set - live_names)
    if unknown:
        return [], f"CSV has columns the table does not: {unknown}"
    blocked = sorted(
        r["COLUMN_NAME"] for r in live
        if r["COLUMN_NAME"].upper() not in csv_set
        and r["IS_NULLABLE"] == "NO" and r["COLUMN_DEFAULT"] is None
    )
    if blocked:
        return [], (f"table has NOT NULL columns with no default that the seed does not "
                    f"carry: {blocked}. Reset the table to its contracted shape first")
    return [c.upper() for c in csv_columns], None


def load_all(conn, tables: list[str] | None = None) -> list[tuple[str, int | str]]:
    """Load every seed CSV. Returns (table, rows) or (table, reason it was skipped)."""
    execute(conn, f"CREATE STAGE IF NOT EXISTS {STAGE} FILE_FORMAT = ({FILE_FORMAT})")
    files = sorted(SEED_OUT.glob("*.csv"))
    if not files:
        raise SystemExit("no CSVs in seeds/out/. Run: python seeds/generate.py")

    results: list[tuple[str, int | str]] = []
    for f in files:
        table = f.stem.upper()
        if tables and table not in tables:
            continue
        with f.open(newline="") as fh:
            header = next(csv.reader(fh))
        live = _live_columns(conn, table)
        if not live:
            results.append((table, "skipped: table does not exist in RAW"))
            continue
        cols, why = plan_load(header, live)
        if why:
            results.append((table, f"skipped: {why}"))
            continue

        execute(
            conn,
            f"PUT 'file://{f.as_posix()}' @{STAGE} OVERWRITE = TRUE AUTO_COMPRESS = TRUE",
        )
        execute(conn, f"TRUNCATE TABLE IF EXISTS FIN_AIWH.RAW.{table}")
        # Explicit column list, positional from the CSV. Any live column the
        # seed does not carry arrives as NULL, which plan_load has already
        # confirmed is allowed.
        selects = ", ".join(f"${i + 1}" for i in range(len(cols)))
        execute(
            conn,
            f"COPY INTO FIN_AIWH.RAW.{table} ({', '.join(cols)}) "
            f"FROM (SELECT {selects} FROM @{STAGE}/{f.name}.gz) "
            f"FILE_FORMAT = ({FILE_FORMAT}) ON_ERROR = 'ABORT_STATEMENT'",
        )
        # The seed carries a fixed LOADED_AT. A load time that never moves would
        # make every table read as stale a day after loading, so it is stamped
        # with the actual load time.
        if "LOADED_AT" in cols:
            execute(conn, f"UPDATE FIN_AIWH.RAW.{table} SET LOADED_AT = SYSDATE()::TIMESTAMP_NTZ")
        n = query(conn, f"SELECT COUNT(*) AS C FROM FIN_AIWH.RAW.{table}")[0]["C"]
        results.append((table, n))
    return results
```
**`control/lineage.py`**

```python
"""What a drift event actually breaks.

A severity says how bad a change is in principle. Impact says what it costs
here: which models stop being correct, which tests will fail, which marts a
finance user reads. That turns "BANK_REF was dropped" into "BANK_REF was
dropped, which breaks fct_ap_open_items and the AP aging report".

Lineage comes from dbt's manifest, which dbt builds from the real SQL, so the
model graph is exact. Column level lineage is not something dbt publishes, so
it is derived here by reading each model's SQL. That part is a heuristic and
says so: every column result carries a confidence.
"""
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import ROOT

MANIFEST = ROOT / "dbt" / "target" / "manifest.json"

# How sure we are that a model really uses the column
EXACT = "references the column"
WILDCARD = "selects * from the source, so it carries the column"
INHERITED = "downstream of an affected model"


@dataclass
class Impact:
    dataset_key: str
    column: str | None
    models: list[str] = field(default_factory=list)      # staging
    marts: list[str] = field(default_factory=list)       # what people read
    tests: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)    # dbt exposures: what people open
    confidence: str | None = None
    manifest_seen: bool = True

    @property
    def empty(self) -> bool:
        return not (self.models or self.marts or self.tests or self.reports)

    def summary(self) -> str:
        """One line for a table cell."""
        if not self.manifest_seen:
            return "unknown (no dbt manifest)"
        if self.empty:
            return "nothing downstream"
        bits = []
        if self.reports:
            bits.append(f"{len(self.reports)} report" + ("s" if len(self.reports) > 1 else ""))
        if self.marts:
            bits.append(f"{len(self.marts)} mart" + ("s" if len(self.marts) > 1 else ""))
        if self.models:
            bits.append(f"{len(self.models)} staging")
        if self.tests:
            bits.append(f"{len(self.tests)} test" + ("s" if len(self.tests) > 1 else ""))
        return ", ".join(bits)

    def as_dict(self) -> dict:
        return asdict(self)

    def markdown(self) -> str:
        """A block for a pull request or an issue body."""
        if not self.manifest_seen:
            return ("_Downstream impact unknown: no dbt manifest was available. "
                    "Run `dbt parse` and re-run detection._")
        if self.empty:
            return "_Nothing downstream depends on this yet._"
        lines = []
        if self.reports:
            lines.append("**Reports affected** (what people open)")
            for r in self.reports:
                who = f" ({r['owner']})" if r.get("owner") else ""
                lines.append(f"- {r['label']}{who}")
            lines.append("")
        if self.marts:
            lines.append("**Marts affected** (what people read)")
            lines += [f"- `{m}`" for m in self.marts]
        if self.models:
            lines.append("")
            lines.append("**Staging models affected**")
            lines += [f"- `{m}`" for m in self.models]
        if self.tests:
            lines.append("")
            lines.append("**Tests that cover this path**")
            lines += [f"- `{t}`" for t in self.tests]
        if self.confidence:
            lines.append("")
            lines.append(f"_Column attribution: {self.confidence}._")
        return "\n".join(lines)


class Lineage:
    """The dbt graph, queried from the source side."""

    def __init__(self, manifest: dict | None):
        self.manifest = manifest or {}
        self.nodes = self.manifest.get("nodes", {})
        self.sources = self.manifest.get("sources", {})
        self.exposures = self.manifest.get("exposures", {})
        self.child_map = self.manifest.get("child_map", {})

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Lineage":
        p = path or MANIFEST
        if not p.exists():
            return cls(None)
        try:
            return cls(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            return cls(None)

    @property
    def available(self) -> bool:
        return bool(self.nodes)

    # -- graph ------------------------------------------------------------

    def source_id(self, dataset_key: str) -> str | None:
        """RAW.AP_PAYMENT -> source.fin_aiwh.raw.AP_PAYMENT"""
        table = dataset_key.split(".")[-1].upper()
        for uid, s in self.sources.items():
            if s.get("name", "").upper() == table:
                return uid
        return None

    def children(self, uid: str) -> list[str]:
        return self.child_map.get(uid, [])

    def descendants(self, uid: str) -> set[str]:
        """Everything reachable downstream, tests included."""
        seen, stack = set(), list(self.children(uid))
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.children(n))
        return seen

    def _sql(self, uid: str) -> str:
        n = self.nodes.get(uid, {})
        return n.get("raw_code") or n.get("compiled_code") or ""

    def _name(self, uid: str) -> str:
        return self.nodes.get(uid, {}).get("name", uid.split(".")[-1])

    def _is(self, uid: str, kind: str) -> bool:
        if kind == "exposure":
            return uid in self.exposures
        return self.nodes.get(uid, {}).get("resource_type") == kind

    def _report(self, uid: str) -> dict:
        e = self.exposures.get(uid, {})
        owner = e.get("owner") or {}
        return {
            "name": e.get("name", uid.split(".")[-1]),
            "label": e.get("label") or e.get("name", uid.split(".")[-1]),
            "type": e.get("type"),
            "owner": owner.get("name") or owner.get("email"),
        }

    def _schema(self, uid: str) -> str:
        return (self.nodes.get(uid, {}).get("schema") or "").upper()

    # -- impact -----------------------------------------------------------

    def _uses_column(self, uid: str, column: str) -> str | None:
        """Does this model reference the column? Returns a confidence, or None."""
        sql = self._sql(uid)
        if not sql:
            return None
        if re.search(rf"\b{re.escape(column)}\b", sql, re.IGNORECASE):
            return EXACT
        if re.search(r"select\s+\*", sql, re.IGNORECASE):
            return WILDCARD
        return None

    def impact(self, dataset_key: str, column: str | None = None) -> Impact:
        if not self.available:
            return Impact(dataset_key, column, manifest_seen=False)

        src = self.source_id(dataset_key)
        if src is None:
            return Impact(dataset_key, column)

        if column is None:
            affected = self.descendants(src)
            confidence = None
        else:
            # Direct children that actually touch the column, then everything
            # downstream of those. A model that does not reference the column is
            # not affected, and neither is anything beyond it through that path.
            affected, confidence = set(), None
            for child in self.children(src):
                if not self._is(child, "model"):
                    continue
                c = self._uses_column(child, column)
                if not c:
                    continue
                confidence = confidence or c
                affected.add(child)
                affected |= self.descendants(child)
            if affected and confidence != EXACT:
                confidence = confidence or INHERITED
            # column scoped tests on the source itself
            for child in self.children(src):
                if self._is(child, "test") and \
                        (self.nodes.get(child, {}).get("column_name") or "").upper() == column.upper():
                    affected.add(child)

        models = sorted({self._name(u) for u in affected
                         if self._is(u, "model") and self._schema(u) == "STAGING"})
        marts = sorted({self._name(u) for u in affected
                        if self._is(u, "model") and self._schema(u) == "MARTS"})
        tests = sorted({self._name(u) for u in affected if self._is(u, "test")})
        reports = sorted(
            (self._report(u) for u in affected if self._is(u, "exposure")),
            key=lambda r: r["label"],
        )
        return Impact(dataset_key, column, models, marts, tests, reports, confidence)
```
**`control/detect.py`**

```python
"""Contract versus reality.

The detector answers one question per dataset: does the warehouse still match
what we agreed it would be? Every divergence is classified by what it breaks,
not by how unusual it looks. Severity drives what happens next:

  LOW       safe to adopt automatically, contract bumps on merge
  MEDIUM    needs a decision, but nothing downstream is broken yet
  BREAKING  something downstream is already wrong or about to be
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .contracts import Contract, ContractColumn
from .lineage import Impact, Lineage
from .snow import query, execute

LOW, MEDIUM, BREAKING = "LOW", "MEDIUM", "BREAKING"


@dataclass
class ObservedColumn:
    name: str
    type: str
    nullable: bool
    ordinal: int
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def signature(self) -> str:
        if self.type == "TEXT" and self.length:
            return f"TEXT({self.length})"
        if self.type == "NUMBER" and self.precision is not None:
            return f"NUMBER({self.precision},{self.scale})"
        return self.type


@dataclass
class Finding:
    dataset_key: str
    change_type: str
    severity: str
    object_name: str | None
    before: dict | None
    after: dict | None
    rationale: str
    impact: Impact | None = None

    def fingerprint(self) -> str:
        blob = json.dumps(
            [self.dataset_key, self.change_type, self.object_name, self.after],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------

OBSERVE_SQL = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
       IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
FROM   %(db)s.INFORMATION_SCHEMA.COLUMNS
WHERE  TABLE_SCHEMA = %%(schema)s
ORDER  BY TABLE_NAME, ORDINAL_POSITION
"""


def fetch_observed(conn, database: str, schema: str) -> dict[str, dict[str, ObservedColumn]]:
    rows = query(conn, OBSERVE_SQL % {"db": database}, {"schema": schema})
    out: dict[str, dict[str, ObservedColumn]] = {}
    for r in rows:
        key = f"{r['TABLE_SCHEMA']}.{r['TABLE_NAME']}"
        out.setdefault(key, {})[r["COLUMN_NAME"]] = ObservedColumn(
            name=r["COLUMN_NAME"],
            type=r["DATA_TYPE"],
            nullable=(r["IS_NULLABLE"] == "YES"),
            ordinal=r["ORDINAL_POSITION"],
            length=r["CHARACTER_MAXIMUM_LENGTH"],
            precision=r["NUMERIC_PRECISION"],
            scale=r["NUMERIC_SCALE"],
        )
    return out


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def _compare_type(c: ContractColumn, o: ObservedColumn) -> tuple[str, str] | None:
    """Return (severity, rationale) when the type diverges, else None."""
    if c.type != o.type:
        return (
            BREAKING,
            f"base type changed from {c.type} to {o.type}; every downstream cast "
            f"and comparison on this column is now suspect",
        )

    if c.type == "TEXT" and c.length is not None and o.length is not None:
        if o.length > c.length:
            return (
                LOW,
                f"widened from {c.signature()} to {o.signature()}; existing values "
                f"still fit, but downstream columns sized to {c.length} will truncate",
            )
        if o.length < c.length:
            return (
                BREAKING,
                f"narrowed from {c.signature()} to {o.signature()}; values the "
                f"contract permits no longer fit",
            )

    if c.type == "NUMBER":
        if c.scale is not None and o.scale is not None and c.scale != o.scale:
            return (
                BREAKING,
                f"scale changed from {c.signature()} to {o.signature()}; monetary "
                f"precision moved, reconciliations and totals will disagree",
            )
        if c.precision is not None and o.precision is not None:
            if o.precision > c.precision:
                return (
                    LOW,
                    f"widened from {c.signature()} to {o.signature()}; larger values "
                    f"now possible than downstream models were sized for",
                )
            if o.precision < c.precision:
                return (
                    BREAKING,
                    f"narrowed from {c.signature()} to {o.signature()}; values the "
                    f"contract permits will overflow",
                )
    return None


def diff_dataset(contract: Contract, observed: dict[str, ObservedColumn] | None) -> list[Finding]:
    key = contract.dataset

    if observed is None:
        return [
            Finding(
                dataset_key=key,
                change_type="DATASET_MISSING",
                severity=BREAKING,
                object_name=None,
                before={"exists": True},
                after={"exists": False},
                rationale="a contracted dataset is not present in the warehouse; "
                          "every model reading it will fail",
            )
        ]

    findings: list[Finding] = []
    contract_names = {c.name for c in contract.columns}

    for c in contract.columns:
        o = observed.get(c.name)
        if o is None:
            in_pk = c.name in contract.primary_key
            findings.append(
                Finding(
                    dataset_key=key,
                    change_type="COLUMN_REMOVED",
                    severity=BREAKING,
                    object_name=c.name,
                    before={"type": c.signature(), "nullable": c.nullable},
                    after=None,
                    rationale=(
                        "a primary key column was dropped; row identity is gone and "
                        "incremental models cannot merge"
                        if in_pk
                        else "a contracted column was dropped; anything selecting it fails"
                    ),
                )
            )
            continue

        t = _compare_type(c, o)
        if t:
            sev, why = t
            findings.append(
                Finding(
                    dataset_key=key,
                    change_type="TYPE_CHANGED",
                    severity=sev,
                    object_name=c.name,
                    before={"type": c.signature(), "nullable": c.nullable},
                    after={"type": o.signature(), "nullable": o.nullable},
                    rationale=why,
                )
            )

        if c.nullable != o.nullable:
            if not c.nullable and o.nullable:
                findings.append(
                    Finding(
                        dataset_key=key,
                        change_type="NULLABILITY_RELAXED",
                        severity=BREAKING,
                        object_name=c.name,
                        before={"nullable": False},
                        after={"nullable": True},
                        rationale="the contract guarantees this column is never null and "
                                  "downstream joins and aggregates rely on that; nulls are "
                                  "now permitted",
                    )
                )
            else:
                findings.append(
                    Finding(
                        dataset_key=key,
                        change_type="NULLABILITY_TIGHTENED",
                        severity=MEDIUM,
                        object_name=c.name,
                        before={"nullable": True},
                        after={"nullable": False},
                        rationale="upstream now rejects nulls here; safe for readers, but "
                                  "loads carrying nulls will start failing",
                    )
                )

    for name, o in observed.items():
        if name in contract_names:
            continue
        findings.append(
            Finding(
                dataset_key=key,
                change_type="COLUMN_ADDED",
                severity=MEDIUM if not o.nullable else LOW,
                object_name=name,
                before=None,
                after={"type": o.signature(), "nullable": o.nullable},
                rationale=(
                    "a new NOT NULL column appeared; any writer unaware of it will fail"
                    if not o.nullable
                    else "a new nullable column appeared; nothing breaks, but it is "
                         "ungoverned until the contract adopts it"
                ),
            )
        )

    return findings


def diff_all(
    contracts: list[Contract], observed: dict[str, dict[str, ObservedColumn]]
) -> list[Finding]:
    findings: list[Finding] = []
    contracted = set()

    for c in contracts:
        contracted.add(c.dataset)
        findings.extend(diff_dataset(c, observed.get(c.dataset)))

    for key in sorted(observed):
        if key in contracted:
            continue
        findings.append(
            Finding(
                dataset_key=key,
                change_type="DATASET_UNGOVERNED",
                severity=MEDIUM,
                object_name=None,
                before=None,
                after={"columns": len(observed[key])},
                rationale="a table exists in the landing zone with no contract; it is "
                          "outside review and cannot be safely modelled",
            )
        )

    order = {BREAKING: 0, MEDIUM: 1, LOW: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.dataset_key, f.object_name or ""))


def attach_impact(findings: list[Finding], lineage: Lineage | None = None) -> list[Finding]:
    """Work out what each finding breaks. Cheap: the graph is already in memory."""
    lg = lineage or Lineage.load()
    for f in findings:
        # A dataset level change affects everything downstream of the dataset.
        # A column level change affects only the paths that carry that column.
        col = f.object_name if f.change_type not in (
            "DATASET_MISSING", "DATASET_UNGOVERNED") else None
        f.impact = lg.impact(f.dataset_key, col)
    return findings


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

# A divergence already being worked on must not be raised again. OPEN is waiting
# for triage, PROPOSED has a pull request, ESCALATED has an issue. All three are
# live workflow items. DISMISSED and MERGED are finished, so if the same
# divergence turns up afterwards it is genuinely new and deserves a new event.
ACTIVE_STATUSES = ("OPEN", "PROPOSED", "ESCALATED")


def active_fingerprints(conn) -> dict[str, str]:
    """fingerprint -> EVENT_ID for every event still in a live state."""
    rows = query(
        conn,
        "SELECT FINGERPRINT, EVENT_ID FROM FIN_AIWH.META.DRIFT_EVENT WHERE STATUS IN (%s)"
        % ",".join(f"'{s}'" for s in ACTIVE_STATUSES),
    )
    return {r["FINGERPRINT"]: r["EVENT_ID"] for r in rows}


def event_id(fingerprint: str, run_id: str) -> str:
    """Unique per raise. The fingerprint says what; the run says when."""
    return hashlib.sha256(f"{fingerprint}:{run_id}".encode()).hexdigest()[:32]


def persist(conn, run_id: str, findings: list[Finding], contracts: list[Contract]) -> tuple[int, int]:
    """Write new findings. Re-detecting the same divergence does not duplicate it.

    Returns (written, suppressed) so a run can say how much it deliberately
    stayed quiet about.
    """
    versions = {c.dataset: c.version for c in contracts}
    already = active_fingerprints(conn)
    suppressed = 0
    written = 0
    for f in findings:
        fp = f.fingerprint()
        if fp in already:
            # Do not raise it again, but do keep its impact current: lineage
            # changes as the dbt project changes, and an event open for a week
            # should say what it breaks today, not what it broke when raised.
            if f.impact is not None:
                execute(
                    conn,
                    "UPDATE FIN_AIWH.META.DRIFT_EVENT SET IMPACT = TRY_PARSE_JSON(%(impact)s) "
                    "WHERE EVENT_ID = %(event_id)s",
                    {"impact": json.dumps(f.impact.as_dict()), "event_id": already[fp]},
                )
            suppressed += 1
            continue
        execute(
            conn,
            """
            INSERT INTO META.DRIFT_EVENT
              (EVENT_ID, FINGERPRINT, RUN_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE,
               SEVERITY, OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS,
               IMPACT)
            SELECT %(event_id)s, %(fingerprint)s, %(run_id)s, %(dataset)s, %(version)s, %(change_type)s,
                   %(severity)s, %(object_name)s,
                   TRY_PARSE_JSON(%(before)s), TRY_PARSE_JSON(%(after)s),
                   %(rationale)s, 'OPEN', TRY_PARSE_JSON(%(impact)s)
            """,
            {
                "event_id": event_id(fp, run_id),
                "fingerprint": fp,
                "run_id": run_id,
                "dataset": f.dataset_key,
                "version": versions.get(f.dataset_key),
                "change_type": f.change_type,
                "severity": f.severity,
                "object_name": f.object_name,
                "before": json.dumps(f.before) if f.before is not None else None,
                "after": json.dumps(f.after) if f.after is not None else None,
                "rationale": f.rationale,
                "impact": json.dumps(f.impact.as_dict()) if f.impact else None,
            },
        )
        written += 1
    return written, suppressed


def snapshot_observed(conn, run_id: str, observed: dict[str, dict[str, ObservedColumn]]) -> int:
    n = 0
    for key, cols in observed.items():
        for o in cols.values():
            execute(
                conn,
                """
                INSERT INTO META.OBSERVED_SCHEMA
                  (RUN_ID, DATASET_KEY, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
                   IS_NULLABLE, NUMERIC_PRECISION, NUMERIC_SCALE, CHARACTER_LENGTH)
                VALUES (%(run_id)s, %(dataset)s, %(col)s, %(ord)s, %(type)s,
                        %(nullable)s, %(prec)s, %(scale)s, %(len)s)
                """,
                {
                    "run_id": run_id, "dataset": key, "col": o.name, "ord": o.ordinal,
                    "type": o.type, "nullable": o.nullable, "prec": o.precision,
                    "scale": o.scale, "len": o.length,
                },
            )
            n += 1
    return n


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
```
**`control/quality.py`**

```python
"""Content governance: what is in the columns, not only their shape.

Schema drift compares INFORMATION_SCHEMA to the contract. This compares the
data to the contract, using Snowflake Data Metric Functions as the measuring
instrument. Every check is derived from something the contract already states.
Nothing is invented:

| Contract says                | Check                              | Severity |
|------------------------------|------------------------------------|----------|
| primary_key: [A, B]          | DUPLICATE_COUNT on (A, B) == 0     | BREAKING |
| nullable: false              | NULL_COUNT on col == 0             | BREAKING |
| freshness: max_lag_hours: N  | hours since max(column) <= N       | MEDIUM   |
| expectations: (optional)     | any system DMF with min or max     | as stated |

Severity by consequence, the same rule as schema drift. A duplicate key makes
every join fan out and every total double count. A null in a contracted NOT
NULL column is exactly what scenario 06's guard test caught, caught at source.
Stale data leaves every report correct but old, which a human should decide
about rather than the system.

Two ways the DMFs are used, and the split matters:

  Synchronous  `SELECT SNOWFLAKE.CORE.NULL_COUNT(SELECT col FROM t)` runs now
               and answers now. This is what `detect` uses. The control plane
               decides on a measurement it just took, not one Snowflake took
               on its own schedule.

  Attached     `ALTER TABLE t ADD DATA METRIC FUNCTION ...` gives Snowflake
               side history in DATA_QUALITY_MONITORING_RESULTS for audit and
               for people who never run this CLI. `quality attach` reconciles
               the attachments to the contracts; nothing else depends on them.

Freshness uses a custom DMF (ops/20_quality.sql) because the system one refuses
TIMESTAMP_NTZ, which is every LOADED_AT in RAW. A DMF body must be
deterministic, so it cannot read the clock: it returns the newest timestamp and
the calling statement subtracts it from SYSDATE(). The session is pinned to UTC
so the NTZ values and the clock agree.
"""
import json
from dataclasses import dataclass

from .contracts import Contract
from .detect import BREAKING, MEDIUM, LOW, Finding
from .snow import execute, query

DMF_SCHEMA = "FIN_AIWH.META"
FRESHNESS_DMF = f"{DMF_SCHEMA}.NEWEST_EPOCH_NTZ"

SYSTEM_DMFS = {
    "NULL_COUNT", "NULL_PERCENT", "DUPLICATE_COUNT", "UNIQUE_COUNT",
    "ROW_COUNT", "BLANK_COUNT", "BLANK_PERCENT", "AVG", "MIN", "MAX", "STDDEV",
}
QUALITY_TYPES = {"DUPLICATE_KEY", "NULL_IN_REQUIRED", "STALE", "EXPECTATION_BREACHED"}


# SNOWFLAKE.CORE.NULL_COUNT refuses a BOOLEAN argument, cast or not. Columns of
# these types use a custom DMF with the type declared (ops/20_quality.sql),
# which is also attachable. Same pattern as freshness.
DMF_BY_TYPE = {"BOOLEAN": f"{DMF_SCHEMA}.NULL_COUNT_BOOL"}

# A DMF argument is a column reference and nothing else: every expression,
# cast or concatenation is refused whatever its declared type. So a composite
# key, which has no single column to name, is not measured by a DMF at all.
# It is measured by plain SQL in the same statement. Same number, no function.
PLAIN_SQL = "SQL"


@dataclass(frozen=True)
class Check:
    dataset_key: str
    change_type: str          # one of QUALITY_TYPES
    dmf: str                  # fully qualified function
    columns: tuple[str, ...]  # () for table level
    min: float | None
    max: float | None
    severity: str
    why: str                  # what the contract said that justifies this

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[-1]

    @property
    def object_name(self) -> str:
        return ",".join(self.columns) if self.columns else "*"

    @property
    def attachable(self) -> bool:
        return self.dmf != PLAIN_SQL

    @property
    def label(self) -> str:
        name = "DUPLICATE_COUNT" if self.dmf == PLAIN_SQL else self.dmf.rsplit(".", 1)[-1]
        return f"{name}({self.object_name})"

    def sql(self) -> str:
        if self.dmf == PLAIN_SQL:
            keys = ", ".join(self.columns)
            return (f"(SELECT COUNT(*) - COUNT(DISTINCT {keys}) "
                    f"FROM FIN_AIWH.{self.dataset_key})")
        cols = ", ".join(self.columns) if self.columns else "*"
        call = f"{self.dmf}(SELECT {cols} FROM FIN_AIWH.{self.dataset_key})"
        if self.change_type == "STALE":
            # The DMF returns the newest load as epoch seconds (a DMF body may
            # not read the clock). Hours behind is computed here, against
            # SYSDATE() in the same statement, so one clock is used throughout.
            return f"(DATE_PART(EPOCH_SECOND, SYSDATE()) - {call}) / 3600"
        return call

    def breached(self, value: float | None) -> str | None:
        """A reason, or None if the value is within the contract."""
        if value is None:
            return "measurement returned NULL"
        if self.max is not None and value > self.max:
            return f"{self.label} = {value:g}, contract allows at most {self.max:g}"
        if self.min is not None and value < self.min:
            return f"{self.label} = {value:g}, contract requires at least {self.min:g}"
        return None


# --------------------------------------------------------------------------
# what the contract implies
# --------------------------------------------------------------------------

def desired(contract: Contract) -> list[Check]:
    out: list[Check] = []
    key = contract.dataset

    if contract.primary_key:
        out.append(Check(
            dataset_key=key, change_type="DUPLICATE_KEY",
            dmf=PLAIN_SQL if len(contract.primary_key) > 1 else "SNOWFLAKE.CORE.DUPLICATE_COUNT",
            columns=tuple(contract.primary_key), min=None, max=0, severity=BREAKING,
            why=f"primary_key is {contract.primary_key}; a duplicate makes every join fan out",
        ))

    for c in contract.columns:
        if not c.nullable:
            out.append(Check(
                dataset_key=key, change_type="NULL_IN_REQUIRED",
                dmf=DMF_BY_TYPE.get(c.type.upper(), "SNOWFLAKE.CORE.NULL_COUNT"),
                columns=(c.name,), min=None, max=0, severity=BREAKING,
                why=f"{c.name} is contracted NOT NULL",
            ))

    f = contract.freshness or {}
    if f.get("column") and f.get("max_lag_hours") is not None:
        out.append(Check(
            dataset_key=key, change_type="STALE",
            dmf=FRESHNESS_DMF,
            columns=(f["column"],), min=None, max=float(f["max_lag_hours"]), severity=MEDIUM,
            why=f"freshness allows {f['max_lag_hours']}h behind on {f['column']}",
        ))

    for e in contract.raw.get("expectations", []) or []:
        metric = str(e.get("metric", "")).upper()
        if metric not in SYSTEM_DMFS:
            continue
        cols = e.get("columns") or ([e["column"]] if e.get("column") else [])
        sev = str(e.get("severity", MEDIUM)).upper()
        out.append(Check(
            dataset_key=key, change_type="EXPECTATION_BREACHED",
            dmf=f"SNOWFLAKE.CORE.{metric}",
            columns=tuple(cols), min=e.get("min"), max=e.get("max"),
            severity=sev if sev in (LOW, MEDIUM, BREAKING) else MEDIUM,
            why=e.get("description") or f"contract expectation on {metric}",
        ))
    return out


# --------------------------------------------------------------------------
# measuring, synchronously
# --------------------------------------------------------------------------

def measure(conn, checks: list[Check]) -> dict[Check, float | None]:
    """One statement per table. Each DMF call is a column in the result."""
    out: dict[Check, float | None] = {}
    by_table: dict[str, list[Check]] = {}
    for c in checks:
        by_table.setdefault(c.dataset_key, []).append(c)
    for key, group in by_table.items():
        cols = ", ".join(f"{c.sql()} AS M{i}" for i, c in enumerate(group))
        row = query(conn, f"SELECT {cols}")[0]
        for i, c in enumerate(group):
            v = row[f"M{i}"]
            out[c] = float(v) if v is not None else None
    return out


def diff_quality(contracts: list[Contract], measured: dict[Check, float | None]) -> list[Finding]:
    """Findings in the same shape as schema drift, so one event table holds both.

    `after` carries the threshold, not the measured value, so a breach that
    persists across runs has one stable fingerprint and one event. The value
    goes in the rationale, where a human reads it.
    """
    findings = []
    for c in measured:
        reason = c.breached(measured[c])
        if reason is None:
            continue
        findings.append(Finding(
            dataset_key=c.dataset_key,
            change_type=c.change_type,
            severity=c.severity,
            object_name=c.object_name,
            before={"contract": {"min": c.min, "max": c.max}},
            after={"check": c.label, "min": c.min, "max": c.max},
            rationale=f"{reason}. {c.why}",
        ))
    order = {BREAKING: 0, MEDIUM: 1, LOW: 2}
    findings.sort(key=lambda f: (order[f.severity], f.dataset_key, f.object_name or ""))
    return findings


def check_all(conn, contracts: list[Contract]) -> list[Finding]:
    checks = [c for k in contracts for c in desired(k)]
    return diff_quality(contracts, measure(conn, checks)) if checks else []


# --------------------------------------------------------------------------
# attaching, for Snowflake side history
# --------------------------------------------------------------------------

def attached(conn, dataset_key: str) -> set[tuple[str, tuple[str, ...]]]:
    rows = query(conn, f"""
        SELECT METRIC_DATABASE_NAME, METRIC_SCHEMA_NAME, METRIC_NAME, REF_ARGUMENTS
        FROM TABLE(FIN_AIWH.INFORMATION_SCHEMA.DATA_METRIC_FUNCTION_REFERENCES(
            REF_ENTITY_NAME => 'FIN_AIWH.{dataset_key}', REF_ENTITY_DOMAIN => 'TABLE'))
    """)
    out = set()
    for r in rows:
        fn = f"{r['METRIC_DATABASE_NAME']}.{r['METRIC_SCHEMA_NAME']}.{r['METRIC_NAME']}"
        args = r["REF_ARGUMENTS"]
        if isinstance(args, str):
            args = json.loads(args)
        names = tuple(a["name"].upper() for a in (args or []))
        out.add((fn.upper(), names))
    return out


def reconcile(conn, contract: Contract, schedule: str = "TRIGGER_ON_CHANGES") -> tuple[list[str], list[str]]:
    """Make the attached DMFs equal what the contract implies. Returns (added, dropped)."""
    want = {(c.dmf.upper(), tuple(x.upper() for x in c.columns))
            for c in desired(contract) if c.attachable}
    have = attached(conn, contract.dataset)
    table = f"FIN_AIWH.{contract.dataset}"
    added, dropped = [], []
    for fn, cols in sorted(want - have):
        execute(conn, f"ALTER TABLE {table} ADD DATA METRIC FUNCTION {fn} ON ({', '.join(cols)})")
        added.append(f"{fn}({', '.join(cols)})")
    for fn, cols in sorted(have - want):
        if not fn.startswith("SNOWFLAKE.CORE.") and not fn.startswith(DMF_SCHEMA):
            continue        # someone else's attachment, not ours to remove
        execute(conn, f"ALTER TABLE {table} DROP DATA METRIC FUNCTION {fn} ON ({', '.join(cols)})")
        dropped.append(f"{fn}({', '.join(cols)})")
    if added or dropped:
        execute(conn, f"ALTER TABLE {table} SET DATA_METRIC_SCHEDULE = '{schedule}'")
    return added, dropped
```
**`control/shield.py`**

```python
"""Compatibility shields for breaking drift.

A breaking change upstream leaves every downstream report wrong until someone
fixes the source. A shield is a change to the staging model that restores the
contracted shape over the broken source, so the marts keep building and the
aging pack stays correct, while the issue stays open and the drift stays
visible.

Everything here is deterministic. The transformations are mechanical, and a
wrong one on a finance mart costs more than a model's fluency is worth.

What a shield can and cannot do:

| Change               | Shield                                              | Honest about |
|----------------------|-----------------------------------------------------|--------------|
| COLUMN_REMOVED       | NULL cast to the contracted type, same alias        | the values are gone |
| TYPE_CHANGED         | CAST back to the contracted type, same expression   | rounding, if any |
| NULLABILITY_RELAXED  | pass through, plus a test that fails on any null    | nothing is hidden |
| DATASET_MISSING      | nothing. There is no source to shield               | |

A shield never edits a contract, never touches RAW, and never auto merges.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .contracts import Contract, ContractColumn

MARK = "-- shield:"
STAGING = ROOT / "dbt" / "models" / "staging"
TESTS = ROOT / "dbt" / "tests"

SHIELDABLE = {"COLUMN_REMOVED", "TYPE_CHANGED", "NULLABILITY_RELAXED"}


@dataclass
class Patch:
    column: str
    change_type: str
    expression: str | None          # new select expression, None for pass-through
    test_sql: str | None            # a singular test to add, if any
    note: str                       # the comment left in the model
    honest: str                     # what the shield does not fix


@dataclass
class Shield:
    dataset_key: str
    table: str
    patches: list[Patch] = field(default_factory=list)
    unshieldable: list[str] = field(default_factory=list)   # "COLUMN.CHANGE: why"
    staging_before: str = ""
    staging_after: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.patches) and not self.errors

    @property
    def staging_path(self) -> Path:
        return STAGING / f"stg_{self.table.lower()}.sql"

    @property
    def branch(self) -> str:
        return f"shield/{self.table.lower()}"

    def test_files(self) -> dict[Path, str]:
        out = {}
        for p in self.patches:
            if p.test_sql:
                out[TESTS / f"shield_{self.table.lower()}_{p.column.lower()}_not_null.sql"] = p.test_sql
        return out


# --------------------------------------------------------------------------
# types
# --------------------------------------------------------------------------

def ddl_type(c: ContractColumn) -> str:
    """Contract type back to something CAST accepts."""
    if c.type == "TEXT":
        return f"varchar({c.length})" if c.length else "varchar"
    if c.type == "NUMBER":
        return f"number({c.precision},{c.scale})" if c.precision is not None else "number"
    return c.type.lower()


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    key = events[0]["DATASET_KEY"]
    table = key.split(".")[-1]
    sh = Shield(dataset_key=key, table=table)
    ref = f"see {issue_url}" if issue_url else "see the open drift issue"

    if contract is None:
        sh.errors.append("no contract for this dataset, nothing to restore to")
        return sh

    for e in events:
        ct, col = e["CHANGE_TYPE"], e.get("OBJECT_NAME")
        if ct not in SHIELDABLE or not col:
            sh.unshieldable.append(f"{col or '*'}.{ct}: cannot be shielded in staging")
            continue
        cc = contract.column(col)
        if cc is None:
            sh.unshieldable.append(f"{col}.{ct}: column not in the contract")
            continue
        t = ddl_type(cc)
        lower = col.lower()

        if ct == "COLUMN_REMOVED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"null::{t}",
                test_sql=None,
                note=f"{MARK} {col} dropped upstream, restored as NULL, {ref}",
                honest=f"{col} carries no values until upstream restores it",
            ))
        elif ct == "TYPE_CHANGED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=f"cast({{expr}} as {t})",
                test_sql=None,
                note=f"{MARK} {col} type changed upstream, cast back to {t}, {ref}",
                honest=f"{col} is cast to {t}; a wider or more precise upstream value is rounded or truncated",
            ))
        elif ct == "NULLABILITY_RELAXED":
            sh.patches.append(Patch(
                column=col, change_type=ct,
                expression=None,
                test_sql=(
                    f"-- shield test: {col} is contracted NOT NULL but upstream relaxed it, {ref}\n"
                    f"-- Fails the build the moment a null arrives, so it cannot flow into a report unseen.\n"
                    f"select {lower}\nfrom {{{{ ref('stg_{table.lower()}') }}}}\nwhere {lower} is null\n"
                ),
                note=f"{MARK} {col} may now be null upstream, guarded by a test, {ref}",
                honest=f"nulls in {col} are not filled in; the build fails so a human sees them",
            ))
    return sh


# --------------------------------------------------------------------------
# applying to the staging model
# --------------------------------------------------------------------------

_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<expr>.+?)(?:\s+as\s+(?P<alias>\w+))?(?P<tail>\s*,?\s*(?:--.*)?)$",
    re.IGNORECASE,
)


def _column_of(line: str) -> tuple[str | None, str | None]:
    """(column name this select line produces, the expression), or (None, None)."""
    s = line.strip()
    if not s or s.lower().startswith(("select", "from", "where", "with", "union", "--", "{{", "}}")):
        return None, None
    m = _LINE.match(line)
    if not m:
        return None, None
    expr, alias = m.group("expr").strip(), m.group("alias")
    name = alias or (expr if re.fullmatch(r"\w+", expr) else None)
    return (name.upper() if name else None), expr


def apply(shield: Shield, sql: str) -> Shield:
    """Rewrite the select lines the patches name. Refuse anything it cannot find."""
    shield.staging_before = sql
    lines = sql.splitlines()
    todo = {p.column.upper(): p for p in shield.patches if p.expression is not None}
    done = set()

    for i, line in enumerate(lines):
        name, expr = _column_of(line)
        if not name or name not in todo:
            continue
        p = todo[name]
        m = _LINE.match(line)
        indent, tail = m.group("indent"), m.group("tail") or ""
        comma = "," if "," in tail else ""
        new_expr = p.expression.replace("{expr}", expr)
        lines[i] = f"{indent}{new_expr} as {p.column.lower()}{comma}  {p.note}"
        done.add(name)

    missing = set(todo) - done
    for name in sorted(missing):
        shield.errors.append(f"{name}: select line not found in the staging model, refusing to guess")

    # pass-through patches only need the note somewhere visible
    header = [f"{p.note}" for p in shield.patches if p.expression is None]
    if header and not shield.errors:
        lines = header + [""] + lines if not lines[0].startswith(MARK) else header + lines

    shield.staging_after = "\n".join(lines) + ("\n" if sql.endswith("\n") else "")
    if "source('raw'" not in shield.staging_after:
        shield.errors.append("staging model no longer reads from source('raw', ...)")
    return shield


def build(events: list[dict], contract: Contract | None, issue_url: str | None) -> Shield:
    sh = plan(events, contract, issue_url)
    if not sh.patches:
        return sh
    if not sh.staging_path.exists():
        sh.errors.append(f"no staging model at {sh.staging_path.name}")
        return sh
    return apply(sh, sh.staging_path.read_text())


# --------------------------------------------------------------------------
# stale shields: the upstream got fixed, the shield is still there
# --------------------------------------------------------------------------

def installed() -> list[tuple[str, str, str]]:
    """(table, column, note) for every shield marker in every staging model."""
    out = []
    for p in sorted(STAGING.glob("stg_*.sql")):
        table = p.stem.removeprefix("stg_").upper()
        for line in p.read_text().splitlines():
            if MARK in line:
                note = line[line.index(MARK) + len(MARK):].strip()
                col = note.split(" ", 1)[0].upper()
                out.append((table, col, note))
    return out


def stale(active_columns: set[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Shields whose column no longer diverges. Safe to remove."""
    return [(t, c, n) for t, c, n in installed() if (t, c) not in active_columns]


# --------------------------------------------------------------------------
# pull request text
# --------------------------------------------------------------------------

def pr_title(sh: Shield) -> str:
    return f"Shield {sh.table}: keep reports correct over breaking upstream drift"


def pr_body(sh: Shield, issue_url: str | None, impact_md: str | None) -> str:
    lines = [
        f"Upstream broke `{sh.dataset_key}`. This restores the contracted shape in "
        f"`stg_{sh.table.lower()}` so the marts keep building and the reports stay "
        f"correct while the source is fixed.",
        "",
        "**This does not fix the data.** The contract is unchanged, the drift is still "
        "open, and the issue stays open until upstream is repaired. Remove this shield "
        "when it is.",
        "",
        "| column | change | shield | what it does not fix |",
        "|---|---|---|---|",
    ]
    for p in sh.patches:
        how = p.expression.replace("{expr}", "…") if p.expression else "pass through + not_null test"
        lines.append(f"| `{p.column}` | {p.change_type} | `{how}` | {p.honest} |")
    if sh.unshieldable:
        lines += ["", "**Not shielded**", *[f"- {u}" for u in sh.unshieldable]]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    if issue_url:
        lines += ["", f"Tracking issue: {issue_url}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# retirement: upstream was repaired, the shield has to come back out
#
# The inverse of apply(), and held to the same standard. A shield that is
# removed wrongly silently changes what a finance report says, so every
# restoration is derived from the shield's own line and anything that does not
# match the shape apply() wrote is refused rather than guessed at.
# --------------------------------------------------------------------------

_NULL_SHIELD = re.compile(
    r"^(?P<indent>\s*)null::[\w(),\s]+\s+as\s+(?P<col>\w+)(?P<comma>,?)\s*" + re.escape(MARK),
    re.IGNORECASE)
_CAST_SHIELD = re.compile(
    r"^(?P<indent>\s*)cast\(\s*(?P<expr>.+?)\s+as\s+[\w(),\s]+\)\s+as\s+(?P<col>\w+)(?P<comma>,?)\s*"
    + re.escape(MARK), re.IGNORECASE)

_ISSUE = re.compile(r"see (https://\S+)")


@dataclass
class Retirement:
    table: str
    columns: list[str] = field(default_factory=list)
    staging_before: str = ""
    staging_after: str = ""
    drop_tests: list[Path] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.columns or self.drop_tests) and not self.errors

    @property
    def staging_path(self) -> Path:
        return STAGING / f"stg_{self.table.lower()}.sql"

    @property
    def branch(self) -> str:
        return f"retire/{self.table.lower()}"


def retire_line(line: str) -> str | None:
    """A shielded select line restored to what it was. None if the shape is unknown."""
    m = _NULL_SHIELD.match(line)
    if m:
        return f"{m.group('indent')}{m.group('col').lower()}{m.group('comma')}"
    m = _CAST_SHIELD.match(line)
    if m:
        expr, col = m.group("expr").strip(), m.group("col")
        body = col.lower() if expr.lower() == col.lower() else f"{expr} as {col.lower()}"
        return f"{m.group('indent')}{body}{m.group('comma')}"
    return None


def plan_retirement(table: str, columns: set[str]) -> Retirement:
    """Remove the shields for these columns from one staging model."""
    r = Retirement(table=table)
    path = r.staging_path
    if not path.exists():
        r.errors.append(f"no staging model at {path.name}")
        return r

    sql = path.read_text()
    r.staging_before = sql
    out, wanted = [], {c.upper() for c in columns}
    seen = set()

    for line in sql.splitlines():
        if MARK not in line:
            out.append(line)
            continue
        note = line[line.index(MARK) + len(MARK):].strip()
        col = note.split(" ", 1)[0].upper()
        if col not in wanted:
            out.append(line)
            continue
        seen.add(col)
        found = _ISSUE.search(note)
        if found and found.group(1) not in r.issues:
            r.issues.append(found.group(1))

        if line.strip().startswith(MARK):
            # a pass-through shield: the note is the whole line, and its guard
            # is a singular test file that goes with it
            t = TESTS / f"shield_{table.lower()}_{col.lower()}_not_null.sql"
            if t.exists():
                r.drop_tests.append(t)
            r.columns.append(col)
            continue

        restored = retire_line(line)
        if restored is None:
            r.errors.append(
                f"{col}: shield line does not match a shape this can safely undo, "
                f"refusing to guess: {line.strip()}")
            out.append(line)
            continue
        out.append(restored)
        r.columns.append(col)

    for missing in sorted(wanted - seen):
        r.errors.append(f"{missing}: no shield marker found in {path.name}")

    text = "\n".join(out)
    # a pass-through shield leaves its blank separator line behind
    while text.startswith("\n"):
        text = text[1:]
    r.staging_after = text + ("\n" if sql.endswith("\n") else "")
    if "source('raw'" not in r.staging_after:
        r.errors.append("staging model no longer reads from source('raw', ...)")
    return r


def retire_title(r: Retirement) -> str:
    return f"Retire the {r.table} shield: upstream is repaired"


def retire_body(r: Retirement) -> str:
    lines = [
        f"`RAW.{r.table}` matches its contract again, so the shield in "
        f"`stg_{r.table.lower()}` has nothing left to do. Leaving it would keep "
        f"serving the shield's value instead of the real one.",
        "",
        "| column | shield removed | now reads |",
        "|---|---|---|",
    ]
    for c in r.columns:
        lines.append(f"| `{c}` | yes | the live column |")
    if r.drop_tests:
        lines += ["", "**Guard tests removed**", *[f"- `{p.name}`" for p in r.drop_tests]]
    lines += [
        "",
        "The detector confirmed the drift is gone before this was opened. If it "
        "reappears, the next cycle raises it again and proposes a fresh shield.",
    ]
    for url in r.issues:
        lines += ["", f"Closes {url}"]
    return "\n".join(lines)
```
**`control/onboard.py`**

```python
"""Onboarding a new source: contract to buildable model.

An ungoverned table used to stop at a contract. A contract alone changes
nothing: the table is still not declared as a dbt source, has no staging model,
and no tests. Onboarding finishes the job in one reviewed pull request.

Deterministic where the work is mechanical, model where it is judgement:

| Artifact                    | Who       | Why |
|-----------------------------|-----------|-----|
| sources.yml entry           | code      | one line, no judgement |
| tests from the contract     | code      | the contract already states the keys and nullability |
| staging model               | model     | which codes to upper, what to trim, what to name things |
| verification of all of it   | code      | the column set must match the contract exactly |

A mart is never wired up automatically. Where a new dataset belongs in the
reporting layer is a modelling decision with accounting consequences, and the
PR says so instead of guessing.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .contracts import Contract

STAGING = ROOT / "dbt" / "models" / "staging"
SOURCES = STAGING / "sources.yml"
SCHEMA = STAGING / "staging.yml"


@dataclass
class Onboarding:
    dataset_key: str
    table: str
    staging_sql: str = ""
    sources_after: str = ""
    schema_after: str = ""
    notes: str = ""                 # the model's note on where this might belong
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def model_name(self) -> str:
        return f"stg_{self.table.lower()}"

    @property
    def staging_path(self) -> Path:
        return STAGING / f"{self.model_name}.sql"

    @property
    def branch(self) -> str:
        return f"onboard/{self.table.lower()}"


# --------------------------------------------------------------------------
# deterministic artifacts
# --------------------------------------------------------------------------

def add_source(table: str, sources_yml: str) -> tuple[str, str | None]:
    """Append the table to the raw source list. Returns (text, error)."""
    if re.search(rf"^\s*-\s*name:\s*{re.escape(table)}\s*$", sources_yml, re.MULTILINE):
        return sources_yml, None
    lines = sources_yml.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip() == "tables:"), None)
    if idx is None:
        return sources_yml, "no `tables:` block in sources.yml"
    # indentation of the first entry under tables:
    entry_indent = None
    last = idx
    for i in range(idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if entry_indent is None:
            if not stripped.startswith("- "):
                return sources_yml, "unexpected shape under `tables:`"
            entry_indent = indent
        if indent < entry_indent:
            break
        last = i
    if entry_indent is None:
        return sources_yml, "`tables:` block is empty"
    lines.insert(last + 1, f"{' ' * entry_indent}- name: {table}")
    return "\n".join(lines) + "\n", None


def tests_for(contract: Contract) -> list[str]:
    """dbt tests the contract already justifies. Nothing invented."""
    out = []
    pk = [c.upper() for c in contract.primary_key]
    for c in contract.columns:
        tests = []
        if c.name.upper() in pk:
            tests = ["unique", "not_null"]
        elif not c.nullable:
            tests = ["not_null"]
        if tests:
            out.append(f"      - name: {c.name.lower()}\n"
                       f"        data_tests: [{', '.join(tests)}]")
    return out


def add_schema(contract: Contract, model: str, schema_yml: str) -> tuple[str, str | None]:
    if re.search(rf"^\s*-\s*name:\s*{re.escape(model)}\s*$", schema_yml, re.MULTILINE):
        return schema_yml, None
    tests = tests_for(contract)
    if not tests:
        return schema_yml, None
    desc = contract.description or f"Staging over {contract.dataset}"
    block = [f"  - name: {model}",
             # JSON strings are valid YAML double quoted scalars, so a colon or a
             # hash in the contract description cannot break the file.
             f"    description: {json.dumps(desc)}",
             "    columns:", *tests]
    text = schema_yml.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    return text, None


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _split_top_level(body: str) -> list[str]:
    """Split a select list on commas that are not inside brackets.

    `nullif(trim(x), '')` and `rate::number(18,8)` both carry commas that do not
    separate columns, so a plain split would invent columns that do not exist.
    """
    out, depth, buf = [], 0, []
    for ch in body:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def selected_columns(sql: str) -> set[str]:
    """Column names a simple `select a, b as c from ...` model produces."""
    # Comments go first: a shield comment carries prose with commas in it, and
    # splitting before stripping would mistake that prose for a column.
    clean = re.sub(r"--[^\n]*", "", sql)
    m = re.search(r"\bselect\b(.*?)\bfrom\b", clean, re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    out = set()
    for item in _split_top_level(m.group(1)):
        expr = " ".join(item.split()).strip()
        if not expr:
            continue
        alias = re.search(r"\bas\s+(\w+)\s*$", expr, re.IGNORECASE)
        name = alias.group(1) if alias else (expr if re.fullmatch(r"\w+", expr) else None)
        if name:
            out.add(name.upper())
    return out


def _source_ref(table: str) -> re.Pattern:
    return re.compile(r"source\(\s*['\"]raw['\"]\s*,\s*['\"]" + re.escape(table)
                      + r"['\"]\s*\)", re.IGNORECASE)


def canonical_source(sql: str, table: str) -> str:
    """Rewrite the source reference to the one spelling dbt will resolve.

    dbt matches a source name against sources.yml case-sensitively, and the
    entries there are upper case. A model that reads source('raw', 'ap_accrual')
    parses fine and then fails to compile. Spelling is mechanical, so it is
    corrected here rather than bounced back to the model.
    """
    return _source_ref(table).sub(f"source('raw', '{table}')", sql)


def verify(ob: Onboarding, contract: Contract) -> Onboarding:
    sql = ob.staging_sql
    if not sql.strip():
        ob.errors.append("no staging model was produced")
        return ob
    if not _source_ref(ob.table).search(sql):
        ob.errors.append(f"staging model does not read from source('raw', '{ob.table}')")

    want = {c.name.upper() for c in contract.columns}
    got = selected_columns(sql)
    missing, extra = sorted(want - got), sorted(got - want)
    if missing:
        ob.errors.append(f"staging model omits contracted columns {missing}")
    if extra:
        ob.errors.append(f"staging model produces columns the contract does not declare {extra}")
    for k in contract.primary_key:
        if k.upper() not in got:
            ob.errors.append(f"primary key column {k} is not in the staging model")
    if re.search(r"\bselect\s+\*", sql, re.IGNORECASE):
        ob.errors.append("staging model uses select *, which hides the column list from review")
    return ob


def build(contract: Contract, staging_sql: str, notes: str = "") -> Onboarding:
    table = contract.table
    ob = Onboarding(dataset_key=contract.dataset, table=table,
                    staging_sql=canonical_source(staging_sql.rstrip(), table) + "\n",
                    notes=notes)
    verify(ob, contract)

    src, err = add_source(table, SOURCES.read_text())
    if err:
        ob.errors.append(f"sources.yml: {err}")
    ob.sources_after = src

    sch, err = add_schema(contract, ob.model_name, SCHEMA.read_text())
    if err:
        ob.errors.append(f"staging.yml: {err}")
    ob.schema_after = sch
    return ob


# --------------------------------------------------------------------------
# pull request text
# --------------------------------------------------------------------------

def pr_title(ob: Onboarding) -> str:
    return f"Onboard {ob.dataset_key}: contract, source, staging model and tests"


def pr_body(ob: Onboarding, contract: Contract, impact_md: str | None) -> str:
    tests = tests_for(contract)
    lines = [
        f"`{ob.dataset_key}` landed in the warehouse with no contract. This onboards it.",
        "",
        "| Artifact | What |",
        "|---|---|",
        f"| `contracts/raw/{ob.table.lower()}.yml` | v{contract.version} contract, "
        f"{len(contract.columns)} columns, key `{', '.join(contract.primary_key)}` |",
        f"| `dbt/models/staging/sources.yml` | declares `{ob.table}` as a dbt source |",
        f"| `dbt/models/staging/{ob.model_name}.sql` | staging model over every contracted column |",
        f"| `dbt/models/staging/staging.yml` | {len(tests)} column(s) tested from the contract's own keys and nullability |",
        "",
        "**Not wired into any mart.** Where this belongs in the reporting layer is a "
        "modelling decision with accounting consequences, so it is left to a reviewer.",
    ]
    if ob.notes:
        lines += ["", f"Agent note: {ob.notes}"]
    lines += ["", "**Review checklist**",
              f"- Is `{', '.join(contract.primary_key)}` really the key?",
              "- Are the inferred column descriptions right?",
              "- Should this feed a mart, and which?"]
    if impact_md:
        lines += ["", "## Downstream impact", "", impact_md]
    return "\n".join(lines)
```
**`control/agent.py`**

```python
"""The drift agent.

Reads open drift events and turns them into pull requests a reviewer can
merge, or into escalations a reviewer must handle. The model drafts; the code
decides. Every proposal is verified deterministically against the live schema
before anything touches git, so a wrong draft is discarded, not merged.

Routing, by the worst event in a dataset:
  LOW       draft a contract bump, open a PR, enable auto merge
  MEDIUM    draft a contract bump, open a PR, wait for a human
  BREAKING  no PR. Open an issue with the evidence and mark the events ESCALATED.
"""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT
from .contracts import CONTRACT_DIR, Contract, load_contracts, parse_contract
from .detect import BREAKING, LOW, MEDIUM, ObservedColumn, diff_dataset
from .lineage import Lineage
from .snow import execute, query

SEV_ORDER = {LOW: 0, MEDIUM: 1, BREAKING: 2}
CONTRACT_TOOL = "propose_contract_change"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

@dataclass
class Bundle:
    """Every open event for one dataset, plus what the agent needs to reason."""
    dataset_key: str
    events: list[dict]
    contract: Contract | None
    observed: dict[str, ObservedColumn]
    lineage: Lineage | None = None

    def impact_for(self, event: dict):
        """What this one event breaks downstream."""
        if self.lineage is None:
            return None
        col = event.get("OBJECT_NAME") if event.get("CHANGE_TYPE") not in (
            "DATASET_MISSING", "DATASET_UNGOVERNED") else None
        return self.lineage.impact(self.dataset_key, col)

    def dataset_impact(self):
        """Everything downstream of the dataset, regardless of column."""
        return self.lineage.impact(self.dataset_key) if self.lineage else None

    @property
    def worst(self) -> str:
        return max((e["SEVERITY"] for e in self.events), key=SEV_ORDER.get)

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[1]

    @property
    def event_ids(self) -> list[str]:
        return [e["EVENT_ID"] for e in self.events]


@dataclass
class Proposal:
    bundle: Bundle
    contract_yaml: str
    pr_title: str
    pr_body: str
    reasoning: str
    staging_sql: str | None = None
    contract: Contract | None = None          # parsed, set by verify()
    errors: list[str] = field(default_factory=list)
    merge_note: str = "awaiting review"

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def contract_path(self) -> Path:
        return CONTRACT_DIR / "raw" / f"{self.bundle.table.lower()}.yml"

    @property
    def staging_path(self) -> Path:
        return ROOT / "dbt" / "models" / "staging" / f"stg_{self.bundle.table.lower()}.sql"

    @property
    def branch(self) -> str:
        v = self.contract.version if self.contract else "x"
        return f"drift/{self.bundle.table.lower()}-v{v}"


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def open_events(conn) -> list[dict]:
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, DETECTED_AT
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS = 'OPEN'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def bundle_events(
    events: list[dict],
    contracts: list[Contract],
    observed: dict[str, dict[str, ObservedColumn]],
    lineage: Lineage | None = None,
) -> list[Bundle]:
    by_key = {c.dataset: c for c in contracts}
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(e["DATASET_KEY"], []).append(e)
    return [
        Bundle(
            dataset_key=k,
            events=v,
            contract=by_key.get(k),
            observed=observed.get(k, {}),
            lineage=lineage,
        )
        for k, v in sorted(groups.items())
    ]


# --------------------------------------------------------------------------
# drafting (the only place the model is involved)
# --------------------------------------------------------------------------

TOOL_SCHEMA = {
    "name": CONTRACT_TOOL,
    "description": "Return the updated contract and, only if needed, an updated staging model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contract_yaml": {
                "type": "string",
                "description": "Complete contract file content. Same format as the current one.",
            },
            "staging_sql": {
                "type": ["string", "null"],
                "description": "Complete updated dbt staging model, or null if no change is needed.",
            },
            "pr_title": {"type": "string", "description": "Under 70 characters, imperative."},
            "pr_body": {
                "type": "string",
                "description": "Markdown. What changed upstream, what this PR adopts, what a "
                               "reviewer should check. Plain language for a finance reviewer.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences on why these choices, for the audit log.",
            },
        },
        "required": ["contract_yaml", "staging_sql", "pr_title", "pr_body", "reasoning"],
    },
}

SYSTEM = """You maintain data contracts for a finance data warehouse. A contract is the
agreement of record for the shape of a source dataset. Upstream changed a dataset
without telling anyone; the detector has classified the divergence. Your job is to
draft the contract change that adopts what is safe to adopt.

Rules you must follow exactly:
- Bump `version` by exactly one. For a dataset with no contract, version is 1.
- The new contract must describe the live schema exactly: every live column present,
  with the live type, length or precision/scale, and nullability. Nothing else.
- Never remove a column that exists in the live schema.
- Keep every existing description. Write a plausible finance description for a new
  column from its name and type. Say so in the description if you are inferring.
- Keep owner, classification, primary_key and freshness unchanged unless the change
  makes them wrong. For a new dataset use owner `unassigned-needs-review`.
- Only return staging_sql when a new column should flow through to staging. Keep the
  model's existing structure and add the column in the same style.
- Do not invent business rules. If you are unsure, adopt the column plainly and say
  so in pr_body so a reviewer can decide.
"""


def _observed_json(observed: dict[str, ObservedColumn]) -> str:
    rows = [
        {
            "name": o.name, "type": o.type, "nullable": o.nullable,
            "length": o.length, "precision": o.precision, "scale": o.scale,
            "ordinal": o.ordinal,
        }
        for o in sorted(observed.values(), key=lambda x: x.ordinal)
    ]
    return json.dumps(rows, indent=2)


def _events_json(events: list[dict]) -> str:
    keep = ("CHANGE_TYPE", "SEVERITY", "OBJECT_NAME", "BEFORE_STATE", "AFTER_STATE", "RATIONALE")
    return json.dumps([{k: e.get(k) for k in keep} for e in events], indent=2, default=str)


def build_prompt(bundle: Bundle, example_contract: str) -> str:
    current = (
        bundle.contract.source_path.read_text()
        if bundle.contract and bundle.contract.source_path
        else None
    )
    staging = None
    p = ROOT / "dbt" / "models" / "staging" / f"stg_{bundle.table.lower()}.sql"
    if p.exists():
        staging = p.read_text()

    parts = [f"Dataset: {bundle.dataset_key}", "", "Drift events:", _events_json(bundle.events), ""]

    impacts = [(e, bundle.impact_for(e)) for e in bundle.events]
    lines = [f"- {e['OBJECT_NAME'] or bundle.dataset_key}: {i.summary()}"
             + (f" ({', '.join(i.marts)})" if i and i.marts else "")
             for e, i in impacts if i and not i.empty]
    if lines:
        parts += ["Downstream impact of these changes, from dbt lineage:", *lines, ""]
    parts += ["Live schema from INFORMATION_SCHEMA:", _observed_json(bundle.observed), ""]
    if current:
        parts += ["Current contract:", "```yaml", current, "```", ""]
    else:
        parts += [
            "There is no contract for this dataset. Draft version 1 in the same "
            "format as this example:", "```yaml", example_contract, "```", "",
        ]
    if staging:
        parts += ["Current staging model:", "```sql", staging, "```", ""]
    parts.append(f"Call {CONTRACT_TOOL} with your proposal.")
    return "\n".join(parts)


def draft(bundle: Bundle, client=None, model: str | None = None) -> Proposal:
    """Ask the model for a proposal. Returns an unverified Proposal."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (CONTRACT_DIR / "raw" / "ap_invoice.yml").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": CONTRACT_TOOL},
        messages=[{"role": "user", "content": build_prompt(bundle, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    d = block.input
    return Proposal(
        bundle=bundle,
        contract_yaml=d["contract_yaml"],
        staging_sql=d.get("staging_sql") or None,
        pr_title=d["pr_title"],
        pr_body=d["pr_body"],
        reasoning=d["reasoning"],
    )


# --------------------------------------------------------------------------
# verification (deterministic, no model)
# --------------------------------------------------------------------------

def verify(proposal: Proposal) -> Proposal:
    """Reject anything the model got wrong. Populates proposal.errors."""
    b = proposal.bundle
    errs: list[str] = []
    tmp = ROOT / ".agent_tmp.yml"
    try:
        tmp.write_text(proposal.contract_yaml)
        new = parse_contract(tmp)
    except Exception as e:  # noqa: BLE001
        proposal.errors = [f"contract does not parse: {e}"]
        return proposal
    finally:
        if tmp.exists():
            tmp.unlink()

    if new.dataset != b.dataset_key:
        errs.append(f"dataset is {new.dataset}, expected {b.dataset_key}")

    expected_version = (b.contract.version + 1) if b.contract else 1
    if new.version != expected_version:
        errs.append(f"version is {new.version}, expected {expected_version}")

    if b.contract:
        old_names = {c.name for c in b.contract.columns}
        dropped = old_names - {c.name for c in new.columns}
        if dropped:
            errs.append(f"proposal drops contracted columns {sorted(dropped)}")
        if new.primary_key != b.contract.primary_key:
            errs.append("primary key changed")

    residual = diff_dataset(new, b.observed)
    for f in residual:
        errs.append(f"still diverges from live: {f.change_type} {f.object_name or ''}".strip())

    if proposal.staging_sql is not None and "source('raw'" not in proposal.staging_sql:
        errs.append("staging model no longer reads from source('raw', ...)")

    proposal.contract = new
    proposal.errors = errs
    return proposal


# --------------------------------------------------------------------------
# publishing (git + gh, runs on the machine that owns the repo)
# --------------------------------------------------------------------------

def _run(cmd: list[str], **kw) -> str:
    """Run a command, and on failure say what it actually said.

    CalledProcessError prints the command and the exit code but not the output,
    so a failing `gh pr create` used to produce a traceback with no reason in it.
    """
    try:
        return subprocess.check_output(cmd, cwd=ROOT, text=True,
                                       stderr=subprocess.STDOUT, **kw).strip()
    except subprocess.CalledProcessError as e:
        out = (e.output or "").strip()
        raise SystemExit(f"{' '.join(cmd[:3])} failed (exit {e.returncode}):\n{out}") from None


def _start_branch(branch: str, base: str):
    """Branch from the remote base, discarding any leftover from a failed run.

    Always from origin, never from local HEAD: otherwise an unpushed local commit
    is swept into the PR. A branch left behind by a run that died after the push
    is deleted first, so a retry is not blocked by its own wreckage.
    """
    _run(["git", "fetch", "-q", "origin", base])
    subprocess.run(["git", "branch", "-q", "-D", branch], cwd=ROOT,
                   capture_output=True, text=True)
    _run(["git", "checkout", "-q", "-b", branch, f"origin/{base}"])


def _push(branch: str):
    # --force-with-lease only ever overwrites a branch from an earlier failed run
    # of this same agent: if a PR existed, the caller returned before reaching here.
    _run(["git", "push", "-q", "--force-with-lease", "-u", "origin", branch])


LABELS = {
    "drift": ("0E8A16", "raised by the drift agent"),
    "low": ("C5DEF5", "additive, safe to auto merge"),
    "medium": ("FBCA04", "needs a decision"),
    "breaking": ("B60205", "release gate fails until resolved"),
}


def _ensure_labels():
    for name, (colour, desc) in LABELS.items():
        subprocess.run(
            ["gh", "label", "create", name, "--color", colour, "--description", desc, "--force"],
            cwd=ROOT, capture_output=True, text=True,
        )


def _ensure_clean_tree():
    if _run(["git", "status", "--porcelain"]):
        raise SystemExit("working tree is not clean; commit or stash before running the agent")


def _ensure_pushed(base: str):
    """Refuse to open a PR against a base that is missing your local commits.

    The agent branches from origin/<base> so unpushed local work is never swept
    into a PR. The other half of that rule: if local <base> is ahead of the
    remote, every file you have changed locally but not pushed shows up in the
    PR diff as a deletion or a revert. The PR is not wrong, it is just built on
    a base nobody else can see yet.
    """
    _run(["git", "fetch", "-q", "origin", base])
    ahead = _run(["git", "rev-list", "--count", f"origin/{base}..{base}"])
    if ahead != "0":
        raise SystemExit(
            f"local '{base}' is {ahead} commit(s) ahead of origin/{base}. The agent "
            f"branches from origin, so the PR would read as reverting them.\n"
            f"Run: git push"
        )


def required_checks(base: str) -> list[str]:
    """Contexts GitHub will actually wait for before an auto merge completes.

    Empty means auto merge is not a gate: GitHub merges as soon as the PR is
    mergeable, whether or not the workflow ever ran.
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{base}/protection"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("required_status_checks", {}).get("contexts", []) or []


def existing_pr(branch: str) -> str | None:
    """A previous run may have opened the PR and failed afterwards. Reuse it."""
    out = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
         "--jq", ".[0].url"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    return out or None


def publish(proposal: Proposal, auto_merge: bool) -> str:
    """Write files, branch, commit, push, open PR. Returns the PR url."""
    assert proposal.ok and proposal.contract
    _ensure_clean_tree()
    _ensure_labels()
    base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    branch = proposal.branch
    existing = existing_pr(branch)
    if existing:
        return existing

    # Branch from the remote base, never from local HEAD. Otherwise any local
    # commit not yet pushed is swept into the PR, and a contract change arrives
    # carrying unrelated work.
    try:
        _start_branch(branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        files = [str(proposal.contract_path.relative_to(ROOT))]
        if proposal.staging_sql is not None:
            proposal.staging_path.write_text(proposal.staging_sql)
            files.append(str(proposal.staging_path.relative_to(ROOT)))
        _run(["git", "add", *files])
        impact = proposal.bundle.dataset_impact()
        impact_block = (
            f"\n## Downstream impact\n\n{impact.markdown()}\n"
            if impact and not impact.empty else ""
        )
        body = (
            f"{proposal.pr_body}\n{impact_block}\n---\n"
            f"Agent reasoning: {proposal.reasoning}\n\n"
            f"Drift events: {', '.join(proposal.bundle.event_ids)}\n"
        )
        _run(["git", "commit", "-q", "-m", proposal.pr_title, "-m", body])
        _push(branch)
        labels = ["drift", proposal.bundle.worst.lower()]
        url = _run([
            "gh", "pr", "create", "--title", proposal.pr_title, "--body", body,
            "--base", base, "--head", branch, *sum((["--label", l] for l in labels), []),
        ]).splitlines()[-1]
        if auto_merge and proposal.bundle.worst == LOW:
            if required_checks(base):
                _run(["gh", "pr", "merge", url, "--auto", "--squash", "--delete-branch"])
                proposal.merge_note = "auto merge on"
            else:
                proposal.merge_note = (
                    "auto merge NOT enabled: branch protection on "
                    f"'{base}' requires no status checks, so GitHub would merge "
                    "without waiting for the gate"
                )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


def escalate(bundle: Bundle) -> str:
    """BREAKING gets an issue, never a PR. Returns the issue url."""
    _ensure_labels()
    imps = [bundle.impact_for(e) for e in bundle.events]
    reports = sorted({r["label"] for i in imps if i for r in i.reports})
    marts = sorted({m for i in imps if i for m in i.marts})
    hit = [f"**{r}**" for r in reports] or [f"`{m}`" for m in marts]
    headline = (
        f"Breaking drift on `{bundle.dataset_key}`."
        + (f" This affects {', '.join(hit)}." if hit else "")
        + " The release gate will fail until this is resolved."
    )
    lines = [
        headline, "",
        "| severity | change | object | breaks | why |",
        "|---|---|---|---|---|",
    ]
    for e in bundle.events:
        imp = bundle.impact_for(e)
        lines.append(
            f"| {e['SEVERITY']} | {e['CHANGE_TYPE']} | {e['OBJECT_NAME'] or '-'} "
            f"| {imp.summary() if imp else '-'} | {e['RATIONALE']} |"
        )
    detail = bundle.dataset_impact()
    if detail and not detail.empty:
        lines += ["", "<details><summary>Full downstream impact</summary>", "",
                  detail.markdown(), "", "</details>"]
    lines += [
        "",
        "Options: revert the upstream change, or agree a new contract version through a reviewed PR.",
        "",
        f"Drift events: {', '.join(bundle.event_ids)}",
    ]
    return _run([
        "gh", "issue", "create",
        "--title", f"Breaking drift: {bundle.dataset_key}",
        "--body", "\n".join(lines),
        "--label", "drift", "--label", "breaking",
    ]).splitlines()[-1]


def mark(conn, event_ids: list[str], status: str, ref: str):
    params = {"st": status, "ref": ref}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    execute(
        conn,
        "UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, RESOLUTION_REF = %(ref)s "
        "WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )


# --------------------------------------------------------------------------
# reconciliation: GitHub is the source of truth for what happened to the work
# --------------------------------------------------------------------------

def _gh_json(args: list[str]) -> dict | None:
    r = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def github_outcome(url: str) -> tuple[str, str] | None:
    """What became of the pull request or issue behind an event.

    Returns (new_status, human reason), or None when nothing has changed yet or
    the reference cannot be read.
    """
    if "/pull/" in url:
        d = _gh_json(["pr", "view", url, "--json", "state,mergedAt"])
        if not d:
            return None
        if d.get("mergedAt"):
            return "MERGED", "pull request merged"
        if d.get("state") == "CLOSED":
            return "OPEN", "pull request closed without merging, so the drift is unresolved"
        return None
    if "/issues/" in url:
        d = _gh_json(["issue", "view", url, "--json", "state"])
        if not d:
            return None
        if d.get("state") == "CLOSED":
            return "DISMISSED", "issue closed"
        return None
    return None


def pending_events(conn) -> list[dict]:
    return query(
        conn,
        "SELECT EVENT_ID, DATASET_KEY, OBJECT_NAME, SEVERITY, STATUS, RESOLUTION_REF "
        "FROM FIN_AIWH.META.DRIFT_EVENT "
        "WHERE STATUS IN ('PROPOSED', 'ESCALATED') AND RESOLUTION_REF IS NOT NULL "
        "ORDER BY DATASET_KEY, OBJECT_NAME",
    )


def set_status(conn, event_ids: list[str], status: str):
    params = {"st": status}
    keys = []
    for i, e in enumerate(event_ids):
        params[f"e{i}"] = e
        keys.append(f"%(e{i})s")
    resolved = "RESOLVED_AT = SYSDATE(), " if status in ("MERGED", "DISMISSED") else ""
    execute(
        conn,
        f"UPDATE FIN_AIWH.META.DRIFT_EVENT SET STATUS = %(st)s, {resolved}"
        "RESOLUTION_REF = RESOLUTION_REF WHERE EVENT_ID IN (" + ",".join(keys) + ")",
        params,
    )


# --------------------------------------------------------------------------
# shields: a PR that keeps the reports correct while upstream is fixed
# --------------------------------------------------------------------------

def breaking_events(conn) -> list[dict]:
    """Escalated events too, so a shield can follow an issue opened last run."""
    return query(
        conn,
        """
        SELECT EVENT_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE, SEVERITY,
               OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS, RESOLUTION_REF
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS IN ('OPEN', 'ESCALATED') AND SEVERITY = 'BREAKING'
        ORDER BY DATASET_KEY, OBJECT_NAME
        """,
    )


def publish_shield(sh, issue_url: str | None, impact_md: str | None, base: str | None = None) -> str:
    """Branch from origin, write the staging model and any tests, open a PR. Never auto merges."""
    from .shield import pr_body, pr_title
    assert sh.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "shield", "--color", "5319E7",
                    "--description", "restores contracted shape over breaking drift", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(sh.branch)
    if existing:
        return existing
    try:
        _start_branch(sh.branch, base)
        sh.staging_path.write_text(sh.staging_after)
        files = [str(sh.staging_path.relative_to(ROOT))]
        for path, sql in sh.test_files().items():
            path.write_text(sql)
            files.append(str(path.relative_to(ROOT)))
        _run(["git", "add", *files])
        body = pr_body(sh, issue_url, impact_md)
        _run(["git", "commit", "-q", "-m", pr_title(sh), "-m", body])
        _push(sh.branch)
        url = _run([
            "gh", "pr", "create", "--title", pr_title(sh), "--body", body,
            "--base", base, "--head", sh.branch,
            "--label", "drift", "--label", "breaking", "--label", "shield",
        ]).splitlines()[-1]
        if issue_url:
            subprocess.run(
                ["gh", "issue", "comment", issue_url, "--body",
                 f"Shield proposed: {url}\n\nKeeps the reports correct while this is fixed. "
                 f"Remove it when this issue closes."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


# --------------------------------------------------------------------------
# onboarding a new source
#
# A contract alone leaves the table unusable: not declared as a dbt source, no
# staging model, no tests. Onboarding produces all four artifacts in one PR.
# The contract and the staging model are drafted by the model; the source
# entry, the tests and every check on the result are deterministic code.
# --------------------------------------------------------------------------

ONBOARD_TOOL = "propose_staging_model"

ONBOARD_TOOL_SCHEMA = {
    "name": ONBOARD_TOOL,
    "description": "Return a dbt staging model over a newly contracted raw table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "staging_sql": {
                "type": "string",
                "description": "Complete dbt model file. One select over the raw source, "
                               "one output column per contracted column, same names.",
            },
            "notes": {
                "type": "string",
                "description": "Two or three sentences for the reviewer: what this table "
                               "appears to hold, which mart it might belong to and why you "
                               "did not wire it, anything you had to guess.",
            },
        },
        "required": ["staging_sql", "notes"],
    },
}

ONBOARD_SYSTEM = """You write dbt staging models for a finance data warehouse. A new raw
table has just been put under contract. Write the staging model that makes it usable.

The staging layer normalises, it does not interpret. Rules you must follow exactly:
- Output exactly one column per column in the contract, with the contract's own name
  in lower case. No extra columns, no derived or calculated columns, none dropped.
  A reviewer adds business logic later; inventing it here hides it from review.
- Read from {{ source('raw', 'TABLE') }} and nothing else. No joins, no ref().
- Never use select *. The column list is what a reviewer reads.
- Clean only where the column's own type and meaning justify it: upper() on codes,
  statuses and currencies, trim() on free text, nullif(trim(x), '') where an empty
  string is really a missing value. Leave amounts, dates, ids and flags untouched:
  casting or rounding money in staging is a business decision.
- Do not wire the table into any mart. Say in notes where you think it belongs.
"""


def build_onboard_prompt(contract_yaml: str, observed: dict[str, ObservedColumn],
                         table: str, example_model: str) -> str:
    return "\n".join([
        f"Raw table: RAW.{table}", "",
        "Its contract, merged in this same pull request:", "```yaml", contract_yaml, "```", "",
        "Live schema from INFORMATION_SCHEMA:", _observed_json(observed), "",
        "An existing staging model in this project, for style:", "```sql", example_model, "```", "",
        f"Call {ONBOARD_TOOL} with the staging model for this table.",
    ])


def draft_staging(contract_yaml: str, observed: dict[str, ObservedColumn], table: str,
                  client=None, model: str | None = None) -> tuple[str, str]:
    """Ask the model for the staging SQL. Returns (staging_sql, notes), unverified."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise SystemExit(
                "the anthropic package is not installed. Run: pip install -r requirements.txt"
            )
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env")
        client = anthropic.Anthropic()
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    example = (ROOT / "dbt" / "models" / "staging" / "stg_ap_vendor.sql").read_text()
    msg = client.messages.create(
        model=model,
        max_tokens=3000,
        system=ONBOARD_SYSTEM,
        tools=[ONBOARD_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": ONBOARD_TOOL},
        messages=[{"role": "user",
                   "content": build_onboard_prompt(contract_yaml, observed, table, example)}],
    )
    block = next(b for b in msg.content if getattr(b, "type", "") == "tool_use")
    return block.input["staging_sql"], block.input.get("notes", "")


def publish_onboarding(proposal: Proposal, ob, impact_md: str | None,
                       base: str | None = None) -> str:
    """Contract, source entry, staging model and tests in one PR. Never auto merges."""
    from .onboard import SCHEMA, SOURCES, pr_body, pr_title
    assert proposal.ok and proposal.contract and ob.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "onboard", "--color", "0E8A16",
                    "--description", "brings an ungoverned table under contract", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(ob.branch)
    if existing:
        return existing
    try:
        _start_branch(ob.branch, base)
        proposal.contract_path.write_text(proposal.contract_yaml)
        ob.staging_path.write_text(ob.staging_sql)
        SOURCES.write_text(ob.sources_after)
        SCHEMA.write_text(ob.schema_after)
        files = [str(p.relative_to(ROOT)) for p in
                 (proposal.contract_path, ob.staging_path, SOURCES, SCHEMA)]
        _run(["git", "add", *files])
        title = pr_title(ob)
        body = (f"{pr_body(ob, proposal.contract, impact_md)}\n\n---\n"
                f"Agent reasoning: {proposal.reasoning}\n\n"
                f"Drift events: {', '.join(proposal.bundle.event_ids)}\n")
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(ob.branch)
        return _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", ob.branch,
            "--label", "drift", "--label", "medium", "--label", "onboard",
        ]).splitlines()[-1]
    finally:
        _run(["git", "checkout", "-q", base])


# --------------------------------------------------------------------------
# retiring a shield
#
# A shield is temporary by definition. When the live schema matches the contract
# again, the shield is serving a substitute value where the real one is now
# available. This opens the PR that takes it out. The PR body carries
# "Closes <issue>", so merging it closes the tracking issue, and the next sync
# moves the escalated event to DISMISSED through the existing lifecycle. No new
# state was added for this.
# --------------------------------------------------------------------------

def publish_retirement(r, base: str | None = None) -> str:
    from .shield import retire_body, retire_title
    assert r.ok
    _ensure_clean_tree()
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "retire", "--color", "BFD4F2",
                    "--description", "removes a shield whose drift is repaired", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    base = base or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _ensure_pushed(base)
    existing = existing_pr(r.branch)
    if existing:
        return existing
    try:
        _start_branch(r.branch, base)
        r.staging_path.write_text(r.staging_after)
        _run(["git", "add", str(r.staging_path.relative_to(ROOT))])
        for t in r.drop_tests:
            _run(["git", "rm", "-q", str(t.relative_to(ROOT))])
        title, body = retire_title(r), retire_body(r)
        _run(["git", "commit", "-q", "-m", title, "-m", body])
        _push(r.branch)
        url = _run([
            "gh", "pr", "create", "--title", title, "--body", body,
            "--base", base, "--head", r.branch,
            "--label", "drift", "--label", "retire",
        ]).splitlines()[-1]
        for issue in r.issues:
            subprocess.run(
                ["gh", "issue", "comment", issue, "--body",
                 f"Upstream is repaired and the detector no longer raises this. "
                 f"Retirement proposed: {url}. Merging it closes this issue."],
                cwd=ROOT, capture_output=True, text=True,
            )
        return url
    finally:
        _run(["git", "checkout", "-q", base])


# --------------------------------------------------------------------------
# content breaches
#
# A duplicate key, a null in a required column or a stale table cannot be fixed
# by changing a contract. The contract is right and the data is wrong. So a
# content breach is always an issue, whatever its severity, and it is closed by
# the agent itself the moment the measurement is back inside the contract.
# --------------------------------------------------------------------------

def escalate_quality(bundle: Bundle) -> str:
    _ensure_labels()
    subprocess.run(["gh", "label", "create", "quality", "--color", "D93F0B",
                    "--description", "data breaches its contract", "--force"],
                   cwd=ROOT, capture_output=True, text=True)
    imps = [bundle.impact_for(e) for e in bundle.events]
    reports = sorted({r["label"] for i in imps if i for r in i.reports})
    hit = ", ".join(f"**{r}**" for r in reports)
    lines = [
        f"The data in `{bundle.dataset_key}` breaches its contract. The schema is "
        f"fine; the contents are not." + (f" Feeds {hit}." if hit else ""),
        "",
        "| severity | breach | on | measured |",
        "|---|---|---|---|",
    ]
    for e in bundle.events:
        lines.append(f"| {e['SEVERITY']} | {e['CHANGE_TYPE']} | `{e['OBJECT_NAME']}` | {e['RATIONALE']} |")
    lines += [
        "",
        "No pull request is proposed: no contract change makes bad data good. "
        "Fix the data at source. This issue closes itself on the next agent run "
        "after the measurement is back within the contract.",
        "",
        f"Drift events: {', '.join(bundle.event_ids)}",
    ]
    worst = bundle.worst.lower()
    return _run([
        "gh", "issue", "create",
        "--title", f"Data breaches contract: {bundle.dataset_key}",
        "--body", "\n".join(lines),
        "--label", "drift", "--label", "quality", "--label", worst,
    ]).splitlines()[-1]


def close_quality_issue(url: str, why: str):
    subprocess.run(["gh", "issue", "close", url, "--comment",
                    f"Measurement is back within the contract: {why}. Closed by the agent."],
                   cwd=ROOT, capture_output=True, text=True)
```
**`control/cli.py`**

```python
"""fin_aiwh control plane CLI."""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from .config import ROOT, load_settings
from .contracts import load_contracts
from .detect import (
    BREAKING, MEDIUM, attach_impact, diff_all, fetch_observed, new_run_id, persist,
    snapshot_observed,
)
from .agent import (BREAKING as _B, breaking_events, bundle_events, close_quality_issue,
                    draft, draft_staging, escalate, escalate_quality, github_outcome, mark,
                    open_events, pending_events, publish, publish_onboarding,
                    publish_retirement, publish_shield, set_status, verify)
from .lineage import Lineage
from .load import load_all
from .register import register
from . import onboard as onboard_mod
from . import quality as quality_mod
from . import shield as shield_mod
from .snow import connect, execute, execute_script, query

console = Console()
SEV_STYLE = {"BREAKING": "bold red", "MEDIUM": "yellow", "LOW": "cyan"}


def _log_run(conn, run_id: str, run_type: str, started, scanned, events, status, detail=None):
    execute(
        conn,
        """
        INSERT INTO META.RUN_LOG
          (RUN_ID, STARTED_AT, FINISHED_AT, RUN_TYPE, DATASETS_SCANNED,
           EVENTS_RAISED, STATUS, DETAIL)
        SELECT %(run_id)s, %(started)s, SYSDATE(), %(run_type)s, %(scanned)s,
               %(events)s, %(status)s, TRY_PARSE_JSON(%(detail)s)
        """,
        {
            "run_id": run_id, "started": started.replace(tzinfo=None),
            "run_type": run_type, "scanned": scanned, "events": events,
            "status": status, "detail": json.dumps(detail) if detail else None,
        },
    )


def cmd_ping(_args):
    s = load_settings()
    with connect(s) as conn:
        r = query(
            conn,
            "SELECT CURRENT_ACCOUNT() A, CURRENT_USER() U, CURRENT_ROLE() R, "
            "CURRENT_WAREHOUSE() W, CURRENT_DATABASE() D, CURRENT_VERSION() V",
        )[0]
    console.print("[green]connected[/green]")
    for k, label in [("A", "account"), ("U", "user"), ("R", "role"),
                     ("W", "warehouse"), ("D", "database"), ("V", "snowflake")]:
        console.print(f"  {label:<10} {r[k]}")
    return 0


_DDL = re.compile(
    r"\b(CREATE(?:\s+OR\s+REPLACE)?\s+(?:TRANSIENT\s+|TEMPORARY\s+)?TABLE|ALTER\s+TABLE|DROP\s+TABLE)"
    r"(?:\s+IF\s+(?:NOT\s+)?EXISTS)?\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)


def _unqualified_ddl(sql: str) -> list[str]:
    """Table DDL that names fewer than three parts depends on session state."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    return [
        f"{m.group(1)} {m.group(2)}"
        for m in _DDL.finditer(stripped)
        if m.group(2).count(".") < 2
    ]


def cmd_apply(args):
    path = ROOT / args.path
    if not path.exists():
        console.print(f"[red]no such file:[/red] {args.path}")
        return 1
    sql = path.read_text()
    bad = _unqualified_ddl(sql)
    if bad:
        console.print("[red]refusing to apply: unqualified table names in DDL[/red]")
        for b in bad:
            console.print(f"  {b}")
        console.print("[dim]use FIN_AIWH.<SCHEMA>.<TABLE> so the statement cannot land in the wrong schema[/dim]")
        return 1
    with connect() as conn:
        n = execute_script(conn, sql)
    console.print(f"[green]applied[/green] {args.path} ({n} statements)")
    return 0


def cmd_load(args):
    with connect() as conn:
        results = load_all(conn, args.tables or None)
    rc = 0
    for table, n in results:
        if isinstance(n, str):
            rc = 1
            console.print(f"  [yellow]{table:<18}[/yellow] {n}")
        else:
            console.print(f"  [green]loaded[/green] {table:<18} {n:>8,} rows")
    return rc


def cmd_register(_args):
    contracts = load_contracts()
    console.print(f"loaded {len(contracts)} contracts from contracts/")
    with connect() as conn:
        result = register(conn, contracts)
    for k in ("added", "updated"):
        for d in result[k]:
            console.print(f"  [green]{k[:-1]:<8}[/green] {d}")
    if result["unchanged"]:
        console.print(f"  [dim]unchanged {len(result['unchanged'])}[/dim]")
    return 0


def cmd_detect(args):
    started = datetime.now(timezone.utc)
    run_id = new_run_id()
    contracts = load_contracts()
    s = load_settings()

    if args.dataset:
        wanted = {d.upper() for d in args.dataset}
        contracts = [c for c in contracts if c.dataset in wanted]
        if not contracts:
            console.print(f"[red]no contracts match {sorted(wanted)}[/red]")
            return 1

    with connect(s) as conn:
        observed = fetch_observed(conn, s.database, s.raw_schema)
        if args.dataset:
            # scoped run: only judge the named datasets, never flag others as ungoverned
            observed = {k: v for k, v in observed.items() if k in wanted}
        findings = diff_all(contracts, observed)

        # Content, by the same contracts. Only tables that exist and match are
        # measured: a DMF on a column that was dropped upstream would just fail.
        content: list = []
        if not args.no_quality:
            # A DMF on a dropped column fails; on a relaxed one it is exactly
            # the question worth asking. Exclude only what cannot be measured.
            unmeasurable = {f.dataset_key for f in findings
                            if f.change_type in ("DATASET_MISSING", "COLUMN_REMOVED", "TYPE_CHANGED")}
            measurable = [c for c in contracts if c.dataset in observed and c.dataset not in unmeasurable]
            try:
                content = quality_mod.check_all(conn, measurable)
            except Exception as e:  # noqa: BLE001
                # Snowflake puts the code on line 1 and the reason on line 2.
                reason = " ".join(l.strip() for l in str(e).splitlines()[:3] if l.strip())
                console.print(f"[yellow]content checks skipped:[/yellow] {reason}")
                console.print("[dim]run ops/20_quality.sql once as ACCOUNTADMIN, or pass --no-quality[/dim]")
        findings += content

    lineage = Lineage.load()
    attach_impact(findings, lineage)

    with connect(s) as conn:
        if not args.dry_run:
            snapshot_observed(conn, run_id, observed)
            written, suppressed = persist(conn, run_id, findings, contracts)
            _log_run(conn, run_id, "DETECT", started, len(observed), written, "SUCCESS")
        else:
            written = suppressed = 0

    console.print(
        f"run [bold]{run_id}[/bold]  scanned {len(observed)} datasets  "
        f"found {len(findings)} divergences"
        + ("  [dim](dry run, nothing written)[/dim]" if args.dry_run
           else f"  new {written}"
                + (f"  [dim]already being worked on {suppressed}[/dim]" if suppressed else ""))
    )

    schema_only = [f for f in findings if f.change_type not in quality_mod.QUALITY_TYPES]
    active = {(f.dataset_key.split(".")[-1], (f.object_name or "").upper()) for f in schema_only}
    for table, col, note in shield_mod.stale(active):
        console.print(f"[yellow]stale shield:[/yellow] stg_{table.lower()} {col} no longer "
                      f"diverges, the shield can be removed  [dim]{note}[/dim]")

    if not findings:
        console.print("[green]warehouse matches every registered contract[/green]")
        return 0

    table = Table(show_lines=False, header_style="bold")
    for col in ("severity", "dataset", "change", "object", "breaks", "why"):
        table.add_column(col, overflow="fold")
    for f in findings:
        table.add_row(
            f"[{SEV_STYLE[f.severity]}]{f.severity}[/]",
            f.dataset_key, f.change_type, f.object_name or "-",
            f.impact.summary() if f.impact else "-",
            f.rationale,
        )
    console.print(table)

    if not lineage.available:
        console.print("[yellow]no dbt manifest, so downstream impact is unknown.[/yellow] "
                      "[dim]run: python -m control.cli dbt parse[/dim]")
    else:
        worst = [f for f in findings if f.severity == BREAKING and f.impact and not f.impact.empty]
        if worst:
            reports = sorted({r["label"] for f in worst for r in f.impact.reports})
            marts = sorted({m for f in worst for m in f.impact.marts})
            if reports:
                console.print(f"\n[bold red]reports affected by breaking drift:[/bold red] "
                              + ", ".join(reports))
            if marts:
                console.print(f"[red]marts:[/red] " + ", ".join(marts))

    if args.fail_on_breaking and any(f.severity == BREAKING for f in findings):
        console.print("[bold red]breaking drift present[/bold red]")
        return 2
    return 0


def cmd_dbt(args):
    """Run dbt with .env loaded and the key path made absolute."""
    s = load_settings()
    env = dict(os.environ)
    env.update({
        "SNOWFLAKE_ACCOUNT": s.account,
        "SNOWFLAKE_USER": s.user,
        "SNOWFLAKE_PRIVATE_KEY_PATH": str(s.private_key_path),
        "SNOWFLAKE_ROLE": s.role,
        "SNOWFLAKE_WAREHOUSE": s.warehouse,
        "SNOWFLAKE_DATABASE": s.database,
    })
    dbt_dir = ROOT / "dbt"
    cmd = ["dbt", *args.dbt_args, "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.call(cmd, env=env, cwd=dbt_dir)


def cmd_resolve(args):
    """Close open drift events once the warehouse or the contract has been fixed."""
    if not args.all and not args.event:
        console.print("[red]pass --all or one or more event ids[/red]")
        return 1
    where = "STATUS = 'OPEN'" if args.all else "EVENT_ID IN (%s)" % ",".join(
        f"'{e}'" for e in args.event
    )
    with connect() as conn:
        n = query(conn, f"SELECT COUNT(*) AS C FROM META.DRIFT_EVENT WHERE {where}")[0]["C"]
        execute(
            conn,
            f"UPDATE META.DRIFT_EVENT SET STATUS = %(st)s, RESOLVED_AT = SYSDATE(), "
            f"RESOLUTION_REF = %(ref)s WHERE {where}",
            {"st": args.status, "ref": args.ref},
        )
    console.print(f"[green]{n} event(s) marked {args.status}[/green]")
    return 0


def _live_active(contracts, observed) -> set[tuple[str, str]]:
    """(table, column) pairs that diverge right now, from the live schema.

    The event table records what was true when an event was raised. The live
    schema is what is true now. Anything that acts on a column consults this,
    never the event's own status.
    """
    return {(f.dataset_key.split(".")[-1], (f.object_name or "").upper())
            for f in diff_all(contracts, observed)}


def _shield(b, issue_url, settings, active: set[tuple[str, str]]):
    """Try to keep the reports correct while upstream is fixed.

    Two things are skipped, and both are idempotency, not policy. A column that
    no longer diverges live belongs to retirement, not to a fresh shield. A
    column whose shield is already installed on the base branch would produce
    an identical file and an empty commit.
    """
    installed = {(tb, c) for tb, c, _ in shield_mod.installed()}
    breaking = []
    for e in b.events:
        if e["SEVERITY"] != "BREAKING":
            continue
        key = (b.table, (e.get("OBJECT_NAME") or "").upper())
        if key not in active:
            console.print(f"  [dim]{key[1] or b.table}: no longer diverges live, "
                          f"retirement handles it[/dim]")
            continue
        if key in installed:
            console.print(f"  [dim]{key[1]}: already shielded on the base branch[/dim]")
            continue
        breaking.append(e)
    if not breaking:
        return
    sh = shield_mod.build(breaking, b.contract, issue_url)
    if not sh.patches:
        if sh.unshieldable or sh.errors:
            console.print("  [dim]no shield possible: "
                          + "; ".join(sh.unshieldable + sh.errors) + "[/dim]")
        return
    if not sh.ok:
        console.print("  [yellow]shield refused:[/yellow] " + "; ".join(sh.errors))
        return
    di = b.dataset_impact()
    url = publish_shield(sh, issue_url, di.markdown() if di and not di.empty else None)
    cols = ", ".join(p.column for p in sh.patches)
    console.print(f"  [magenta]shield PR[/magenta] {url}  [dim]{cols}[/dim]")
    for u in sh.unshieldable:
        console.print(f"  [dim]not shielded: {u}[/dim]")


DRY = "dry-run"


def _onboard(b, p, dry_run: bool):
    """An ungoverned table needs more than a contract to be usable.

    The contract says what the table is. The source entry, the staging model and
    the tests are what let anything read it. All four land in one PR, and the
    column list of the staging model is checked against the contract before the
    PR is opened.
    """
    sql, notes = draft_staging(p.contract_yaml, b.observed, b.table)
    ob = onboard_mod.build(p.contract, sql, notes)
    if not ob.ok:
        console.print("  [red]staging model rejected by verification:[/red]")
        for e in ob.errors:
            console.print(f"    {e}")
        console.print("  [dim]rejected model:[/dim]")
        console.print(ob.staging_sql)
        return None
    console.print(f"  onboarding: contract v{p.contract.version}, source entry, "
                  f"[bold]{ob.model_name}[/bold], "
                  f"{len(onboard_mod.tests_for(p.contract))} tested column(s)")
    console.print(f"  [dim]{notes}[/dim]")
    if dry_run:
        console.print("  [dim]dry run, printing staging model:[/dim]")
        console.print(ob.staging_sql)
        return DRY
    di = b.dataset_impact()
    return publish_onboarding(p, ob, di.markdown() if di and not di.empty else None)


def _retire_stale(active: set[tuple[str, str]], dataset: str | None, dry_run: bool) -> int:
    """Open a retirement PR for every shield whose drift is gone.

    Judged against the live schema, not the event table. An escalated event can
    outlive the divergence it recorded; the shield's own column is the truth.
    """
    by_table: dict[str, set[str]] = {}
    for table, col, _ in shield_mod.stale(active):
        if dataset and f"RAW.{table}" != dataset.upper():
            continue
        by_table.setdefault(table, set()).add(col)
    if not by_table:
        return 0
    rc = 0
    for table, cols in sorted(by_table.items()):
        r = shield_mod.plan_retirement(table, cols)
        console.print(f"[bold]RAW.{table}[/bold]  shield stale  {', '.join(sorted(cols))}")
        if not r.ok:
            rc = 1
            console.print("  [yellow]retirement refused:[/yellow] " + "; ".join(r.errors))
            continue
        if dry_run:
            console.print("  → would open a retirement PR and close " + ", ".join(r.issues))
            console.print("  [dim]dry run, printing restored model:[/dim]")
            console.print(r.staging_after)
            continue
        url = publish_retirement(r)
        console.print(f"  [cyan]retirement PR[/cyan] {url}")
        if r.drop_tests:
            console.print(f"  [dim]removes {', '.join(p.name for p in r.drop_tests)}[/dim]")
        console.print("")
    return rc


def _quality_pass(conn, s, contracts, observed, dataset: str | None, dry_run: bool) -> int:
    """Content breaches: open an issue for each new one, close the ones that cleared.

    Both directions consult a fresh measurement, never the event's own status.
    """
    live = quality_mod.check_all(conn, [c for c in contracts if c.dataset in observed])
    live_keys = {(f.dataset_key, f.object_name) for f in live}

    # 1. breaches that cleared: close the issue, dismiss the event
    esc = query(conn, """
        SELECT EVENT_ID, DATASET_KEY, CHANGE_TYPE, OBJECT_NAME, RESOLUTION_REF
        FROM FIN_AIWH.META.DRIFT_EVENT
        WHERE STATUS = 'ESCALATED' AND CHANGE_TYPE IN (%s)
    """ % ",".join(f"'{q}'" for q in sorted(quality_mod.QUALITY_TYPES)))
    for e in esc:
        if dataset and e["DATASET_KEY"] != dataset.upper():
            continue
        if (e["DATASET_KEY"], e["OBJECT_NAME"]) in live_keys:
            continue
        console.print(f"[bold]{e['DATASET_KEY']}[/bold]  {e['CHANGE_TYPE']} on {e['OBJECT_NAME']} cleared")
        if dry_run:
            console.print(f"  → would close {e['RESOLUTION_REF']} and dismiss the event")
            continue
        if e["RESOLUTION_REF"]:
            close_quality_issue(e["RESOLUTION_REF"], f"{e['CHANGE_TYPE']} on {e['OBJECT_NAME']}")
        set_status(conn, [e["EVENT_ID"]], "DISMISSED")
        console.print(f"  [green]closed[/green] {e['RESOLUTION_REF']}")

    # 2. new breaches with an OPEN event: one issue per dataset
    opened = [e for e in open_events(conn) if e["CHANGE_TYPE"] in quality_mod.QUALITY_TYPES]
    if dataset:
        opened = [e for e in opened if e["DATASET_KEY"] == dataset.upper()]
    rc = 0
    for b in bundle_events(opened, contracts, observed, Lineage.load()):
        console.print(f"[bold]{b.dataset_key}[/bold]  data breaches contract  "
                      f"worst [{SEV_STYLE[b.worst]}]{b.worst}[/]  {len(b.events)} breach(es)")
        for e in b.events:
            console.print(f"  [dim]{e['CHANGE_TYPE']} {e['OBJECT_NAME']}: {e['RATIONALE']}[/dim]")
        if dry_run:
            console.print("  → would open an issue (never a PR)\n")
            continue
        url = escalate_quality(b)
        mark(conn, b.event_ids, "ESCALATED", url)
        console.print(f"  [red]issue[/red] {url}\n")
    return rc


def cmd_agent(args):
    s = load_settings()
    contracts = load_contracts()
    with connect(s) as conn:
        events = open_events(conn)
        observed = fetch_observed(conn, s.database, s.raw_schema)
        # Content breaches are handled on their own: no contract change fixes
        # bad data, so they never reach the drafting path below.
        events = [e for e in events if e["CHANGE_TYPE"] not in quality_mod.QUALITY_TYPES]
        try:
            quality_rc = _quality_pass(conn, s, contracts, observed, args.dataset, args.dry_run)
        except Exception as e:  # noqa: BLE001
            quality_rc = 0
            reason = " ".join(l.strip() for l in str(e).splitlines()[:3] if l.strip())
            console.print(f"[yellow]content pass skipped:[/yellow] {reason}")
    bundles = bundle_events(events, contracts, observed, Lineage.load())
    if args.dataset:
        bundles = [b for b in bundles if b.dataset_key == args.dataset.upper()]

    # Shields whose drift has been repaired come out first. This does not depend
    # on any open event, so it runs even when there is nothing else to do.
    active = _live_active(contracts, observed)
    retired_rc = _retire_stale(active, args.dataset, args.dry_run)

    if not bundles and args.dry_run:
        console.print("[green]no open drift, nothing to do[/green]")
        return retired_rc or quality_rc
    if bundles:
        console.print(f"{len(events)} open event(s) across {len(bundles)} dataset(s)\n")
    rc = 0

    # Breaking drift escalated on an earlier run has an issue but may have no
    # shield yet. Offer one now, once, without re-escalating.
    if not args.dry_run:
        with connect(s) as conn:
            prior = [e for e in breaking_events(conn) if e["STATUS"] == "ESCALATED"]
        if not bundles and not prior:
            console.print("[green]no open drift, nothing to do[/green]")
            return retired_rc or quality_rc
        open_keys = {b.dataset_key for b in bundles}
        for pb in bundle_events(prior, contracts, observed, Lineage.load()):
            if pb.dataset_key in open_keys:
                continue
            if args.dataset and pb.dataset_key != args.dataset.upper():
                continue
            issue = next((e["RESOLUTION_REF"] for e in pb.events if e.get("RESOLUTION_REF")), None)
            console.print(f"[bold]{pb.dataset_key}[/bold]  already escalated  {issue or ''}")
            _shield(pb, issue, s, active)
            console.print("")
    for b in bundles:
        di = b.dataset_impact()
        breaks = f"  [dim]touches {di.summary()}[/dim]" if di and not di.empty else ""
        console.print(f"[bold]{b.dataset_key}[/bold]  worst [{SEV_STYLE[b.worst]}]{b.worst}[/]  "
                      f"{len(b.events)} event(s){breaks}")

        if b.worst == BREAKING:
            if args.dry_run:
                console.print("  → would escalate (issue) and propose a shield PR\n")
                continue
            url = escalate(b)
            with connect(s) as conn:
                mark(conn, b.event_ids, "ESCALATED", url)
            console.print(f"  [red]escalated[/red] {url}")
            _shield(b, url, s, active)
            console.print("")
            continue

        p = verify(draft(b))
        if not p.ok:
            rc = 1
            console.print("  [red]proposal rejected by verification:[/red]")
            for e in p.errors:
                console.print(f"    {e}")
            console.print("")
            continue

        # No contract at all means the table is not modelled either. Onboarding
        # ships the contract together with everything needed to build on it.
        if b.contract is None:
            url = _onboard(b, p, args.dry_run)
            if url is None:
                rc = 1
                console.print("")
                continue
            if url is DRY:
                console.print("")
                continue
            with connect(s) as conn:
                mark(conn, b.event_ids, "PROPOSED", url)
            console.print(f"  [green]onboarding PR[/green] {url}")
            console.print("  [dim]review required, never auto merged[/dim]\n")
            continue

        console.print(f"  proposal: [bold]{p.pr_title}[/bold]  → contract v{p.contract.version}"
                      + ("  + staging model" if p.staging_sql else ""))
        console.print(f"  [dim]{p.reasoning}[/dim]")
        if args.dry_run:
            console.print("  [dim]dry run, printing contract:[/dim]")
            console.print(p.contract_yaml)
            if p.staging_sql:
                console.print(p.staging_sql)
            console.print("")
            continue

        url = publish(p, auto_merge=not args.no_merge)
        with connect(s) as conn:
            mark(conn, b.event_ids, "PROPOSED", url)
        style = "yellow" if "NOT enabled" in p.merge_note else "dim"
        console.print(f"  [green]PR[/green] {url}")
        console.print(f"  [{style}]{p.merge_note}[/]\n")
    return rc or retired_rc or quality_rc


def cmd_sync(args):
    """Bring event status back in line with what happened on GitHub."""
    with connect() as conn:
        pend = pending_events(conn)
    if not pend:
        console.print("[green]nothing proposed or escalated, nothing to reconcile[/green]")
        return 0

    console.print(f"checking {len(pend)} event(s) against GitHub\n")
    moves: dict[str, list[str]] = {}
    for e in pend:
        outcome = github_outcome(e["RESOLUTION_REF"])
        label = f"{e['DATASET_KEY']}.{e['OBJECT_NAME'] or '*'}"
        if not outcome:
            console.print(f"  [dim]{label:<34} {e['STATUS']} still[/dim]")
            continue
        new, why = outcome
        moves.setdefault(new, []).append(e["EVENT_ID"])
        console.print(f"  {label:<34} [green]{e['STATUS']} → {new}[/green]  [dim]{why}[/dim]")

    if not moves:
        console.print("\n[dim]nothing to change[/dim]")
        return 0
    if args.dry_run:
        console.print("\n[dim]dry run, nothing written[/dim]")
        return 0

    with connect() as conn:
        for status, ids in moves.items():
            set_status(conn, ids, status)
    total = sum(len(v) for v in moves.values())
    console.print(f"\n[green]{total} event(s) updated[/green]")
    console.print("[dim]a MERGED contract is not in force until you run: register[/dim]")
    return 0


def cmd_status(_args):
    with connect() as conn:
        contracts = query(
            conn,
            "SELECT CONTRACT_KEY, VERSION FROM FIN_AIWH.META.ACTIVE_CONTRACT ORDER BY CONTRACT_KEY",
        )
        events = query(
            conn,
            """
            SELECT DATASET_KEY, OBJECT_NAME, CHANGE_TYPE, SEVERITY, STATUS,
                   RESOLUTION_REF, IMPACT:marts AS MARTS, IMPACT:reports AS REPORTS
            FROM FIN_AIWH.META.DRIFT_EVENT
            WHERE STATUS IN ('OPEN', 'PROPOSED', 'ESCALATED')
            ORDER BY CASE STATUS WHEN 'OPEN' THEN 0 WHEN 'ESCALATED' THEN 1 ELSE 2 END,
                     CASE SEVERITY WHEN 'BREAKING' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                     DATASET_KEY, OBJECT_NAME
            """,
        )

    console.print(f"[bold]{len(contracts)} active contracts[/bold]")
    for c in contracts:
        console.print(f"  v{c['VERSION']}  {c['CONTRACT_KEY']}")

    def _json(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    def line(r):
        reports = [x["label"] for x in (_json(r.get("REPORTS")) or [])]
        marts = _json(r.get("MARTS")) or []
        hit = reports or marts
        breaks = f"  [dim]breaks {', '.join(hit)}[/dim]" if hit else ""
        ref = f"  [dim]{r['RESOLUTION_REF']}[/dim]" if r.get("RESOLUTION_REF") else ""
        return (f"  [{SEV_STYLE[r['SEVERITY']]}]{r['SEVERITY']:<8}[/] "
                f"{r['DATASET_KEY']}.{r['OBJECT_NAME'] or '*'}  {r['CHANGE_TYPE']}{breaks}{ref}")

    groups = [
        ("OPEN", "open, waiting for triage"),
        ("ESCALATED", "escalated, issue open"),
        ("PROPOSED", "proposed, pull request open"),
    ]
    for status, label in groups:
        rows = [r for r in events if r["STATUS"] == status]
        console.print(f"\n[bold]{len(rows)} {label}[/bold]")
        for r in rows:
            console.print(line(r))
    if not events:
        console.print("\n[green]nothing in flight. the warehouse matches every registered contract.[/green]")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="fin-aiwh", description="fin_aiwh control plane")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="verify the Snowflake connection").set_defaults(fn=cmd_ping)

    a = sub.add_parser("apply", help="run a SQL file from the repo")
    a.add_argument("path")
    a.set_defaults(fn=cmd_apply)

    l = sub.add_parser("load", help="load seed CSVs into RAW")
    l.add_argument("tables", nargs="*", help="limit to these tables")
    l.set_defaults(fn=cmd_load)

    sub.add_parser("register", help="publish contracts to the control plane").set_defaults(
        fn=cmd_register
    )

    d = sub.add_parser("detect", help="compare the warehouse against registered contracts")
    d.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    d.add_argument("--fail-on-breaking", action="store_true", help="exit 2 on breaking drift")
    d.add_argument("--dataset", action="append",
                   help="limit to these datasets, e.g. RAW.AP_INVOICE (repeatable)")
    d.add_argument("--no-quality", action="store_true",
                   help="schema only, skip the content checks (the gate uses this)")
    d.set_defaults(fn=cmd_detect)

    sub.add_parser("status", help="contracts and open drift").set_defaults(fn=cmd_status)

    g = sub.add_parser("agent", help="turn open drift into PRs or escalations")
    g.add_argument("--dry-run", action="store_true", help="draft and verify, touch nothing")
    g.add_argument("--no-merge", action="store_true", help="never enable auto merge, even for LOW")
    g.add_argument("--dataset", help="only this dataset, e.g. RAW.AP_INVOICE")
    g.set_defaults(fn=cmd_agent)

    y = sub.add_parser("sync", help="reconcile event status with GitHub")
    y.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    y.set_defaults(fn=cmd_sync)

    r = sub.add_parser("resolve", help="close open drift events")
    r.add_argument("event", nargs="*", help="event ids to close")
    r.add_argument("--all", action="store_true", help="close every OPEN event")
    r.add_argument("--status", default="DISMISSED", choices=["DISMISSED", "MERGED", "PROPOSED", "ESCALATED"])
    r.add_argument("--ref", default=None, help="PR url or ticket that resolved it")
    r.set_defaults(fn=cmd_resolve)

    b = sub.add_parser("dbt", help="run dbt with the control plane's connection settings")
    b.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed to dbt")
    b.set_defaults(fn=cmd_dbt)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
```
**`control/ui.py`**

```python
"""One page control console for the fin_aiwh PoC.

Runs on the machine that owns the repo and the Snowflake connection. Fires
drift scenarios, runs the detector and the agent, and shows the control plane
state as it changes.

Every action is the same CLI command a person would type. The page runs them
as subprocesses and streams the output, so what you see here is exactly what
you would see in a terminal. No second implementation to drift out of sync.

    python -m control.ui          then open http://127.0.0.1:8765
"""
import json
import queue
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import ROOT, load_settings
from .contracts import load_contracts
from .snow import connect, query

STATIC = Path(__file__).parent / "static"
PORT = 8765

# Only these may be run from the page. The scenario argument is checked against
# the files that actually exist, so the page cannot ask for an arbitrary path.
ALLOWED = {
    "detect":       ["detect"],
    "detect-strict": ["detect", "--fail-on-breaking"],
    "register":     ["register"],
    "load":         ["load"],
    "dbt-build":    ["dbt", "build"],
    "dbt-parse":    ["dbt", "parse"],
    "agent-dry":    ["agent", "--dry-run"],
    "agent":        ["agent"],
    "sync":         ["sync"],
    "resolve-all":  ["resolve", "--all"],
}


def scenarios() -> list[dict]:
    out = []
    for p in sorted((ROOT / "ops" / "scenarios").glob("*.sql")):
        first = ""
        for line in p.read_text().splitlines():
            if line.startswith("-- Scenario:"):
                first = line.removeprefix("-- Scenario:").strip()
                break
            if line.startswith("-- Undo"):
                first = "Rebuild RAW to the version 1 shape."
                break
        out.append({"file": p.name, "key": p.stem, "title": first or p.stem})
    return out


SCENARIO_FILES = {s["key"]: s["file"] for s in scenarios()}


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def start_job(args: list[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {"lines": [], "done": False, "rc": None, "cmd": " ".join(args)}

    def run():
        cmd = [sys.executable, "-u", "-m", "control.cli", *args]
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**__import__("os").environ, "COLUMNS": "150"},
        )
        for line in proc.stdout:
            with LOCK:
                JOBS[job_id]["lines"].append(line.rstrip("\n"))
        proc.wait()
        with LOCK:
            JOBS[job_id]["done"] = True
            JOBS[job_id]["rc"] = proc.returncode

    threading.Thread(target=run, daemon=True).start()
    return job_id


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def read_state() -> dict:
    s = load_settings()
    with connect(s) as conn:
        events = query(
            conn,
            """
            SELECT EVENT_ID, DATASET_KEY, CHANGE_TYPE, SEVERITY, OBJECT_NAME,
                   RATIONALE, STATUS, RESOLUTION_REF, IMPACT,
                   TO_VARCHAR(DETECTED_AT, 'YYYY-MM-DD HH24:MI') AS DETECTED
            FROM FIN_AIWH.META.DRIFT_EVENT
            ORDER BY CASE STATUS WHEN 'OPEN' THEN 0 ELSE 1 END,
                     CASE SEVERITY WHEN 'BREAKING' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                     DETECTED_AT DESC
            LIMIT 60
            """,
        )
        contracts = query(
            conn,
            "SELECT CONTRACT_KEY, VERSION, OWNER, "
            "TO_VARCHAR(REGISTERED_AT, 'YYYY-MM-DD HH24:MI') AS REGISTERED "
            "FROM FIN_AIWH.META.ACTIVE_CONTRACT ORDER BY CONTRACT_KEY",
        )
        runs = query(
            conn,
            "SELECT RUN_ID, RUN_TYPE, DATASETS_SCANNED, EVENTS_RAISED, STATUS, "
            "TO_VARCHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STARTED "
            "FROM FIN_AIWH.META.RUN_LOG ORDER BY STARTED_AT DESC LIMIT 8",
        )
    return {
        "events": events,
        "contracts": contracts,
        "runs": runs,
        "files": [
            {"dataset": c.dataset, "version": c.version, "path": str(c.source_path.name)}
            for c in load_contracts()
        ],
    }


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "console.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/meta":
            self._json({"scenarios": scenarios(), "actions": sorted(ALLOWED)})
        elif path == "/api/state":
            try:
                self._json(read_state())
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
        elif path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            self._json(job or {"error": "no such job"}, 200 if job else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/run/"):
            key = path.rsplit("/", 1)[-1]
            if key not in ALLOWED:
                return self._json({"error": f"action not allowed: {key}"}, 400)
            return self._json({"job": start_job(ALLOWED[key])})
        if path.startswith("/api/scenario/"):
            key = path.rsplit("/", 1)[-1]
            fname = SCENARIO_FILES.get(key)
            if not fname:
                return self._json({"error": f"no such scenario: {key}"}, 400)
            return self._json({"job": start_job(["apply", f"ops/scenarios/{fname}"])})
        self._json({"error": "not found"}, 404)


def main():
    load_settings()  # fail fast on a bad .env rather than in the browser
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"fin_aiwh console on http://127.0.0.1:{PORT}   (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
```
