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

## Current direction (2026-10-13)

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
- **Uplink *selection* among the four is new work.** Existing logic only answers "which SSID should
  this interface join", not "which interface carries the default route".

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
