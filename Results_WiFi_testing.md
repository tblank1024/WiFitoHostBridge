# Results_WiFi_testing.md — RV WiFi throughput investigation

**Subject:** why the Roku on Sophie's WiFi is slow.
**Sessions:** 2026-10-09 07:11–10:05 PDT, 2026-10-12 21:08–21:35 PDT.
**Run by:** a Claude Code agent on Sophie (RV1-PI5), per `plan-speed.md`, with the user performing the
physical moves. Raw output under `~/speed-results/<timestamp>/`.

```
Roku --(1) RP5 AP wlan0/br0 --(2) USB link 10.10.0.x --(3) Zero wlan0 2.4 GHz --> house AP --> Internet
```

The agent's own API traffic traverses hops 2 and 3, which constrained what could be tested (see Constraints).

---

## Headline findings

1. **The AP hop is not the bottleneck, and the plan's premise was wrong.** `plan-speed.md` assumed hostapd was
   left at 802.11g (54 Mbps cap), channel 7, no WMM. The live config was hand-corrected on 2025-08-21 to
   `channel=1`, `ieee80211n=1`, `wmm_enabled=1`, `country_code=US`. Clients link at 57.7–72.2 Mbit/s.
   **Fixes C and D in the plan should be skipped.**
2. **The USB hop is irrelevant.** 182–216 Mbps, zero retransmits, 0.218 ms mean RTT, no sample above 5 ms in
   600. Three orders of magnitude of headroom over the uplink.
3. **The Zero's uplink is the limiting hop, and its signal strength is the lever.** Moving the Zero from
   inside the window to outside gained **+6.6 dB** and **2.2× single-stream download** (8.8 → 19.3 Mbps).
4. **Severe upload-side bufferbloat.** Idle RTT 15 ms; under upload load the median reaches 325–1072 ms and
   peaks at 1302 ms. The Zero's `wlan0` root qdisc is `pfifo_fast` with no AQM. **This defect is not in the
   plan's A–E fix list** and is independent of signal strength.
5. **`speedtest-cli` is unreliable on this setup** and understated download by roughly 2.5×. All Test 3 / Test 4
   numbers below are tagged invalid for that reason.

---

## Methodology warnings — read before trusting any number here

Three measurement errors were made and caught during the investigation. They are recorded because several
intermediate conclusions were built on them and had to be retracted.

| # | Error | Consequence |
|---|-------|-------------|
| M1 | **`speedtest-cli` understates throughput.** Outside, it reported 7.6–10.4 Mbps download while `curl` against Cloudflare got 17.9–20.6 Mbps on the same link minutes apart. Not a CPU limit: the RP5 (far faster) reported *lower* than the Zero, so it is the tool's server selection/method. | **Tests 3, 4 and 8's throughput figures are invalid.** Use `curl` or `iperf3` instead. The bufferbloat *latency* finding in Test 8 stands — it does not depend on the reported rate. |
| M2 | **`iw` rate readings on an idle link reflect rate-control decay, not the channel.** `iw` reports the *last used* rate; with no traffic, minstrel decays toward the low rates and the reading goes stale. Observed directly: the link sat at 5.5 Mbit/s before a test and recovered to 62 Mbit/s within 15 s once the test's own SSH traffic started. | **The Test 0b rx-rate histogram overstates the problem** (its "12% of samples in 802.11b fallback" is partly decay, not channel). RSSI is unaffected — it is measured from received beacons. All later rate samples were taken with a `ping` running to keep rate control live. |
| M3 | **`tx failed` is a cumulative counter since association, not a current rate.** The 1.90% figure covered six days of uptime, including every bad period. | Cannot support a claim about present link quality. Use a delta across a known interval instead. |

A fourth error affected one measurement in this file: the 4-parallel-stream download test used
`curl --max-time 60`, and both runs terminated at exactly 60.08 s — the cutoff, not completion. Computing
throughput as 4 × 40 MB was therefore wrong in both positions, and the apparent "identical 21.3 Mbps ceiling"
was an artifact of identical timeouts. **The true aggregate ceiling is still unmeasured.**

---

## Baseline configuration (2026-10-09 07:11, read-only)

### RP5 (Sophie / RV1-PI5) — access point

| Item | Value |
|------|-------|
| AP | SSID `Sophie`, ch **1** (2412 MHz), 20 MHz, txpower 31 dBm, `wlan0` in AP mode bridged to `br0` |
| hostapd | `hw_mode=g` **+ `ieee80211n=1` + `wmm_enabled=1`** + `country_code=US`; mtime 2025-08-21 |
| Missing from hostapd | `ht_capab` (no SHORT-GI-20, no HT40 advertised), `rsn_pairwise` |
| LAN | `br0` 10.0.0.1/24, dnsmasq |
| Uplink | default via 10.10.0.1 dev `usb0`, src 10.10.0.17 |
| Radio capability | phy0 band 1 (2.4) **and band 2 (5 GHz, ch 36–64, VHT)** — 5 GHz is possible |
| Associated clients | laptop `28:d0:ea:d4:f7:47` tx 72.2 / rx 21.6–43.3; Galaxy S20 tx 72.2; HS300 tx 1.0 |
| Roku | `d4:e2:2f:c8:58:0e` / 10.0.0.163 `SophieRoku` — **powered off for the entire investigation** |

### Zero (RP2W) — uplink

| Item | Value |
|------|-------|
| Uplink | SSID `tellMyWIFILoveHer`, BSSID `7a:a7:41:81:21:83`, ch **6** (2437 MHz), 20 MHz, 11n 1×1 |
| Addressing | Zero 192.168.1.212, gw 192.168.1.1; NAT `-s 10.10.0.0/24 -o wlan0 MASQUERADE` |
| Power save | **on**; NM `802-11-wireless.powersave: 0 (default)` |
| Bluetooth | `hci0` unblocked, `bluetooth` service **active**, `hciuart` inactive |
| Root qdisc on wlan0 | **`pfifo_fast`** (no AQM — cause of finding 4) |
| Alternative BSSIDs | **none** — exactly one AP for that SSID |

AP ch 1 vs uplink ch 6 is the non-overlapping 2.4 GHz pair, so spectral overlap is minimal despite the radios
being physically close.

### Topology note — double NAT

The Zero masquerades `10.10.0.0/24 → wlan0`. Its rule does not match `10.0.0.0/24`, so the RP5 must already be
masquerading RV clients to `10.10.0.17` (not directly verified — `iptables` needs sudo on the RP5, which was
unavailable). This matters for qdisc placement; see Recommendation 2.

---

## Results

### Hop capacity

| Test | Measurement | Result | Valid? |
|------|-------------|--------|--------|
| 2 | USB RP5→Zero, iperf3 20 s | **182.4 Mbps**, 0 retransmits | yes |
| 2 | USB Zero→RP5, iperf3 20 s `-R` | **216.4 Mbps**, 0 retransmits | yes |
| 3 | Zero uplink, speedtest-cli ×3 | 8.33 / 5.77 / 7.32 down; 5.28 / 2.96 / 2.24 up | **no — M1** |
| 4 | Through RP5, speedtest-cli ×3 | 2.90 / 2.58 / 6.47 down; 7.21 / 5.69 / 6.90 up | **no — M1** |

Zero CPU during the USB test: 85% idle, load 0.50 — not a factor.

### Latency

| Test | Path | min / avg / max | Loss | Notes |
|------|------|-----------------|------|-------|
| 6 | RP5 → Zero (USB), 600 pkts @0.2 s | 0.145 / **0.218** / 0.421 ms | 0% | 0 samples >5 ms. Pristine |
| 6 | Zero → house gw, 600 pkts @0.2 s | 1.301 / **10.035** / 356.780 ms | 0.67% | 112/596 samples >10 ms on a *single hop* |

Zero→gw spikes landed at t = 38.0 s (226 ms), 59.8 s (357 ms), 60.0 s (156 ms), 95.2 s (102 ms) — **irregular,
not the ~30 s periodic pattern of NetworkManager background scans. Plan Fix E (BSSID lock) is not indicated**,
and with only one BSSID present it is moot regardless.

### Test 8 — bufferbloat (the clearest defect found)

RTT to 8.8.8.8 at 0.5 s intervals while a speedtest ran. Rate figures discarded under M1; the latency
distribution is the result.

| Phase | Median | p90 | Max |
|-------|--------|-----|-----|
| Idle (t=0–5 s) | **15 ms** | — | 82 ms |
| Download phase (t=20–30 s) | ~19 ms | — | 204 ms |
| **Upload phase (t=30–55 s)** | **325–1072 ms** | 845 ms | **1302 ms** |

26% of loaded samples exceeded 200 ms; 24 exceeded 500 ms; 9 exceeded 1000 ms; 6 packets lost. Per 5 s bucket
the upload phase ran 542 / 325 / 1072 / 361 / 719 ms median.

The queue is specifically on **egress**, and the Zero's `wlan0` root qdisc is `pfifo_fast` — a FIFO deep enough
that TCP's steady state is a full buffer. Practical effect: any upload in the RV stalls Roku control traffic,
and this agent's own session, by up to a second.

### Position A/B — the main result (2026-10-12)

Identical method both positions: 30 × 1 s `iw dev wlan0 link` samples with a `ping` running to keep rate control
live (per M2), then `curl` of a 40 MB Cloudflare object.

| | **Inside, at window** | **Outside, hanging from USB cable** | Delta |
|---|---|---|---|
| RSSI min / mean / max | −67 / **−64.3** / −62 dBm | −60 / **−57.7** / −54 dBm | **+6.6 dB** |
| RSSI spread | 5 dB | 6 dB | both stable |
| rx PHY mean / median | 50.4 / 52.0 Mbit/s | 62.3 / **65.0** Mbit/s | +25% |
| rx PHY min | 19.5 Mbit/s | 26.0 Mbit/s | no 11b fallback either position |
| tx PHY median | **72.2** Mbit/s | **72.2** Mbit/s | pinned at MCS7 SGI both |
| **Single-stream download (curl)** | 11.46 / 6.21 → **~8.8 Mbps** | 20.64 / 17.92 → **~19.3 Mbps** | **2.2×** |
| Post-load RTT to 1.1.1.1 | 7.6 / **648.7** / 3066.9 ms, pipe 4 | 7.0 / **39.8** / 246.4 ms | 16× worse inside |
| 4-stream aggregate | *invalid (timeout cutoff)* | *invalid (timeout cutoff)* | — |

Single-stream transfers completed inside the timeout in both positions (15.5–17.9 s outside, 27.9–51.5 s
inside), so those throughput figures are sound.

**Mechanism.** tx PHY is pinned at 72.2 Mbit/s — MCS7 short-GI, the hard ceiling for 1×1 20 MHz — in both
positions, so the uplink's transmit side is already maxed and position does not change it. The difference is on
receive: 6.6 dB of additional margin raises rx PHY 52 → 65 Mbit/s and, more importantly, cuts 802.11
retransmissions. Those retransmissions are what produce the post-load latency spikes (649 ms avg, 3067 ms peak
inside vs 40 ms outside), and a single TCP flow facing multi-second RTT excursions collapses its congestion
window. Hence a 6.6 dB RF change producing a 2.2× transport-layer change.

### Day-to-day variation

Same position, same AP, same channel:

| Date | Position | RSSI mean | Spread |
|------|----------|-----------|--------|
| 2026-10-09 07:11 | inside window | −68 dBm (single sample) | — |
| 2026-10-09 10:00 | inside window | **−73.8 dBm** | 12 dB |
| 2026-10-12 21:11 | inside window | **−65.3 dBm** | 6 dB |
| 2026-10-12 21:30 | inside window | **−64.3 dBm** | 5 dB |

**Variation between days is ~9 dB — larger than the 6.6 dB window-vs-outside effect.** Any future A/B must
measure before and after within the same session, or the drift will swamp the result. The 12 dB intra-sample
spread on 10-09 also suggests that day's link had no dominant direct path and was surviving on multipath.

---

## What is established, and what is not

**Established:** the USB hop and the AP hop both have ample headroom; the uplink is the limiting hop; uplink
RSSI converts to download throughput at roughly 2.2× per 6.6 dB in this regime; tx PHY is at the 1×1 20 MHz
ceiling; the Zero runs `pfifo_fast` and exhibits ~1 s of upload-side queueing; there is only one usable BSSID;
the AP already runs 11n with WMM.

**Not established:**
- **The absolute download ceiling**, in either position — the parallel test was invalid. This is the open
  question that decides whether 4K is reachable from inside. Roku/Netflix 4K wants 15–25 Mbps; inside
  single-stream is 8.8 Mbps, outside 19.3 Mbps.
- **Whether the house service or the RF link binds at the top end.** Unresolved pending the ceiling test.
- **Anything about the Roku itself.** It was powered off throughout — no Roku link rate, and Test 5 never ran.
- **Tests 1 and 7** (AP hop, co-channel contention): not run. The laptop was placed at 10.0.0.96 and
  associated at 72.2 Mbit/s tx, but nothing was listening on port 5201. Of note: ping RTT to it over the AP hop
  was 13.6 / 31.4 / 50.4 ms, high for a local hop and worth explaining when Test 1 runs.
- **Whether Test 4's apparent loss through the RP5 is real** — it rests on invalid speedtest-cli data.

---

## Recommendations

**1. Improve the Zero's RF link — the only lever that moves download throughput.**
Measured at 2.2× per 6.6 dB. The Pi Zero 2W has a PCB trace antenna, no external connector, inside a metal
shell behind glass.
- **USB WiFi adapter with an external antenna**, mounted outside or at least clear of the frame. Should beat
  hand-held-outside, and avoids leaving the Zero exposed on its USB cable.
- Note the ceiling: tx PHY is already pinned at MCS7. Beyond ~6–10 dB more margin, further gains need a wider
  channel, more spatial streams, or 5 GHz — not more signal.

**2. Add CAKE egress shaping for the bufferbloat.** This is a latency fix; it will *not* raise throughput, but
it addresses the 1302 ms spikes that stall Roku control traffic and anything interactive.
Place it on the **RP5's `usb0` egress, not the Zero's `wlan0`.** A qdisc runs after POSTROUTING, so on the Zero
every RV client appears as `10.10.0.17` and CAKE's `nat` option could only recover that same address, collapsing
per-host fairness. On the RP5, conntrack still holds the real `10.0.0.x` addresses. Shaping there also keeps the
Zero's `pfifo_fast` drained.
```
tc qdisc replace dev usb0 root cake bandwidth 2500kbit nat triple-isolate   # needs sudo on the RP5
tc qdisc del dev usb0 root                                                  # revert
```
`sch_cake.ko` is present on both hosts and `tc` 6.1.0 parses it (verified on `lo`). Start near 2500 kbit and
tune up. Caveat: the uplink is WiFi and its capacity is not constant, so any fixed rate is a compromise; 802.11
framing is not cleanly modelable, so absorb overhead in a lower rate rather than guessing `overhead`.

**3. Plan Fixes A / A′ (power save off) and B (Bluetooth off)** — cheap and reversible. Both conditions were
confirmed present. Expect them in Test 6 jitter, not Test 3 throughput. Re-measure with curl, not speedtest-cli.

**4. Skip plan Fixes C, D and E.** C is already applied in substance; D cannot help because the AP hop has
roughly 10× the capacity of the uplink feeding it; E is contraindicated by the spike timing and moot with one
BSSID.

**5. Fix `rv/buildRP5RaspAP/install_raspap_bridge.sh` regardless of the speed work.** Lines 195–197 still write
`hw_mode=g`, `channel=7`, `wmm_enabled=0` and **would regress the live AP to 802.11g with no WMM if re-run.**
Line 205 still sets `rsn_pairwise=CCMP`, which the live file has lost. Consider also adding `ht_capab` with
`[SHORT-GI-20]`.

---

## Open items

| # | Item | Owner |
|---|------|-------|
| 1 | Aggregate download ceiling, parallel streams, no timeout cutoff, both positions | agent |
| 2 | Re-measure Tests 3 and 4 with curl/iperf3 to replace invalid speedtest-cli data | agent |
| 3 | Start `iperf3 -s` on the laptop at 10.0.0.96 → unblocks Tests 1 and 7 | user |
| 4 | Power the Roku on → unblocks Test 5 and the Roku's own link rate | user |
| 5 | Confirm the RP5 masquerades `10.0.0.0/24` (needs sudo) — affects CAKE placement | user/agent |
| 6 | Secure the Zero's antenna position properly; it is currently hanging outside by its USB cable | user |

## Constraints that shaped this investigation

The agent's uplink runs through hops 2 and 3, so anything that reconnects the Zero's WiFi would have killed the
session mid-test. All `nmcli con up/down`, `dev reapply`, BSSID locks and Zero reboots were therefore excluded,
as were hostapd restarts (which drop every WiFi client on Sophie). No passwordless sudo on the RP5; the Zero was
reachable as root via `ssh zero-root` (the plan's `pi@10.10.0.1` was wrong — the hosts are `zero` / `zero-root`
with users `tblank` / `root`). Installing iperf3 on the Zero first required `dpkg --configure -a` to clear a
dpkg state interrupted since 2025-05-06 (`libc-bin`, `man-db`; `xrdp` remains half-installed and was left alone).
