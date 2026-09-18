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
from .agent import (BREAKING as _B, breaking_events, bundle_events, draft, draft_staging,
                    escalate, github_outcome, mark, open_events, pending_events, publish,
                    publish_onboarding, publish_retirement, publish_shield, set_status,
                    verify)
from .lineage import Lineage
from .load import load_all
from .register import register
from . import onboard as onboard_mod
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

    active = {(f.dataset_key.split(".")[-1], (f.object_name or "").upper()) for f in findings}
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


def _shield(b, issue_url, settings):
    """Try to keep the reports correct while upstream is fixed."""
    breaking = [e for e in b.events if e["SEVERITY"] == "BREAKING"]
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


def _retire_stale(contracts, observed, dataset: str | None, dry_run: bool) -> int:
    """Open a retirement PR for every shield whose drift is gone.

    Judged against the live schema, not the event table. An escalated event can
    outlive the divergence it recorded; the shield's own column is the truth.
    """
    findings = diff_all(contracts, observed)
    active = {(f.dataset_key.split(".")[-1], (f.object_name or "").upper()) for f in findings}
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


def cmd_agent(args):
    s = load_settings()
    contracts = load_contracts()
    with connect(s) as conn:
        events = open_events(conn)
        observed = fetch_observed(conn, s.database, s.raw_schema)
    bundles = bundle_events(events, contracts, observed, Lineage.load())
    if args.dataset:
        bundles = [b for b in bundles if b.dataset_key == args.dataset.upper()]

    # Shields whose drift has been repaired come out first. This does not depend
    # on any open event, so it runs even when there is nothing else to do.
    retired_rc = _retire_stale(contracts, observed, args.dataset, args.dry_run)

    if not bundles and args.dry_run:
        console.print("[green]no open drift, nothing to do[/green]")
        return retired_rc
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
            return retired_rc
        open_keys = {b.dataset_key for b in bundles}
        for pb in bundle_events(prior, contracts, observed, Lineage.load()):
            if pb.dataset_key in open_keys:
                continue
            if args.dataset and pb.dataset_key != args.dataset.upper():
                continue
            issue = next((e["RESOLUTION_REF"] for e in pb.events if e.get("RESOLUTION_REF")), None)
            console.print(f"[bold]{pb.dataset_key}[/bold]  already escalated  {issue or ''}")
            _shield(pb, issue, s)
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
            _shield(b, url, s)
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
    return rc or retired_rc


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
