"""Generate docker-compose.yml and per-node swanctl.conf for the lab.

Profiles change only lifetimes, so the same lab can be brought up healthy and
then re-loaded into a rekey storm without restarting anything:

  healthy  child rekey 180 s with 60 s of random jitter, IKE rekey 600 s +/- 120 s
  storm    child rekey 90 s with no jitter, IKE rekey 300 s with no jitter
"""

from __future__ import annotations

from pathlib import Path

import click
import yaml

HUB_UNDERLAY = "10.0.0.10"
HUB_LAN_NET = "10.100.0.0/24"
HUB_LAN_IP = "10.100.0.1"
HUB_CLIENT_IP = "10.100.0.100"
PSK = "lab-psk-not-for-production"

PROFILES = {
    "healthy": dict(child_rekey="180s", child_rand="60s", ike_rekey="600s", ike_rand="120s"),
    "storm": dict(child_rekey="90s", child_rand="0s", ike_rekey="300s", ike_rand="0s"),
}


def branch_underlay(i: int) -> str:
    return f"10.0.0.{10 + i}"


def branch_lan_net(i: int) -> str:
    return f"10.1.{i}.0/24"


def branch_lan_ip(i: int) -> str:
    return f"10.1.{i}.1"


def branch_client_ip(i: int) -> str:
    return f"10.1.{i}.100"


def conn_block(name: str, local: str, remote: str, local_id: str, remote_id: str, mark: int,
               p: dict, ike_prop: str, esp_prop: str, initiator: bool) -> str:
    start = "start" if initiator else "none"
    return f"""    {name} {{
        version = 2
        local_addrs = {local}
        remote_addrs = {remote}
        local {{
            auth = psk
            id = {local_id}
        }}
        remote {{
            auth = psk
            id = {remote_id}
        }}
        proposals = {ike_prop}
        rekey_time = {p['ike_rekey']}
        rand_time = {p['ike_rand']}
        dpd_delay = 5s
        keyingtries = 0
        children {{
            net {{
                local_ts = 0.0.0.0/0
                remote_ts = 0.0.0.0/0
                esp_proposals = {esp_prop}
                mark_in = {mark}
                mark_out = {mark}
                rekey_time = {p['child_rekey']}
                rand_time = {p['child_rand']}
                start_action = {start}
                dpd_action = restart
                close_action = {start}
            }}
        }}
    }}
"""


def secrets_block(pairs: list[tuple[str, str]]) -> str:
    out = "secrets {\n"
    for n, (a, b) in enumerate(pairs, 1):
        out += f'    ike-{n} {{\n        id-1 = {a}\n        id-2 = {b}\n        secret = "{PSK}"\n    }}\n'
    return out + "}\n"


@click.command()
@click.option("--branches", default=8, show_default=True, help="number of branch gateways")
@click.option("--profile", type=click.Choice(sorted(PROFILES)), default="healthy", show_default=True)
@click.option("--ike-proposal", default="aes256gcm16-prfsha384-modp4096", show_default=True)
@click.option("--esp-proposal", default="aes256gcm16-modp4096", show_default=True, help="PFS group makes rekeys cost CPU")
@click.option("--ipsec-mtu", default=1400, show_default=True)
@click.option("--mss-clamp", default="", help="e.g. 1360; empty = no clamp (the default, on purpose)")
@click.option("--wan-delay-ms", default=20, show_default=True)
@click.option("--hub-cpus", default="1.0", show_default=True, help="cap hub CPU so storms are visible")
@click.option("--out", type=click.Path(path_type=Path), default=Path("lab/generated"), show_default=True)
def main(branches, profile, ike_proposal, esp_proposal, ipsec_mtu, mss_clamp, wan_delay_ms, hub_cpus, out):
    """Write lab/generated/docker-compose.yml and conf/<node>/swanctl.conf."""
    p = PROFILES[profile]
    out.mkdir(parents=True, exist_ok=True)
    conf = out / "conf"

    # hub config: one connection per branch
    hub_conns = "connections {\n"
    pairs = []
    hub_peers = []
    for i in range(1, branches + 1):
        hub_conns += conn_block(f"branch{i}", HUB_UNDERLAY, branch_underlay(i), "hub", f"branch{i}", i, p,
                                ike_proposal, esp_proposal, initiator=False)
        pairs.append(("hub", f"branch{i}"))
        hub_peers.append(f"{i}:{branch_lan_net(i)}:{branch_underlay(i)}")
    hub_conns += "}\n"
    (conf / "hub").mkdir(parents=True, exist_ok=True)
    (conf / "hub" / "swanctl.conf").write_text(hub_conns + secrets_block(pairs))

    services = {
        "hub": {
            "image": "ipsec-lab:latest",
            "container_name": "hub",
            "privileged": True,
            "cpus": float(hub_cpus),
            "networks": {"underlay": {"ipv4_address": HUB_UNDERLAY}},
            "volumes": [f"./conf/hub/swanctl.conf:/etc/swanctl/swanctl.conf", "../../data/telemetry:/data"],
            "environment": {
                "ROLE": "hub", "NODE": "hub", "UNDERLAY_IP": HUB_UNDERLAY,
                "LAN_IP": HUB_LAN_IP, "CLIENT_IP": HUB_CLIENT_IP, "HUB_LAN": HUB_CLIENT_IP,
                "IPSEC_MTU": str(ipsec_mtu), "MSS_CLAMP": mss_clamp, "WAN_DELAY_MS": "0",
                "PEERS": " ".join(hub_peers),
            },
        }
    }

    for i in range(1, branches + 1):
        name = f"branch{i}"
        c = "connections {\n" + conn_block("hub", branch_underlay(i), HUB_UNDERLAY, name, "hub", i, p,
                                            ike_proposal, esp_proposal, initiator=True) + "}\n"
        (conf / name).mkdir(parents=True, exist_ok=True)
        (conf / name / "swanctl.conf").write_text(c + secrets_block([(name, "hub")]))
        services[name] = {
            "image": "ipsec-lab:latest",
            "container_name": name,
            "privileged": True,
            "depends_on": ["hub"],
            "networks": {"underlay": {"ipv4_address": branch_underlay(i)}},
            "volumes": [f"./conf/{name}/swanctl.conf:/etc/swanctl/swanctl.conf", "../../data/telemetry:/data"],
            "environment": {
                "ROLE": "branch", "NODE": name, "UNDERLAY_IP": branch_underlay(i),
                "LAN_IP": branch_lan_ip(i), "CLIENT_IP": branch_client_ip(i), "HUB_LAN": HUB_CLIENT_IP,
                "IPSEC_MTU": str(ipsec_mtu), "MSS_CLAMP": mss_clamp, "WAN_DELAY_MS": str(wan_delay_ms),
                "PEERS": f"{i}:{HUB_LAN_NET}:{HUB_UNDERLAY}",
            },
        }

    compose = {
        "services": services,
        "networks": {"underlay": {"driver": "bridge", "ipam": {"config": [{"subnet": "10.0.0.0/24"}]}}},
    }
    (out / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    click.echo(f"wrote {out}/docker-compose.yml ({branches} branches, profile={profile}) and {branches + 1} swanctl.conf files")


if __name__ == "__main__":
    main()
