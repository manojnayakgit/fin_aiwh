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
    BREAKING, MEDIUM, diff_all, fetch_observed, new_run_id, persist, snapshot_observed,
)
from .agent import BREAKING as _B, bundle_events, draft, escalate, mark, open_events, publish, verify
from .load import load_all
from .register import register
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
    for table, n in results:
        console.print(f"  [green]loaded[/green] {table:<18} {n:>8,} rows")
    return 0


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

    with connect(s) as conn:
        observed = fetch_observed(conn, s.database, s.raw_schema)
        findings = diff_all(contracts, observed)
        if not args.dry_run:
            snapshot_observed(conn, run_id, observed)
            written = persist(conn, run_id, findings, contracts)
            _log_run(conn, run_id, "DETECT", started, len(observed), written, "SUCCESS")
        else:
            written = 0

    console.print(
        f"run [bold]{run_id}[/bold]  scanned {len(observed)} datasets  "
        f"found {len(findings)} divergences"
        + ("  [dim](dry run, nothing written)[/dim]" if args.dry_run else f"  new {written}")
    )

    if not findings:
        console.print("[green]warehouse matches every registered contract[/green]")
        return 0

    table = Table(show_lines=False, header_style="bold")
    for col in ("severity", "dataset", "change", "object", "why"):
        table.add_column(col, overflow="fold")
    for f in findings:
        table.add_row(
            f"[{SEV_STYLE[f.severity]}]{f.severity}[/]",
            f.dataset_key, f.change_type, f.object_name or "-", f.rationale,
        )
    console.print(table)

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


def cmd_agent(args):
    s = load_settings()
    contracts = load_contracts()
    with connect(s) as conn:
        events = open_events(conn)
        observed = fetch_observed(conn, s.database, s.raw_schema)
    bundles = bundle_events(events, contracts, observed)
    if args.dataset:
        bundles = [b for b in bundles if b.dataset_key == args.dataset.upper()]
    if not bundles:
        console.print("[green]no open drift, nothing to do[/green]")
        return 0

    console.print(f"{len(events)} open event(s) across {len(bundles)} dataset(s)\n")
    rc = 0
    for b in bundles:
        console.print(f"[bold]{b.dataset_key}[/bold]  worst [{SEV_STYLE[b.worst]}]{b.worst}[/]  "
                      f"{len(b.events)} event(s)")

        if b.worst == BREAKING:
            if args.dry_run:
                console.print("  → would escalate (issue), no PR\n")
                continue
            url = escalate(b)
            with connect(s) as conn:
                mark(conn, b.event_ids, "ESCALATED", url)
            console.print(f"  [red]escalated[/red] {url}\n")
            continue

        p = verify(draft(b))
        if not p.ok:
            rc = 1
            console.print("  [red]proposal rejected by verification:[/red]")
            for e in p.errors:
                console.print(f"    {e}")
            console.print("")
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
        merged = "auto merge on" if (b.worst == "LOW" and not args.no_merge) else "awaiting review"
        console.print(f"  [green]PR[/green] {url}  ({merged})\n")
    return rc


def cmd_status(_args):
    with connect() as conn:
        rows = query(conn, "SELECT * FROM META.OPEN_DRIFT")
        contracts = query(
            conn,
            "SELECT CONTRACT_KEY, VERSION, REGISTERED_AT FROM META.ACTIVE_CONTRACT "
            "ORDER BY CONTRACT_KEY",
        )
    console.print(f"[bold]{len(contracts)} active contracts[/bold]")
    for c in contracts:
        console.print(f"  v{c['VERSION']}  {c['CONTRACT_KEY']}")
    console.print(f"\n[bold]{len(rows)} open drift events[/bold]")
    for r in rows:
        console.print(
            f"  [{SEV_STYLE[r['SEVERITY']]}]{r['SEVERITY']:<8}[/] "
            f"{r['DATASET_KEY']}.{r['OBJECT_NAME'] or '*'}  {r['CHANGE_TYPE']}"
        )
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
    d.set_defaults(fn=cmd_detect)

    sub.add_parser("status", help="contracts and open drift").set_defaults(fn=cmd_status)

    g = sub.add_parser("agent", help="turn open drift into PRs or escalations")
    g.add_argument("--dry-run", action="store_true", help="draft and verify, touch nothing")
    g.add_argument("--no-merge", action="store_true", help="never enable auto merge, even for LOW")
    g.add_argument("--dataset", help="only this dataset, e.g. RAW.AP_INVOICE")
    g.set_defaults(fn=cmd_agent)

    r = sub.add_parser("resolve", help="close open drift events")
    r.add_argument("event", nargs="*", help="event ids to close")
    r.add_argument("--all", action="store_true", help="close every OPEN event")
    r.add_argument("--status", default="DISMISSED", choices=["DISMISSED", "MERGED"])
    r.add_argument("--ref", default=None, help="PR url or ticket that resolved it")
    r.set_defaults(fn=cmd_resolve)

    b = sub.add_parser("dbt", help="run dbt with the control plane's connection settings")
    b.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed to dbt")
    b.set_defaults(fn=cmd_dbt)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
