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
