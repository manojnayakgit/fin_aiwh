from lib import *

S = []
S.append(r"""
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

""")
S.append(file("control/onboard.py"))
S.append(defs("control/agent.py", ["ONBOARD_TOOL", "ONBOARD_TOOL_SCHEMA", "ONBOARD_SYSTEM", "build_onboard_prompt", "draft_staging", "publish_onboarding"]))
S.append(defs("control/cli.py", ["DRY", "_onboard"]))
S.append(sh("""
gh pr close 1
python -m control.cli sync
python -m control.cli agent --dataset RAW.AP_ACCRUAL --dry-run
python -m control.cli agent --dataset RAW.AP_ACCRUAL
""", "Commands"))
S.append(r"""
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

""")
S.append(defs("control/agent.py", ["_run", "_start_branch", "_push", "_ensure_pushed"]))
S.append(r"""
**Then the gate failed PR #7 on a test.** `test_all_contracts_parse` asserted
`len(contracts) == 8`. The PR added a ninth. A test that fails whenever the
product succeeds is measuring the wrong property. It now derives the count
from the contract directory. The same fix exposed an owner check comparing
against the literal `"unassigned"` while the agent writes
`"unassigned-needs-review"`.

""")
S.append(file("tests/test_contracts.py"))
S.append(sh("""
git checkout onboard/ap_accrual
git merge origin/main -m "Merge main for the contract test fix"
git push
git checkout main
""", "Commands: bring the PR branch up to date"))
S.append(r"""
**Merged:** PR #7 `Onboard RAW.AP_ACCRUAL: contract, source, staging model and
tests` as `99760b0`. All four artifacts on `main`.

""")
S.append(file("contracts/raw/ap_accrual.yml", rev="99760b0", title="`contracts/raw/ap_accrual.yml` as merged in PR #7 (agent-drafted)"))
S.append(file("dbt/models/staging/stg_ap_accrual.sql", rev="99760b0", title="`dbt/models/staging/stg_ap_accrual.sql` as merged in PR #7 (agent-drafted)"))
S.append(sh("""
git pull
python -m control.cli register
python -m control.cli sync
python -m control.cli detect
python -m control.cli dbt build
""", "Commands: after the merge"))
S.append(r"""
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

""")
S.append(sh("""
git bundle create .git/backup-preRewrite.bundle --all
git config user.name  manojnayakgit
git config user.email 39649907+manojnayakgit@users.noreply.github.com

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f \\
  --env-filter '
    export GIT_AUTHOR_NAME=manojnayakgit
    export GIT_AUTHOR_EMAIL=39649907+manojnayakgit@users.noreply.github.com
    export GIT_COMMITTER_NAME=manojnayakgit
    export GIT_COMMITTER_EMAIL=39649907+manojnayakgit@users.noreply.github.com
  ' \\
  --msg-filter 'python3 .git/msgfilter.py' \\
  -- main drift/ap_accrual-v1 onboard/ap_accrual shield/ap_payment shield/ar_invoice

git push --force origin main drift/ap_accrual-v1 onboard/ap_accrual shield/ap_payment shield/ar_invoice
gh auth refresh -s workflow
""", "Commands"))
S.append(r"""
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

""")
S.append(file("ops/scenarios/08_upstream_fixed.sql"))
S.append(defs("control/shield.py", ["_NULL_SHIELD", "_CAST_SHIELD", "_ISSUE", "Retirement", "retire_line", "plan_retirement", "retire_title", "retire_body"]))
S.append(defs("control/agent.py", ["publish_retirement"]))
S.append(defs("control/cli.py", ["_retire_stale"]))
S.append(sh("""
python -m control.cli apply ops/scenarios/08_upstream_fixed.sql
python -m control.cli detect
python -m control.cli agent --dry-run
python -m control.cli agent
""", "Commands"))
S.append(r"""
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

""")
S.append(sh("""
git pull
python -m control.cli sync
python -m control.cli dbt build
python -m control.cli agent
""", "Commands: close the loop"))
S.append(r"""
`sync` dismissed both events, `dbt build` passed with one fewer test, and the
final `agent` run, the rerun that used to crash, reported nothing to do.

**Eight scenarios, every path proven live**, no manual step other than
clicking Merge on a pull request the system wrote. 120 tests.

---
""")
