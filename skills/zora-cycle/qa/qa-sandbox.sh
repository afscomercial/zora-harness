#!/usr/bin/env bash
# Dedicated Docker daemon and Linux namespaces for one QA attempt. Root-only, Linux-only.
set -euo pipefail
umask 077
action="${1:?usage: qa-sandbox.sh start|exec|down|verify|check ATTEMPT_DIR [command ...]}"
shift
dir="${1:?attempt directory required}"
shift
[[ "$dir" = /* && "$dir" = "$(realpath -m -- "$dir")" && ! -L "$dir" ]] || { echo 'absolute canonical attempt directory required' >&2; exit 2; }
attempt="$(basename "$dir")"
[[ "$attempt" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { echo 'invalid attempt ID' >&2; exit 2; }
state="$dir/sandbox"
identity="$dir/sandbox.json"
unit="zora-qa-sandbox-$attempt.service"
slice="zora-qa-sandbox-$attempt.slice"
endpoint="unix://$state/docker.sock"
self="$(realpath "$0")"
helper="$(dirname "$self")/qa-network.sh"
qa_root="${QA_HOME:-$(realpath "$dir/../..")}"
check() {
  python3 - "$identity" "$attempt" "$endpoint" <<'PY'
import json,os,subprocess,sys
path,attempt,endpoint=sys.argv[1:]
d=json.load(open(path))
assert d['attempt_id']==attempt and d['docker_endpoint']==endpoint
assert os.environ.get('DOCKER_HOST')==endpoint, 'private DOCKER_HOST required'
cgroup=open('/proc/self/cgroup').read().strip().split('::',1)[1]
assert cgroup.startswith(d['cgroup']+'/'), 'process outside sandbox resource budget'
for key,proc in [('mount','mnt'),('net','net'),('pid','pid'),('uts','uts'),('ipc','ipc')]:
    actual=os.readlink('/proc/self/ns/'+proc)
    assert actual==d['namespaces'][key] and actual!=d['host_namespaces'][key], 'namespace mismatch: '+key
result=subprocess.check_output(['docker','--host',endpoint,'info','--format','{{.ID}}'],text=True,timeout=15).strip()
assert result and result==d['daemon_id'], 'private Docker identity mismatch'
PY
}
verify() {
  python3 - "$identity" "$attempt" "$unit" "$endpoint" <<'PY'
import json,os,subprocess,sys
path,attempt,unit,endpoint=sys.argv[1:]
d=json.load(open(path))
assert d['attempt_id']==attempt and d['unit']==unit and d['docker_endpoint']==endpoint
pid=d['init_pid']
assert unit in open(f'/proc/{pid}/cgroup').read(), 'sandbox process moved or reused'
assert subprocess.check_output(['systemctl','is-active',unit],text=True,timeout=10).strip()=='active'
for key,proc in [('mount','mnt'),('net','net'),('pid','pid'),('uts','uts'),('ipc','ipc')]:
    actual=os.readlink(f'/proc/{pid}/ns/'+proc)
    assert actual==d['namespaces'][key] and actual!=d['host_namespaces'][key], 'namespace mismatch: '+key
result=subprocess.check_output(['nsenter','--target',str(pid),'--mount','--net','--pid','--uts','--ipc',
    'docker','--host',endpoint,'info','--format','{{.ID}}'],text=True,timeout=15).strip()
assert result and result==d['daemon_id'], 'private Docker identity mismatch'
PY
}
network() {
  QA_NS="${QA_NS:?}" QA_HOST_IP="${QA_HOST_IP:?}" QA_PEER_IP="${QA_PEER_IP:?}" \
    timeout --signal=TERM --kill-after=10 45 bash "$helper" "$1"
}
down() {
  [ -f "$state/owner.json" ] || { [ ! -e "$identity" ] || { echo 'identity exists without ownership' >&2; return 1; }; return 0; }
  # Recover namespace values from the owned record, not mutable worker configuration.
  read -r QA_NS QA_HOST_IP QA_PEER_IP < <(python3 - "$state/owner.json" "$attempt" "$dir" <<'PY'
import json,sys
x=json.load(open(sys.argv[1])); assert x['attempt_id']==sys.argv[2] and x['directory']==sys.argv[3]
print(x['netns'],x['host_ip'],x['peer_ip'])
PY
)
  export QA_NS QA_HOST_IP QA_PEER_IP
  local aggregate
  aggregate=$(systemctl show "$slice" --property=ControlGroup --value)
  if [ -n "$aggregate" ]; then
    [[ "$aggregate" = /* && "$aggregate" = *"/$slice" ]] || { echo 'unexpected aggregate cgroup' >&2; return 1; }
    # Includes cgroupfs Docker children that are not separately registered systemd units.
    if [ -f "/sys/fs/cgroup$aggregate/cgroup.kill" ]; then
      printf '1\n' > "/sys/fs/cgroup$aggregate/cgroup.kill"
    fi
  fi
  if [ "$(systemctl show "$slice" --property=LoadState --value)" != not-found ]; then
    timeout 90 systemctl stop "$slice" || return 1
  fi
  if [ "$(systemctl show "$unit" --property=LoadState --value)" != not-found ]; then
    timeout 30 systemctl stop "$unit" || return 1
  fi
  # A stopped/dead unit and empty aggregate cgroup are required before removing files.
  local status cg
  status=$(systemctl show "$unit" --property=ActiveState --value)
  [[ "$status" = inactive || "$status" = failed ]] || { echo 'sandbox unit still active' >&2; return 1; }
  cg=$(systemctl show "$slice" --property=ControlGroup --value)
  if [ -n "$cg" ] && [ -d "/sys/fs/cgroup$cg" ]; then
    python3 - "/sys/fs/cgroup$cg" <<'PY'
from pathlib import Path
import sys
for p in Path(sys.argv[1]).rglob('cgroup.procs'):
    assert not p.read_text().strip(), 'owned cgroup still populated'
PY
  fi
  network down || return 1
  rm -f -- "$state/docker.sock" "$state/ready"
  systemctl reset-failed "$unit" "$slice" >/dev/null 2>&1 || true
  # Remove only runtime resource-property files created for this exact private slice.
  rm -rf -- "/run/systemd/system.control/$slice.d"
  systemctl daemon-reload
  printf 'clean\n' > "$state/cleaned"
}
case "$action" in
__init)
  # Called only by the supervised service, after unshare has created all namespaces.
  # Keep the QA root reachable when its original path is beneath private /tmp.
  resolver_target=$(readlink -f /etc/resolv.conf)
  exec 7< "$qa_root"
  mount --make-rprivate /
  mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
  mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /var/tmp
  mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run
  mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm
  case "$qa_root" in /tmp/*|/var/tmp/*|/run/*)
    mkdir -p "$qa_root"
    mount --no-canonicalize --bind /proc/self/fd/7 "$qa_root" ;;
  esac
  exec 7<&-
  mkdir -p "$(dirname "$resolver_target")"
  [ -e "$resolver_target" ] || touch "$resolver_target"
  mount --bind "$state/resolv.conf" "$resolver_target"
  hostname "qa-${attempt:0:50}"
  mkdir -p /run/qa-runtime
  chmod 700 /run/qa-runtime
  export HOME="$state/home" DOCKER_CONFIG="$state/home/.docker"
  export DOCKER_HOST="$endpoint"
  unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
  aggregate_cgroup=$(dirname "$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)")
  [[ "$aggregate_cgroup" = /* && "$aggregate_cgroup" = *"/$slice" ]] || { echo 'unexpected sandbox cgroup' >&2; exit 1; }
  dockerd --config-file "$state/daemon.json" --host "$endpoint" \
    --data-root "$state/docker-data" --exec-root /run/qa-docker --pidfile /run/qa-docker.pid \
    --exec-opt native.cgroupdriver=cgroupfs --cgroup-parent "$aggregate_cgroup" \
    > "$state/dockerd.log" 2>&1 &
  daemon_pid=$!
  trap 'kill -TERM "$daemon_pid" 2>/dev/null || true; wait "$daemon_pid" || true; exit 0' TERM INT
  for _ in $(seq 1 90); do
    kill -0 "$daemon_pid" 2>/dev/null || { tail -40 "$state/dockerd.log"; exit 1; }
    if docker --host "$endpoint" info --format '{{.ID}}' > "$state/daemon-id" 2>/dev/null; then
      touch "$state/ready"
      wait "$daemon_pid"
      exit $?
    fi
    sleep 1
  done
  echo 'private Docker daemon startup timed out' >&2
  exit 1
  ;;
start)
  [ "$(id -u)" = 0 ] || { echo 'sandbox requires root' >&2; exit 2; }
  : "${QA_NS:?}" "${QA_HOST_IP:?}" "${QA_PEER_IP:?}" "${QA_SANDBOX_MEMORY_MB:?}"
  [[ "$QA_SANDBOX_MEMORY_MB" =~ ^[1-9][0-9]*$ ]] || { echo 'invalid sandbox memory budget' >&2; exit 2; }
  if [ -e "$identity" ]; then verify; exit $?; fi
  if [ -e "$state/owner.json" ]; then echo 'incomplete sandbox exists; run down first' >&2; exit 1; fi
  if systemctl is-active --quiet "$unit" || systemctl is-active --quiet "$slice"; then echo 'unit name collision' >&2; exit 1; fi
  mkdir -p "$state/home"
  python3 - "$state/owner.json" "$attempt" "$dir" "$QA_NS" "$QA_HOST_IP" "$QA_PEER_IP" <<'PY'
import json,sys
path,attempt,directory,ns,host,peer=sys.argv[1:]
json.dump(dict(attempt_id=attempt,directory=directory,netns=ns,host_ip=host,peer_ip=peer),open(path,'w'))
PY
  printf '{}\n' > "$state/daemon.json"
  # Auth files are never logged and remain within this attempt's private HOME.
  source_home="${QA_AUTH_HOME:-$HOME}"
  for relative in .gitconfig .git-credentials .npmrc .ssh .codex/auth.json .config/gh/hosts.yml .docker/config.json; do
    if [ -e "$source_home/$relative" ]; then
      mkdir -p "$state/home/$(dirname "$relative")"
      cp -aL -- "$source_home/$relative" "$state/home/$relative"
    fi
  done
  trap 'rc=$?; if [ "$rc" != 0 ]; then down || true; fi; exit "$rc"' EXIT
  network up
  cp "/etc/netns/$QA_NS/resolv.conf" "$state/resolv.conf"
  systemctl set-property --runtime "$slice" "MemoryMax=${QA_SANDBOX_MEMORY_MB}M" MemorySwapMax=0 MemoryAccounting=yes CPUAccounting=yes
  systemd-run --quiet --collect --unit="$unit" --slice="$slice" \
    --property=Type=exec --property=KillMode=control-group --property=TimeoutStopSec=30 \
    --property=Delegate=yes --property=UMask=0077 --property="NetworkNamespacePath=/run/netns/$QA_NS" \
    --property="StandardOutput=append:$state/init.log" --property="StandardError=append:$state/init.log" \
    env "QA_HOME=$qa_root" unshare --mount --uts --ipc --pid --fork --kill-child=TERM --mount-proc \
    bash "$self" __init "$dir"
  for _ in $(seq 1 100); do
    if [ -f "$state/ready" ]; then break; fi
    systemctl is-active --quiet "$unit" || { cat "$state/init.log"; exit 1; }
    sleep 1
  done
  [ -f "$state/ready" ] || { echo 'sandbox start timed out' >&2; exit 1; }
  python3 - "$identity" "$state" "$attempt" "$unit" "$slice" "$endpoint" <<'PY'
import json,os,subprocess,sys
path,state,attempt,unit,slice,endpoint=sys.argv[1:]
main=int(subprocess.check_output(['systemctl','show',unit,'--property=MainPID','--value'],text=True))
children=open(f'/proc/{main}/task/{main}/children').read().split()
assert len(children)==1, 'cannot identify namespace init process'
pid=int(children[0])
names={'mount':'mnt','net':'net','pid':'pid','uts':'uts','ipc':'ipc'}
cgroup=subprocess.check_output(['systemctl','show',slice,'--property=ControlGroup','--value'],text=True).strip()
assert cgroup.startswith('/') and cgroup!='/'
data=dict(attempt_id=attempt,unit=unit,slice=slice,cgroup=cgroup,docker_endpoint=endpoint,
          daemon_id=open(state+'/daemon-id').read().strip(),init_pid=pid,
          namespaces={k:os.readlink(f'/proc/{pid}/ns/{v}') for k,v in names.items()},
          host_namespaces={k:os.readlink('/proc/self/ns/'+v) for k,v in names.items()})
json.dump(data,open(path,'w'),indent=2)
PY
  verify
  trap - EXIT
  cat "$identity"
  ;;
exec)
  [ "$#" -gt 0 ] || { echo 'exec requires a command' >&2; exit 2; }
  verify
  pid=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["init_pid"])' "$identity")
  scope="zora-qa-exec-$attempt-$$.scope"
  exec systemd-run --quiet --scope --collect --unit="$scope" --slice="$slice" \
    nsenter --target "$pid" --mount --net --pid --uts --ipc \
    env -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
      "DOCKER_HOST=$endpoint" "DOCKER_CONFIG=$state/home/.docker" "HOME=$state/home" \
      "CODEX_HOME=$state/home/.codex" "XDG_CACHE_HOME=$state/home/.cache" \
      "XDG_CONFIG_HOME=$state/home/.config" "XDG_DATA_HOME=$state/home/.local/share" \
      "XDG_STATE_HOME=$state/home/.local/state" "XDG_RUNTIME_DIR=/run/qa-runtime" \
      QA_SANDBOX=1 QA_INSIDE_NETNS=1 \
      bash "$self" __exec "$dir" "$@"
  ;;
__exec)
  check
  # OpenSSH otherwise resolves ~ from passwd, ignoring HOME for root's default key paths.
  if [ -d "$HOME/.ssh" ] && [ -z "${GIT_SSH_COMMAND:-}" ]; then
    export GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=$HOME/.ssh/known_hosts"
    for key in "$HOME"/.ssh/id_*; do
      [[ "$key" = *.pub || ! -f "$key" ]] || GIT_SSH_COMMAND="$GIT_SSH_COMMAND -i $key"
    done
  fi
  exec "$@"
  ;;
verify) verify ;;
check) check ;;
down) down ;;
*) echo 'unknown sandbox action' >&2; exit 2 ;;
esac
