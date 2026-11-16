#!/usr/bin/env bash
# Reproducible fault timeline. Baseline, then the three failure modes, then heal.
# Usage: lab/scenario.sh [branches] [baseline_minutes]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ROOT="$(dirname "${HERE}")"
BRANCHES="${1:-12}"; BASELINE_MIN="${2:-10}"
cd "${ROOT}"
rm -f data/telemetry/*.csv data/faults.csv
.venv/bin/python -m ipseclab.gen --branches "${BRANCHES}" --profile healthy --out lab/generated
( cd lab/generated && docker compose down -t 1 >/dev/null 2>&1 || true; docker compose up -d )
echo "[$(date -u +%H:%M:%S)] lab up, settling 60s"; sleep 60
echo "timestamp,event,node,detail" > data/faults.csv
echo "$(date -u +%Y-%m-%dT%H:%M:%S+00:00),baseline-start,all," >> data/faults.csv
sleep $(( BASELINE_MIN * 60 ))
lab/inject.sh dpd-flap branch3 30 30 40
sleep 180
lab/inject.sh mtu-blackhole branch5
sleep 180
lab/inject.sh mss-clamp branch5 1360
sleep 180
lab/inject.sh rekey-storm
sleep 360
lab/inject.sh heal all
sleep 240
echo "$(date -u +%Y-%m-%dT%H:%M:%S+00:00),scenario-end,all," >> data/faults.csv
echo "[$(date -u +%H:%M:%S)] scenario complete"
