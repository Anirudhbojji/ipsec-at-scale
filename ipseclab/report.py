"""Pull the numbers the article charts out of a run: rekeys per tick on the hub,
and per-node anomaly-score timelines around each fault."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pandas as pd


def rekeys_per_tick(data: Path, minutes: int = 8) -> list[dict]:
    hub = pd.read_csv(data / "telemetry" / "hub.csv", parse_dates=["timestamp"])
    faults = pd.read_csv(data / "faults.csv", parse_dates=["timestamp"], engine="python",
                         on_bad_lines=lambda r: r[:3] + [",".join(r[3:])])
    t0 = pd.to_datetime(faults.loc[faults.event == "baseline-start", "timestamp"].iloc[0], utc=True)
    hub["timestamp"] = pd.to_datetime(hub["timestamp"], utc=True)
    h = hub.drop_duplicates("timestamp")
    h = h[(h.timestamp >= t0) & (h.timestamp < t0 + pd.Timedelta(minutes=minutes))]
    return [{"t": int((r.timestamp - t0).total_seconds()), "rekeys": int(r.ike_rekeys + r.child_rekeys)}
            for r in h.itertuples() if r.ike_rekeys + r.child_rekeys > 0]


def timeline(data: Path, node: str, fault_event: str, before: int = 60, after: int = 240) -> dict:
    sc = pd.read_csv(data / "results" / "scored.csv", parse_dates=["timestamp"])
    sc["timestamp"] = pd.to_datetime(sc["timestamp"], utc=True)
    faults = pd.read_csv(data / "faults.csv", parse_dates=["timestamp"], engine="python",
                         on_bad_lines=lambda r: r[:3] + [",".join(r[3:])])
    faults["timestamp"] = pd.to_datetime(faults["timestamp"], utc=True)
    t0 = faults.loc[faults.event == fault_event, "timestamp"].iloc[0]
    g = sc[(sc.node == node) & (sc.timestamp >= t0 - pd.Timedelta(seconds=before)) & (sc.timestamp <= t0 + pd.Timedelta(seconds=after))]
    g = g.drop_duplicates("timestamp")
    rows = [{"t": int((r.timestamp - t0).total_seconds()), "z": round(float(r.score_zscore), 2),
             "iforest": round(float(r.score_iforest), 3), "down": int(r.ike_down_event),
             "loss": round(float(r.loss_mean), 1), "pmtud": int(r.pmtud_min), "burst": int(r.hub_rekey_burst)}
            for r in g.itertuples()]
    marks = [{"event": r.event, "t": int((r.timestamp - t0).total_seconds())} for r in faults.itertuples()
             if -before <= (r.timestamp - t0).total_seconds() <= after and (r.node in (node, "all"))]
    return {"node": node, "fault": fault_event, "rows": rows, "marks": marks}


@click.command()
@click.option("--data", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--out", type=click.Path(path_type=Path), default=None)
def main(data: Path, out: Path | None):
    rep = {
        "rekeys_per_tick": rekeys_per_tick(data),
        "flap": timeline(data, "branch3", "dpd-flap-start"),
        "blackhole": timeline(data, "branch5", "mtu-blackhole-start"),
        "storm": timeline(data, "branch1", "rekey-storm-start", before=120, after=360),
    }
    out = out or data / "results" / "article.json"
    out.write_text(json.dumps(rep, indent=1))
    rk = rep["rekeys_per_tick"]
    click.echo(f"rekeys/tick points: {len(rk)}, max in one tick: {max((r['rekeys'] for r in rk), default=0)}")
    click.echo(f"wrote {out}")


if __name__ == "__main__":
    main()
