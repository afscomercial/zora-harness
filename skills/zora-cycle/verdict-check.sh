#!/usr/bin/env bash
# verdict-check — is there a PASS on disk for the code that is checked out right now?
#
#   verdict-check.sh <run-dir> [repo-dir]      (repo-dir defaults to the current directory)
#
# Reads <run-dir>/verdict.json, written by the zora-validator agent, and checks
# that the verdict actually holds for the working tree in front of it:
#
#   - the verdict is for the current HEAD, and nothing uncommitted has changed since
#   - rungs 1 and 2 (static gates, tests) ran; a skipped rung 3-5 gives its reason
#   - every rung that ran points at evidence files that exist and are not empty
#   - a PASS carries no finding of severity "defect"
#
# Exit 0  a PASS that holds for this exact tree
# Exit 1  not a pass: a FAIL or INCOMPLETE verdict, or a PASS that does not hold up
# Exit 2  usage error, no verdict file, or a verdict file that is not valid
set -euo pipefail

run_dir="${1:-}"
repo_dir="${2:-$PWD}"
if [ -z "$run_dir" ]; then
  echo "usage: verdict-check.sh <run-dir> [repo-dir]" >&2
  exit 2
fi
if [ ! -f "$run_dir/verdict.json" ]; then
  echo "NO VERDICT  $run_dir/verdict.json does not exist" >&2
  exit 2
fi
if ! head_sha="$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null)"; then
  echo "NOT A REPO  $repo_dir is not a git checkout" >&2
  exit 2
fi
if [ -n "$(git -C "$repo_dir" status --porcelain)" ]; then dirty=1; else dirty=0; fi

python3 - "$run_dir" "$head_sha" "$dirty" <<'PY'
import json, os, sys

run_dir, head, dirty = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
path = os.path.join(run_dir, "verdict.json")
try:
    with open(path) as f:
        v = json.load(f)
except (OSError, ValueError) as e:
    print(f"INVALID     {path}: {e}")
    sys.exit(2)
if not isinstance(v, dict):
    print(f"INVALID     {path}: top level is not an object")
    sys.exit(2)

verdict = v.get("verdict")
if verdict not in ("PASS", "FAIL", "INCOMPLETE"):
    print(f"INVALID     verdict is {verdict!r}; expected PASS, FAIL or INCOMPLETE")
    sys.exit(2)

problems = []

commit = str(v.get("commit") or "")
if commit != head:
    problems.append(f"stale: the verdict is for {commit[:12] or '(no commit)'}, HEAD is {head[:12]}; the code changed after validation")
if dirty:
    problems.append("the working tree has uncommitted changes the verdict never saw")

rungs = {}
for r in v.get("rungs") or []:
    if isinstance(r, dict) and isinstance(r.get("rung"), int):
        rungs[r["rung"]] = r

for n in (1, 2, 3, 4, 5):
    r = rungs.get(n)
    if r is None:
        problems.append(f"rung {n}: missing from the verdict")
        continue
    label = f"rung {n} ({r.get('name') or '?'})"
    status = r.get("status")
    if status not in ("pass", "fail", "not-run", "skipped"):
        problems.append(f"{label}: status is {status!r}; expected pass, fail, not-run or skipped")
        continue
    if status == "skipped":
        if n in (1, 2):
            problems.append(f"{label}: can never be skipped")
        elif not str(r.get("reason") or "").strip():
            problems.append(f"{label}: skipped with no reason")
        continue
    if status == "not-run":
        problems.append(f"{label}: never ran")
        continue
    if status == "fail":
        problems.append(f"{label}: failed")
    evidence = r.get("evidence") or []
    if not evidence:
        problems.append(f"{label}: ran but lists no evidence")
    for e in evidence:
        p = e if os.path.isabs(e) else os.path.join(run_dir, e)
        if not os.path.isfile(p):
            problems.append(f"{label}: evidence file missing: {e}")
        elif os.path.getsize(p) == 0:
            problems.append(f"{label}: evidence file is empty: {e}")

defects = [f for f in (v.get("findings") or []) if isinstance(f, dict) and f.get("severity") == "defect"]
if verdict == "PASS" and defects:
    problems.append(f"a PASS cannot carry defects, and this one carries {len(defects)}")

for p in problems:
    print(f"  - {p}")
if verdict == "PASS" and not problems:
    print(f"PASS        holds for {head[:12]}, clean tree, every rung backed by evidence")
    sys.exit(0)
if verdict == "PASS":
    print(f"NOT A PASS  the verdict file says PASS, but {len(problems)} check(s) above do not hold")
else:
    print(f"NOT A PASS  the validator's verdict is {verdict}")
sys.exit(1)
PY
