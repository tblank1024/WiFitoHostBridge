# Plan: conventional WiFi picker for the Pi Zero uplink

## Context

The RV's internet can arrive through the USB-attached Pi Zero 2W ("the Zero", hostname `RP2W`)
acting as a WiFi client bridge: its `wlan0` joins a campground AP, and it hands the RP5 a routed
`10.10.0.0/24` link (Zero = `10.10.0.1` = the RP5's default gateway over `usb0`).

Today the only way to point it at a network is a blind three-field form on the Internet page —
SSID, password, "store permanently" — posting to `/api/wifi-config`. You must know the SSID
exactly and retype the password every time, even for a network the Zero already has credentials
for. Nothing shows what is in range, how strong it is, or which networks are already known.

**Goal:** a conventional picker. SSIDs in range with signal strength, click to connect, password
prompted only when the Zero has no saved credential. Plus a live status panel and per-network
Forget.

## Decisions (settled)

1. **Replace the raw socket with a small HTTP+JSON service on the Zero**, stdlib only. The RP5's
   FastAPI calls it with `urllib` — no vendored client script, no subprocess layer, no
   three-tier timeout juggling, no second hand-synced copy of a client file.
2. **Scope:** picker + per-network Forget + live status panel. No hidden-SSID entry, no
   autoconnect-priority editing.
3. **Map SSID → profile via `802-11-wireless.ssid`**, leaving existing profiles untouched (one
   deliberate exception: see "password replacement" below).
4. **Remove the old form entirely** — picker only.

## Measured ground truth (verified on live hardware)

| Finding | Consequence |
|---|---|
| Scanning **requires root**. As `tblank`, `--rescan yes` returns in 0.3 s with only the connected AP and no error; as root, 12–18 APs with signal | Scan must run inside the root service |
| Root `--rescan yes` took **0.92 s**; concurrent ping through the Zero: **0% loss** (40/40), RTT peak ~130 ms vs ~25 ms baseline | On-demand scan is safe while the RP5 rides the link |
| **Profile name ≠ SSID**: `ListenerManagedWifi`→`Salty Fox`, `asta`→`UpstairsEXT2.4G`, `preconfigured`→`Buckley Clan 2`, `RV_tblank`→`tblank` | The `RV_<ssid>` convention cannot detect saved networks |
| Two profiles hold SSID `Buckley Clan 2` (`preconfigured` ts=1763836527, `Buckley Clan 2` ts=0) — and one is **named** `Buckley Clan 2` | Disambiguate by timestamp; a new profile named after the SSID would collide |
| Terse output escapes `:` and `\` (a BSSID came back `7A\:A7\:41\:81\:21\:83`) | Need an unescape-aware splitter, or the colon bug replaces today's comma bug |
| **Multi-field `-g` omits inapplicable fields** — 4 fields requested on the loopback profile returned 3 lines | Never positionally parse multi-field `-g`; one field per call, or `-t -f` |
| `nmcli -t -f UUID,NAME,TYPE,TIMESTAMP connection show` returns all four in **one** call | No per-UUID timestamp query needed |
| All 9 wifi profiles are `autoconnect=yes, priority=0`; NM breaks ties by timestamp | NM's own autoconnect pick agrees with our rule — and this is the recovery net |
| Zero sees the RV's own AP **`Sophie`** (RP5 `/etc/hostapd/hostapd.conf:7`) at signal 57 | Joining it loops the uplink through the RP5 — block it both ends |
| Scan has duplicate SSIDs across BSSIDs (3× `tellMyWIFILoveHer`) and one empty-SSID hidden AP; SIGNAL is 0–100 | Dedupe by SSID keeping max signal; drop empty SSIDs |
| `psk-flags=0` on all 9 profiles; reading a PSK needs root | `has_psk` is a proxy, never read secrets |
| Zero has Python **3.11.2** with `http.server` | Stdlib service, no dependencies |
| `usb0` is **unmanaged by NetworkManager** (`managed=false`), addressed by `usb0-up.service` | The RP5↔Zero control path never traverses `wlan0`. You lose internet, never control. |
| `sudo` on the Zero needs a password for `tblank` | Deploys are one `ssh -t` with one prompt |

## Two problems found that are not about WiFi

**1. `isc-dhcp-server` reports `failed` while `dhcpd` (pid 686) is actually running**, and the RP5's
`10.10.0.17` is a DHCP lease from it (`default via 10.10.0.1 dev usb0 proto dhcp src 10.10.0.17`).
If that unsupervised process dies, the RP5 loses its address *and* its default route, including the
ability to fix it over the network. **Fix the unit, or give the RP5 a static fallback address on
`usb0`, before Phase 4.** Biggest strand-yourself risk in the system and unrelated to this feature.

**2. The Zero's control plane lives in units that exist in no repository:** `usb0-up.service`
(brings up `usb0`, assigns `10.10.0.1/24`), `iptables.service`/`ip6tables.service` (the
`MASQUERADE` rule out `wlan0`), the dhcpd config, and a corrupt dead `host-bridge.service`
(`ExecStart=/path/to/your/script/host-bridge.py`). One SD-card failure makes the bridge
unreproducible. Capture all of it into this repo — three files, cheapest durability win available.

## Phase 0 — small fixes first

- **Redact secrets from the listener's logs.** `RPZero2WListener.py:353` prints the entire
  `SET_WIFI,<ssid>,<password>` packet and `:87` prints the full nmcli argv, which contains
  `wifi-sec.psk <password>`. Both go to journald. **This was dormant because Python was
  block-buffering stdout; the `PYTHONUNBUFFERED=1` fix deployed on 2026-10-06 makes it live on the
  next form submission.** Redact before anything else, and design redaction into the new service.
- `server.py:568`: `async def wifi_config` → `def ... # Removed async`. It blocks the event loop
  for up to 120 s, stalling every other endpoint. Matches the file's own convention (`# Removed
  async` at 994, 1043, …).
- `server.py:490-493`: delete the dead duplicate `WiFiConfigData` (shadowed by 495-498).
- Capture the Zero's units (above) into this repo; delete the corrupt `host-bridge.service`.

## Phase 1 — Zero service, read-only endpoints, new port

New files here: `rpzero_wifi_api.py` (one file — deploy is `scp` + `cp` into `/usr/local/sbin`),
`wifi-bridge-api.service`, and a mode argument for `setup_services.sh` (`listener|api|both`).

`ThreadingHTTPServer` on **`10.10.0.1:12346`** — a new port, so it never contends with the old
listener on 12345 and both can run for weeks. Non-obvious requirements:

- `protocol_version = "HTTP/1.1"` **and** a `Content-Length` on every response, or urllib hangs.
- `timeout = 15` per connection, `daemon_threads`, `allow_reuse_address`, a
  `BoundedSemaphore(8)` → 503 (ThreadingMixIn is unbounded; this is a 512 MB Pi Zero).
- Source check: client IP in `10.10.0.0/24` else 403, plus a DROP rule for `wlan0:12346`.
- Unit: `After=usb0-up.service network-online.target`, `Wants=usb0-up.service`, `Restart=always`,
  plus a bind-retry loop — the existing listener only survives a too-early start by luck of
  `Restart=on-failure`.
- Unit env: `WIFI_API_BIND/PORT`, `WIFI_IFNAME=wlan0`, `SELF_AP_SSID=Sophie`, `CONNECT_TIMEOUT=45`,
  `SCAN_MIN_INTERVAL=10`, `PYTHONUNBUFFERED=1`, and **`LC_ALL=C`** — the Zero's locale is broken
  (`setlocale: LC_ALL: cannot change locale`) and nmcli output is locale-dependent.
- `SCRIPT_VERSION` logged at start and returned by `/api/health`, so a deploy can be confirmed
  from the RP5 without SSH.

Endpoints (read-only this phase):

| Route | Notes |
|---|---|
| `GET /api/health` | version, uptime |
| `GET /api/status` | `device status`, active UUID, IP/gateway as **separate single-field** calls, in-use scan row for live signal. Cached 1 s. No internet test — that is the RP5's job |
| `GET /api/networks?rescan=0\|1` | `-t -f IN-USE,SSID,SIGNAL,SECURITY,BSSID,FREQ device wifi list ifname wlan0`. **Hard 10 s floor between real rescans** so a stuck UI can't rescan while NM is associating. Returns `age_seconds` |
| `GET /api/profiles` | one `-t -f UUID,NAME,TYPE,TIMESTAMP` call + per-UUID single-field `-g` for ssid and psk-flags. Cached 15 s |

Row shape: `{ssid, signal, security, saved, in_use, profile, profile_uuid, profile_count,
joinable, blocked}`. `profile*` exists so the Forget dialog can name what it deletes.
Dedupe by SSID (max signal, OR the in-use flags); drop empty SSIDs; sort in_use → saved → signal →
casefolded ssid. `Sophie` is returned with `blocked:"self"` rather than filtered, so the UI can
explain why it is unselectable. Normalize security and add `unjoinable_reason` for WEP/802.1X.

Caching per resource with a single-flight guard; a `threading.Lock` reserved for mutating ops only.

## Phase 2 — RP5 backend, read-only

Insert after the existing WiFi endpoint (ends `server.py:640`), before
`# Internet Control Models and Endpoints` at 643, and well before the static mount at 2103 which
must stay last. `/api/wifi/...` does not collide with `@app.get("/wifi")` at 182 (the Plex page).

- Helper over `urllib.request` with `build_opener(ProxyHandler({}))` — **mandatory**, or the
  container's `http_proxy` env can hijack the call. `HTTPError` is a response: read its JSON body
  before classifying. Exceptions per failure mode, modelled on `kasa_power_strip.py:63-152`.
- Timeouts: status 2 s, profiles 6 s, networks-with-rescan 30 s, connect/forget 6 s.
- Routes `GET /api/wifi/{status,networks,profiles}`, `POST /api/wifi/{connect,forget}`,
  `POST /api/wifi/clear-cache`. All `def`, not `async def`.
- Return `-> dict` with `success`/`message`/collection — house style (`GET /api/debug/usb/status`
  at 1399-1441). No `List[...]` exists in this file and `typing` imports only `Annotated`; don't
  add the first typed list model.
- TTL caches on module globals mirroring `_kasa_strip_cache` (795-867): networks 20 s / 5 s
  failure, profiles 30 s. **Do not cache `/api/wifi/status`** — it is what the UI polls to watch a
  connect progress. No lock, matching the lock-free kasa cache, with a comment saying why.
- **Drop the `exit_code` contract for new endpoints** — there is no subprocess, and codes 100/101
  promise "SSID/PW were updated", which becomes false once a failed connect deletes the profile it
  created. Error codes instead: `bridge_unreachable`, `bridge_timeout`, `busy`, `blocked_self`,
  `auth_failed`, `no_ap`, `nm_failed`, …

## Phase 3 — mutating endpoints on the Zero

`POST /api/connect` returns **`202 {job_id}`** immediately and runs in a job thread; `GET
/api/status` carries `operation:{state, phase, ssid, error, started, finished}`. The status panel
becomes the single source of truth and no timeout needs to exceed ~30 s. Validate early (SSID
1–32 bytes, PSK 8–63, reject `Sophie`) rather than waiting 45 s for nmcli to refuse.

Three cases:
- **saved, no psk** → `nmcli -w 50 connection up uuid <UUID>` (the `uuid` keyword disambiguates
  from name).
- **saved + psk** (retyping a wrong password) → `connection modify <UUID> ...psk` then up. This is
  the one deliberate exception to "leave profiles untouched"; without it a saved-but-wrong-password
  network is a dead end. Irreversible, so the modal must say *"this replaces the saved password for
  profile X."*
- **not saved** → derive key-mgmt from the scan's SECURITY (open → no security section,
  WPA/WPA2 → `wpa-psk`, WPA3-only → `sae`); the existing `add_nm_wifi_connection` hardcodes
  `wpa-psk`. Profile named after the SSID, suffixed ` 2` on a name collision. **On any failure,
  delete the profile just added** — otherwise a typo permanently adds a bogus "saved" network.

Use blocking `nmcli -w 50 connection up` in the job thread rather than the existing polling
`check_nm_connection_status`: its stderr classifies failures far better (`Secrets were required` →
`auth_failed` in ~8 s, not 45 s) and the existing poller burns ~60 subprocesses on a 1 GHz Zero.
Also note that poller compares the profile **name**, which is wrong once names are arbitrary and
duplicable — compare UUID.

`POST /api/forget` takes `uuid` (SSID only when unambiguous). Guards: refuse non-wifi types
(protects the `usb0` and `lo` profiles), refuse the active connection unless `force:true`, hold the
mutate lock.

## Phase 4 — frontend

New `WifiPicker.jsx` in `page-internet/` (Internet.jsx is already 692 lines; `AlertsPanel.jsx` is
precedent for a standalone component). CSS appended to `Internet.css` under a banner — one file per
page folder is the convention. In `Internet.jsx`, replace lines 576-690 with
`{selectedOption === 'wifi' && <WifiPicker />}`, delete the form state and `handleWifiSubmit`
(279-343), and rewrite the `=== WIFI CONFIGURATION ===` block comment at 40-44.

- **Rows must be `.wifi-row`, not the existing unused `.status-item`** — that class flips to
  `flex-direction: column` below 768 px (`Internet.css:188-192`), which would make every row ~80 px
  on the primary device. Re-assert `flex-direction: row` inside that media block.
- Usable width is ~340 px, so **tap-to-act, not a button per row**: the whole row is a `<button>`
  (min-height 44 px); saved rows expand a one-line Connect/Forget strip. `.wifi-row__name` needs
  `min-width: 0` or a long SSID blows out the container.
- Signal bars hand-rolled (4 spans, heights 4/7/10/13 px, filled at 25/50/75) — Semantic's `wifi`
  icon has no strength variants. `lock` icon for secured, an "Open" chip for open.
- In-use row accented; `Sophie` shown disabled as "(this RV's own network)"; WEP/802.1X disabled
  with a reason, mirroring the disabled-row precedent at `debug.jsx:461-514`.
- Password Modal copying `AlertsPanel.jsx:176-231` — the app has **no text input inside a Modal
  anywhere**, so budget a second pass. Client-side 8–63 validation, Show toggle, and the password
  never enters the log.
- Forget modal names the profile it deletes, and warns when the SSID has 2 profiles or when the
  count of saved networks is getting low (NM autoconnect is the recovery net).
- Status poll: **5 s idle, 2 s while an operation is pending**, `silent` flag per
  `debug.jsx:163-187`, back off to 15 s after 3 consecutive `bridge_unreachable` with a message
  naming `10.10.0.1`, and stop on `visibilitychange` (always-on dashboard tablet).
- **Scan is not polled** — on mount and on explicit Rescan only, with a 10 s cooldown mirroring
  the Zero's floor.
- The output `TextArea` keeps its affordance but changes content: client-timestamped one-liners
  derived from status transitions, collapsed behind an "Activity log" toggle.

## Phase 5 — retire the old path

Only after a week of real use and a successful Zero reboot test. Delete `/api/wifi-config`
(567-640) and its models; delete `rv/webserver/server/RP5toRPZero2WControl.py` and its `COPY` line
(`webserver/Dockerfile:71`); swap `WIFI_BRIDGE_PORT=12345` for `WIFI_BRIDGE_API_PORT=12346` in
compose. **Keep** `WifitoHostBridge/RP5toRPZero2WControl.py` as the emergency CLI — the only tool
that works if both the new service and docker are down. `systemctl disable --now
wifi-bridge-listener` on the Zero, leaving the files installed as the documented fallback.

## Verification

Phase 1 is read-only; rollback is `systemctl stop wifi-bridge-api`, which cannot affect the uplink.

1. From the RP5: `curl` each endpoint. Expect no empty SSID, collapsed duplicates, exactly one
   `in_use`, `Sophie` with `blocked:"self"`, `Buckley Clan 2` twice with different UUIDs, and **no
   PSK anywhere in any response**.
2. **Escaping check — most likely thing to be silently wrong.** Create a throwaway profile with a
   colon in its name and SSID (`autoconnect no`, so it can never activate), confirm `/api/profiles`
   returns `test:colon` and not `test`, then delete it by UUID.
3. **Uplink impact:** `ping -i 0.3 -c 40 8.8.8.8` during a forced rescan — baseline is 0/40 lost,
   130 ms peak.
4. **Concurrency:** a `/api/status` loop in one shell while `/api/networks?rescan=1` runs in
   another; neither blocks.
5. **Negative:** from a device on `Sophie`, `curl http://<zero-wlan0-ip>:12346/` must fail (not
   bound). Garbage POST → 400, no traceback. `{"ssid":"Sophie"}` → 400 `blocked_self`.

Phase 3 is the risky one. **Test from a phone joined to `Sophie`, not from a session routing
through the Zero** — `br0` is `10.0.0.1/24` and the RP5 runs hostapd, so that device reaches the
dashboard regardless of the Zero's state. This inverts the README's current advice, which was
written for a laptop on normal internet. Before starting: save the profile list off-box (a deleted
PSK is unrecoverable), know the current AP's password, and resolve the dhcpd question.

In order: Forget an inactive out-of-range profile → Forget guards (non-wifi type refused, active
refused without `force`) → connect to the **already-connected** SSID → connect to a second saved AP
and back, watching that `default via 10.10.0.1` never changes (NAT is `-o wlan0`, interface-based,
so it should survive) → **deliberate bad password**, expecting `auth_failed` in <15 s *and the
just-added profile gone* → deliberate out-of-range SSID → only then a real new network → finally
reboot the Zero, which tests `After=usb0-up.service`.

**Recovery.** `usb0` is static and NM-unmanaged, so the Zero is always reachable at `10.10.0.1`
even with no internet: phone on `Sophie` → dashboard → pick a known network; else
`curl -X POST .../api/connect` from the RP5; else `ssh zero` + `sudo nmcli connection up uuid <id>`;
last resort, power-cycle the Zero from the Internet page (USB port 2) and let NM autoconnect to the
highest-timestamp in-range profile. **Always keep at least two known-good saved profiles.**

## Risks

- A failed connect strands the RV's internet → auto-cleanup of the failed profile, NM autoconnect,
  and the recovery card above. Test the recovery path before Phase 3 touches anything real.
- **Destructive Forget is new.** Any device on the RV LAN can already make the RV join a network
  (the form does that today), but deleting saved profiles is a new unauthenticated capability on a
  dashboard with no auth at all. Accept that consciously or gate it.
- A PSK still crosses the USB link in plaintext, exactly as today. Fine on a point-to-point cable;
  noted so it is not mistaken for a regression.
- Deploys travel over the link being reconfigured — mitigated by `usb0` being independent of
  `wlan0`, verified three times this session by restarting services with zero packet loss.
