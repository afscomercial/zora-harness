#!/usr/bin/env bash
# qa-job.sh — runs ON THE QA VM, one job at a time. run-codex-qa uploads a fresh copy with
# every job, so the VM always runs the harness version that dispatched it.
#
#   qa-job.sh --job <id> --dir <job-dir> --repo <url> --base <sha> --commit <sha>
#             (--profile <p> | --services "<svc> <svc> ...")
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
RUNNER_VERSION=3

JOB="" DIR="" REPO="" BASE="" COMMIT="" PROFILE="" SERVICES=""
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; }
  case "$1" in
    --job) JOB="$2" ;; --dir) DIR="$2" ;; --repo) REPO="$2" ;;
    --base) BASE="$2" ;; --commit) COMMIT="$2" ;; --profile) PROFILE="$2" ;;
    --services) SERVICES="$2" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift 2
done
for v in JOB DIR REPO BASE COMMIT; do
  [ -n "${!v}" ] || { echo "missing --${v,,}" >&2; exit 2; }
done
if [ -z "$PROFILE$SERVICES" ]; then echo "need --profile or --services" >&2; exit 2; fi
if [[ ! "$SERVICES" =~ ^[a-z0-9\ -]*$ ]]; then echo "--services takes plain names only" >&2; exit 2; fi

IN="$DIR/in"; OUT="$DIR/out"; EVID="$OUT/evidence"; WORK="$DIR/work"; SCRATCH="$DIR/scratch"
QA_HOME="$(cd "$DIR/../.." && pwd)"
mkdir -p "$EVID" "$SCRATCH"

if [ -f "$QA_HOME/vm.env" ]; then set -a; . "$QA_HOME/vm.env"; set +a; fi
CODEX_BIN="${CODEX_BIN:-codex}"
QA_MODEL="${QA_MODEL:-gpt-6-astra}"
QA_EFFORT="${QA_EFFORT:-high}"
QA_CLUSTER_DRIVER="${QA_CLUSTER_DRIVER:-k3d}"
# Unique names prevent cleanup from touching the user's existing cluster.
QA_CLUSTER="zora-qa-${JOB,,}"
QA_CLUSTER="${QA_CLUSTER:0:35}"; QA_CLUSTER="${QA_CLUSTER%-}"  # k3d rejects names over 35 characters
K3D_CLUSTER="$QA_CLUSTER"
QA_CONTEXT="$QA_CLUSTER_DRIVER-$QA_CLUSTER"
QA_STOP_SERVICES="${QA_STOP_SERVICES:-${QA_PAUSE_SERVICES:-}}"
QA_NODE_IMAGE="${QA_NODE_IMAGE:-kindest/node:v1.35.0}"
export TILT_PORT="${TILT_PORT:-10350}"
export KUBECONFIG="$DIR/kubeconfig"
# Tilt selection, in the Tiltfile's own priority: a service list wins over a profile.
# With a list, TILT_PROFILE must be unset, because an empty value fails the Tiltfile check.
if [ -n "$SERVICES" ]; then
  read -r -a TILT_ARGS <<< "-- $SERVICES"; TILT_ENV=(env -u TILT_PROFILE)
else
  TILT_ARGS=(); TILT_ENV=(env TILT_PROFILE="$PROFILE")
fi
CLUSTER_STARTED=false; TILT_STARTED=false
K3D_REGISTRY="${K3D_REGISTRY:-zora-qa-registry}"
K3D_REGISTRY_PORT="${K3D_REGISTRY_PORT:-5050}"
TILT_READY_TIMEOUT="${TILT_READY_TIMEOUT:-2400}"
# The browser uses localhost: Clerk returns there after sign-in (CLERK_AUTHORIZED_PARTIES),
# so one host keeps the session cookies together. The dev server binds 127.0.0.1 only.
WEB_APP_URL="http://localhost:5173/"
WEB_APP_PROBE="http://127.0.0.1:5173/"
API_URL="http://127.0.0.1:30080"
MONGO_URI="mongodb://127.0.0.1:27017"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CODEX_VERSION="$("$CODEX_BIN" --version 2>/dev/null | head -1)"
LOCKED=false; ENV_READY=false; ENV_REASON=""; WEB_APP_READY=false; SEEDED=none
CODEX_RAN=false; CODEX_EXIT=""; HEAD_BEFORE=""; CLEAN_BEFORE=false; HEAD_AFTER=""; FINALIZED=0

log()        { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$EVID/0-runner.log"; }
set_status() { printf '%s\n' "$1" > "$DIR/status"; log "status: $1"; }
env_fail()   { ENV_READY=false; ENV_REASON="$1"; log "environment not ready: $1"; }

# Stop a process group started with setsid; the pid file holds its leader.
stop_group() {
  [ -f "$1" ] || return 0
  local pid; pid="$(cat "$1")"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  sleep 3
  kill -KILL -- "-$pid" 2>/dev/null
  rm -f "$1"
}

stop_existing_clusters() {
  python3 - <<'CLUSTERS'
import json, subprocess
ids = subprocess.check_output(['docker','ps','-q'], text=True).split()
if ids:
    containers = json.loads(subprocess.check_output(['docker','inspect',*ids], text=True))
    for container in containers:
        labels = container.get('Config',{}).get('Labels') or {}
        if 'io.x-k8s.kind.cluster' in labels or 'k3d.cluster' in labels:
            print('Stopping existing Kubernetes container: '+container['Name'].lstrip('/'), flush=True)
            subprocess.run(['docker','stop','--time','30',container['Id']], check=True, stdout=subprocess.DEVNULL)
CLUSTERS
}

prepare_exclusive_environment() {
  local unit
  for unit in $QA_STOP_SERVICES; do
    if systemctl is-active --quiet "$unit"; then
      systemctl stop "$unit" || { env_fail "could not stop $unit"; return 1; }
    fi
  done
  stop_existing_clusters || { env_fail "could not stop existing Kubernetes clusters"; return 1; }
  # Refuse a stale server instead of accepting its HTTP response as job evidence.
  python3 - "$TILT_PORT" <<'PORTS'
import socket, sys
for port in [int(sys.argv[1]), 5173, 27017, 30080, 30081, 30082, 30083, 30084, 30085, 30086, 30087, 30088, 30089, 30090, 30091, 30092, 30093, 30094, 30095]:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
        except OSError:
            print(f'Port {port} is occupied; stop its owner before QA', file=sys.stderr)
            sys.exit(1)
PORTS
}

create_cluster() {
  mkdir -p "$WORK/tilt/data/mongo"
  case "$QA_CLUSTER_DRIVER" in
    kind)
      python3 - "$WORK" "$DIR/kind.json" <<'KIND'
import json, sys
work, target = sys.argv[1:]
json.dump({'kind':'Cluster', 'apiVersion':'kind.x-k8s.io/v1alpha4',
    'nodes':[{'role':'control-plane', 'extraMounts':[{
        'hostPath':work+'/tilt/data', 'containerPath':'/mnt/mac'+work+'/tilt/data'}]}]}, open(target,'w'))
KIND
      CLUSTER_STARTED=true
      kind create cluster --name "$QA_CLUSTER" --image "$QA_NODE_IMAGE" \
        --config "$DIR/kind.json" --kubeconfig "$KUBECONFIG" --wait 180s
      ;;
    k3d)
      if ! k3d registry list "$K3D_REGISTRY" >/dev/null 2>&1; then
        k3d registry create "$K3D_REGISTRY" --port "$K3D_REGISTRY_PORT" || return 1
      fi
      CLUSTER_STARTED=true
      k3d cluster create "$QA_CLUSTER" --wait \
        --registry-use "k3d-$K3D_REGISTRY:$K3D_REGISTRY_PORT" \
        --volume "$WORK/tilt/data:/mnt/mac$WORK/tilt/data@server:0" \
        --kubeconfig-update-default=false --kubeconfig-switch-context=false || return 1
      k3d kubeconfig get "$QA_CLUSTER" > "$KUBECONFIG"
      ;;
    *) env_fail "unsupported QA_CLUSTER_DRIVER: $QA_CLUSTER_DRIVER"; return 1 ;;
  esac
}

start_tilt() {
  ( cd "$WORK" && exec setsid "${TILT_ENV[@]}" tilt up -f tilt/Tiltfile \
      --context "$QA_CONTEXT" --host 127.0.0.1 --port "$TILT_PORT" --stream "${TILT_ARGS[@]}" ) \
      > "$EVID/0-tilt.log" 2>&1 < /dev/null &
  echo $! > "$DIR/tilt.pid"
  TILT_STARTED=true
}

prepare() {
  log "job $JOB: commit $COMMIT, base $BASE, profile ${PROFILE:-none}, services ${SERVICES:-none}, runner v$RUNNER_VERSION, $CODEX_VERSION"
  local mirror="$QA_HOME/cache/zora-pantheon.git"
  command -v "$CODEX_BIN" >/dev/null || { env_fail "Codex CLI is missing"; return 1; }
  "$CODEX_BIN" login status > "$EVID/0-codex-login.txt" 2>&1 \
    || { env_fail "Codex needs login: codex login --device-auth"; return 1; }
  case "$QA_CLUSTER_DRIVER" in kind|k3d) ;; *) env_fail "unsupported cluster driver"; return 1 ;; esac
  command -v "$QA_CLUSTER_DRIVER" >/dev/null || { env_fail "$QA_CLUSTER_DRIVER is missing"; return 1; }

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
    # The web-app's entity cache is a shared Upstash instance. QA must neither read nor
    # write it; without these keys the app runs uncached.
    if [ -f "$WORK/apps/web-app/.env" ] && grep -qE '^KV_REST_API_(URL|TOKEN)=' "$WORK/apps/web-app/.env"; then
      sed -i -E '/^KV_REST_API_(URL|TOKEN)=/d' "$WORK/apps/web-app/.env"
      log "disabled the shared web-app entity cache for this job"
    fi
  else
    log "no secrets/ folder: services start without their runtime .env files"
  fi
  [ -z "$(git -C "$WORK" status --porcelain --untracked-files=no)" ] \
    || { env_fail "tracked files changed while placing runtime files"; return 1; }

  ( cd "$WORK" && pnpm install --frozen-lockfile ) > "$EVID/0-pnpm-install.log" 2>&1 \
    || { env_fail "pnpm install failed (evidence/0-pnpm-install.log)"; return 1; }

  # A fresh clone has no workspace dist files; lean Tilt profiles do not build them.
  ( cd "$WORK" && CI=true NO_COLOR=1 TURBO_UI=false pnpm turbo build --filter="web-app^..." ) \
    > "$EVID/0-bootstrap-web-dependencies.log" 2>&1 \
    || { env_fail "web-app dependencies did not build (evidence/0-bootstrap-web-dependencies.log)"; return 1; }

  prepare_exclusive_environment >> "$EVID/0-runner.log" 2>&1 \
    || { env_fail "could not reserve QA ports (evidence/0-runner.log)"; return 1; }
  create_cluster >> "$EVID/0-cluster.log" 2>&1 \
    || { env_fail "fresh cluster creation failed (evidence/0-cluster.log)"; return 1; }
  # Linux compatibility without the VPS wrapper that forces the usual kubeconfig.
  mkdir -p "$DIR/bin"
  printf '#!/bin/bash\nexec /bin/bash "$@"\n' > "$DIR/bin/sh"
  local kubectl_bin; kubectl_bin="$(command -v kubectl)"
  printf '#!/bin/bash\nexport KUBECONFIG=%q\nexec %q "$@"\n' "$KUBECONFIG" "$kubectl_bin" > "$DIR/bin/kubectl"
  # Prefer a host shell: it can read scripts from QA_SCRATCH.
  if ! command -v mongosh >/dev/null; then
    printf '#!/bin/bash\nexec kubectl --context %q exec -i deploy/mongo -- mongosh "$@"\n' "$QA_CONTEXT" > "$DIR/bin/mongosh"
  fi
  chmod 700 "$DIR/bin/"*
  export PATH="$DIR/bin:$PATH"
  start_tilt
  python3 - "$TILT_READY_TIMEOUT" "$EVID/0-tilt-resources.json" >> "$EVID/0-runner.log" 2>&1 <<'PY' \
    || { env_fail "Tilt resources did not become ready (evidence/0-tilt-resources.json, evidence/0-tilt.log)"; return 1; }
import json, os, subprocess, sys, time
timeout, out_path = int(sys.argv[1]), sys.argv[2]
deadline, first_error, items = time.time() + timeout, {}, []
pending, erroring = [], []
while time.time() < deadline:
    try:
        r = subprocess.run(["tilt", "get", "uiresource", "-o", "json", "--port", os.environ["TILT_PORT"]], capture_output=True, text=True, timeout=30)
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
  ( cd "$WORK/apps/web-app" && exec setsid env HN_ENV=local API_URL="$API_URL" \
      pnpm exec react-router dev --host 127.0.0.1 --port 5173 --strictPort ) \
      > "$EVID/0-web-app.log" 2>&1 < /dev/null &
  echo $! > "$DIR/webapp.pid"
  local i
  for i in $(seq 1 60); do
    kill -0 "$(cat "$DIR/webapp.pid")" 2>/dev/null || break
    if curl --max-time 10 -fsS -o /dev/null "$WEB_APP_PROBE"; then WEB_APP_READY=true; break; fi
    sleep 3
  done
  if $WEB_APP_READY; then log "web-app ready at $WEB_APP_PROBE (browser uses $WEB_APP_URL)"
  else log "web-app did not answer at $WEB_APP_PROBE (evidence/0-web-app.log); Codex reports it on rung 4"; fi

  # Codex browses only through this boundary; prove it blocks before trusting it.
  QA_WORK="$WORK" QA_AUTH_ORIGINS="${QA_AUTH_ORIGINS:-}" node "$IN/qa-browser.cjs" --self-test \
      > "$EVID/0-browser-boundary.json" 2>&1 \
    || { env_fail "browser network boundary self-test failed (evidence/0-browser-boundary.json)"; return 1; }
  log "browser network boundary holds"

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
  # Credentials live only on the VPS and enter the child process environment.
  # Never write their values into the prompt, charter, manifest, or evidence.
  export BASE_URL="$WEB_APP_URL"
  QA_AUTH_AVAILABLE=false
  if [ -f "${QA_AUTH_FILE:-$QA_HOME/auth.json}" ]; then
    local auth_file="${QA_AUTH_FILE:-$QA_HOME/auth.json}"
    CLERK_TEST_EMAIL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["email"])' "$auth_file") || { env_fail "invalid QA credentials file"; return 1; }
    CLERK_TEST_PASSWORD=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["password"])' "$auth_file") || { env_fail "invalid QA credentials file"; return 1; }
    if [ -n "$CLERK_TEST_EMAIL" ] && [ -n "$CLERK_TEST_PASSWORD" ]; then
      export CLERK_TEST_EMAIL CLERK_TEST_PASSWORD
      QA_AUTH_AVAILABLE=true
    fi
  fi
  export QA_AUTH_ORIGINS="${QA_AUTH_ORIGINS:-}"
  export QA_BROWSER_HELPER="$IN/qa-browser.cjs" QA_WORK="$WORK"

  HEAD_BEFORE="$(git -C "$WORK" rev-parse HEAD)"
  [ -z "$(git -C "$WORK" status --porcelain --untracked-files=no)" ] && CLEAN_BEFORE=true
  local prompt="$DIR/prompt.md"
  {
    cat "$IN/codex-qa-prompt.md"
    printf '\n\n---\n\n## Environment (filled by the VM runner)\n\n'
    printf -- '- Checkout (detached HEAD; never edit it): `%s`\n' "$WORK"
    printf -- '- Base commit: `%s`\n- Feature commit: `%s`\n' "$BASE" "$COMMIT"
    if [ -n "$SERVICES" ]; then
      printf -- '- Tilt services (infrastructure derived automatically): `%s` on Kubernetes context `%s`\n' "$SERVICES" "$QA_CONTEXT"
    else
      printf -- '- Tilt profile: `%s` on Kubernetes context `%s`\n' "$PROFILE" "$QA_CONTEXT"
    fi
    printf -- '- api-gateway: %s\n' "$API_URL"
    printf -- '- MongoDB: %s (Tilt port-forward)\n' "$MONGO_URI"
    printf -- '- web-app: %s (answered at startup: %s)\n' "$WEB_APP_URL" "$WEB_APP_READY"
    printf -- '- Clerk credentials available via CLERK_TEST_EMAIL / CLERK_TEST_PASSWORD: %s (never print values)\n' "$QA_AUTH_AVAILABLE"
    printf -- '- Approved authentication origins (exact origins only): %s\n' "${QA_AUTH_ORIGINS:-none}"
    printf -- '- Browser boundary helper ($QA_BROWSER_HELPER): `%s` (self-test: evidence/0-browser-boundary.json)\n' "$IN/qa-browser.cjs"
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
    if $TILT_STARTED; then
      ( cd "$WORK" && timeout 180 "${TILT_ENV[@]}" tilt down -f tilt/Tiltfile --context "$QA_CONTEXT" "${TILT_ARGS[@]}" ) >> "$EVID/0-runner.log" 2>&1
      stop_group "$DIR/tilt.pid"
    fi
    if $CLUSTER_STARTED && [ "${QA_KEEP_CLUSTER:-0}" != 1 ]; then
      case "$QA_CLUSTER_DRIVER" in
        kind) kind delete cluster --name "$QA_CLUSTER" >> "$EVID/0-cluster.log" 2>&1 ;;
        k3d) k3d cluster delete "$QA_CLUSTER" >> "$EVID/0-cluster.log" 2>&1 ;;
      esac
    fi
    # QA owns this dedicated VM. Never restart previous services or clusters.
  fi
  cp "$DIR/job.log" "$EVID/0-job.log" 2>/dev/null
  find "$OUT" -type l -delete
  # Tilt echoes build args and pod env values; scrub every known secret before hashing.
  python3 "$IN/redact-evidence.py" "$OUT" "$QA_HOME" >> "$DIR/job.log" 2>&1 \
    || log "evidence redaction failed; the result may contain secrets"

  M_RUNNER_VERSION="$RUNNER_VERSION" M_JOB="$JOB" M_REPO="$REPO" M_BASE="$BASE" M_COMMIT="$COMMIT" \
  M_PROFILE="$PROFILE" M_SERVICES="$SERVICES" M_STARTED_AT="$STARTED_AT" M_HEAD_BEFORE="$HEAD_BEFORE" \
  M_CLEAN_BEFORE="$CLEAN_BEFORE" M_HEAD_AFTER="$HEAD_AFTER" M_ENV_READY="$ENV_READY" \
  M_ENV_REASON="$ENV_REASON" M_WEB_APP_READY="$WEB_APP_READY" M_SEEDED="$SEEDED" \
  M_CLUSTER="$QA_CONTEXT" M_CODEX_RAN="$CODEX_RAN" M_CODEX_EXIT="$CODEX_EXIT" \
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
    "profile": e["M_PROFILE"] or None,
    "services": e.get("M_SERVICES", "").split() or None,
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
trap 'exit 130' INT
trap 'exit 143' TERM
set_status preparing
if $LOCKED && prepare; then run_codex; fi
