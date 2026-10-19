# CLAUDE.md — WiFitoHostBridge

Gives the RP5 ("Sophie", hostname `RV1-PI5`) an internet uplink through a USB-attached Pi Zero 2W
("the Zero", hostname `RP2W`) whose `wlan0` joins a foreign AP. The Zero hands the RP5 a routed
`10.10.0.0/24` link and is its default gateway.

**Architecture is mid-transition — read "Current direction" before planning anything.**

## Where things are documented

Don't duplicate these; they are current and detailed.

| Doc | Covers |
|-----|--------|
| `README.md` | Component/file map, both deployments, the HTTP API, deploy steps, troubleshooting order |
| `plan_wifi.md` | WiFi picker plan, Phases 0–5, and a large **measured ground truth** table (nmcli quirks, root-required scanning, profile-name≠SSID). Read the table before touching nmcli parsing. |
| `zero-system/README.md` | The Zero's system units (they exist in no other repo) + 3 known problems, incl. the unsupervised `dhcpd` strand risk |
| `Results_WiFi_testing.md` | Throughput/RF measurements, and a **methodology-warnings** table of tools that lie |
| `plan-speed.md` | Throughput test procedures. Tests 1, 5, 7 unrun; its Background premise is falsified |

## Current direction (updated 2026-10-18)

The Zero has been **replaced**. An Alfa AWUS036ACM (MT7612U, 2×2 dual-band) now runs on the RP5 as
`wlan1`, one of four uplink options alongside wired USB-ethernet, Starlink and cellular.

Done, on hardware:

- **The adapter needed nothing.** `mt76x2u` is in the stock 6.12.96 kernel; no modeswitch, no
  firmware package, regulatory domain already `US`. Its power LED stays dark until something brings
  the interface up — not a fault. Measurements: `rv/plan_new_WAN_WiFi.md`, "Measured on hardware".
- **Names are pinned by MAC** (deployed 2026-10-18, udev confirms both applied):
  `/etc/systemd/network/10-wlan-ap.link` → `wlan0` (onboard `2c:cf:67:ab:1b:94`),
  `11-wlan-wan.link` → `wlan1` (Alfa `00:c0:ca:be:29:62`). Needed because enumeration order can
  swap at boot and a hub-port toggle could otherwise return the adapter as `wlan2`.
- **`wlan0` is NM-unmanaged** via `/etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf`, so hostapd
  keeps it and nothing on the uplink path can take the AP down. Reference copies of all three files
  live in `rv/raspap/`; no installer produces them.
- **`rpzero_wifi_api.py` needed no interface changes.** `IFNAME` comes from `WIFI_IFNAME` and
  threads through every nmcli call. The RP5 deployment is `wifi-api-rp5.service`
  (`WIFI_IFNAME=wlan1`, `WIFI_API_BIND=127.0.0.1`, `WIFI_API_ALLOWED_NET=127.0.0.0/8`), with
  `WIFI_BRIDGE_HOST=127.0.0.1` for the web server. `setup_services.sh rp5|zero` installs either and
  refuses to run both — they would collide on 12346 and fight over one NetworkManager.
- **`plan_wifi.md` Phase 5 is done** (2026-10-18): `RPZero2WListener.py`, both copies of
  `RP5toRPZero2WControl.py`, `wifi-bridge-listener.service`, TCP:12345 and `/api/wifi-config` are
  deleted. Recover with `git show 2897e46:<file>`. The copies on the Zero's SD card were never
  removed, but the Zero was **physically disconnected from Sophie on 2026-10-18** — no `usb0`,
  `10.10.0.1` unreachable — so it is not a live fallback and the unit cannot be disabled remotely.
  Do that if it is ever powered up again.
- **The picker now saves two profiles per dual-band SSID** — `<name>-5g` locked to 5 GHz at
  autoconnect-priority 10, plus an unlocked fallback — because NM ranks BSSes by signal alone and
  measured 10–40× slower on the stronger 2.4 GHz band. Forget therefore deletes *every* profile for
  an SSID, not one; the picker calls it by `ssid`, not `uuid`.
- **Keep `SELF_AP_SSID=Sophie`.** Joining the RV's own AP loops the uplink back through the RP5.
- **Binding matters more on the RP5, not less.** The service comment warns against `0.0.0.0`
  because the Zero's `wlan0` faces a foreign network. On the RP5, `br0` carries RV guest devices,
  so a root API reachable from `br0` lets any RV WiFi client reconfigure the uplink.

Still open: the antenna/outside mount, moving the adapter onto the hub (it is on a direct RP5 USB
port today, and the Pi 5's 600 mA total USB cap applies — see the plan's issue 6), captive-portal
handling in `pi5connect.sh`, and `plan_firewall.md`.

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
- **Only one WAN source is normally enumerated, so the interface name does not identify the WAN
  type** — don't key policy on it. The four are WiFi (now the Alfa on `wlan1`), cellular, Starlink
  and wired. ~~Starlink and wired share one USB-ethernet adapter.~~ **Corrected by the user
  2026-10-18: Starlink and wired are two separate adapters on two separate USB ports.** The
  2026-10-17 sweep inferred a shared adapter from finding nothing on Coolgear port 4 — but port 4
  is where the FT232R control chip lives, so "nothing attached" never implied "shared". On the
  Plugable each WAN source has its own socket (`/etc/usbhub/sockets.json`, roles 1=wifi, 2=wired,
  3=cell, 4=starlink).

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

- **The agent's own internet goes out the live WAN, so losing it freezes the session.** Check
  `/run/pi5connect-wan` and `ip route` for which interface that is right now — it has been `usb0`
  (via the Zero), `eth1` (wired) and could be `wlan1`. Anything that reconnects *that* interface
  (`nmcli con up/down`, `dev reapply`, BSSID lock, a reboot, `dtoverlay`) kills the session; hand
  those to the user, and prefer a **timed auto-revert** so a mistake restores itself:
  `sudo systemd-run --unit=netrevert --on-active=5min <restore command>`.
  `iw ... set power_save` and `nmcli con modify` (without reactivating) are safe.
  When adding a new uplink, give it a **higher** `ipv4.route-metric` than the live one so it cannot
  steal the default route, test it interface-bound (`curl --interface`, `ping -I`), and only then
  lower the metric. Note `pi5connect.sh` v2.1 also keeps a healthy incumbent WAN rather than being
  preempted, which is a second layer of the same protection.
- **hostapd restarts drop every WiFi client on Sophie**, possibly the user's own terminal. Back up
  the config, state the revert first, ask.
- **No passwordless sudo on the RP5.** Most diagnostics work unprivileged (`iw`, `ip`, `ethtool`,
  `bridge`, `nmcli` reads, and `hostapd.conf` is world-readable). Hand sudo commands to the user.
  Caveat: sudoers is `(ALL : ALL) ALL` with `timestamp_type=global`, so after the user runs any `sudo`
  command `sudo -n` starts working for a while and then silently stops. Don't build on it — re-check
  `sudo -n true` before a sequence that must complete, and verify a handed-off command actually landed
  (`ls -l` the target) rather than trusting "done".
- **The Zero is unplugged (2026-10-18) — don't try to reach it.** No `usb0`, and `10.10.0.1` does
  not answer. If it is ever re-plugged: it is `ssh zero` (user `tblank`, no passwordless sudo) and
  `ssh zero-root` (root), **not** `pi@10.10.0.1`; its locale is broken, so set `LC_ALL=C` and
  filter `setlocale` noise from ssh output.
- **The design property worth copying from the Zero:** its `usb0` was NM-unmanaged and statically
  addressed by `usb0-up.service`, so losing the AP or restarting NM cost *internet* but never
  *control*. The RP5 has the same split today — `br0` is independent of the uplink, so a failed
  `wlan1` leaves the RV LAN and SSH over `br0` intact. Preserve that: never put the uplink and the
  control path on one interface.
- Wrap network tests in `timeout`. Save raw output under `~/speed-results/<YYYYMMDD-HHMM>/`.

## Environment

- RP5: `br0` 10.0.0.1/24 (AP `Sophie` + dnsmasq), `eth0` bridged into `br0`, onboard `wlan0` the AP
  radio (5 GHz capable: phy0 band 2, ch36–64), Alfa `wlan1` the WiFi uplink, `eth1` the
  USB-ethernet uplink. Docker services per `rv/docker/docker-compose.yml`. Python 3.11.2, stdlib
  only for the API.
- Zero (gone since 2026-10-18, kept for reference): `wlan0` to the foreign AP (2.4 GHz only),
  `usb0` 10.10.0.1 gadget to the RP5, masqueraded `10.10.0.0/24` out `wlan0`.
- Upstream subnet varies by location (home `192.168.2.0/24`; campgrounds differ). Don't hardcode it.
- RV LAN devices of note: `SophieRoku` 10.0.0.163, `TomsLaptop` 10.0.0.96.
