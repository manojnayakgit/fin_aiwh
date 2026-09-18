"""Load generated seed CSVs into RAW via an internal stage."""
from pathlib import Path

from .config import ROOT
from .snow import execute, query

SEED_OUT = ROOT / "seeds" / "out"
STAGE = "FIN_AIWH.RAW.SEED_STAGE"

FILE_FORMAT = (
    "TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
    "NULL_IF = ('') EMPTY_FIELD_AS_NULL = TRUE TRIM_SPACE = FALSE"
)


def load_all(conn, tables: list[str] | None = None) -> list[tuple[str, int]]:
    execute(conn, f"CREATE STAGE IF NOT EXISTS {STAGE} FILE_FORMAT = ({FILE_FORMAT})")
    files = sorted(SEED_OUT.glob("*.csv"))
    if not files:
        raise SystemExit("no CSVs in seeds/out/. Run: python seeds/generate.py")

    results = []
    for f in files:
        table = f.stem.upper()
        if tables and table not in tables:
            continue
        execute(
            conn,
            f"PUT 'file://{f.as_posix()}' @{STAGE} OVERWRITE = TRUE AUTO_COMPRESS = TRUE",
        )
        execute(conn, f"TRUNCATE TABLE IF EXISTS FIN_AIWH.RAW.{table}")
        execute(
            conn,
            f"COPY INTO FIN_AIWH.RAW.{table} FROM @{STAGE}/{f.name}.gz "
            f"FILE_FORMAT = ({FILE_FORMAT}) ON_ERROR = 'ABORT_STATEMENT'",
        )
        # The seed carries a fixed LOADED_AT. A load time that never moves would
        # make every table read as stale a day after loading, so it is stamped
        # with the actual load time. The seed value is still what the CSV says.
        cols = {r["COLUMN_NAME"] for r in query(
            conn, "SELECT COLUMN_NAME FROM FIN_AIWH.INFORMATION_SCHEMA.COLUMNS "
                  "WHERE TABLE_SCHEMA = 'RAW' AND TABLE_NAME = %(t)s", {"t": table})}
        if "LOADED_AT" in cols:
            execute(conn, f"UPDATE FIN_AIWH.RAW.{table} SET LOADED_AT = SYSDATE()::TIMESTAMP_NTZ")
        n = query(conn, f"SELECT COUNT(*) AS C FROM FIN_AIWH.RAW.{table}")[0]["C"]
        results.append((table, n))
    return results
