#!/usr/bin/env bash
# Opt-in live Linux test: creates two disposable Kind clusters, then deletes only them.
# QA_NODE_IMAGE must name an explicitly selected, preferably digest-pinned node image.
set -euo pipefail
umask 077
: "${QA_NODE_IMAGE:?set QA_NODE_IMAGE to the pinned worker Kind node image}"
if [ "$(id -u)" != 0 ]; then echo 'Run as root on the QA worker (network namespaces require root).' >&2; exit 2; fi
for tool in kind docker ip iptables nsenter kubectl curl python3 timeout; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool" >&2; exit 2; }
done
helper="$(cd "$(dirname "$0")" && pwd)/qa-network.sh"
proof_dir=$(mktemp -d /tmp/zora-network-proof.XXXXXXXX)
# Two adjacent /30 networks; helper refuses overlap with any existing host route.
base="${QA_PROOF_SUBNET:-10.77.249.0/29}"
mapfile -t addresses < <(python3 - "$base" <<'PY'
import ipaddress,sys
network=ipaddress.ip_network(sys.argv[1],strict=True)
if network.version != 4 or network.prefixlen != 29:
    raise SystemExit('QA_PROOF_SUBNET must be an aligned IPv4 /29')
for subnet in network.subnets(new_prefix=30):
    print(*subnet.hosts())
PY
)
[ "${#addresses[@]}" = 2 ] || { echo 'invalid proof address pool' >&2; exit 2; }
token=$(basename "$proof_dir" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | tail -c 16)
namespaces=("qa-${token}-a" "qa-${token}-b")
clusters=("zora-qa-${token}-a" "zora-qa-${token}-b")
owned=(0 0)
network() {
  local index="$1" host peer
  read -r host peer <<< "${addresses[$index]}"
  QA_NS="${namespaces[$index]}" QA_HOST_IP="$host" QA_PEER_IP="$peer" timeout --signal=TERM --kill-after=10 45 bash "$helper" "$2"
}
cleanup() {
  local result=$? i remaining
  trap - EXIT INT TERM
  for i in 0 1; do
    if [ "${owned[$i]}" = 1 ]; then
      timeout 90 kind delete cluster --name "${clusters[$i]}" || result=1
      network "$i" down || result=1
      remaining=$(timeout 20 docker ps -aq --filter "label=io.x-k8s.kind.cluster=${clusters[$i]}") || result=1
      if [ -n "$remaining" ]; then result=1; fi
    fi
  done
  printf 'cleanup_complete exit=%s evidence=%s\n' "$result" "$proof_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec > >(tee "$proof_dir/proof.log") 2>&1
printf 'proof=%s address_pool=%s image=%s\n' "$proof_dir" "$base" "$QA_NODE_IMAGE"
for i in 0 1; do
  if ip netns list | awk '{print $1}' | grep -qx "${namespaces[$i]}"; then echo 'namespace collision'; exit 1; fi
  if [ -n "$(docker ps -aq --filter "label=io.x-k8s.kind.cluster=${clusters[$i]}")" ]; then echo 'cluster collision'; exit 1; fi
  owned[$i]=1
  network "$i" up
  mkdir -p "$proof_dir/$i/www"
  printf 'environment-%s\n' "$i" > "$proof_dir/$i/www/index.html"
  ip netns exec "${namespaces[$i]}" python3 -m http.server 5173 --bind 127.0.0.1 \
    --directory "$proof_dir/$i/www" > "$proof_dir/$i/http.log" 2>&1 &
  read -r host peer <<< "${addresses[$i]}"
  python3 - "$proof_dir/$i/kind.json" "$host" <<'PY'
import json,sys
json.dump({'kind':'Cluster','apiVersion':'kind.x-k8s.io/v1alpha4',
           'networking':{'apiServerAddress':sys.argv[2]}},open(sys.argv[1],'w'))
PY
  timeout --signal=TERM --kill-after=30 300 ip netns exec "${namespaces[$i]}" nsenter --net=/proc/1/ns/net \
    kind create cluster --name "${clusters[$i]}" --image "$QA_NODE_IMAGE" \
    --config "$proof_dir/$i/kind.json" --kubeconfig "$proof_dir/$i/kubeconfig" --wait 180s
  ip netns exec "${namespaces[$i]}" kubectl --kubeconfig "$proof_dir/$i/kubeconfig" --request-timeout=30s get --raw=/readyz
  ip netns exec "${namespaces[$i]}" kubectl --kubeconfig "$proof_dir/$i/kubeconfig" --request-timeout=30s \
    create configmap parallel-proof --from-literal="sentinel=environment-$i"
done
for i in 0 1; do
  response=$(ip netns exec "${namespaces[$i]}" curl -fsS --max-time 10 http://127.0.0.1:5173/)
  value=$(ip netns exec "${namespaces[$i]}" kubectl --kubeconfig "$proof_dir/$i/kubeconfig" --request-timeout=30s \
    get configmap parallel-proof -o 'jsonpath={.data.sentinel}')
  test "$response" = "environment-$i"
  test "$value" = "environment-$i"
  printf 'isolated %s HTTP=%s configmap=%s\n' "$i" "$response" "$value"
done
timeout 90 kind delete cluster --name "${clusters[0]}"
network 0 down
ip netns exec "${namespaces[1]}" kubectl --kubeconfig "$proof_dir/1/kubeconfig" --request-timeout=30s get --raw=/readyz
remaining=$(ip netns exec "${namespaces[1]}" curl -fsS --max-time 10 http://127.0.0.1:5173/)
test "$remaining" = environment-1
value=$(ip netns exec "${namespaces[1]}" kubectl --kubeconfig "$proof_dir/1/kubeconfig" --request-timeout=30s \
  get configmap parallel-proof -o 'jsonpath={.data.sentinel}')
test "$value" = environment-1
printf '\nPASS: deleting A preserved B cluster and same-port HTTP server\n'
