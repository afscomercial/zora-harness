#!/usr/bin/env bash
# Host-only network lifecycle. All resources are deterministically attempt-owned.
set -euo pipefail
: "${QA_NS:?}" "${QA_HOST_IP:?}" "${QA_PEER_IP:?}"
[[ "$QA_NS" =~ ^qa-[a-zA-Z0-9_-]{1,48}$ ]] || { echo 'invalid namespace name' >&2; exit 2; }
python3 - "$QA_HOST_IP" "$QA_PEER_IP" <<'PY'
import ipaddress, sys
host, peer = map(ipaddress.IPv4Address, sys.argv[1:])
network = ipaddress.ip_network(str(host) + '/30', strict=False)
assert peer in network and host != peer and host in network.hosts() and peer in network.hosts()
PY
suffix=$(printf %s "$QA_NS" | sha256sum | cut -c1-10)
veth="qh$suffix"
peer="qp$suffix"
subnet=$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1]+"/30", strict=False))' "$QA_HOST_IP")
rule() { iptables -w "$@"; }
down() {
  # Stop namespace processes before removing its name. No host-wide pruning.
  if ip netns list | awk '{print $1}' | grep -qx "$QA_NS"; then
    pids=$(ip netns pids "$QA_NS")
    if [ -n "$pids" ]; then
      kill -TERM $pids 2>/dev/null || true
      sleep 1
      kill -KILL $pids 2>/dev/null || true
    fi
    ip netns delete "$QA_NS"
  fi
  ip link delete "$veth" 2>/dev/null || true
  for chain in DOCKER-USER FORWARD; do
    rule -D "$chain" -i "$veth" -s "$subnet" -m comment --comment "$QA_NS" -j ACCEPT 2>/dev/null || true
    rule -D "$chain" -o "$veth" -d "$subnet" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$QA_NS" -j ACCEPT 2>/dev/null || true
  done
  rule -t nat -D POSTROUTING -s "$subnet" -m comment --comment "$QA_NS" -j MASQUERADE 2>/dev/null || true
  rm -f -- "/etc/netns/$QA_NS/resolv.conf"
  rmdir "/etc/netns/$QA_NS" 2>/dev/null || true
  # Deletion errors are harmless only if a read-back proves the resources absent.
  # Query complete tables rather than a possibly missing DOCKER-USER chain.
  local namespaces links filter_rules nat_rules
  namespaces=$(ip netns list) || return 1
  links=$(ip -j link show) || return 1
  filter_rules=$(rule -t filter -S) || return 1
  nat_rules=$(rule -t nat -S) || return 1
  if printf '%s\n' "$namespaces" | awk '{print $1}' | grep -qx "$QA_NS"; then
    echo "namespace survived cleanup: $QA_NS" >&2; return 1
  fi
  if ! python3 -c 'import json,sys; assert not any(x["ifname"] == sys.argv[1] for x in json.load(sys.stdin)), "owned veth survived cleanup"' "$veth" <<< "$links"; then
    return 1
  fi
  if printf '%s\n%s\n' "$filter_rules" "$nat_rules" | grep -F -- "--comment $QA_NS" >/dev/null; then
    echo "owned firewall rules survived cleanup: $QA_NS" >&2; return 1
  fi
  # iptables versions may quote comments; also check that representation.
  if printf '%s\n%s\n' "$filter_rules" "$nat_rules" | grep -F -- "--comment \"$QA_NS\"" >/dev/null; then
    echo "owned firewall rules survived cleanup: $QA_NS" >&2; return 1
  fi
  if [ -e "/etc/netns/$QA_NS" ]; then
    echo "namespace mount configuration survived cleanup: $QA_NS" >&2; return 1
  fi
}
case "${1:-}" in
up)
  [ "$(sysctl -n net.ipv4.ip_forward)" = 1 ] || { echo 'enable IPv4 forwarding on worker' >&2; exit 1; }
  if ip netns list | awk '{print $1}' | grep -qx "$QA_NS"; then echo 'namespace already exists; recovery required' >&2; exit 1; fi
  # Never overlap an existing host/Docker network.
  ip -j route show table all | python3 -c 'import ipaddress,json,sys; n=ipaddress.ip_network(sys.argv[1]); routes=json.load(sys.stdin); assert not any(n.overlaps(ipaddress.ip_network(r["dst"],strict=False)) for r in routes if r.get("dst") not in (None,"default") and ":" not in r["dst"]), "QA subnet overlaps host routes"' "$subnet"
  trap down ERR
  ip netns add "$QA_NS"
  ip link add "$veth" type veth peer name "$peer"
  ip link set "$peer" netns "$QA_NS"
  ip addr add "$QA_HOST_IP/30" dev "$veth"
  ip link set "$veth" up
  ip -n "$QA_NS" addr add "$QA_PEER_IP/30" dev "$peer"
  ip -n "$QA_NS" link set lo up
  ip -n "$QA_NS" link set "$peer" up
  ip -n "$QA_NS" route add default via "$QA_HOST_IP"
  mkdir -p "/etc/netns/$QA_NS"
  # systemd-resolved's loopback stub is unavailable inside a new netns.
  if [ -s /run/systemd/resolve/resolv.conf ]; then cp /run/systemd/resolve/resolv.conf "/etc/netns/$QA_NS/resolv.conf"
  else awk '$1 != "nameserver" || $2 !~ /^127\./' /etc/resolv.conf > "/etc/netns/$QA_NS/resolv.conf"; fi
  grep -q '^nameserver ' "/etc/netns/$QA_NS/resolv.conf" || { echo 'no reachable DNS resolver configured' >&2; false; }
  chain=FORWARD
  if rule -nL DOCKER-USER >/dev/null 2>&1; then chain=DOCKER-USER; fi
  rule -I "$chain" 1 -i "$veth" -s "$subnet" -m comment --comment "$QA_NS" -j ACCEPT
  rule -I "$chain" 1 -o "$veth" -d "$subnet" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$QA_NS" -j ACCEPT
  rule -t nat -A POSTROUTING -s "$subnet" -m comment --comment "$QA_NS" -j MASQUERADE
  ;;
down) down ;;
probe) ip netns exec "$QA_NS" kubectl --kubeconfig "${KUBECONFIG:?}" --request-timeout=30s get --raw=/readyz ;;
*) echo 'usage: qa-network.sh up|down|probe' >&2; exit 2 ;;
esac
