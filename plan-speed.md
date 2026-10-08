# plan-speed.md — Throughput test plan (Roku on Sophie WiFi)

**Executed by:** a Claude Code agent running on Sophie (RP5), with the user helping on the steps marked
**[USER]**.

Goal: find which hop limits throughput to the Roku, then A/B the candidate fixes.

```
Roku --(1) RP5 AP wlan0/br0 --(2) USB link 10.10.0.x --(3) Zero wlan0 2.4 GHz --> campground AP --> Internet
                ^ agent runs here; its own internet goes out through hops 2 and 3
```

Background (from laptop review, 2026-10-08): the RaspAP install script configured hostapd as
`hw_mode=g`, `channel=7`, `wmm_enabled=0`, no `ieee80211n` => 802.11g only (54 Mbps PHY). The Zero's uplink is
also 2.4 GHz, and nearby => airtime contention. The Zero's brcmfmac power save is on by default. None of this has
been confirmed on the live system yet; Step 0 does that.

## Rules for the agent

1. **Don't cut your own internet.** Claude's API traffic goes RP5 -> Zero -> campground WiFi. Anything that
   reconnects or reboots the Zero's WiFi (`nmcli con up/down`, `nmcli dev reapply`, BSSID lock, Zero reboot,
   `dtoverlay` changes) will drop this session. Don't run those; give the user the exact commands and ask them to
   run them, then resume when they return. `iw ... set power_save` and `nmcli con modify` (without reactivating)
   are safe.
2. **hostapd changes drop every WiFi client on Sophie**, possibly including the user's own terminal/SSH. Ask
   before restarting hostapd, back up the config first, and state the revert command up front.
3. Wrap every network test in a timeout (`timeout 60 ...`) and never run interactive commands. Use
   `ssh -o BatchMode=yes -o ConnectTimeout=5 pi@10.10.0.1 '<cmd>'` for the Zero.
4. Save raw outputs to `~/speed-results/<YYYYMMDD-HHMM>/` (small files; OK for the SD card) and fill in the
   Results table at the bottom of this file.
5. Your own API traffic shares the uplink during tests. It's small, but don't run other heavy work in parallel.
6. If a prerequisite fails (no passwordless sudo, no SSH key to the Zero), stop and ask the user. Don't loop.

## Prerequisites (agent)

```bash
sudo -n true && echo sudo-ok                                   # passwordless sudo on RP5
ssh -o BatchMode=yes -o ConnectTimeout=5 pi@10.10.0.1 'sudo -n true && echo zero-ok'
sudo apt-get install -y iperf3 speedtest-cli iw                # RP5 (answer "no" to iperf3 daemon prompt: preseed below)
ssh -o BatchMode=yes pi@10.10.0.1 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iperf3 speedtest-cli iw'
sudo iptables -L INPUT -n | head                               # policy must allow tcp/5201 from br0
ip -4 addr show br0                                            # Sophie LAN IP => <SOPHIE> below
cat /var/lib/misc/dnsmasq.leases                               # find the Roku's MAC/IP => <ROKU_MAC>
```
To avoid the iperf3 debconf prompt on the RP5:
`echo "iperf3 iperf3/start_daemon boolean false" | sudo debconf-set-selections` before installing.

## Step 0 — Capture current config (agent, read-only)

RP5:
```bash
cat /etc/hostapd/hostapd.conf
iw dev wlan0 info                          # channel + width in use
iw dev wlan0 station get <ROKU_MAC>        # tx/rx bitrate, signal, tx retries/failed
iw dev wlan0 get power_save
```
Zero (via ssh):
```bash
iw dev wlan0 link                          # SSID, freq, signal dBm, tx bitrate
iw dev wlan0 info
iw dev wlan0 get power_save
nmcli -t -f NAME,DEVICE connection show --active
nmcli -f 802-11-wireless.powersave,802-11-wireless.bssid connection show <active profile>
rfkill list; systemctl is-active bluetooth hciuart
ip route | grep default                    # => <ZERO_GW>
```
Report: Roku link rate (<= 54 Mbit/s confirms the 11g cap), AP channel vs Zero channel (within ±4 = contention),
Zero signal (weaker than -65 dBm => NM background scans are active).

## Test 2 — USB hop (RP5 <-> Zero) — agent

```bash
ssh pi@10.10.0.1 'iperf3 -s -1 -D -B 10.10.0.1'
timeout 40 iperf3 -c 10.10.0.1 -t 20 --json > usb_up.json
ssh pi@10.10.0.1 'iperf3 -s -1 -D -B 10.10.0.1'
timeout 40 iperf3 -c 10.10.0.1 -t 20 -R --json > usb_down.json
```
Expect well over 50 Mbps. If not: check the USB link speed (`ethtool <uplink iface>` on RP5) and Zero CPU
(`ssh ... 'top -bn1 | head -15'` during a run).

## Test 3 — Zero uplink — agent

```bash
for i in 1 2 3; do ssh pi@10.10.0.1 'timeout 90 speedtest-cli --simple'; done
```
This is the ceiling for everything downstream (expect ~10–25 Mbps best case).

## Test 4 — Uplink through the RP5 — agent

```bash
for i in 1 2 3; do timeout 90 speedtest-cli --simple; done
```
Should be close to Test 3. A big drop => USB hop / NAT / routing.

## Test 6 — Latency / stalls — agent

```bash
ssh pi@10.10.0.1 'ping -i 0.2 -c 600 <ZERO_GW>' > ping_zero_gw.txt     # 2 min
timeout 150 ping -i 0.2 -c 600 10.10.0.1 > ping_rp5_zero.txt
```
Look for RTT spikes of hundreds of ms. Spikes repeating ~every 30 s => NM background scan. Irregular 100+ ms
jitter at idle => power save. Report max/avg and spike timing (`awk` the `icmp_seq`/`time=` columns).

## Test 8 — Bufferbloat — agent

```bash
timeout 120 ping -i 0.5 8.8.8.8 > ping_loaded.txt &
sleep 5; timeout 90 speedtest-cli --simple; wait
```
Compare the RTTs during the speedtest with idle RTTs from the first 5 s. An increase of more than ~100 ms => queueing at
the bottleneck (candidate: `cake` on the Zero).

## Laptop at the Roku position — [USER] one-time setup

Tests 1 and 7 use the Windows laptop as the far end of the WiFi hop, so the agent can drive them from the RP5.
Ask the user to do this once and report the laptop's IP:

1. Put the laptop exactly where the Roku sits (same height/orientation, if practical). **Plug it in** (battery
   power saving throttles WiFi).
2. Connect it to **Sophie WiFi only**: unplug Ethernet, disconnect VPN, check it didn't join another saved SSID.
3. Install iperf3 for Windows (iperf.fr build or a winget package) and open the firewall (admin PowerShell):
   `New-NetFirewallRule -DisplayName iperf3 -Direction Inbound -Protocol TCP -LocalPort 5201 -Action Allow`
4. Start the server and leave it running: `iperf3 -s`
5. Report `ipconfig` (IPv4 address on the WiFi adapter => `<LAPTOP>`) and `netsh wlan show interfaces`
   (radio type, channel, signal %, receive/transmit rate).

Agent check: `ping -c 3 <LAPTOP>` and `iw dev wlan0 station dump` (find the laptop's MAC => `<LAPTOP_MAC>`).

Caveat: the laptop's radio (probably 2x2, better antennas) is better than the Roku's, so absolute numbers are
optimistic for the Roku. Bottlenecks (11g cap, 2.4 GHz contention) still show up the same way. The laptop also keeps
using the Zero uplink for its own traffic; ask the user to keep it idle during tests.

## Test 1 — AP hop (Roku position <-> RP5) — agent (laptop placed by [USER])

Direction: client on RP5 sends by default => default = **download to the Roku position** (what the Roku cares about);
`-R` = upload.
```bash
for i in 1 2 3; do timeout 40 iperf3 -c <LAPTOP> -t 20 --json > ap_down_$i.json; done
for i in 1 2 3; do timeout 40 iperf3 -c <LAPTOP> -t 20 -R --json > ap_up_$i.json; done
timeout 40 iperf3 -c <LAPTOP> -t 20 -P 4 --json > ap_down_P4.json      # 4 streams; if much higher than 1 stream => per-flow limits (latency/loss), not airtime
iw dev wlan0 station get <LAPTOP_MAC>                                  # capture during/after: tx bitrate, retries, failed
```
Also capture `iw dev wlan0 station get <ROKU_MAC>` to compare the Roku's link rate with the laptop's.
Interpretation: < ~20 Mbps download with good signal => AP config is a limiter. Laptop link rate stuck at <= 54
Mbit/s => confirms the 11g/no-WMM cap.

## Test 5 — End to end — [USER]

Ask the user to run fast.com or speedtest.net in a browser on the laptop at the Roku position (3 runs) and report the
numbers. Optional: Roku Settings > Network > Check connection. Compare with min(Test 1, Test 3).

## Test 7 — Co-channel contention — agent

Run the AP hop and the Zero uplink at the same time:
```bash
timeout 80 iperf3 -c <LAPTOP> -t 60 --json > ap_down_concurrent.json &
sleep 10; ssh pi@10.10.0.1 'timeout 90 speedtest-cli --simple' > zero_concurrent.txt; wait
```
If the two results together are much lower than the same tests run separately => the two 2.4 GHz radios share
airtime => 5 GHz AP helps.

## A/B fixes (one at a time; re-run the listed tests)

| Fix | Who | How | Revert | Re-run |
|-----|-----|-----|--------|--------|
| A. Zero power save off (runtime) | agent | `ssh pi@10.10.0.1 'sudo iw dev wlan0 set power_save off'` | `... power_save on` | 3, 6, (5) |
| A'. Make A persistent | agent | `sudo nmcli con modify <profile> 802-11-wireless.powersave 2` (do **not** reactivate; applies on next connect). Also patch `RPZero2WListener.py` add command | `... powersave 0` | — |
| B. Zero Bluetooth off | [USER] (Zero reboot drops this session) | add `dtoverlay=disable-bt` to Zero `/boot/firmware/config.txt`, reboot Zero | remove line, reboot | 3, 6 |
| C. AP 11n on 2.4 GHz | agent after asking | back up `/etc/hostapd/hostapd.conf`; set `ieee80211n=1`, `wmm_enabled=1`, `ht_capab=[SHORT-GI-20]`, `country_code=US`, `rsn_pairwise=CCMP`, channel 5+ away from the Zero's; `sudo systemctl restart hostapd` | restore backup, restart | 1, 5, 7 |
| D. AP on 5 GHz | agent after asking; confirm the Roku model is dual-band first | `hw_mode=a`, `channel=36`, `ieee80211n=1`, `ieee80211ac=1`, `wmm_enabled=1`, `ht_capab=[HT40+][SHORT-GI-20][SHORT-GI-40]`, `vht_oper_chwidth=1`, `vht_oper_centr_freq_seg0_idx=42`, `country_code=US`, `ieee80211d=1` | restore backup, restart | 1, 5, 7 |
| E. Lock Zero to BSSID (only if Test 6 shows ~30 s spikes) | [USER] (reactivation drops this session) | `sudo nmcli con modify <profile> 802-11-wireless.bssid <AP_MAC> && sudo nmcli con up <profile>` | set bssid `""`, con up | 6 |

Notes:
- RaspAP's web UI may rewrite `/etc/hostapd/hostapd.conf` (and may keep its own copy under `/etc/raspap/`). After
  C/D, check whether RaspAP's settings need the same change so it doesn't revert on the next UI save.
- After C/D, check `journalctl -u hostapd -n 50` for "Could not set channel"/"driver" errors. If hostapd fails to
  start, revert immediately, since the RV has no WiFi until you do.
- A hostapd restart drops the laptop too. Before re-running Test 1, ask the user to confirm the laptop rejoined
  Sophie WiFi (on 5 GHz after D) and that `iperf3 -s` is still running; re-check `<LAPTOP>` (DHCP may change it).
- Once a fix is proven, update `rv/buildRP5RaspAP/install_raspap_bridge.sh` (hostapd block) and/or
  `RPZero2WListener.py` to match, and commit.

## Results

Fill in during the run. Date / location / campground SSID / Zero signal (dBm) / Zero channel / AP channel / Roku link rate / laptop link rate:

| Test | Run 1 | Run 2 | Run 3 | Notes |
|------|-------|-------|-------|-------|
| 1 AP down to laptop (Mbps) | | | | |
| 1 AP up from laptop (Mbps) | | | | |
| 1 AP down, 4 streams | | | | |
| 2 USB RP5->Zero | | | | |
| 2 USB Zero->RP5 | | | | |
| 3 Zero down/up | | | | |
| 4 RP5 down/up | | | | |
| 5 Laptop at Roku (browser) | | | | |
| 6 Zero->GW ping min/avg/max | | | | |
| 6 RP5->Zero ping min/avg/max | | | | |
| 7 Concurrent (AP / uplink) | | | | |
| 8 Idle vs loaded RTT | | | | |

### After fixes

| Fix | Tests re-run | Before | After | Keep? |
|-----|--------------|--------|-------|-------|
| | | | | |

## Conclusion

(agent: one paragraph — limiting hop, fixes applied, fixes still pending for the user)
