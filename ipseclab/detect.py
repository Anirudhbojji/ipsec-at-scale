"""Anomaly detection on per-tunnel telemetry.

Two detectors, same features, same ground truth:

  * zscore   per-branch robust z-score against the healthy baseline, alarm when
             any feature is more than `z` scaled MADs away for `persist` ticks
  * iforest  an IsolationForest fitted on the pooled healthy baseline, alarm when
             the anomaly score exceeds the 99.5th percentile of baseline scores
             for `persist` ticks

Features are 60-second rolling aggregates over the 5-second telemetry so that a
single lost probe does not page anyone. Hub-side aggregates (rekeys, CPU,
retransmits across all peers) are joined onto every branch row, because a rekey
storm is only visible as a storm from the hub.

Ground truth comes from data/faults.csv written by lab/inject.sh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import click
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

WINDOW = 12  # ticks of 5 s = 60 s

# pps_out was dropped: the probes themselves dominate packet rate and it fired
# on every post-reconnect throughput change. hub_retrans_sum was dropped from
# the per-tunnel feature set: one flapping peer's retransmits on the hub made
# every other tunnel alarm. Both are still computed for inspection.
BRANCH_FEATURES = [
    "rtt_mean", "rtt_max", "jitter_mean", "loss_mean", "pmtud_min", "tcp_min",
    "retrans_sum", "rekey_sum", "down_sum",
]
HUB_FEATURES = ["hub_rekey_sum", "hub_rekey_burst", "hub_cpu_max"]
EXTRA = ["pps_out", "hub_retrans_sum"]
FEATURES = BRANCH_FEATURES + HUB_FEATURES


# --------------------------------------------------------------------------- load
def load_telemetry(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    for f in sorted((data_dir / "telemetry").glob("*.csv")):
        d = pd.read_csv(f, parse_dates=["timestamp"])
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    hub = df[df["role"] == "hub"].copy()
    br = df[df["role"] == "branch"].copy()
    return br, hub


def load_faults(data_dir: Path) -> pd.DataFrame:
    f = pd.read_csv(data_dir / "faults.csv", parse_dates=["timestamp"], engine="python", on_bad_lines=lambda r: r[:3] + [",".join(r[3:])])
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    return f


# ----------------------------------------------------------------------- features
def hub_aggregates(hub: pd.DataFrame) -> pd.DataFrame:
    """Hub telemetry has one row per peer per tick; collapse to one row per tick."""
    h = hub.copy()
    h["tick"] = h["timestamp"].dt.floor("5s")
    g = h.groupby("tick").agg(
        hub_rekeys=("ike_rekeys", "max"),       # event counts are per tick, repeated per peer row
        hub_child_rekeys=("child_rekeys", "max"),
        hub_cpu=("charon_cpu_pct", "max"),
        hub_retrans=("ike_retransmits", "max"),
    )
    g["hub_rekeys_all"] = g["hub_rekeys"] + g["hub_child_rekeys"]
    g["hub_rekey_sum"] = g["hub_rekeys_all"].rolling(WINDOW, min_periods=1).sum()
    # a storm is 12 rekeys in ONE tick; 12 rekeys spread over a minute is a quiet Tuesday
    g["hub_rekey_burst"] = g["hub_rekeys_all"].rolling(WINDOW, min_periods=1).max()
    g["hub_cpu_max"] = g["hub_cpu"].rolling(WINDOW, min_periods=1).max()
    g["hub_retrans_sum"] = g["hub_retrans"].rolling(WINDOW, min_periods=1).sum()
    return g[HUB_FEATURES + ["hub_retrans_sum"]].reset_index()


def branch_features(br: pd.DataFrame, hub_agg: pd.DataFrame) -> pd.DataFrame:
    out = []
    for node, g in br.groupby("node"):
        g = g.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        f = pd.DataFrame({"timestamp": g["timestamp"], "node": node})
        # probes: the bulk probe runs every other tick, carry it forward
        tcp = g["tcp_bulk_ok"].ffill()
        f["rtt_mean"] = g["rtt_ms"].rolling(WINDOW, min_periods=3).mean()
        f["rtt_max"] = g["rtt_ms"].rolling(WINDOW, min_periods=3).max()
        f["jitter_mean"] = g["jitter_ms"].rolling(WINDOW, min_periods=3).mean()
        f["loss_mean"] = g["loss_pct"].rolling(WINDOW, min_periods=3).mean()
        f["pmtud_min"] = g["pmtud_ok"].rolling(WINDOW, min_periods=3).min()
        f["tcp_min"] = tcp.rolling(WINDOW, min_periods=3).min()
        f["retrans_sum"] = g["ike_retransmits"].rolling(WINDOW, min_periods=1).sum()
        f["rekey_sum"] = (g["ike_rekeys"] + g["child_rekeys"]).rolling(WINDOW, min_periods=1).sum()
        f["down_sum"] = (g["ike_downs"] + g["child_downs"]).rolling(WINDOW, min_periods=1).sum()
        dt = g["timestamp"].diff().dt.total_seconds()
        pps = g["pkts_out"].diff() / dt
        pps = pps.where(pps >= 0)  # SA rekey resets counters; ignore the negative step
        f["pps_out"] = pps.rolling(WINDOW, min_periods=3).mean()
        f["ike_down_event"] = (g["ike_downs"] > 0).astype(int)
        f["tick"] = g["timestamp"].dt.floor("5s")
        out.append(f)
    feats = pd.concat(out, ignore_index=True)
    feats = feats.merge(hub_agg, on="tick", how="left").drop(columns="tick")
    feats[HUB_FEATURES + ["hub_retrans_sum"]] = feats[HUB_FEATURES + ["hub_retrans_sum"]].ffill()
    return feats.dropna(subset=FEATURES).reset_index(drop=True)


# ---------------------------------------------------------------------- detectors
@dataclass
class Detector:
    name: str
    persist: int = 2

    def fit(self, train: pd.DataFrame) -> "Detector":
        raise NotImplementedError

    def score(self, feats: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def alarms(self, feats: pd.DataFrame) -> pd.Series:
        """Boolean per row: score above threshold for `persist` consecutive ticks on that node."""
        s = pd.Series(self.score(feats) > self.threshold, index=feats.index)
        out = pd.Series(False, index=feats.index)
        for _, idx in feats.groupby("node").indices.items():
            run = s.iloc[idx].astype(int).rolling(self.persist, min_periods=self.persist).sum()
            out.iloc[idx] = (run >= self.persist).to_numpy()
        return out


class ZScoreDetector(Detector):
    def __init__(self, z: float = 6.0, persist: int = 2):
        super().__init__("zscore", persist)
        self.z = z
        self.center: dict[str, pd.Series] = {}
        self.scale: dict[str, pd.Series] = {}
        self.threshold = z

    def fit(self, train: pd.DataFrame) -> "ZScoreDetector":
        for node, g in train.groupby("node"):
            med = g[FEATURES].median()
            mad = (g[FEATURES] - med).abs().median() * 1.4826
            std = g[FEATURES].std()
            # floor the scale so a feature that is almost constant in the baseline
            # (loss, pmtud, retransmits) does not produce infinite z-scores
            floor = pd.Series(
                {"rtt_mean": 1.0, "rtt_max": 5.0, "jitter_mean": 0.5, "loss_mean": 2.0, "pmtud_min": 0.1,
                 "tcp_min": 0.1, "retrans_sum": 1.0, "rekey_sum": 1.0, "down_sum": 0.5, "pps_out": 20.0,
                 "hub_rekey_sum": 2.0, "hub_rekey_burst": 1.0, "hub_cpu_max": 10.0, "hub_retrans_sum": 1.0}
            )
            self.center[node] = med
            self.scale[node] = pd.concat([mad, 0.5 * std, floor], axis=1).max(axis=1)
        return self

    def score(self, feats: pd.DataFrame) -> np.ndarray:
        out = np.zeros(len(feats))
        for node, idx in feats.groupby("node").indices.items():
            c = self.center.get(node)
            if c is None:
                continue
            z = (feats.iloc[idx][FEATURES] - c) / self.scale[node]
            # direction matters: lower pmtud/tcp is bad, higher everything else is bad
            z[["pmtud_min", "tcp_min"]] *= -1
            out[idx] = z.max(axis=1).to_numpy()
        return out


class IForestDetector(Detector):
    def __init__(self, persist: int = 2, quantile: float = 0.995):
        super().__init__("iforest", persist)
        self.quantile = quantile
        self.scaler = RobustScaler()
        self.model = IsolationForest(n_estimators=300, contamination="auto", random_state=0)
        self.threshold = 0.0

    def fit(self, train: pd.DataFrame) -> "IForestDetector":
        X = self.scaler.fit_transform(train[FEATURES])
        self.model.fit(X)
        s = -self.model.decision_function(X)
        self.threshold = float(np.quantile(s, self.quantile))
        return self

    def score(self, feats: pd.DataFrame) -> np.ndarray:
        return -self.model.decision_function(self.scaler.transform(feats[FEATURES]))


# --------------------------------------------------------------------- evaluation
def fault_windows(faults: pd.DataFrame, end: pd.Timestamp) -> list[dict]:
    """Turn the fault log into (fault, node, start, end) windows."""
    wins = []
    ev = faults.sort_values("timestamp").reset_index(drop=True)
    for i, r in ev.iterrows():
        e = r["event"]
        if e.endswith("-start") and e != "baseline-start":
            fault = e[: -len("-start")]
            later = ev.iloc[i + 1:]
            stop = later[(later["event"].isin([f"{fault}-end", "heal"])) & (
                (later["node"] == r["node"]) | (later["node"] == "all") | (r["node"] == "all"))]
            w_end = stop["timestamp"].iloc[0] if len(stop) else end
            wins.append({"fault": fault, "node": r["node"], "start": r["timestamp"], "end": w_end})
    return wins


def evaluate(feats: pd.DataFrame, alarms: pd.Series, faults: pd.DataFrame, train_end: pd.Timestamp) -> list[dict]:
    rows = []
    end = feats["timestamp"].max()
    wins = fault_windows(faults, end)
    nodes = sorted(feats["node"].unique())
    for w in wins:
        targets = nodes if w["node"] == "all" else [w["node"]]
        for node in targets:
            g = feats[(feats["node"] == node) & (feats["timestamp"] >= w["start"]) & (feats["timestamp"] <= w["end"] + pd.Timedelta(seconds=90))]
            a = alarms.loc[g.index]
            first_alarm = g.loc[a, "timestamp"].min() if a.any() else pd.NaT
            down = g.loc[g["ike_down_event"] > 0, "timestamp"].min()
            rows.append({
                "fault": w["fault"], "node": node,
                "detected": bool(a.any()),
                "detect_delay_s": (first_alarm - w["start"]).total_seconds() if pd.notna(first_alarm) else None,
                "dpd_down_s": (down - w["start"]).total_seconds() if pd.notna(down) else None,
                "lead_over_dpd_s": (down - first_alarm).total_seconds() if pd.notna(first_alarm) and pd.notna(down) else None,
            })

    # false positives: alarms on nodes/times outside any fault window (with 3 min grace after)
    in_fault = pd.Series(False, index=feats.index)
    for w in wins:
        m = (feats["timestamp"] >= w["start"]) & (feats["timestamp"] <= w["end"] + pd.Timedelta(minutes=3))
        if w["node"] != "all":
            m &= feats["node"] == w["node"]
        in_fault |= m
    healthy = feats[(~in_fault) & (feats["timestamp"] > train_end)]
    fp_ticks = int(alarms.loc[healthy.index].sum())
    # count distinct episodes (runs) rather than ticks
    episodes = 0
    for _, idx in healthy.groupby("node").indices.items():
        a = alarms.loc[healthy.index[idx]].astype(int).to_numpy()
        episodes += int(((a[1:] == 1) & (a[:-1] == 0)).sum() + (a[:1] == 1).sum())
    hours = len(healthy) * 5 / 3600
    rows.append({"fault": "false_positive", "node": "healthy_time", "fp_episodes": episodes, "fp_ticks": fp_ticks,
                 "healthy_node_hours": round(hours, 2), "fp_per_node_hour": round(episodes / hours, 2) if hours else None})
    return rows


@click.command()
@click.option("--data", type=click.Path(path_type=Path), default=Path("data"), show_default=True)
@click.option("--z", default=6.0, show_default=True, help="z-score alarm threshold")
@click.option("--persist", default=2, show_default=True, help="consecutive ticks above threshold")
@click.option("--out", type=click.Path(path_type=Path), default=Path("data/results"), show_default=True)
def main(data: Path, z: float, persist: int, out: Path):
    """Fit both detectors on the healthy baseline, score the rest, report lead times and false positives."""
    br, hub = load_telemetry(data)
    faults = load_faults(data)
    feats = branch_features(br, hub_aggregates(hub))

    base_start = faults.loc[faults["event"] == "baseline-start", "timestamp"].iloc[0]
    first_fault = faults.loc[faults["event"].str.endswith("-start") & (faults["event"] != "baseline-start"), "timestamp"].min()
    if pd.isna(first_fault):
        first_fault = feats["timestamp"].max()  # no faults yet: everything is baseline
    train = feats[(feats["timestamp"] >= base_start) & (feats["timestamp"] < first_fault)]
    click.echo(f"features: {len(feats)} rows, {feats['node'].nunique()} nodes; baseline {len(train)} rows "
               f"({(first_fault - base_start).total_seconds() / 60:.1f} min)")

    out.mkdir(parents=True, exist_ok=True)
    report = {}
    scored = feats[["timestamp", "node", "ike_down_event"] + FEATURES + EXTRA].copy()
    for det in (ZScoreDetector(z=z, persist=persist), IForestDetector(persist=persist)):
        det.fit(train)
        scored[f"score_{det.name}"] = det.score(feats)
        al = det.alarms(feats)
        scored[f"alarm_{det.name}"] = al
        res = evaluate(feats, al, faults, first_fault)
        report[det.name] = res
        click.echo(f"\n== {det.name} (threshold {det.threshold:.3f}) ==")
        click.echo(pd.DataFrame(res).to_string(index=False))

    scored.to_csv(out / "scored.csv", index=False)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    click.echo(f"\nwrote {out}/scored.csv and {out}/report.json")


if __name__ == "__main__":
    main()
