#!/usr/bin/env python3
"""Per-node tunnel telemetry. Runs inside every lab container.

Every `interval` seconds it writes one CSV row per child SA with:
  * SA counters from vici (bytes, packets, age, seconds to rekey),
  * IKE/child rekey and up/down events since the last tick (vici event stream),
  * IKE retransmits since the last tick (counted from charon.log),
  * charon CPU %, measured from /proc/<pid>/stat,
  * probes from the LAN client namespace: small-ping RTT/jitter/loss, a
    PMTUD probe (does a too-big DF packet get an ICMP answer?), and a short
    TCP bulk transfer (does a large-packet flow actually complete?).

Nothing here is specific to the lab. Point it at any strongSwan node.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import vici

CLK_TCK = os.sysconf("SC_CLK_TCK")
FIELDS = [
    "timestamp", "node", "role", "peer", "ike_state", "child_state",
    "bytes_in", "bytes_out", "pkts_in", "pkts_out", "sa_age_s", "rekey_in_s",
    "ike_rekeys", "child_rekeys", "ike_ups", "ike_downs", "child_downs", "ike_retransmits",
    "charon_cpu_pct", "rtt_ms", "jitter_ms", "loss_pct", "pmtud_ok", "tcp_bulk_ok", "tcp_bulk_mbps",
]


class EventCounter(threading.Thread):
    """Counts vici events in the background; drained by the sampler each tick."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.counts: Counter = Counter()

    def run(self):
        while True:
            try:
                s = vici.Session()
                for label, msg in s.listen(["ike-updown", "child-updown", "ike-rekey", "child-rekey"]):
                    key = label.decode() if isinstance(label, bytes) else label
                    if key in ("ike-updown", "child-updown"):
                        up = msg.get("up", b"no") == b"yes"
                        key = key.replace("updown", "up" if up else "down")
                    with self.lock:
                        self.counts[key] += 1
            except Exception:
                time.sleep(1)

    def drain(self) -> Counter:
        with self.lock:
            c, self.counts = self.counts, Counter()
        return c


class LogCounter:
    """Counts 'retransmit N of request' lines appended to charon.log since last call."""

    RE = re.compile(r"retransmit \d+ of request")

    def __init__(self, path: Path):
        self.path = path
        self.pos = 0

    def drain(self) -> int:
        try:
            with self.path.open("rb") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
        except FileNotFoundError:
            return 0
        return len(self.RE.findall(chunk.decode(errors="replace")))


class CpuMeter:
    def __init__(self):
        self.pid = None
        self.last = None

    def _find(self):
        out = subprocess.run(["pidof", "charon"], capture_output=True, text=True).stdout.split()
        self.pid = int(out[0]) if out else None

    def sample(self) -> float:
        if self.pid is None:
            self._find()
        if self.pid is None:
            return 0.0
        try:
            parts = Path(f"/proc/{self.pid}/stat").read_text().split()
            ticks = int(parts[13]) + int(parts[14])
        except (FileNotFoundError, IndexError):
            self.pid = None
            return 0.0
        now = time.monotonic()
        if self.last is None:
            self.last = (ticks, now)
            return 0.0
        dt = now - self.last[1]
        cpu = (ticks - self.last[0]) / CLK_TCK / dt * 100 if dt > 0 else 0.0
        self.last = (ticks, now)
        return round(cpu, 1)


def run_ns(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(["ip", "netns", "exec", "lan", *cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, ""


def probe_ping(target: str, client_ip: str) -> tuple[float, float, float]:
    rc, out = run_ns(["ping", "-c", "4", "-i", "0.2", "-W", "1", "-I", client_ip, target], timeout=6)
    m = re.search(r"(\d+)% packet loss", out)
    loss = float(m.group(1)) if m else 100.0
    m = re.search(r"(?:rtt|round-trip)[^=]*= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    if m:
        return float(m.group(2)), float(m.group(4)), loss
    return float("nan"), float("nan"), loss


def flush_pmtu_cache() -> None:
    """The client caches the path MTU for 10 minutes after one ICMP frag-needed.
    Flushing before each probe makes every probe test the ICMP path afresh,
    which is what a *new* flow or a new destination experiences."""
    run_ns(["ip", "route", "flush", "cache"], timeout=3)


def probe_pmtud(target: str, client_ip: str) -> int:
    """1 if a 1500-byte DF probe gets *any* answer (echo reply or ICMP frag-needed), 0 if it vanishes."""
    flush_pmtu_cache()
    rc, out = run_ns(["ping", "-c", "1", "-W", "2", "-M", "do", "-s", "1472", "-I", client_ip, target], timeout=4)
    if "bytes from" in out or "Frag needed" in out or "frag needed" in out.lower() or "too long" in out.lower():
        return 1
    return 0


def probe_tcp_bulk(target: str, client_ip: str, port: int) -> tuple[int, float]:
    """Short iperf3 transfer; a blackholed path hangs and times out."""
    flush_pmtu_cache()
    rc, out = run_ns(["iperf3", "-c", target, "-p", str(port), "-B", client_ip, "-t", "1", "-J", "--connect-timeout", "2000"], timeout=8)
    if rc != 0:
        return 0, 0.0
    m = re.search(r'"sum_sent":\s*{[^}]*"bits_per_second":\s*([\d.e+]+)', out, re.S)
    mbps = float(m.group(1)) / 1e6 if m else 0.0
    return (1 if mbps > 0.01 else 0), round(mbps, 2)


def _s(d: dict, key: str, default: str = "?") -> str:
    v = d.get(key, default)
    return v.decode() if isinstance(v, bytes) else str(v)


def _i(d: dict, key: str) -> int:
    try:
        return int(_s(d, key, "0"))
    except ValueError:
        return 0


def list_sas() -> list[dict]:
    """vici returns str keys and bytes values; flatten to one row per child SA."""
    rows = []
    try:
        s = vici.Session()
        for ike in s.list_sas():
            for ike_name, ike_d in ike.items():
                peer = _s(ike_d, "remote-id")
                ike_state = _s(ike_d, "state")
                children = ike_d.get("child-sas") or {}
                if not children:
                    rows.append({"peer": peer, "ike_state": ike_state, "child_state": "NONE"})
                for _, ch in children.items():
                    rows.append(
                        {
                            "peer": peer,
                            "ike_state": ike_state,
                            "child_state": _s(ch, "state"),
                            "bytes_in": _i(ch, "bytes-in"),
                            "bytes_out": _i(ch, "bytes-out"),
                            "pkts_in": _i(ch, "packets-in"),
                            "pkts_out": _i(ch, "packets-out"),
                            "sa_age_s": _i(ch, "install-time"),
                            "rekey_in_s": _i(ch, "rekey-time"),
                        }
                    )
    except Exception:
        pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--lan-ip", required=True)
    ap.add_argument("--client-ip", required=True)
    ap.add_argument("--hub-lan", required=True)
    ap.add_argument("--iperf-port", type=int, default=5201)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    events = EventCounter()
    events.start()
    logc = LogCounter(Path("/var/log/charon.log"))
    cpu = CpuMeter()
    out = Path(args.out)
    new = not out.exists()
    f = out.open("a", newline="")
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if new:
        w.writeheader()
        f.flush()

    tick = 0
    while True:
        t0 = time.monotonic()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ev = events.drain()
        retrans = logc.drain()
        cpu_pct = cpu.sample()
        sas = list_sas() or [{"peer": "none", "ike_state": "NONE", "child_state": "NONE"}]

        if args.role == "branch":
            rtt, jit, loss = probe_ping(args.hub_lan, args.client_ip)
            pmtud = probe_pmtud(args.hub_lan, args.client_ip)
            # the bulk probe is heavier; run it every other tick
            if tick % 2 == 0:
                tcp_ok, mbps = probe_tcp_bulk(args.hub_lan, args.client_ip, args.iperf_port)
            else:
                tcp_ok, mbps = float("nan"), float("nan")
        else:
            rtt = jit = loss = float("nan")
            pmtud = tcp_ok = mbps = float("nan")

        for sa in sas:
            row = {k: sa.get(k, float("nan")) for k in FIELDS}
            row.update(
                timestamp=ts, node=args.node, role=args.role,
                ike_rekeys=ev["ike-rekey"], child_rekeys=ev["child-rekey"],
                ike_ups=ev["ike-up"], ike_downs=ev["ike-down"], child_downs=ev["child-down"],
                ike_retransmits=retrans, charon_cpu_pct=cpu_pct,
                rtt_ms=rtt, jitter_ms=jit, loss_pct=loss, pmtud_ok=pmtud, tcp_bulk_ok=tcp_ok, tcp_bulk_mbps=mbps,
            )
            w.writerow(row)
        f.flush()
        tick += 1
        time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
