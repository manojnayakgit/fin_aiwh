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
    "agent-dry":    ["agent", "--dry-run"],
    "agent":        ["agent"],
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
                   RATIONALE, STATUS, RESOLUTION_REF,
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
