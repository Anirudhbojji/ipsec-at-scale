# Triage report

## Incident 1: 1 of 12 tunnels at 02:11:46 UTC
Ground truth (hidden from the model): **dpd-flap** on branch3. Model said **underlay-loss** → correct.

| Tunnel | Features that moved (z) | State | Next 30 s |
| --- | --- | --- | --- |
| branch3 | tcp_min=0.0 (z 10.0), loss_mean=6.25 (z 3.1) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 0.0 |

**Page:** Underlay packet loss on branch3 (loss_mean 29.2%, tcp_min 0); raise DPD tolerance to prevent SA flap.

**Diagnosis (high confidence, runbook `underlay-loss`):** Underlay packet loss on branch3 is causing TCP transfer stalls (tcp_min=0) and rising loss (6.25% at alarm, scaling to 29.2% within 30s). All hub metrics and other tunnels remain completely baseline, pointing to a localized path issue.

Evidence: branch3 tcp_min dropped to 0.0 (z=10.0); branch3 loss_mean rose to 6.25% (z=3.1) and reached 29.2% in next 30s; pmtud_min remained flat at 1.0; down_sum remained 0 with ike_state ESTABLISHED

Ruled out: mtu-blackhole: pmtud_min stayed at 1.0 instead of dropping to 0, and loss_mean increased; rekey-storm: only 1 tunnel affected and hub_rekey_burst (3.0) is at baseline max; tunnel-down: child SA is INSTALLED, down_sum is 0, and loss is under 100%; hub-overload: hub CPU is normal at 1.4% and issue is isolated to a single branch

**Do first:** `raise-dpd-tolerance`. **Verify:** loss_mean returns to baseline; no ike_down_event; retrans_sum stops climbing.

## Incident 2: 1 of 12 tunnels at 02:16:27 UTC
Ground truth (hidden from the model): **mtu-blackhole** on branch5. Model said **mtu-blackhole** → correct.

| Tunnel | Features that moved (z) | State | Next 30 s |
| --- | --- | --- | --- |
| branch5 | pmtud_min=0.0 (z 10.0), tcp_min=0.0 (z 10.0) | ESTABLISHED/INSTALLED | up, pmtud 0.0, tcp 0.0 |

**Page:** PMTUD blackhole on branch5: pmtud_min and tcp_min dropped to 0 while loss_mean stays 0% and SA remains INSTALLED.

**Diagnosis (high confidence, runbook `mtu-blackhole`):** Branch5 is experiencing a PMTUD blackhole where packets exceeding path MTU with the DF bit set are dropped without returning an ICMP fragmentation-needed message. TCP handshakes succeed but data transfers stall, while standard ping latency and loss remain completely normal.

Evidence: pmtud_min = 0.0 (baseline 1.0, z=10.0); tcp_min = 0.0 (baseline 1.0, z=10.0); loss_mean = 0.0%; child_state = INSTALLED

Ruled out: underlay-loss: loss_mean (0.0%), rtt_mean, jitter_mean, and retrans_sum stayed flat at baseline; tunnel-down: child_state is INSTALLED, ike_state is ESTABLISHED, and down_sum is 0; rekey-storm: single tunnel incident; hub_rekey_burst (2.0) and hub_rekey_sum (11.0) are below baseline max; hub-overload: hub_cpu_max is 1.4%, well below threshold

**Do first:** `apply-mss-clamp`. **Verify:** tcp_min returns to 1 within two ticks after applying MSS clamping

## Incident 3: 11 of 12 tunnels at 02:22:30 UTC
Ground truth (hidden from the model): **rekey-storm** on all. Model said **rekey-storm** → correct.

| Tunnel | Features that moved (z) | State | Next 30 s |
| --- | --- | --- | --- |
| branch1 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch4 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch9 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch10 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch11 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch12 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch2 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch7 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch6 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch8 | hub_rekey_burst=14.0 (z 8.1), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |
| branch3 | hub_rekey_burst=14.0 (z 8.1), rekey_sum=4.0 (z 4.0), hub_rekey_sum=23.0 (z 3.2) | ESTABLISHED/INSTALLED | up, pmtud 1.0, tcp 1.0 |

**Page:** Rekey storm across 11/12 tunnels; hub_rekey_burst spiked to 14 (z=8.1). Data path unaffected. Widen rand_time on hub.

**Diagnosis (high confidence, runbook `rekey-storm`):** A synchronised rekey storm occurred across 11 of 12 branch tunnels simultaneously. The hub saw a spike in rekey frequency, but tunnel traffic probes remain healthy and hub CPU remains under control.

Evidence: hub_rekey_burst = 14.0 (z = 8.1); hub_rekey_sum = 23.0 (z = 3.2); 11 of 12 tunnels alarming within 4 seconds; loss_mean = 0.0 across all tunnels; tcp_min = 1.0 across all tunnels; pmtud_min = 1.0 across all tunnels

Ruled out: hub-overload: hub_cpu_max remained low at 5.4% and retrans_sum stayed flat; tunnel-down: all tunnels stayed INSTALLED with 0 down_sum and no ike_down_events; underlay-loss: loss_mean, rtt_mean, and jitter_mean remained flat at baseline; mtu-blackhole: tcp_min and pmtud_min remained at 1.0 across all nodes

**Do first:** `widen-rand-time`. **Verify:** hub_rekey_burst stays at or below baseline max over the next rekey lifetime.
