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

## WAN switch investigation (2026-10-17) — why "wired" never worked

Separate from the RF work above. Raw data in `~/speed-results/*-usb-sweep/`,
`*-usb-deep/`, `*-wired-diag/`, and `/var/log/pi5connect.log`.

### Architecture, as measured

Four WAN sources feed the RP5 through a **Coolgear 4-port USB switch** (the Genesys
`05e3:0610` hub at USB path `1-2`), controlled over serial by an FT232R
(`0403:6001`, serial `B0033FMU`) on `/dev/coolgear-hub`. Only one switch port is
normally powered; both control endpoints are exclusive
(`set_single_port_on` / `port_on` both send "only port N on").

| Switch port | USB hub path | Device | Network seen | Config label |
|---|---|---|---|---|
| 1 | `1-2.1` | `cdc_ether` adapter | `192.168.0.163/24` gw `192.168.0.1`, domain `lan`, **ICMP blocked, TCP works** | `cellular` — correct |
| 2 | `1-2.2` | Pi Zero USB gadget | `10.10.0.x` | `wifi` — correct |
| 3 | `1-2.3` | `r8152` adapter | `206.214.239.194/24` gw `.195`, lease **16 s**, and `192.168.100.1` answers | `starlink` — correct |
| 4 | — | **nothing attached** | — | `wired` — **wrong** |

**Hub port 4 holds the FT232R that controls the switch, and is always powered** —
it stays attached while port 2 is the active WAN. So there are three switchable
jacks, not four, which matches the hardware: Starlink and wired share one ethernet
adapter (you move the RJ45).

### Root cause

`Internet.jsx` sent **`port: 4`** for "Wired". That port has no adapter, so
selecting Wired powered nothing. A `wired-diag` run with port 4 enabled recorded
**zero USB events in four minutes** and no interface.

This also corrects an earlier conclusion in this file: the `eth1` sightings at
gateway `192.168.0.1` on 2026-10-06 and 10-13 were **the cellular modem**, not the
house wire. `192.168.0.1` with domain `lan`, a locally-administered gateway MAC and
ICMP blocked is the cellular router. The house LAN is `192.168.2.0/24` (a laptop on
that wall port gets `192.168.2.239`).

### Second bug: the dispatcher ignored `dhcp4-change`

`/etc/NetworkManager/dispatcher.d/99-pi5connect` triggered only on
`up`/`down`/`connectivity-change`. NM fires `up` as soon as the link is usable,
which can precede DHCP returning a gateway. Measured on the cellular port:

```
11:50:42  port enabled
11:51:18  NM "up"      -> pi5connect: "Error: cannot determine gateway for eth1."
11:51:19  dhcp4-change -> logged as "potential uplink", NEVER acted on
11:51:19  lease arrives (37 s after enable)
```

pi5connect never re-ran, so that WAN was never selected. It is a **race**: a fast
lease (before `up`) works, a slow one silently fails. That explains why `eth1`
worked on 10-05/06/13 and not otherwise. Fixed by adding `dhcp4-change`.

### Third bug: the status endpoint lied

`/api/debug/usb/status` called `get_current_active_port()` **inside** its per-port
loop — 4 serial round-trips, each retrying up to 3×, to answer one question. The
rapid queries clipped each other's replies (`Invalid hub status response length:
FFFFF`), and a truncated read returns −1, so the API reported **every port
`enabled: false`** while port 2 was demonstrably powered. Now queries once and
reports `enabled: null, unknown: true` on failure instead of claiming all-off.

### The FT232R control chip is failing intermittently

Three failures, same signature: `error from flowcontrol urb`, then `-71`
(`-EPROTO`) on every re-enumeration attempt including the kernel's own port power
cycle, ending in `unable to enumerate USB device`. `over_current_count` stayed **0**
throughout, so it is not a power trip — more likely a marginal connector or a
failing chip.

| Failure | Held for |
|---|---|
| 2026-10-14 17:07 | stayed dead 2.5 days |
| 2026-10-17 07:45 | 20 min after recovery |
| (recovered 09:22, re-enumerated on its own 09:27:30 with `failed to set flow control: -71`) | minutes |

**Recovery — toggle hub port 4 only. ~11 s, no reboot, does not disturb the active WAN:**
```bash
P=/sys/bus/usb/devices/1-2:1.0/1-2-port4
echo 1 | sudo tee $P/disable >/dev/null; sleep 3
echo 0 | sudo tee $P/disable >/dev/null; sleep 8
ls -l /dev/coolgear-hub          # expect symlink -> ttyUSB0
```
Verified twice. **Never reset the hub itself (`1-2`)** — that resets every port
including the active WAN. Restarting the webserver cannot fix this: with the chip
off the bus there is no device to open. The container's `/host-dev` is a live bind
mount of the host `/dev`, so it sees the recovered node without a restart.

### pi5connect v2.1 proved itself here

With the Starlink dish in the garage (no satellite), port 3 came up with a valid
address and a pingable gateway but no internet. v2.1 rejected it:

```
Probe FAILED: eth1 could not reach 1.1.1.1 8.8.8.8 or http://1.1.1.1/ ...
Reject eth1: gateway reachable but no internet beyond it.
```

v2.0 would have deleted every default route and committed NAT to it. The cellular
port is the case the TCP fallback exists for: ICMP is blocked there, but
`http://1.1.1.1/` returns 301.

### Remaining work

| Item | Status |
|---|---|
| `Internet.jsx` Wired → port 3 | fixed, needs webserver image rebuild |
| Dispatcher `dhcp4-change` | fixed in repo, needs deploy to `/etc/NetworkManager/dispatcher.d/` |
| Status endpoint single query | fixed, needs image rebuild |
| **Move the house RJ45 into port 3's adapter** | **physical, user** |
| FT232R intermittent failure | recovery procedure documented; physical fix outstanding |
| `EXTENDED_CONNECTION_MAPPING` | dead code (defined, never referenced) |

## Alfa AWUS036ACM bench test (2026-10-17) — see `rv/plan_new_WAN_WiFi.md`

The measurements live in **`rv/plan_new_WAN_WiFi.md`, "Measured on hardware"**, alongside the integration plan
they bear on. Not duplicated here. Headline, because it changes the RF picture above:

**5 GHz beat 2.4 GHz by 10-40x on the same SSID, despite being 16 dB weaker** — 28-30 Mbps steady at -78 dBm on
clean ch36, versus 0.7-3.5 Mbps erratic at -62 dBm on ch6, where five BSSes contend. Wired was 90.5 Mbps for
reference. Co-channel contention dominates signal strength here, which also reframes the Zero's measured ceiling:
being 2.4-only, it cannot escape that congested band at all.

Also recorded there: `mt76x2u` binds with no driver/firmware/modeswitch work; the adapter's power LED stays dark
until the interface is brought up (not a fault); and the Pi 5's 600 mA total USB cap (`usb_max_current_enable`
unset) is a real constraint for a 2x2 radio under sustained transmit.

## Constraints that shaped this investigation

The agent's uplink runs through hops 2 and 3, so anything that reconnects the Zero's WiFi would have killed the
session mid-test. All `nmcli con up/down`, `dev reapply`, BSSID locks and Zero reboots were therefore excluded,
as were hostapd restarts (which drop every WiFi client on Sophie). No passwordless sudo on the RP5; the Zero was
reachable as root via `ssh zero-root` (the plan's `pi@10.10.0.1` was wrong — the hosts are `zero` / `zero-root`
with users `tblank` / `root`). Installing iperf3 on the Zero first required `dpkg --configure -a` to clear a
dpkg state interrupted since 2025-05-06 (`libc-bin`, `man-db`; `xrdp` remains half-installed and was left alone).
