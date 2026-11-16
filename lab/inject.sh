#!/usr/bin/env bash
# Fault injection for the lab. Every fault is logged to data/faults.csv with a
# UTC timestamp so the detector's lead time can be measured against ground truth.
#
#   lab/inject.sh dpd-flap <branchN> [seconds]      70% underlay loss, DPD tears the SA down
#   lab/inject.sh mtu-blackhole <branchN>           drop the gateway's own ICMP frag-needed
#   lab/inject.sh mss-clamp <branchN> [mss]         the fix for the blackhole
#   lab/inject.sh rekey-storm                       align every lifetime, force simultaneous rekeys
#   lab/inject.sh heal <branchN|all>                remove impairments, back to healthy profile
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "${HERE}")"
FAULTS="${ROOT}/data/faults.csv"
PY="${ROOT}/.venv/bin/python"
BRANCHES="$(grep -c '^  branch' "${HERE}/generated/docker-compose.yml" || echo 0)"

mark() { # event node detail
    [[ -f "${FAULTS}" ]] || echo "timestamp,event,node,detail" > "${FAULTS}"
    echo "$(date -u +%Y-%m-%dT%H:%M:%S+00:00),$1,$2,$3" >> "${FAULTS}"
    echo "[$(date -u +%H:%M:%S)] $1 $2 $3"
}

wan_delay() { docker exec "$1" sh -c 'echo "${WAN_DELAY_MS:-20}"'; }

case "${1:-}" in
    dpd-flap)
        # A link that degrades before it dies: 30% loss, then 70%, then 100%, in
        # BOTH directions (netem on egress, iptables statistic on ingress). DPD
        # only gives up during the 100% stage; the detector should fire earlier.
        node="${2:?branch}"; d="$(wan_delay "${node}")"
        s1="${3:-30}"; s2="${4:-30}"; s3="${5:-40}"
        mark dpd-flap-start "${node}" "loss ramp 30%/${s1}s 70%/${s2}s 100%/${s3}s both directions"
        for stage in "30 ${s1}" "70 ${s2}" "100 ${s3}"; do
            set -- ${stage}; pct="$1"; secs="$2"
            docker exec "${node}" tc qdisc change dev eth0 root netem delay "${d}ms" "$((d/10+1))ms" loss "${pct}%"
            docker exec "${node}" iptables -t raw -F PREROUTING
            docker exec "${node}" iptables -t raw -A PREROUTING -i eth0 -m statistic --mode random --probability "0.${pct}" -j DROP 2>/dev/null \
                || docker exec "${node}" iptables -t raw -A PREROUTING -i eth0 -j DROP   # 0.100 is not a probability; 100% = drop all
            mark dpd-flap-stage "${node}" "loss=${pct}%"
            sleep "${secs}"
        done
        docker exec "${node}" iptables -t raw -F PREROUTING
        docker exec "${node}" tc qdisc change dev eth0 root netem delay "${d}ms" "$((d/10+1))ms"
        mark dpd-flap-end "${node}" ""
        ;;
    mtu-blackhole)
        node="${2:?branch}"
        docker exec "${node}" iptables -I OUTPUT -p icmp --icmp-type fragmentation-needed -j DROP
        mark mtu-blackhole-start "${node}" "ICMP frag-needed dropped at gateway"
        ;;
    mss-clamp)
        node="${2:?branch}"; mss="${3:-1360}"
        # both directions: the SYN leaving through the tunnel AND the SYN-ACK
        # arriving from it carry an MSS, and the far side's value is the one
        # that governs what *this* client sends
        docker exec "${node}" iptables -t mangle -I FORWARD -o vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "${mss}"
        docker exec "${node}" iptables -t mangle -I FORWARD -i vti+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "${mss}"
        mark mss-clamp "${node}" "mss=${mss}"
        ;;
    rekey-storm)
        # Reload the storm profile on the HUB only (responder config can be
        # replaced without closing SAs; a branch reload would restart tunnels),
        # then force every branch to rekey its IKE SA in the same second. New SAs
        # inherit the hub's 90 s / 300 s lifetimes with zero jitter, so from here
        # on all twelve rekey together, every time.
        "${PY}" -m ipseclab.gen --branches "${BRANCHES}" --profile storm --out "${HERE}/generated" >/dev/null
        docker exec hub swanctl --load-all >/dev/null
        mark rekey-storm-start all "hub lifetimes 90s/300s with zero jitter; forced simultaneous IKE rekey on ${BRANCHES} branches"
        for i in $(seq 1 "${BRANCHES}"); do docker exec "branch${i}" swanctl --rekey --ike hub >/dev/null 2>&1 & done
        wait
        ;;
    heal)
        node="${2:-all}"
        if [[ "${node}" == "all" ]]; then nodes=$(seq -f "branch%g" 1 "${BRANCHES}"); else nodes="${node}"; fi
        "${PY}" -m ipseclab.gen --branches "${BRANCHES}" --profile healthy --out "${HERE}/generated" >/dev/null
        for n in ${nodes}; do
            d="$(wan_delay "${n}")"
            docker exec "${n}" tc qdisc change dev eth0 root netem delay "${d}ms" "$((d/10+1))ms" 2>/dev/null || true
            docker exec "${n}" iptables -D OUTPUT -p icmp --icmp-type fragmentation-needed -j DROP 2>/dev/null || true
            docker exec "${n}" iptables -t mangle -F FORWARD 2>/dev/null || true
            docker exec "${n}" iptables -t raw -F PREROUTING 2>/dev/null || true
        done
        docker exec hub swanctl --load-all >/dev/null
        mark heal "${node}" "healthy profile reloaded; impairments removed"
        ;;
    *)
        sed -n '2,10p' "$0"; exit 1 ;;
esac
