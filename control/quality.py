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


# System DMFs refuse some types. NULL_COUNT on a BOOLEAN is the one that bites
# here (AP_VENDOR.IS_ACTIVE), and it refuses unbounded VARCHAR too, so the cast
# is to NUMBER(1,0), which the probe proved. TRUE, FALSE and NULL map to 1, 0
# and NULL: nothing about nullness changes. Attaching cannot cast, so such
# checks are measured but not attached.
CAST_FOR_DMF = {"BOOLEAN": "NUMBER(1,0)"}


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
    casts: tuple[str, ...] = ()   # per column: "" or a type to cast to first

    @property
    def attachable(self) -> bool:
        return not any(self.casts)

    @property
    def table(self) -> str:
        return self.dataset_key.split(".")[-1]

    @property
    def object_name(self) -> str:
        return ",".join(self.columns) if self.columns else "*"

    @property
    def label(self) -> str:
        return f"{self.dmf.rsplit('.', 1)[-1]}({self.object_name})"

    def sql(self) -> str:
        casts = self.casts or ("",) * len(self.columns)
        exprs = [f"CAST({c} AS {k})" if k else c for c, k in zip(self.columns, casts)]
        cols = ", ".join(exprs) if exprs else "*"
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
            dmf="SNOWFLAKE.CORE.DUPLICATE_COUNT",
            columns=tuple(contract.primary_key), min=None, max=0, severity=BREAKING,
            why=f"primary_key is {contract.primary_key}; a duplicate makes every join fan out",
        ))

    for c in contract.columns:
        if not c.nullable:
            out.append(Check(
                dataset_key=key, change_type="NULL_IN_REQUIRED",
                dmf="SNOWFLAKE.CORE.NULL_COUNT",
                columns=(c.name,), min=None, max=0, severity=BREAKING,
                why=f"{c.name} is contracted NOT NULL",
                casts=(CAST_FOR_DMF.get(c.type.upper(), ""),),
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
