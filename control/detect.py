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


def active_fingerprints(conn) -> set[str]:
    rows = query(
        conn,
        "SELECT EVENT_ID FROM FIN_AIWH.META.DRIFT_EVENT WHERE STATUS IN (%s)"
        % ",".join(f"'{s}'" for s in ACTIVE_STATUSES),
    )
    return {r["EVENT_ID"] for r in rows}


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
            suppressed += 1
            continue
        execute(
            conn,
            """
            INSERT INTO META.DRIFT_EVENT
              (EVENT_ID, RUN_ID, DATASET_KEY, CONTRACT_VERSION, CHANGE_TYPE,
               SEVERITY, OBJECT_NAME, BEFORE_STATE, AFTER_STATE, RATIONALE, STATUS,
               IMPACT)
            SELECT %(event_id)s, %(run_id)s, %(dataset)s, %(version)s, %(change_type)s,
                   %(severity)s, %(object_name)s,
                   TRY_PARSE_JSON(%(before)s), TRY_PARSE_JSON(%(after)s),
                   %(rationale)s, 'OPEN', TRY_PARSE_JSON(%(impact)s)
            """,
            {
                "event_id": fp,
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
