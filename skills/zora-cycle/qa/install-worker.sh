#!/usr/bin/env bash
# Run on Linux as root: install-worker.sh /absolute/qa-home /path/to/worker.json
set -euo pipefail
[ "$EUID" = 0 ] || { echo 'worker installation requires root' >&2; exit 1; }
[ "$#" = 2 ] || { echo 'usage: install-worker.sh QA_HOME WORKER_CONFIG' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
home=$1
config=$2
[[ "$home" =~ ^/[A-Za-z0-9_./-]+$ ]] && [[ "$home" != / ]] && [[ "$home" != *..* ]] || exit 2
for tool in python3 systemd-run systemctl ip iptables kind docker flock nsenter; do command -v "$tool" >/dev/null; done
mkdir -p "$home/slots" "$home/jobs" "$home/backups"
exec 9> "$home/.admission.lock"
flock 9
# Reconfiguration is a drain operation: neither active units nor retained ownership may exist.
if systemctl list-units 'zora-qa-*.service' --state=active,activating,deactivating --no-legend | grep -q .; then
  echo 'drain active QA attempts before installing' >&2; exit 1
fi
if find "$home/slots" -maxdepth 1 -name '*.json' -print -quit | grep -q .; then
  echo 'release or reconcile reserved/retained slots before installing' >&2; exit 1
fi
# Validate in a temporary QA home before replacing a working installation.
check=$(mktemp -d)
trap 'rm -rf "$check"' EXIT
cp "$config" "$check/worker.json"
python3 "$here/qa-worker.py" --home "$check" check
stamp=$(date -u +%Y%m%dT%H%M%SZ)
for file in qa-worker.py worker.json; do
  if [ -f "$home/$file" ]; then cp -p "$home/$file" "$home/backups/$file.$stamp"; fi
done
install -m 700 "$here/qa-worker.py" "$home/qa-worker.py.new"
install -m 600 "$config" "$home/worker.json.new"
mv "$home/qa-worker.py.new" "$home/qa-worker.py"
mv "$home/worker.json.new" "$home/worker.json"
cat > /etc/systemd/system/zora-qa-reaper.service <<EOF
[Unit]
Description=Reconcile orphaned Zora QA environments
After=docker.service
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 $home/qa-worker.py --home $home reap
EOF
cat > /etc/systemd/system/zora-qa-reaper.timer <<'EOF'
[Unit]
Description=Periodically reconcile orphaned Zora QA environments
[Timer]
OnBootSec=60
OnUnitActiveSec=60
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now zora-qa-reaper.timer
python3 "$home/qa-worker.py" --home "$home" check
