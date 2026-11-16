"""Triage agent: turn a detector alarm into a plain-English diagnosis.

The z-score detector says *that* a tunnel is abnormal and *which* features
moved. This module turns that into what an on-call engineer actually wants:
which runbook applies, why, what to do first, and how to prove it worked.

Pipeline:

  1. Re-run the detector on the recorded run (same code as ipseclab.detect)
     to get per-feature z-scores and alarms for every tick.
  2. Group alarm ticks into episodes (per tunnel) and episodes into incidents
     (episodes that start within 30 s of each other are one incident, which is
     how a fleet-wide rekey storm becomes one page instead of twelve).
  3. Build a signature per incident: the features that moved, by how much,
     against each tunnel's own baseline; tunnel state; hub context; and what
     the next 30 s looked like.
  4. Hand the signature plus the runbooks to Gemini and ask for structured
     JSON: runbook, diagnosis, evidence, a pre-approved action, verification.
  5. Grade against data/faults.csv, which the model never sees.

The agent recommends; it never executes. `--dry-run` prints the prompts and
writes the signatures without calling the API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import click
import pandas as pd
import yaml
from pydantic import BaseModel, Field

from ipseclab import detect

INCIDENT_GAP_S = 30      # episodes starting within this of each other are one incident
EPISODE_MERGE_S = 60     # alarm runs on one node closer than this are one episode
LOOKBACK_S = 60
LOOKAHEAD_S = 30         # what the on-call sees by the time they open the page
Z_INTEREST = 3.0         # features below this are "did not move"

FEATURE_GLOSSARY = {
    "rtt_mean": "60 s mean ping RTT through the tunnel, ms",
    "rtt_max": "60 s max ping RTT, ms",
    "jitter_mean": "60 s mean ping jitter, ms",
    "loss_mean": "60 s mean ping loss, percent",
    "pmtud_min": "1 if the 1472-byte DF probe got ANY reply in the last 60 s (an ICMP frag-needed counts as a reply); 0 means total silence",
    "tcp_min": "1 if every 1 s TCP transfer in the last 60 s completed; 0 if one stalled after handshaking",
    "retrans_sum": "IKE retransmits by this tunnel in the last 60 s",
    "rekey_sum": "IKE + child rekeys on this tunnel in the last 60 s",
    "down_sum": "IKE + child SA down events on this tunnel in the last 60 s",
    "hub_rekey_sum": "rekeys seen by the hub across ALL tunnels in the last 60 s",
    "hub_rekey_burst": "largest number of rekeys the hub saw in any single 5 s tick of the last 60 s",
    "hub_cpu_max": "max charon CPU percent on the hub in the last 60 s",
}

EXPECTED_RUNBOOK = {"dpd-flap": "underlay-loss", "mtu-blackhole": "mtu-blackhole", "rekey-storm": "rekey-storm"}


class Triage(BaseModel):
    runbook: str = Field(description="id of the matched runbook, or 'unknown'")
    confidence: Literal["high", "medium", "low"]
    diagnosis: str = Field(description="two or three sentences, plain English, naming the mechanism")
    evidence: list[str] = Field(description="feature names and values from the signature that support the diagnosis")
    ruled_out: list[str] = Field(description="runbooks considered and why they do not fit")
    recommended_action: str = Field(description="exactly one action id from the matched runbook")
    verify: str = Field(description="which feature should change, and by when, if the action worked")
    page_text: str = Field(description="the one-line page for the on-call, under 200 characters")


# --------------------------------------------------------------------- signatures
def per_feature_z(det: detect.ZScoreDetector, feats: pd.DataFrame) -> pd.DataFrame:
    z = pd.DataFrame(0.0, index=feats.index, columns=detect.FEATURES)
    for node, idx in feats.groupby("node").indices.items():
        c = det.center.get(node)
        if c is None:
            continue
        zz = (feats.iloc[idx][detect.FEATURES] - c[detect.FEATURES]) / det.scale[node][detect.FEATURES]
        zz[["pmtud_min", "tcp_min"]] *= -1
        z.iloc[idx] = zz.to_numpy()
    return z


def episodes(feats: pd.DataFrame, alarms: pd.Series) -> list[dict]:
    out = []
    for node, g in feats[alarms].groupby("node"):
        ts = g["timestamp"].sort_values().to_list()
        start, prev = ts[0], ts[0]
        for t in ts[1:]:
            if (t - prev).total_seconds() > EPISODE_MERGE_S:
                out.append({"node": node, "start": start, "end": prev})
                start = t
            prev = t
        out.append({"node": node, "start": start, "end": prev})
    return sorted(out, key=lambda e: e["start"])


def incidents(eps: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for e in eps:
        if groups and (e["start"] - groups[-1][0]["start"]).total_seconds() <= INCIDENT_GAP_S:
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


def node_signature(ep: dict, feats: pd.DataFrame, z: pd.DataFrame, det: detect.ZScoreDetector, raw: pd.DataFrame) -> dict:
    node, t0 = ep["node"], ep["start"]
    g = feats[feats["node"] == node]
    at = g[g["timestamp"] == t0]
    i = at.index[0]
    moved = []
    for f in detect.FEATURES:
        zi = float(z.at[i, f])
        if zi >= Z_INTEREST:
            moved.append({"feature": f, "z": round(zi, 1), "value": round(float(at[f].iloc[0]), 3),
                          "baseline_median": round(float(det.center[node][f]), 3)})
    moved.sort(key=lambda m: -m["z"])
    flat = [f for f in detect.FEATURES if float(z.at[i, f]) < Z_INTEREST]

    r = raw[(raw["node"] == node) & (raw["timestamp"] <= t0)].sort_values("timestamp").tail(1)
    state = {k: (r[k].iloc[0] if len(r) else None) for k in ("ike_state", "child_state", "sa_age_s", "rekey_in_s")}
    state = {k: (int(v) if isinstance(v, (int, float)) and pd.notna(v) else v) for k, v in state.items()}

    ahead = g[(g["timestamp"] > t0) & (g["timestamp"] <= t0 + pd.Timedelta(seconds=LOOKAHEAD_S))]
    next30 = {
        "ike_down_event": bool((ahead["ike_down_event"] > 0).any()),
        "pmtud_min_end": float(ahead["pmtud_min"].iloc[-1]) if len(ahead) else None,
        "tcp_min_end": float(ahead["tcp_min"].iloc[-1]) if len(ahead) else None,
        "loss_mean_end": round(float(ahead["loss_mean"].iloc[-1]), 1) if len(ahead) else None,
        "max_zscore": round(float(z.loc[ahead.index].max(axis=1).max()), 1) if len(ahead) else None,
    }
    return {"node": node, "alarm_at": t0.isoformat(), "features_moved": moved, "features_flat": flat,
            "tunnel_state_at_alarm": state, "next_30s": next30}


def incident_signature(inc: list[dict], feats: pd.DataFrame, z: pd.DataFrame, det: detect.ZScoreDetector,
                       raw: pd.DataFrame, n_nodes: int) -> dict:
    nodes = [node_signature(e, feats, z, det, raw) for e in inc]
    t0 = inc[0]["start"]
    hub = feats[feats["timestamp"] == t0].iloc[0] if (feats["timestamp"] == t0).any() else None
    return {
        "incident_start": t0.isoformat(),
        "tunnels_alarming": len(inc),
        "tunnels_total": n_nodes,
        "fleet_wide": len(inc) >= max(3, n_nodes // 2),
        "hub_at_alarm": {k: round(float(hub[k]), 2) for k in detect.HUB_FEATURES} if hub is not None else {},
        "hub_baseline_max": {k: round(float(feats.loc[feats["timestamp"] < t0, k].max()), 2) for k in detect.HUB_FEATURES},
        "tunnels": nodes,
    }


# -------------------------------------------------------------------------- prompt
SYSTEM = """You are the triage step behind an anomaly detector for a hub-and-spoke IPsec network.
Topology: one strongSwan hub (responder only) and N branch gateways (initiators), IKEv2, one route-based
child SA per branch over a VTI interface with MTU 1400, AES-GCM, modp4096 for DH. Each branch has a LAN
client behind it in its own network namespace; the probes run from that client through the tunnel to a
host behind the hub. Telemetry is one row per tunnel every 5 s; the detector uses 60 s rolling features
and alarms when any feature sits more than 6 robust deviations from that tunnel's own healthy median.

You get: the incident signature (which features moved and by how much, per alarming tunnel, plus hub
context and what happened in the 30 s after the alarm), a feature glossary, and the runbooks.

Rules:
- Match ONE runbook. Base it only on the signature; never invent measurements.
- Cite the actual features and values as evidence. Mention features that stayed flat when that is what
  rules another runbook out.
- recommended_action must be one action id from the matched runbook. Prefer the least invasive.
- A fleet-wide incident (most tunnels alarming within seconds) is a hub-side problem until proven otherwise.
- Write for an on-call engineer at 02:00: short, concrete, mechanism first."""


def build_prompt(sig: dict, runbooks: dict) -> str:
    return (
        "FEATURE GLOSSARY\n" + json.dumps(FEATURE_GLOSSARY, indent=1) +
        "\n\nRUNBOOKS\n" + yaml.safe_dump(runbooks, sort_keys=False) +
        "\n\nINCIDENT SIGNATURE\n" + json.dumps(sig, indent=1, default=str) +
        "\n\nRespond with the JSON object described by the schema."
    )


def load_api_key(root: Path) -> str | None:
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(k):
            return os.environ[k]
    env = root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                    return v.strip().strip('"').strip("'")
    return None


FALLBACK_MODELS = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-2.5-flash"]


def ask_gemini(client, model: str, prompt: str, tries: int = 3) -> tuple[Triage, str]:
    """Call the model; on 503/429 back off and retry, then fall back to older Flash models."""
    import time
    from google.genai import errors, types
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=Triage,
        temperature=0.2,
    )
    last: Exception | None = None
    for m in [model] + [f for f in FALLBACK_MODELS if f != model]:
        for attempt in range(tries):
            try:
                resp = client.models.generate_content(model=m, contents=prompt, config=cfg)
                return Triage.model_validate_json(resp.text), m
            except errors.APIError as e:
                last = e
                if e.code not in (429, 503):
                    raise
                click.echo(f"   {m}: {e.code}, retry {attempt + 1}/{tries}", err=True)
                time.sleep(5 * (attempt + 1))
    raise click.ClickException(f"all models unavailable: {last}")


# ------------------------------------------------------------------------- grading
def ground_truth(inc: list[dict], faults: pd.DataFrame, end: pd.Timestamp) -> dict | None:
    t0 = inc[0]["start"]
    nodes = {e["node"] for e in inc}
    for w in detect.fault_windows(faults, end):
        if w["start"] - pd.Timedelta(seconds=5) <= t0 <= w["end"] + pd.Timedelta(seconds=90) and (
                w["node"] == "all" or w["node"] in nodes):
            return {"fault": w["fault"], "node": w["node"], "expected_runbook": EXPECTED_RUNBOOK.get(w["fault"])}
    return None


def to_markdown(results: list[dict]) -> str:
    out = ["# Triage report", ""]
    for r in results:
        s, t = r["signature"], r.get("triage")
        out.append(f"## Incident {r['n']}: {s['tunnels_alarming']} of {s['tunnels_total']} tunnels at {s['incident_start'][11:19]} UTC")
        gt = r.get("ground_truth")
        if gt:
            ok = t and t["runbook"] == gt["expected_runbook"]
            out.append(f"Ground truth (hidden from the model): **{gt['fault']}** on {gt['node']}. "
                       f"Model said **{t['runbook'] if t else '-'}** → {'correct' if ok else 'WRONG'}.")
        out.append("")
        out.append("| Tunnel | Features that moved (z) | State | Next 30 s |")
        out.append("| --- | --- | --- | --- |")
        for n in s["tunnels"][:12]:
            mv = ", ".join(f"{m['feature']}={m['value']} (z {m['z']})" for m in n["features_moved"][:4]) or "none above 3"
            st = n["tunnel_state_at_alarm"]
            nx = n["next_30s"]
            out.append(f"| {n['node']} | {mv} | {st.get('ike_state')}/{st.get('child_state')} | "
                       f"{'DOWN' if nx['ike_down_event'] else 'up'}, pmtud {nx['pmtud_min_end']}, tcp {nx['tcp_min_end']} |")
        out.append("")
        if t:
            out.append(f"**Page:** {t['page_text']}")
            out.append("")
            out.append(f"**Diagnosis ({t['confidence']} confidence, runbook `{t['runbook']}`):** {t['diagnosis']}")
            out.append("")
            out.append("Evidence: " + "; ".join(t["evidence"]))
            out.append("")
            out.append("Ruled out: " + "; ".join(t["ruled_out"]))
            out.append("")
            out.append(f"**Do first:** `{t['recommended_action']}`. **Verify:** {t['verify']}")
        else:
            out.append("_dry run: no model call_")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------- cli
@click.command()
@click.option("--data", type=click.Path(path_type=Path), default=Path("data"), show_default=True)
@click.option("--runbooks", type=click.Path(path_type=Path), default=Path(__file__).with_name("runbooks.yaml"), show_default=True)
@click.option("--model", default="gemini-3.8-flash", show_default=True)
@click.option("--z", default=6.0, show_default=True)
@click.option("--incident", "only", type=int, default=None, help="triage only incident N (1-based)")
@click.option("--dry-run", is_flag=True, help="build signatures and prompts, call no API")
@click.option("--window", type=click.Choice(["scenario", "all"]), default="scenario", show_default=True,
              help="scenario = alarms between the first fault and the scenario-end mark; all = everything recorded since")
@click.option("--out", type=click.Path(path_type=Path), default=Path("data/results"), show_default=True)
def main(data: Path, runbooks: Path, model: str, z: float, only: int | None, dry_run: bool, window: str, out: Path):
    """Group detector alarms into incidents and ask Gemini which runbook applies."""
    br, hub = detect.load_telemetry(data)
    faults = detect.load_faults(data)
    feats = detect.branch_features(br, detect.hub_aggregates(hub))
    base_start = faults.loc[faults["event"] == "baseline-start", "timestamp"].iloc[0]
    first_fault = faults.loc[faults["event"].str.endswith("-start") & (faults["event"] != "baseline-start"), "timestamp"].min()
    train = feats[(feats["timestamp"] >= base_start) & (feats["timestamp"] < first_fault)]
    det = detect.ZScoreDetector(z=z).fit(train)
    zf = per_feature_z(det, feats)
    alarms = det.alarms(feats)

    stop = feats["timestamp"].max()
    if window == "scenario":
        ends = faults.loc[faults["event"] == "scenario-end", "timestamp"]
        if len(ends):
            stop = ends.iloc[0]
    eps = [e for e in episodes(feats, alarms) if first_fault <= e["start"] <= stop]
    incs = incidents(eps)
    n_nodes = feats["node"].nunique()
    click.echo(f"{len(eps)} alarm episodes → {len(incs)} incidents")

    key = None if dry_run else load_api_key(Path.cwd())
    client = None
    if not dry_run:
        if not key:
            raise click.ClickException("no GEMINI_API_KEY in the environment or .env; use --dry-run to skip the model")
        from google import genai
        client = genai.Client(api_key=key)

    rb = yaml.safe_load(runbooks.read_text())
    results = []
    for n, inc in enumerate(incs, 1):
        if only and n != only:
            continue
        sig = incident_signature(inc, feats, zf, det, br, n_nodes)
        prompt = build_prompt(sig, rb)
        rec = {"n": n, "signature": sig, "ground_truth": ground_truth(inc, faults, feats["timestamp"].max())}
        click.echo(f"\n== incident {n}: {sig['tunnels_alarming']}/{n_nodes} tunnels at {sig['incident_start']} "
                   f"({'fleet-wide' if sig['fleet_wide'] else sig['tunnels'][0]['node']}) ==")
        for t in sig["tunnels"][:3]:
            click.echo("   " + ", ".join(f"{m['feature']} z={m['z']}" for m in t["features_moved"][:4]))
        if client:
            tri, used = ask_gemini(client, model, prompt)
            rec["triage"] = tri.model_dump()
            rec["model"] = used
            click.echo(f"   → {tri.runbook} ({tri.confidence}, {used}): {tri.page_text}")
        else:
            rec["prompt_chars"] = len(prompt)
            (out / f"triage-prompt-{n}.txt").parent.mkdir(parents=True, exist_ok=True)
            (out / f"triage-prompt-{n}.txt").write_text(SYSTEM + "\n\n" + prompt)
        results.append(rec)

    out.mkdir(parents=True, exist_ok=True)
    (out / "triage.json").write_text(json.dumps(results, indent=2, default=str))
    (out / "triage.md").write_text(to_markdown(results))
    graded = [r for r in results if r.get("triage") and r.get("ground_truth")]
    if graded:
        ok = sum(r["triage"]["runbook"] == r["ground_truth"]["expected_runbook"] for r in graded)
        click.echo(f"\nrunbook match: {ok}/{len(graded)} incidents with ground truth")
    click.echo(f"wrote {out}/triage.json and {out}/triage.md")


if __name__ == "__main__":
    main()
