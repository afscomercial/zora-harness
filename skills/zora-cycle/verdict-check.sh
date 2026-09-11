#!/usr/bin/env bash
# verdict-check — is there a PASS on disk for the code that is checked out right now?
#
#   verdict-check.sh <run-dir> [repo-dir] [--remote]      (repo-dir defaults to the current directory)
#
# <run-dir> holds verdict.json. For a remote QA job it is the job folder
# ($RUN/qa/<job-id>), which also holds remote-manifest.json and dispatch.json.
#
# Always checks that the verdict holds for the working tree in front of it:
#   - the verdict is for the current HEAD, and nothing uncommitted has changed since
#   - rungs 1 and 2 (static gates, tests) ran; a skipped rung 3-5 gives its reason
#   - every rung that ran points at evidence files that exist and are not empty
#   - a PASS carries no finding of severity "defect"
# For a remote QA job (--remote, or whenever remote-manifest.json is present) also:
#   - the VM validated exactly the base and commit the dispatcher sent
#   - the VM's checkout was clean before Codex ran, and Codex left tracked files alone
#   - the QA environment came up, and Codex exited cleanly
#   - every downloaded file matches the checksum the VM recorded
#
# Exit 0  a PASS that holds for this exact tree
# Exit 1  not a pass: FAIL, INCOMPLETE (a broken QA environment included), or a PASS that does not hold up
# Exit 2  usage error, or nothing valid to check
set -euo pipefail

usage() { echo "usage: verdict-check.sh <run-dir> [repo-dir] [--remote]" >&2; exit 2; }
run_dir=""; repo_dir=""; remote=0
for a in "$@"; do
  case "$a" in
    --remote) remote=1 ;;
    -*) usage ;;
    *) if [ -z "$run_dir" ]; then run_dir="$a"; elif [ -z "$repo_dir" ]; then repo_dir="$a"; else usage; fi ;;
  esac
done
[ -n "$run_dir" ] || usage
repo_dir="${repo_dir:-$PWD}"
if [ ! -d "$run_dir" ]; then
  echo "NO RUN DIR  $run_dir does not exist" >&2
  exit 2
fi
if ! head_sha="$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null)"; then
  echo "NOT A REPO  $repo_dir is not a git checkout" >&2
  exit 2
fi
if [ -n "$(git -C "$repo_dir" status --porcelain)" ]; then dirty=1; else dirty=0; fi

python3 - "$run_dir" "$head_sha" "$dirty" "$remote" <<'PY'
import hashlib, json, os, sys

run_dir, head = sys.argv[1], sys.argv[2]
dirty, want_remote = sys.argv[3] == "1", sys.argv[4] == "1"

def load(name):
    path = os.path.join(run_dir, name)
    if not os.path.exists(path):
        return None, path
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"INVALID     {path}: {e}")
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"INVALID     {path}: top level is not an object")
        sys.exit(2)
    return data, path

v, vpath = load("verdict.json")
m, _ = load("remote-manifest.json")
d, _ = load("dispatch.json")
remote = want_remote or m is not None
if remote and m is None:
    print(f"INVALID     no remote-manifest.json in {run_dir}: the remote job never packaged its results")
    sys.exit(2)

def environment_problems():
    probs = []
    env, cx = m.get("environment") or {}, m.get("codex") or {}
    if env.get("ready") is not True:
        probs.append(f"INCOMPLETE (QA environment): {env.get('reason') or 'it never became ready'}. "
                     "Repair the VM and rerun the same commit; never send this to the implementer")
    elif cx.get("ran") is not True:
        probs.append(f"INCOMPLETE (runner): Codex never started: {env.get('reason') or 'no reason recorded'}. "
                     "Repair the VM and rerun the same commit")
    elif cx.get("exit_code") != 0:
        probs.append(f"INCOMPLETE (Codex run): exited with {cx.get('exit_code')!r}. Rerun the same commit")
    return probs

def remote_problems():
    probs = []
    mc = str(m.get("commit") or "")
    if mc != head:
        probs.append(f"the VM validated {mc[:12] or '(no commit)'}, but HEAD is {head[:12]}")
    if d is not None:
        if str(d.get("commit") or "") != mc:
            probs.append(f"the VM validated {mc[:12] or '(no commit)'}, but the dispatcher sent {str(d.get('commit') or '')[:12]}")
        if str(d.get("base") or "") != str(m.get("base") or ""):
            probs.append("the VM used a different base commit from the one the dispatcher sent")
    elif want_remote:
        probs.append("no dispatch.json, so there is no record of what was sent to the VM")
    # The checkout checks describe what Codex did, so they only apply once Codex ran; an
    # environment that failed first is already reported as INCOMPLETE.
    co = m.get("checkout") or {}
    if (m.get("codex") or {}).get("ran") is True:
        if co.get("clean_before") is not True:
            probs.append("the VM's checkout had tracked changes before Codex ran")
        if str(co.get("head_before") or "") != mc:
            probs.append("the VM's checkout was not at the validated commit when Codex started")
        if str(co.get("head_after") or "") != mc:
            probs.append("HEAD moved on the VM during the run: Codex committed or checked something out")
        changed = co.get("tracked_changes_after") or []
        if changed:
            shown = ", ".join(str(c) for c in changed[:5]) + (" ..." if len(changed) > 5 else "")
            probs.append(f"Codex modified tracked files on the VM ({shown}); the run is rejected")
    return probs

def integrity_problems():
    files = m.get("files") or {}
    if not isinstance(files, dict) or not files:
        return ["the manifest lists no files, so the download cannot be verified"]
    probs = []
    for rel, digest in files.items():
        p = os.path.join(run_dir, rel)
        if not os.path.isfile(p):
            probs.append(f"download integrity: {rel} is in the manifest but missing here")
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            probs.append(f"download integrity: {rel} does not match the VM's checksum")
    return probs

if v is None:
    if m is None:
        print(f"NO VERDICT  {vpath} does not exist")
        sys.exit(2)
    probs = environment_problems() or ["Codex exited cleanly but wrote no verdict.json. Rerun the same commit"]
    for p in probs + remote_problems():
        print(f"  - {p}")
    print("NOT A PASS  the remote job returned no verdict")
    sys.exit(1)

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

if remote:
    problems += environment_problems() + remote_problems() + integrity_problems()

for p in problems:
    print(f"  - {p}")
if verdict == "PASS" and not problems:
    where = " on the QA VM" if remote else ""
    print(f"PASS        holds for {head[:12]}{where}, clean tree, every rung backed by evidence")
    sys.exit(0)
if verdict == "PASS":
    print(f"NOT A PASS  the verdict file says PASS, but {len(problems)} check(s) above do not hold")
else:
    print(f"NOT A PASS  the validator's verdict is {verdict}")
sys.exit(1)
PY
