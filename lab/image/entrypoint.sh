#!/bin/bash
# Lab node entrypoint. Role is hub or branch, driven by env vars written by gen.py.
set -euo pipefail

ROLE="${ROLE:?}"            # hub | branch
NODE="${NODE:?}"            # hub | branch3
UNDERLAY_IP="${UNDERLAY_IP:?}"
LAN_IP="${LAN_IP:?}"        # gateway address on the LAN side, e.g. 10.1.3.1
CLIENT_IP="${CLIENT_IP:?}"  # host in the LAN netns, e.g. 10.1.3.100
IPSEC_MTU="${IPSEC_MTU:-1400}"
MSS_CLAMP="${MSS_CLAMP:-}"  # empty = no clamp
PEERS="${PEERS:-}"          # "idx:remote_lan_net:remote_underlay_ip ..." one entry per tunnel
HUB_LAN="${HUB_LAN:-10.100.0.1}"
WAN_DELAY_MS="${WAN_DELAY_MS:-0}"
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-5}"

log() { echo "[$(date -u +%H:%M:%S)] $NODE: $*"; }

# --- underlay impairment: make it look like a WAN ----------------------------
if [[ "${WAN_DELAY_MS}" != "0" ]]; then
    tc qdisc add dev eth0 root netem delay "${WAN_DELAY_MS}ms" "$(( WAN_DELAY_MS / 10 + 1 ))ms"
fi

# --- LAN side: a separate netns with a client host behind this gateway ------
# Forwarded traffic is what hits real MTU blackholes; locally generated traffic
# self-heals via the kernel's own PMTU cache, so the lab needs a real client.
ip netns add lan
ip link add veth-lan type veth peer name veth-cli netns lan
ip addr add "${LAN_IP}/24" dev veth-lan
ip link set veth-lan up
ip netns exec lan ip addr add "${CLIENT_IP}/24" dev veth-cli
ip netns exec lan ip link set veth-cli up
ip netns exec lan ip link set lo up
ip netns exec lan ip route add default via "${LAN_IP}"
sysctl -qw net.ipv4.ip_forward=1

# --- VTI interfaces: one per peer, route-based, mark == vti key -------------
# (xfrm interfaces with if_id are the modern equivalent; Docker Desktop's
#  kernel is built without CONFIG_XFRM_INTERFACE, so the lab uses VTI.)
for entry in ${PEERS}; do
    IFS=: read -r idx net remote <<<"${entry}"
    ifname="vti${idx}"
    ip link add "${ifname}" type vti local "${UNDERLAY_IP}" remote "${remote}" key "${idx}"
    sysctl -qw "net.ipv4.conf.${ifname}.disable_policy=1"
    sysctl -qw "net.ipv4.conf.${ifname}.rp_filter=0"
    ip link set "${ifname}" mtu "${IPSEC_MTU}" up
    ip route add "${net}" dev "${ifname}"
    if [[ -n "${MSS_CLAMP}" ]]; then
        iptables -t mangle -A FORWARD -o "${ifname}" -p tcp --tcp-flags SYN,RST SYN \
            -j TCPMSS --set-mss "${MSS_CLAMP}"
        iptables -t mangle -A FORWARD -i "${ifname}" -p tcp --tcp-flags SYN,RST SYN \
            -j TCPMSS --set-mss "${MSS_CLAMP}"
    fi
done
log "vti interfaces up (mtu=${IPSEC_MTU}, mss_clamp=${MSS_CLAMP:-none}, wan_delay=${WAN_DELAY_MS}ms)"

# --- strongSwan --------------------------------------------------------------
# charon seeds its lifetime-jitter RNG with srandom(time(NULL) + getpid()).
# Twelve containers that start charon in the same second with the same PID
# draw the *same* jitter and rekey in lockstep forever (see the article).
# So: a real random stagger, from /dev/urandom, not bash $RANDOM (which is
# seeded the same way and gave every container the same "random" sleep).
if [[ "${ROLE}" == "branch" ]]; then
    STAGGER="$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 12 ))"
    log "staggering charon start by ${STAGGER}s"
    sleep "${STAGGER}"
fi
: > /var/log/charon.log
/usr/lib/strongswan/charon &
for _ in $(seq 1 50); do [[ -S /var/run/charon.vici ]] && break; sleep 0.2; done
swanctl --load-all >/dev/null
log "charon up, config loaded"

if [[ "${ROLE}" == "hub" ]]; then
    # One iperf3 server per branch (a single server refuses concurrent clients),
    # running in the hub's LAN namespace. A server on the gateway itself would
    # advertise an MSS derived from the tunnel MTU and quietly mask MTU bugs.
    for entry in ${PEERS}; do
        idx="${entry%%:*}"
        ip netns exec lan iperf3 -s -B "${CLIENT_IP}" -p "$(( 5200 + idx ))" -D >/dev/null 2>&1 || true
    done
else
    swanctl --initiate --child net --timeout 30 >/dev/null 2>&1 || true   # start_action=start retries anyway
fi

# --- telemetry sidecar --------------------------------------------------------
IDX="${NODE//[^0-9]/}"
python3 /telemetry.py --node "${NODE}" --role "${ROLE}" --lan-ip "${LAN_IP}" --client-ip "${CLIENT_IP}" \
    --hub-lan "${HUB_LAN}" --iperf-port "$(( 5200 + ${IDX:-0} ))" --interval "${TELEMETRY_INTERVAL}" --out "/data/${NODE}.csv" &

log "ready"
exec tail -F /var/log/charon.log
