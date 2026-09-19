"""The console: the page renders, the action allowlist holds, jobs stream.

No Snowflake and no model. `read_state` is faked, and the job under test is a
fast CLI call, because what is being tested is the job machinery rather than
any particular command.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from control import ui

FAKE = {
    "events": [{
        "EVENT_ID": "e1", "DATASET_KEY": "RAW.AP_INVOICE", "CHANGE_TYPE": "TYPE_CHANGED",
        "SEVERITY": "BREAKING", "OBJECT_NAME": "GROSS_AMOUNT",
        "RATIONALE": "scale changed", "STATUS": "OPEN", "RESOLUTION_REF": None,
        "DETECTED": "2026-09-18 10:00",
    }],
    "contracts": [{"CONTRACT_KEY": "RAW.AP_INVOICE", "VERSION": 2,
                   "OWNER": "finance-data-engineering", "REGISTERED": "2026-09-18 09:00"}],
    "runs": [{"RUN_ID": "r1", "RUN_TYPE": "DETECT", "DATASETS_SCANNED": 8,
              "EVENTS_RAISED": 1, "STATUS": "SUCCESS", "STARTED": "2026-09-18 10:00:00"}],
    "files": [],
}

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def get(path):
    return urllib.request.urlopen(BASE + path).read().decode()


def post(path):
    return urllib.request.urlopen(urllib.request.Request(BASE + path, method="POST"))


@pytest.fixture(scope="module")
def server():
    with patch.object(ui, "read_state", lambda: FAKE):
        srv = ui.ThreadingHTTPServer(("127.0.0.1", PORT), ui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        yield srv
        srv.shutdown()


def test_page_renders(server):
    assert "<title>fin_aiwh console</title>" in get("/")


def test_every_scenario_is_offered(server):
    """Every script in ops/scenarios is a button. No hard count: adding a
    scenario is routine and must not need a test edit."""
    from control.config import ROOT
    on_disk = {p.stem for p in (ROOT / "ops" / "scenarios").glob("*.sql")}
    meta = json.loads(get("/api/meta"))
    assert {s["key"] for s in meta["scenarios"]} == on_disk
    assert {"04_money_scale_changed", "08_upstream_fixed", "99_reset"} <= on_disk
    assert all(s["title"] for s in meta["scenarios"]), "a scenario has no description"


def test_state_is_served(server):
    state = json.loads(get("/api/state"))
    assert state["events"][0]["SEVERITY"] == "BREAKING"
    assert state["contracts"][0]["VERSION"] == 2


def test_an_action_outside_the_allowlist_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/run/rm-rf")
    assert e.value.code == 400
    assert "not allowed" in e.value.read().decode()


def test_a_scenario_path_cannot_escape_the_scenario_folder(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post("/api/scenario/..%2F..%2Fetc%2Fpasswd")
    assert e.value.code in (400, 404)


def test_a_job_streams_and_reports_its_exit_code(server):
    ui.ALLOWED["smoke"] = ["--help"]
    try:
        job = json.loads(post("/api/run/smoke").read())["job"]
        for _ in range(60):
            j = json.loads(get(f"/api/job/{job}"))
            if j["done"]:
                break
            time.sleep(0.25)
        assert j["done"], "job never finished"
        assert j["rc"] == 0
        assert any("usage: fin-aiwh" in line for line in j["lines"])
    finally:
        ui.ALLOWED.pop("smoke", None)


def test_every_cli_command_resolves_its_module_references():
    """A subcommand that references a module the file never imported is a
    NameError at runtime and invisible to every other test. Check every name
    a cmd_* or helper function loads against what the module binds."""
    import ast, builtins
    import control.cli as cli
    tree = ast.parse(open(cli.__file__).read())
    bound = set(dir(cli)) | set(dir(builtins))
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        local = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                local.add(n.id)
            elif isinstance(n, ast.arg):
                local.add(n.arg)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.add(n.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                local.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0] for a in n.names}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                assert node.id in bound or node.id in local, f"{fn.name} uses undefined name {node.id!r}"
