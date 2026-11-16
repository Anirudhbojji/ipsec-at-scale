# IPsec at scale, without the pain

A Docker Compose lab that reproduces the three things that go wrong with IPsec
past a few dozen tunnels (rekey storms, MTU blackholes, DPD flaps), plus a
per-tunnel telemetry sidecar and an anomaly detector that flags a tunnel in
trouble before dead peer detection does.

Everything is strongSwan 5.9 with real ESP in the kernel. No cloud account needed.

## Layout

```
lab/image/        Dockerfile, entrypoint.sh (VTI + charon + probes), telemetry.py (sidecar)
lab/inject.sh     fault injection: dpd-flap | mtu-blackhole | mss-clamp | rekey-storm | heal
lab/scenario.sh   the full reproducible timeline: baseline, three faults, heal
ipseclab/gen.py   writes lab/generated/docker-compose.yml and one swanctl.conf per node
ipseclab/detect.py  features, z-score and IsolationForest detectors, lead-time evaluation
ipseclab/triage.py  turns detector alarms into a runbook match + plain-English diagnosis (Gemini)
ipseclab/runbooks.yaml  the runbooks the triage agent may match, with pre-approved reversible actions
data/telemetry/   one CSV per node, one row per child SA every 5 s
data/faults.csv   ground truth written by inject.sh
```

## Run it

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -e .
docker build -t ipsec-lab:latest lab/image

# hub + 12 branches, healthy profile
python -m ipseclab.gen --branches 12
(cd lab/generated && docker compose up -d)

# look around
docker exec hub swanctl --list-sas
docker exec branch3 ip netns exec lan ip route flush cache
docker exec branch3 ip netns exec lan ping -M do -s 1472 10.100.0.100   # PMTUD probe: expect "Frag needed"
tail -f data/telemetry/branch3.csv

# break things
lab/inject.sh dpd-flap branch3 45
lab/inject.sh mtu-blackhole branch5
lab/inject.sh mss-clamp branch5 1360
lab/inject.sh rekey-storm
lab/inject.sh heal all

# or run the whole scripted timeline (about 35 minutes)
lab/scenario.sh 12 8

# score it
python -m ipseclab.detect --data data
```

## Why VTI and not xfrm interfaces

Docker Desktop's LinuxKit kernel is built without `CONFIG_XFRM_INTERFACE`, so
`ip link add ... type xfrm` fails. VTI (`type vti`, keyed by packet mark) is
compiled in and works. The strongSwan config differs only in `mark_in`/`mark_out`
versus `if_id_in`/`if_id_out`. On a normal Linux host use xfrm interfaces.

## Notes

- Lifetimes in the healthy profile are 100x shorter than production so that a
  40-minute run contains hundreds of rekeys. The ratios (`rand_time` vs
  `rekey_time`) are what carries over.
- The PSK is in `ipseclab/gen.py` and is not a secret. Do not reuse any of this
  config in production without replacing it.

## The jitter gotcha

charon seeds the generator behind `rand_time` with `srandom(time(NULL) + getpid())`
(`src/libcharon/daemon.c`). Containers, pods or VMs that start charon in the same
second with the same PID get identical "random" jitter and rekey in lockstep. The
entrypoint sleeps a `/dev/urandom`-derived 0 to 11 s before starting charon on each
branch for that reason. `data_runs/run2-same-seed/` keeps the telemetry and charon
logs from a run without that stagger, where eight of twelve tunnels rekeyed in the
same second during the baseline.

## Triage agent

`ipseclab/triage.py` sits behind the z-score detector. It groups alarm ticks
into incidents (a fleet-wide storm is one page, not twelve), builds a signature
per incident (which features moved, by how much against that tunnel's own
baseline, tunnel state, hub context, what the next 30 s looked like), and asks
Gemini to pick a runbook from `ipseclab/runbooks.yaml`, cite evidence, rule out
the alternatives, and recommend one pre-approved action. It never executes
anything. `data/faults.csv` is used only to grade the answer afterwards; the
model does not see it.

```bash
uv pip install -e ".[triage]"
cp .env.example .env            # put your GEMINI_API_KEY in it (.env is gitignored)
python -m ipseclab.triage --dry-run      # signatures + prompts, no API call
python -m ipseclab.triage                # scenario window, default model gemini-3.8-flash
python -m ipseclab.triage --window all   # every alarm recorded since, including the unlabelled ones
```

On the recorded run it matched the runbook on 3 of 3 labelled incidents
(`data/results/triage.md`). That is a small, easy test; see the article for
what it does and does not show.
