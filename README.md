# WiFitoHostBridge

The WiFi uplink service for the RV: a root HTTP/JSON API that drives NetworkManager so the
dashboard can scan, join and forget networks. Scanning needs root — unprivileged, `nmcli device
wifi list --rescan yes` silently returns only the already-connected AP — which is why this is a
service and not dashboard code.

One script, `rpzero_wifi_api.py`, with two deployments. The unit's `Environment=` block is the
whole difference.

## Architecture

**Current — the radio is on the RP5** (Alfa AWUS036ACM, `wlan1`):

```
 Internet <~~ 2.4/5 GHz (wlan1) ~~ [RP5 / "Sophie"] ===== br0 ===== RV LAN
                                     ^  wifi-api-rp5.service
                                     |  127.0.0.1:12346, root
 dashboard (Internet page) -> server.py /api/wifi/* --'
```

`wlan0` on the RP5 is the RV's own AP (hostapd) and is NM-unmanaged, so nothing here can touch it.
The dashboard container is `network_mode: host`, so loopback reaches the service.

**Legacy — the radio on the Pi Zero 2W**, reached over the USB gadget link. **Physically
disconnected from Sophie on 2026-10-18**, so this is history, not a live fallback: there is no
`usb0` and `10.10.0.1` does not answer. Reviving it means re-plugging the gadget cable, then
`setup_services.sh zero` on the Zero and `WIFI_BRIDGE_HOST=10.10.0.1` in compose. The Zero was
1x1 and 2.4 GHz only, which is the band that measured 10–40× slower at home
(`Results_WiFi_testing.md`).

```
 Internet <~~ 2.4 GHz (wlan0) ~~ [Pi Zero 2W] -- USB 10.10.0.x -- [RP5] == RV LAN
                                   ^  wifi-bridge-api.service
                                   |  10.10.0.1:12346, root
```

| File | Runs on | Purpose |
|------|---------|---------|
| `rpzero_wifi_api.py` | RP5 or Zero, as root at `/usr/local/sbin/` | The API. `WIFI_IFNAME` picks the radio |
| `wifi-api-rp5.service` | RP5 | Unit for the Alfa (`wlan1`), bound to `127.0.0.1` |
| `wifi-bridge-api.service` | Zero | Unit for the Zero (`wlan0`), bound to `10.10.0.1` |
| `setup_services.sh` | either | Installs script + unit; `rp5` or `zero` |
| `zero-system/` | Zero | The Zero's own system units (`usb0-up`, `dhcpd`, …) — see its README |

The dashboard side lives in the `rv` repo: `webserver/server/server.py` (`/api/wifi/*` proxy,
`WIFI_BRIDGE_HOST` selects the deployment) and `webserver/client/src/page-internet/`.

## The HTTP API (port 12346)

Unauthenticated; **the bind address is the only access control.** Never bind `0.0.0.0`. On the RP5
this matters more, not less: besides the foreign network on `wlan1`, `br0` carries RV guest
devices, and a reachable API there would let any guest reconfigure the uplink.

| Endpoint | |
|---|---|
| `GET /api/health` | version, uptime, ifname |
| `GET /api/status` | state, SSID, signal as both percent and **`signal_dbm`** (real RSSI, read live with `iw`), **band of the associated BSS**, IP, gateway, active profile, and the in-flight operation |
| `GET /api/networks` | scan deduped by SSID; `?rescan=1` forces one (10 s floor). Includes `bands`, `has_5g`, `profile_count`, `signal_dbm` and `signal_dbm_estimated` |
| `GET /api/profiles` | saved WiFi profiles with `band`, `priority`, `has_psk` |
| `POST /api/connect` | `{ssid, psk}`. Returns 202 and a `job_id`; poll `/api/status` |
| `POST /api/forget` | `{uuid}` for one profile, or `{ssid}` for every profile of that network. `force` required if one is active |

No endpoint returns a PSK, and nmcli argv is redacted before logging.

### Signal: percent and dBm

`nmcli`'s `SIGNAL` is a 0-100 quality percentage, not dBm, so RSSI comes from
`iw`: `iw dev <ifname> link` for the live association (exact, and available even
before the first scan) and `iw dev <ifname> scan dump` per BSSID for the list.
`scan dump` only reads what the kernel already has, so it triggers no scan and
cannot disturb the link.

NM's percentage is *not* losslessly reversible -- measured 2026-10-18 it agreed
with `iw` within 1 dB on most BSSes but read 42% where the inverse predicts 37%
on the associated BSS, which NM smooths. And the kernel's dump routinely covers
fewer BSSes than `nmcli` lists, so a network's strongest BSS may have no `iw`
reading. In that case `signal_dbm` is derived from the percentage and
`signal_dbm_estimated` is true; the picker prefixes those with `~`.

The dBm is always tied to **the same BSS the percentage describes**. Reporting
the best reading across all of an SSID's BSSes gave better `iw` coverage but put
-78 dBm beside 65% bars, because the dump held only the 5 GHz BSSes while the
strongest was a 2.4 GHz one near -61.

### Profiles it creates

A dual-band SSID gets **two** profiles: `<name>-5g` locked to 5 GHz at autoconnect-priority 10,
and `<name>` unlocked at priority 0. NetworkManager ranks BSSes by signal strength alone and that
is the wrong metric — measured 2026-10-17, it chose 2.4 GHz at −62 dBm over 5 GHz at −78 dBm, and
the band it chose was 10–40× slower because five networks shared the 2.4 GHz channel. NM has no
band *preference*, only a band lock, and a locked profile fails outright where that band is
absent — hence the unlocked fallback, which `connect` also falls back to if the 5 GHz BSS has gone
since the scan.

A network **already saved** as a single unlocked profile is not restructured on connect — the API
hardens it but does not add a sibling, so it keeps whatever band NM picks. To upgrade one by hand:

```bash
sudo nmcli con add type wifi ifname wlan1 con-name "<name>-5g" ssid "<ssid>" -- \
  802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.psk '<psk>' \
  802-11-wireless.band a connection.autoconnect-priority 10 \
  802-11-wireless.powersave 2 802-11-wireless.cloned-mac-address permanent ipv6.method disabled
```

Every profile it creates or activates gets `802-11-wireless.powersave 2` (off),
`cloned-mac-address permanent` (a captive portal authorizes a MAC; a random one per connection
means logging in again every time) and `ipv6.method disabled` (a park network's SLAAC address
would bypass the v4-only firewall rules).

## Deploying

```bash
sudo ./setup_services.sh rp5      # on the RP5, for the Alfa
sudo ./setup_services.sh zero     # on the Zero  [legacy]
```

It refuses to install one while the other unit is enabled — they would collide on port 12346 and
both drive the same NetworkManager. Then point the dashboard at the live one in
`rv/docker/docker-compose.yml` (`WIFI_BRIDGE_HOST=127.0.0.1` or `10.10.0.1`) and
`docker compose up -d webserver`.

Bump `SCRIPT_VERSION`: it is logged at start-up, so `journalctl` confirms which code is running.
Restarting the service never disturbs the uplink — NetworkManager owns the radio independently of
this process. A `connect` *does*, so don't test one from a session that depends on that uplink.

## Troubleshooting (in order)

1. **Is the service up?** `systemctl status wifi-api-rp5` (or `wifi-bridge-api` on the Zero).
2. **Does it answer?** `curl -s http://127.0.0.1:12346/api/health` (`10.10.0.1` for the Zero).
   The dashboard's error message names the address it tried.
3. **What did it do?** `journalctl -u wifi-api-rp5 -n 100`. Every nmcli command is logged
   (redacted), with the phase of each connect.
4. **Does nmcli work by hand?** `nmcli device wifi list ifname wlan1`, `nmcli connection show`.
   A rescan as a non-root user returns only the connected AP — that is expected, not a fault.
5. **Right interface?** `/api/health` reports `ifname`. On the RP5 the Alfa is pinned to `wlan1`
   by MAC in `/etc/systemd/network/11-wlan-wan.link`; `wlan0` is the AP and must stay NM-unmanaged
   (`nmcli dev status`).
6. **Bad profile left behind?** `/api/profiles`, then `POST /api/forget` with its `uuid`. A failed
   connect deletes the profiles it created, so a leftover means the delete failed — the journal
   says so.
7. Password must be 8–63 chars for WPA-PSK. Open and WPA3 networks are handled; 802.1X is not.

## The retired socket path

`RPZero2WListener.py`, `RP5toRPZero2WControl.py` (both copies), the `wifi-bridge-listener.service`
unit, TCP:12345 and the dashboard's `/api/wifi-config` were removed on 2026-10-18 — `plan_wifi.md`
Phase 5. The protocol could not carry a list (requests were comma-split, so an SSID with a comma
was unparseable, and both ends did one unframed `recv(1024)`, which a 12–18 AP scan overflows).

The CLI was kept in earlier plans as an emergency path, on the grounds that it worked when the
dashboard was down. That stopped being true: it needed the listener service running, and with the
radio on the RP5 the real emergency path is plain `sudo nmcli dev wifi connect` on the host.

Recover any of them from git if the Zero is ever brought back:
`git show 2897e46:RPZero2WListener.py`. The copies installed on the Zero's own SD card under
`/usr/local/sbin/` were never removed — the Zero was unplugged before anyone disabled the unit, so
if it is ever powered up again the listener will start. Disable it then
(`systemctl disable --now wifi-bridge-listener`), not before: it is unreachable now.

## Working on this code

See `CLAUDE.md` for the traps that cost time (interface naming, which uplink is which, the Coolgear
hub's control chip, and the measurement tools that lie). `plan_wifi.md` carries the measured
ground-truth table for nmcli's quirks — read it before touching the parsing.
