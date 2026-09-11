#!/usr/bin/env bash
# qa-job.sh — runs ON THE QA VM, one job at a time. run-codex-qa uploads a fresh copy with
# every job, so the VM always runs the harness version that dispatched it.
#
#   qa-job.sh --job <id> --dir <job-dir> --repo <url> --base <sha> --commit <sha> --profile <p>
#
# Stages, written to <job-dir>/status: preparing -> validating -> packaging -> done | failed.
# "failed" means the environment or the runner broke before Codex could run. Results are
# still packaged and the manifest says why, so the laptop classifies it as INCOMPLETE.
#
# VM layout. QA_HOME is two levels above the job folder (<QA_HOME>/jobs/<id>):
#   vm.env                   settings and sandbox-only tokens (CODEX_BIN, NPM_TOKEN, ...)
#   secrets/                 sandbox-only runtime files mirroring gitignored repo paths,
#                            e.g. secrets/apps/web-app/.env
#   hooks/seed.sh            optional: seeds test data; without it Codex seeds per the
#                            repo's seed-local-db skill
#   cache/zora-pantheon.git  bare mirror, refreshed every job
set -uo pipefail
RUNNER_VERSION=1

JOB="" DIR="" REPO="" BASE="" COMMIT="" PROFILE=""
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }
  case "$1" in
    --job) JOB="$2" ;; --dir) DIR="$2" ;; --repo) REPO="$2" ;;
    --base) BASE="$2" ;; --commit) COMMIT="$2" ;; --profile) PROFILE="$2" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift 2
done
for v in JOB DIR REPO BASE COMMIT PROFILE; do
  [ -n "${!v}" ] || { echo "missing --${v,,}" >&2; exit 2; }
done

IN="$DIR/in"; OUT="$DIR/out"; EVID="$OUT/evidence"; WORK="$DIR/work"; SCRATCH="$DIR/scratch"
QA_HOME="$(cd "$DIR/../.." && pwd)"
mkdir -p "$EVID" "$SCRATCH"

if [ -f "$QA_HOME/vm.env" ]; then set -a; . "$QA_HOME/vm.env"; set +a; fi
CODEX_BIN="${CODEX_BIN:-codex}"
QA_MODEL="${QA_MODEL:-gpt-6-astra}"
QA_EFFORT="${QA_EFFORT:-high}"
K3D_CLUSTER="${K3D_CLUSTER:-zora-qa}"
K3D_REGISTRY="${K3D_REGISTRY:-zora-qa-registry}"
K3D_REGISTRY_PORT="${K3D_REGISTRY_PORT:-5050}"
TILT_READY_TIMEOUT="${TILT_READY_TIMEOUT:-2400}"
WEB_APP_URL="http://127.0.0.1:5173/"
API_URL="http://127.0.0.1:30080"
MONGO_URI="mongodb://127.0.0.1:27017"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CODEX_VERSION="$("$CODEX_BIN" --version 2>/dev/null | head -1)"
LOCKED=false; ENV_READY=false; ENV_REASON=""; WEB_APP_READY=false; SEEDED=none
CODEX_RAN=false; CODEX_EXIT=""; HEAD_BEFORE=""; CLEAN_BEFORE=false; HEAD_AFTER=""; FINALIZED=0

log()        { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$EVID/0-runner.log"; }
set_status() { printf '%s\n' "$1" > "$DIR/status"; log "status: $1"; }
env_fail()   { ENV_REASON="$1"; log "environment not ready: $1"; }

# Stop a process group started with setsid; the pid file holds its leader.
stop_group() {
  [ -f "$1" ] || return 0
  local pid; pid="$(cat "$1")"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 3
  kill -KILL -- "-$pid" 2>/dev/null
  rm -f "$1"
}

prepare() {
  log "job $JOB: commit $COMMIT, base $BASE, profile $PROFILE, runner v$RUNNER_VERSION, $CODEX_VERSION"
  local mirror="$QA_HOME/cache/zora-pantheon.git"

  # A disposable clone at the exact commit, detached, verified clean.
  if [ -d "$mirror" ]; then
    git -C "$mirror" remote update --prune >> "$EVID/0-runner.log" 2>&1 \
      || { env_fail "could not refresh the repository mirror"; return 1; }
  else
    mkdir -p "$QA_HOME/cache"
    git clone --quiet --mirror "$REPO" "$mirror" >> "$EVID/0-runner.log" 2>&1 \
      || { env_fail "could not clone the repository (check the VM's read-only deploy key)"; return 1; }
  fi
  git clone --quiet --no-checkout "$mirror" "$WORK" >> "$EVID/0-runner.log" 2>&1 \
    || { env_fail "could not create the disposable clone"; return 1; }
  git -C "$WORK" checkout --quiet --detach "$COMMIT" >> "$EVID/0-runner.log" 2>&1 \
    || { env_fail "commit $COMMIT is not in the repository; was it pushed?"; return 1; }
  [ "$(git -C "$WORK" rev-parse HEAD)" = "$COMMIT" ] \
    || { env_fail "the checkout did not land on $COMMIT"; return 1; }
  git -C "$WORK" merge-base --is-ancestor "$BASE" "$COMMIT" \
    || { env_fail "base $BASE is not an ancestor of $COMMIT"; return 1; }
  [ -z "$(git -C "$WORK" status --porcelain)" ] \
    || { env_fail "the fresh checkout is not clean"; return 1; }

  # Sandbox-only runtime files. Each must be gitignored, so they can never replace source.
  if [ -d "$QA_HOME/secrets" ]; then
    local f rel
    while IFS= read -r -d '' f; do
      rel="${f#"$QA_HOME/secrets/"}"
      git -C "$WORK" check-ignore -q -- "$rel" \
        || { env_fail "secrets/$rel is not gitignored in the repo; refusing to overwrite tracked content"; return 1; }
      mkdir -p "$WORK/$(dirname "$rel")" && cp "$f" "$WORK/$rel" \
        || { env_fail "could not place secrets/$rel"; return 1; }
    done < <(find "$QA_HOME/secrets" -type f -print0)
    log "placed sandbox runtime files from secrets/"
  else
    log "no secrets/ folder: services start without their runtime .env files"
  fi
  [ -z "$(git -C "$WORK" status --porcelain --untracked-files=no)" ] \
    || { env_fail "tracked files changed while placing runtime files"; return 1; }

  ( cd "$WORK" && pnpm install --frozen-lockfile ) > "$EVID/0-pnpm-install.log" 2>&1 \
    || { env_fail "pnpm install failed (evidence/0-pnpm-install.log)"; return 1; }

  # A fresh cluster every job. The registry persists across jobs, so images stay cached.
  if ! k3d registry list "$K3D_REGISTRY" >/dev/null 2>&1; then
    k3d registry create "$K3D_REGISTRY" --port "$K3D_REGISTRY_PORT" >> "$EVID/0-k3d.log" 2>&1 \
      || { env_fail "could not create the k3d image registry"; return 1; }
  fi
  k3d cluster delete "$K3D_CLUSTER" >> "$EVID/0-k3d.log" 2>&1
  k3d cluster create "$K3D_CLUSTER" --wait \
      --registry-use "k3d-$K3D_REGISTRY:$K3D_REGISTRY_PORT" \
      -p "32701:32701@server:0" >> "$EVID/0-k3d.log" 2>&1 \
    || { env_fail "k3d cluster create failed (evidence/0-k3d.log)"; return 1; }
  kubectl config use-context "k3d-$K3D_CLUSTER" >> "$EVID/0-k3d.log" 2>&1 \
    || { env_fail "cannot switch to the k3d context"; return 1; }

  ( cd "$WORK" && TILT_PROFILE="$PROFILE" setsid tilt up -f tilt/Tiltfile --stream \
      > "$EVID/0-tilt.log" 2>&1 < /dev/null & echo $! > "$DIR/tilt.pid" )
  python3 - "$TILT_READY_TIMEOUT" "$EVID/0-tilt-resources.json" >> "$EVID/0-runner.log" 2>&1 <<'PY' \
    || { env_fail "Tilt resources did not become ready (evidence/0-tilt-resources.json, evidence/0-tilt.log)"; return 1; }
import json, subprocess, sys, time
timeout, out_path = int(sys.argv[1]), sys.argv[2]
deadline, first_error, items = time.time() + timeout, {}, []
pending, erroring = [], []
while time.time() < deadline:
    try:
        r = subprocess.run(["tilt", "get", "uiresource", "-o", "json"], capture_output=True, text=True, timeout=30)
        items = json.loads(r.stdout or "{}").get("items", [])
    except Exception:
        items = []
    pending, erroring, now = [], [], time.time()
    for it in items:
        name = it.get("metadata", {}).get("name", "?")
        st = it.get("status", {})
        rt, up = st.get("runtimeStatus", "unknown"), st.get("updateStatus", "unknown")
        if rt == "error" or up == "error":
            first_error.setdefault(name, now)
            erroring.append(name)
        else:
            first_error.pop(name, None)
            if rt not in ("ok", "not_applicable") or up not in ("ok", "not_applicable", "none"):
                pending.append(name)
    stuck = [n for n in erroring if now - first_error[n] > 300]
    if items and not pending and not erroring:
        json.dump(items, open(out_path, "w"), indent=1)
        print("tilt: all resources ready")
        sys.exit(0)
    if stuck:
        json.dump(items, open(out_path, "w"), indent=1)
        print("tilt: in error for over 5 minutes: " + ", ".join(stuck))
        sys.exit(1)
    time.sleep(10)
json.dump(items, open(out_path, "w"), indent=1)
waiting = sorted(set(pending + erroring)) or ["(no resources reported)"]
print("tilt: timed out; not ready: " + ", ".join(waiting))
sys.exit(1)
PY

  # The web-app is not part of Tilt; start its dev server for the browser rung.
  ( cd "$WORK" && setsid pnpm --filter web-app run dev --host 127.0.0.1 --port 5173 \
      > "$EVID/0-web-app.log" 2>&1 < /dev/null & echo $! > "$DIR/webapp.pid" )
  local i
  for i in $(seq 1 60); do
    if curl -fsS -o /dev/null "$WEB_APP_URL"; then WEB_APP_READY=true; break; fi
    sleep 3
  done
  if $WEB_APP_READY; then log "web-app ready at $WEB_APP_URL"
  else log "web-app did not answer at $WEB_APP_URL (evidence/0-web-app.log); Codex reports it on rung 4"; fi

  if [ -x "$QA_HOME/hooks/seed.sh" ]; then
    QA_WORK="$WORK" QA_MONGO_URI="$MONGO_URI" "$QA_HOME/hooks/seed.sh" > "$EVID/0-seed.txt" 2>&1 \
      || { env_fail "hooks/seed.sh failed (evidence/0-seed.txt)"; return 1; }
    SEEDED=runner
  else
    SEEDED=delegated
  fi

  ENV_READY=true
  log "environment ready"
}

run_codex() {
  HEAD_BEFORE="$(git -C "$WORK" rev-parse HEAD)"
  [ -z "$(git -C "$WORK" status --porcelain --untracked-files=no)" ] && CLEAN_BEFORE=true
  local prompt="$DIR/prompt.md"
  {
    cat "$IN/codex-qa-prompt.md"
    printf '\n\n---\n\n## Environment (filled by the VM runner)\n\n'
    printf -- '- Checkout (detached HEAD; never edit it): `%s`\n' "$WORK"
    printf -- '- Base commit: `%s`\n- Feature commit: `%s`\n' "$BASE" "$COMMIT"
    printf -- '- Tilt profile: `%s` on k3d cluster `k3d-%s`\n' "$PROFILE" "$K3D_CLUSTER"
    printf -- '- api-gateway: %s\n' "$API_URL"
    printf -- '- MongoDB: %s (Tilt port-forward; NodePort 32701 is mapped too)\n' "$MONGO_URI"
    printf -- '- web-app: %s (answered at startup: %s)\n' "$WEB_APP_URL" "$WEB_APP_READY"
    printf -- '- Test data seeded by: %s\n' "$SEEDED"
    printf -- '- Evidence directory ($QA_EVIDENCE_DIR): `%s`\n' "$EVID"
    printf -- '- Scratch directory ($QA_SCRATCH): `%s`\n' "$SCRATCH"
    printf '\n---\n\n'
    cat "$IN/qa-charter.md"
  } > "$prompt"

  set_status validating
  CODEX_RAN=true
  QA_EVIDENCE_DIR="$EVID" QA_SCRATCH="$SCRATCH" \
    "$CODEX_BIN" exec --ephemeral --ignore-user-config \
      --model "$QA_MODEL" -c "model_reasoning_effort=\"$QA_EFFORT\"" \
      --sandbox danger-full-access \
      --json --output-schema "$IN/verdict.schema.json" -o "$OUT/verdict.json" \
      -C "$WORK" - < "$prompt" > "$OUT/codex-events.jsonl" 2> "$EVID/0-codex-stderr.log"
  CODEX_EXIT=$?
  log "codex exited with $CODEX_EXIT"
}

finalize() {
  [ "$FINALIZED" = 1 ] && return
  FINALIZED=1
  set_status packaging
  : > "$DIR/tracked-after.txt"
  if [ -d "$WORK/.git" ]; then
    HEAD_AFTER="$(git -C "$WORK" rev-parse HEAD 2>/dev/null)"
    git -C "$WORK" status --porcelain --untracked-files=no > "$DIR/tracked-after.txt" 2>/dev/null
    git -C "$WORK" status --porcelain > "$EVID/0-checkout-after.txt" 2>/dev/null
  fi
  if $LOCKED; then  # never tear down an environment another job owns
    stop_group "$DIR/webapp.pid"
    [ -d "$WORK" ] && ( cd "$WORK" && timeout 180 tilt down -f tilt/Tiltfile ) >> "$EVID/0-runner.log" 2>&1
    stop_group "$DIR/tilt.pid"
    [ "${QA_KEEP_CLUSTER:-0}" = 1 ] || k3d cluster delete "$K3D_CLUSTER" >> "$EVID/0-k3d.log" 2>&1
  fi
  cp "$DIR/job.log" "$EVID/0-job.log" 2>/dev/null
  find "$OUT" -type l -delete

  M_RUNNER_VERSION="$RUNNER_VERSION" M_JOB="$JOB" M_REPO="$REPO" M_BASE="$BASE" M_COMMIT="$COMMIT" \
  M_PROFILE="$PROFILE" M_STARTED_AT="$STARTED_AT" M_HEAD_BEFORE="$HEAD_BEFORE" \
  M_CLEAN_BEFORE="$CLEAN_BEFORE" M_HEAD_AFTER="$HEAD_AFTER" M_ENV_READY="$ENV_READY" \
  M_ENV_REASON="$ENV_REASON" M_WEB_APP_READY="$WEB_APP_READY" M_SEEDED="$SEEDED" \
  M_CLUSTER="k3d-$K3D_CLUSTER" M_CODEX_RAN="$CODEX_RAN" M_CODEX_EXIT="$CODEX_EXIT" \
  M_CODEX_VERSION="$CODEX_VERSION" M_MODEL="$QA_MODEL" M_EFFORT="$QA_EFFORT" \
  python3 - "$OUT" "$DIR/tracked-after.txt" <<'PY'
import datetime, hashlib, json, os, re, socket, sys
out, tracked_path = sys.argv[1], sys.argv[2]
e = os.environ
flag = lambda k: e.get(k) == "true"
tracked = [line.rstrip("\n")[3:] for line in open(tracked_path) if line.strip()]
files = {}
for root, _, names in os.walk(out):
    for n in names:
        p = os.path.join(root, n)
        rel = os.path.relpath(p, out)
        if rel == "remote-manifest.json" or os.path.islink(p) or not os.path.isfile(p):
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        files[rel] = h.hexdigest()
exit_code = e.get("M_CODEX_EXIT", "")
manifest = {
    "runner_version": int(e["M_RUNNER_VERSION"]),
    "job_id": e["M_JOB"],
    "host": socket.gethostname(),
    "repo": re.sub(r"//[^@/]+@", "//", e["M_REPO"]),
    "base": e["M_BASE"],
    "commit": e["M_COMMIT"],
    "profile": e["M_PROFILE"],
    "started_at": e["M_STARTED_AT"],
    "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "checkout": {
        "head_before": e.get("M_HEAD_BEFORE") or None,
        "clean_before": flag("M_CLEAN_BEFORE"),
        "head_after": e.get("M_HEAD_AFTER") or None,
        "tracked_changes_after": tracked,
    },
    "environment": {
        "ready": flag("M_ENV_READY"),
        "reason": e.get("M_ENV_REASON") or None,
        "cluster": e["M_CLUSTER"],
        "web_app_ready": flag("M_WEB_APP_READY"),
        "seeded_by": e["M_SEEDED"],
    },
    "codex": {
        "ran": flag("M_CODEX_RAN"),
        "exit_code": int(exit_code) if exit_code.lstrip("-").isdigit() else None,
        "version": e.get("M_CODEX_VERSION") or None,
        "model": e["M_MODEL"],
        "reasoning_effort": e["M_EFFORT"],
        "sandbox": "danger-full-access",
    },
    "files": files,
}
json.dump(manifest, open(os.path.join(out, "remote-manifest.json"), "w"), indent=2)
PY
  tar -czf "$DIR/result.tar.gz" -C "$OUT" .
  if $ENV_READY && $CODEX_RAN; then set_status done; else set_status failed; fi
}

exec 9> "$QA_HOME/.qa.lock"
if flock -n 9; then LOCKED=true
else ENV_REASON="another QA job holds the VM; rerun when it finishes"; fi
trap finalize EXIT
set_status preparing
if $LOCKED && prepare; then run_codex; fi
