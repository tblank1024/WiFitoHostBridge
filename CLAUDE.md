# CLAUDE.md — WiFitoHostBridge

Gives the RP5 ("Sophie", hostname `RV1-PI5`) an internet uplink through a USB-attached Pi Zero 2W
("the Zero", hostname `RP2W`) whose `wlan0` joins a foreign AP. The Zero hands the RP5 a routed
`10.10.0.0/24` link and is its default gateway.

**Architecture is mid-transition — read "Current direction" before planning anything.**

## Where things are documented

Don't duplicate these; they are current and detailed.

| Doc | Covers |
|-----|--------|
| `README.md` | Component/file map, the TCP protocol, exit codes, troubleshooting order |
| `plan_wifi.md` | WiFi picker plan, Phases 0–5, and a large **measured ground truth** table (nmcli quirks, root-required scanning, profile-name≠SSID). Read the table before touching nmcli parsing. |
| `zero-system/README.md` | The Zero's system units (they exist in no other repo) + 3 known problems, incl. the unsupervised `dhcpd` strand risk |
| `Results_WiFi_testing.md` | Throughput/RF measurements, and a **methodology-warnings** table of tools that lie |
| `plan-speed.md` | Throughput test procedures. Tests 1, 5, 7 unrun; its Background premise is falsified |

## Current direction (updated 2026-10-17)

The Zero is being **replaced**. An Alfa AWUS036ACM (MT7612U, 2×2 dual-band) moves to the RP5, where
it becomes one of four uplink options: wired via USB-ethernet, Starlink, cellular, and Alfa WiFi.

- **`rpzero_wifi_api.py` needs no code changes for this.** `IFNAME` (line 62) comes from
  `WIFI_IFNAME` and threads through every nmcli call. Relocating means running the same service on
  the RP5 with `WIFI_IFNAME=wlan1`, `WIFI_API_BIND=127.0.0.1`,
  `WIFI_API_ALLOWED_NET=127.0.0.0/8`, and `WIFI_BRIDGE_HOST=127.0.0.1` for the web server
  (`server.py:663` already reads it from env; containers are `network_mode: host`).
- **Keep `SELF_AP_SSID=Sophie`.** Joining the RV's own AP loops the uplink back through the RP5.
- **Binding matters more on the RP5, not less.** The service comment warns against `0.0.0.0`
  because the Zero's `wlan0` faces a foreign network. On the RP5, `br0` carries RV guest devices,
  so a root API reachable from `br0` lets any RV WiFi client reconfigure the uplink.
- Replacing the Zero retires `RPZero2WListener.py`, both copies of `RP5toRPZero2WControl.py`,
  TCP:12345, and `/api/wifi-config`'s subprocess — i.e. it forces `plan_wifi.md` Phase 5.
- **Uplink selection already exists: `/usr/local/sbin/pi5connect.sh`** (repo: `rv/raspap/pi5connect.sh`),
  run by `/etc/NetworkManager/dispatcher.d/99-pi5connect` on every interface up/down/connectivity-change
  and by `setup-bridge.sh` at boot. It picks the WAN, sets the default route, and rewrites the NAT and
  FORWARD rules, writing the choice to `/run/pi5connect-wan`. The Alfa becomes a candidate in it, not a
  new layer. It also means **`/etc/iptables/rules.v4` is stale and does not matter** — it still names
  `eth1`, but pi5connect overwrites the rules at runtime, which is why the system survives reboots.

## Traps that cost time to discover

- **`eth0` is NOT an uplink.** It is a `br0` bridge slave (with `wlan0`), i.e. a LAN port on the RV's
  `10.0.0.0/24`. Enslaved by `raspap-br0-member-eth0.network`; dhcpcd has `denyinterfaces wlan0,eth0`.
  Plugging it into a live home switch bridges that LAN into the RV LAN and puts the RV's dnsmasq
  (serving `10.0.0.50-254`) onto it. The uplink role belongs to a separate interface (`eth1` in the
  original design, `usb0` today).
- **The Zero cannot host a USB WiFi adapter.** Its single data port is in gadget mode
  (`/sys/class/udc/*/state` = `configured`) — that *is* the RP5 link. A port can't be device and
  host at once, and the other micro-USB is power-only. Also the Zero 2W radio is **2.4 GHz only**.
- **`rv/buildRP5RaspAP/install_raspap_bridge.sh:195-197` would regress the live AP** to `hw_mode=g`
  / `channel=7` / `wmm_enabled=0`. The live `/etc/hostapd/hostapd.conf` was hand-fixed on
  2025-08-21 to 11n + WMM on channel 1 and has since lost `rsn_pairwise=CCMP` (line 205 in the
  script). Don't re-run that installer without fixing it.
- **Two copies of `RP5toRPZero2WControl.py`** — here and `rv/webserver/server/`. Edit here, copy over.
- **Deployed code drifts from the repo. Diff before trusting any repo copy.** Three confirmed cases:
  the live `hostapd.conf` (hand-fixed to 11n/WMM while the installer still writes 11g), `rules.v4`
  (names `eth1`; superseded at runtime by pi5connect), and `pi5connect.sh` (deployed was v2.0 while the
  repo held a much older version with a different selection algorithm and different log strings).
  `md5sum` the deployed file against the repo first.
- **Interface names are driver-dependent, and `enx*` never appears.**
  `/etc/systemd/network/73-usb-net-by-mac.link` and `99-default.link` are symlinks to `/dev/null`,
  masking `NamePolicy=mac`. So `asix`/`ax88179_178a`/`r8152` adapters get `eth1`, and
  `cdc_ether`/`cdc_ncm`/`rndis_host` get `usb0`/`usb1`. The Zero's gadget is `cdc_ether` → `usb0`.
  Any `enx<mac>` entry in a config is dead code.
- **The USB-ethernet adapter is not broken — don't re-diagnose it as a driver problem.** It enumerated
  as `eth1`, took a DHCP lease and ran as WAN for 8-13 hour stretches on 2026-10-05/06/13
  (`/var/log/pi5connect.log`). The kernel carries the full USB-ethernet driver set (`asix`,
  `ax88179_178a`, `r8152`, `cdc_ether`, `dm9601`, `mcs7830`, `smsc75xx`, …). The real fault: on Oct 6
  and Oct 13 it landed on **192.168.0.0/24** while the house LAN is **192.168.2.0/24** (a laptop on the
  same wall port gets 192.168.2.x), and v2.0 of pi5connect committed to it without checking. Open
  question: what device serves 192.168.0.0/24 — most likely Starlink, which shares that adapter.
- **The 4 WAN sources share one USB switch port into the RP5, so only one is normally enumerated.**
  WiFi (via the Zero), cellular, Starlink and wired; Starlink and wired share one USB-ethernet adapter.
  Because of that, the interface name does not identify the WAN type — don't key policy on it.

## Known hardware failure: the Coolgear hub's control chip

The USB switch is a **Coolgear 4-port hub = the Genesys `05e3:0610` at USB path `1-2`**. Its FT232R
control chip (`0403:6001`, serial `B0033FMU`) sits on the hub's **own port 4**; the WAN devices are on
ports 1-3 (the Zero was `1-2.2`). Control is serial over `/dev/coolgear-hub` → `/dev/ttyUSB*`.

On 2026-10-14 the FT232R dropped off the bus with repeated `error -71` (`-EPROTO`) and would not
re-enumerate even after the kernel's own port power cycle. `over_current_count` stayed 0, so it was not
a power trip — more likely connector or signal integrity. It stayed gone for 2.5 days.

**Symptom:** internet keeps working but WAN port selection is dead, and the debug page cannot reach the
switch. That is diagnostic: the hub's *data* path is fine (the active WAN runs through it); only its
control interface is gone. **Restarting the cmd app cannot fix this** — there is no device to open.

**Recovery — toggle only port 4. ~11 s, no reboot, does not disturb the active WAN:**
```bash
P=/sys/bus/usb/devices/1-2:1.0/1-2-port4
echo 1 | sudo tee $P/disable >/dev/null; sleep 3
echo 0 | sudo tee $P/disable >/dev/null; sleep 8
ls -l /dev/coolgear-hub          # expect a symlink to ttyUSB0
```
Verified working 2026-10-17. Safe because port 4 holds only the control chip. **Never unbind or reset
the hub itself (`1-2`)** — that resets every port, including the active WAN, and kills the session.
The container's `/host-dev` is a live bind mount of the host `/dev`, so it sees the recovered node
without a restart; a stale `/dev/ttyUSB0` inside the container works again once the minor is reused.

## Measurement tools that lie

Established by direct comparison; see `Results_WiFi_testing.md` for the evidence.

- **`speedtest-cli` understated download by ~2.5×** on this hardware (7.6–10.4 vs curl's 17.9–20.6
  Mbps on the same link). Not a CPU limit — the faster RP5 reported *lower* than the Zero. Use
  `curl` against a large object, or `iperf3`.
- **`iw` rate readings decay on an idle link.** It reports the *last used* rate; with no traffic
  minstrel decays toward the low rates. Observed sitting at 5.5 Mbit/s, recovering to 62 within 15 s
  once traffic flowed. **Keep a `ping` running while sampling rates.** RSSI is unaffected (beacons).
- **`tx failed` is cumulative since association**, not a current rate. Use a delta over a known interval.
- Bound a throughput test by bytes, not `--max-time`, or a timeout cutoff silently becomes your result.

## Working on this system

- **The agent's own internet goes RP5 → Zero → foreign AP.** Anything that reconnects the Zero's
  WiFi (`nmcli con up/down`, `dev reapply`, BSSID lock, Zero reboot, `dtoverlay`) kills the session.
  Hand those to the user. `iw ... set power_save` and `nmcli con modify` (without reactivating) are safe.
  When adding a new uplink, give it a **higher** `ipv4.route-metric` than the live one (currently
  `usb0` at 101) so it cannot steal the default route, test it interface-bound
  (`curl --interface`, `ping -I`), and only then lower the metric.
- **hostapd restarts drop every WiFi client on Sophie**, possibly the user's own terminal. Back up
  the config, state the revert first, ask.
- **No passwordless sudo on the RP5.** Most diagnostics work unprivileged (`iw`, `ip`, `ethtool`,
  `bridge`, `nmcli` reads, and `hostapd.conf` is world-readable). Hand sudo commands to the user.
  Caveat: sudoers is `(ALL : ALL) ALL` with `timestamp_type=global`, so after the user runs any `sudo`
  command `sudo -n` starts working for a while and then silently stops. Don't build on it — re-check
  `sudo -n true` before a sequence that must complete, and verify a handed-off command actually landed
  (`ls -l` the target) rather than trusting "done".
- **The Zero is `ssh zero` (user `tblank`, no passwordless sudo) and `ssh zero-root` (root).**
  Not `pi@10.10.0.1`. Use `zero-root` for anything privileged.
- `usb0` is **unmanaged by NetworkManager** on the Zero and statically addressed by
  `usb0-up.service`. This is the system's best property: losing the AP or restarting NM costs
  *internet* but never *control* — `10.10.0.1` stays reachable. Preserve it.
- The Zero's locale is broken; set `LC_ALL=C` and filter `setlocale` noise from ssh output.
- Wrap network tests in `timeout`. Save raw output under `~/speed-results/<YYYYMMDD-HHMM>/`.

## Environment

- RP5: `br0` 10.0.0.1/24 (AP `Sophie` + dnsmasq), `usb0` 10.10.0.x to the Zero, `eth0` bridged into
  `br0`, 5 GHz capable (phy0 band 2, ch36–64). Docker services per `rv/docker/docker-compose.yml`.
- Zero: `wlan0` to the foreign AP (2.4 GHz only), `usb0` 10.10.0.1 gadget to the RP5, masquerades
  `10.10.0.0/24` out `wlan0`. Python 3.11.2, stdlib only.
- Upstream subnet varies by location (home `192.168.2.0/24`; campgrounds differ). Don't hardcode it.
- RV LAN devices of note: `SophieRoku` 10.0.0.163, `TomsLaptop` 10.0.0.96.
