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
