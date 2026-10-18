#!/usr/bin/env python3
"""
WiFi bridge HTTP API — runs as root on whichever host owns the uplink radio.

Two deployments, same code; the unit's Environment= block is the whole
difference (see wifi-bridge-api.service for the Zero, wifi-api-rp5.service for
the RP5):

  * Pi Zero 2W ("the Zero"): WIFI_IFNAME=wlan0, bound to 10.10.0.1, the USB
    link to the RP5.  Being retired.
  * RP5 ("Sophie") with the Alfa USB adapter: WIFI_IFNAME=wlan1, bound to
    127.0.0.1.  wlan0 there is the RV's own AP and is NM-unmanaged, so nothing
    here can touch it.

Why this exists
---------------
The RP5's dashboard needs to show the networks the uplink radio can see, with
signal strength, and which of them already have stored credentials. Scanning
requires root (as an unprivileged user, `nmcli device wifi list --rescan yes`
silently returns only the connected AP), so the privileged work has to live in a
service like this one.

It replaced a comma-delimited socket protocol (RPZero2WListener.py, deleted
2026-10-18) that could not carry a list: requests were split on commas, so an
SSID containing a comma was unparseable, and both ends did a single unframed
recv(1024), which a 12-18 AP scan result overflows.

Exposure
--------
The bind address is the only access control; there is no authentication, in
keeping with the RP5 dashboard this serves. Binding 0.0.0.0 would publish a
root-privileged "join this network" API, so don't.

  * On the Zero: 10.10.0.1, the point-to-point USB link, with 10.10.0.0/24
    allowed. Its wlan0 faces a foreign network (campground, marina).
  * On the RP5: 127.0.0.1 with 127.0.0.0/8 allowed. Binding matters MORE here,
    not less — besides the foreign network on wlan1, br0 carries RV guest
    devices, and a reachable API there lets any RV WiFi client reconfigure the
    uplink. The dashboard container is network_mode: host, so loopback reaches
    it.

Secrets
-------
Passwords are never logged. nmcli argv is redacted before printing, and no
endpoint returns a PSK — `has_psk` is derived from psk-flags, so the secret is
never read in the first place.

Endpoints:
    GET  /api/health     version and uptime
    GET  /api/status     interface state, SSID, signal, IP, active profile
    GET  /api/networks   scan results, deduped by SSID   (?rescan=1 to force)
    GET  /api/profiles   saved WiFi profiles, with has_psk
    POST /api/connect    join an SSID (async; poll /api/status)
    POST /api/forget     delete the saved profile(s) for an SSID

Logs
----
print() to stdout, captured by journald. The unit MUST set PYTHONUNBUFFERED=1 or
Python block-buffers stdout into the journal pipe and nothing appears until ~8KB
has accumulated — a long-lived service never gets there.
    journalctl -u wifi-bridge-api -f
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse, parse_qs

SCRIPT_VERSION = "0.3.0"

# --- Configuration (all overridable from the unit's Environment=) ---
BIND_ADDR = os.environ.get("WIFI_API_BIND", "10.10.0.1")
BIND_PORT = int(os.environ.get("WIFI_API_PORT", "12346"))
IFNAME = os.environ.get("WIFI_IFNAME", "wlan0")
# The RV's own AP, broadcast by the RP5 on wlan0. Joining it would loop the
# uplink back through the RP5 that depends on it. Never offered -- and it is
# visible in every scan from either host, so this is not optional.
SELF_AP_SSID = os.environ.get("SELF_AP_SSID", "Sophie")
ALLOWED_NET = ip_network(os.environ.get("WIFI_API_ALLOWED_NET", "10.10.0.0/24"))
# Floor between real rescans, however often the UI asks. A rescan while
# NetworkManager is mid-association fights the association.
SCAN_MIN_INTERVAL = float(os.environ.get("SCAN_MIN_INTERVAL", "10"))
NMCLI_TIMEOUT = float(os.environ.get("NMCLI_TIMEOUT", "25"))
STATUS_TTL = float(os.environ.get("STATUS_TTL", "1"))
NETWORKS_TTL = float(os.environ.get("NETWORKS_TTL", "10"))
PROFILES_TTL = float(os.environ.get("PROFILES_TTL", "60"))
MAX_WORKERS = int(os.environ.get("WIFI_API_MAX_WORKERS", "8"))
BIND_RETRY_SECONDS = float(os.environ.get("BIND_RETRY_SECONDS", "60"))
MAX_BODY_BYTES = 4096
# --- End configuration ---

WIFI_TYPE = "802-11-wireless"

# Settings put on every profile this API creates.
#   powersave 2 = off.  A dozing uplink costs latency and throughput, and
#     NetworkManager's default is driver-dependent.
#   cloned-mac-address permanent = use the adapter's real MAC, not a random
#     one per connection.  A captive portal authorizes a MAC; a new MAC on
#     every reconnect means logging in again every time.
#   ipv6.method disabled = a park network offering SLAAC would give the uplink
#     a global v6 address, which bypasses the v4-only firewall rules entirely.
PROFILE_HARDENING = (
    "802-11-wireless.powersave", "2",
    "802-11-wireless.cloned-mac-address", "permanent",
    "ipv6.method", "disabled",
)

# Dual-band SSIDs get two profiles: a 5 GHz-locked one at a higher autoconnect
# priority, and an unlocked fallback.  NetworkManager ranks BSSes by signal
# strength alone, and that is the wrong metric -- measured 2026-10-17 at home,
# it chose 2.4 GHz at -62 dBm over 5 GHz at -78 dBm and the band it picked was
# 10-40x slower, because five networks shared the 2.4 GHz channel.  A clean
# weak channel beats a strong congested one.  The lock has to be explicit
# because NM has no band *preference*, only a band lock, and a locked profile
# fails outright where that band is absent -- hence the unlocked fallback.
FIVE_GHZ_SUFFIX = "-5g"
FIVE_GHZ_PRIORITY = "10"

START_MONOTONIC = time.monotonic()


# ---------------------------------------------------------------------------
# nmcli
# ---------------------------------------------------------------------------

class NmcliError(Exception):
    """Base for every nmcli failure mode."""


class NmcliNotFound(NmcliError):
    pass


class NmcliTimeout(NmcliError):
    pass


class NmcliFailed(NmcliError):
    def __init__(self, returncode, stderr):
        super().__init__(f"nmcli exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


# Argument names whose VALUE is a secret. The token after any of these is
# masked before the command is logged.
_SECRET_ARGS = {
    "wifi-sec.psk",
    "802-11-wireless-security.psk",
    "password",
    "psk",
}


def redact_args(args):
    """Returns args as a loggable string with secret values masked."""
    out = []
    mask_next = False
    for token in args:
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        out.append(token)
        mask_next = token in _SECRET_ARGS
    return " ".join(out)


def nmcli(*args, timeout=None):
    """
    Runs nmcli and returns stdout. Raises an NmcliError subclass on failure.

    LC_ALL/LANG are pinned to C: this Zero's locale is broken (every login
    prints 'setlocale: cannot change locale (en_US.UTF-8)') and nmcli's field
    values and messages are locale-dependent.
    """
    cmd = ["nmcli", *args]
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=NMCLI_TIMEOUT if timeout is None else timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NmcliNotFound("nmcli is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise NmcliTimeout(f"timed out: {redact_args(cmd)}") from exc
    if proc.returncode != 0:
        raise NmcliFailed(proc.returncode, proc.stderr.strip())
    return proc.stdout


def split_terse(line):
    """
    Splits one line of `nmcli -t` output on unescaped colons and unescapes the
    fields. nmcli escapes ':' and '\\' in terse values -- a BSSID arrives as
    7A\\:A7\\:41\\:81\\:21\\:83 -- so a naive split(':') corrupts BSSIDs and any
    SSID containing a colon. Also correct for single-field `-g` output, which is
    escaped the same way.
    """
    fields = []
    current = []
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if char == ":":
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    fields.append("".join(current))
    return fields


def nmcli_props(uuid, *fields):
    """
    Reads several properties of one profile in a single call.

    `-t -f` output is NAME-tagged ("802-11-wireless.ssid:MyNet"), unlike `-g`,
    which emits bare values positionally and silently omits inapplicable
    fields. Tagged output makes omission harmless, so this is safe where
    multi-field `-g` is not -- and it costs one nmcli process per profile
    instead of one per property. On this Zero that is 9 invocations per refresh
    instead of 36, which took 4.3s and measurably delayed packet forwarding.
    """
    out = nmcli("-t", "-f", ",".join(fields), "connection", "show", uuid)
    props = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = split_terse(line)
        if len(parts) >= 2:
            props[parts[0]] = ":".join(parts[1:])
    return props


def nmcli_get(field, *args):
    """
    Reads one field with `-g`. Always one field per call: with several fields,
    nmcli OMITS inapplicable ones rather than emitting them empty (asking a
    loopback profile for 4 fields returns 3 lines), so positional parsing of
    multi-field -g output silently shifts.
    """
    out = nmcli("-g", field, *args).strip()
    return split_terse(out)[0] if out else ""


# ---------------------------------------------------------------------------
# Caching: one entry per resource, with single-flight
# ---------------------------------------------------------------------------

class Cache:
    """
    Holds one value with a TTL. The lock is held across the load, so ten
    concurrent pollers cause one nmcli run and the rest get the fresh value.
    """

    def __init__(self, ttl, loader):
        self.ttl = ttl
        self.loader = loader
        self.lock = threading.Lock()
        self.value = None
        self.stamp = 0.0

    def get(self, force=False):
        """Returns (value, age_seconds)."""
        with self.lock:
            age = time.monotonic() - self.stamp
            if self.value is not None and not force and age < self.ttl:
                return self.value, age
            self.value = self.loader()
            self.stamp = time.monotonic()
            return self.value, 0.0

    def peek(self):
        """
        Returns (value, age) without waiting for a load in progress; value is
        None if nothing has been cached yet. Callers that must never block on
        another request's work use this instead of get().
        """
        value = self.value   # single attribute read; no lock needed
        return value, (time.monotonic() - self.stamp if value is not None else None)

    def invalidate(self):
        with self.lock:
            self.value = None
            self.stamp = 0.0


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def load_profiles():
    """
    Every saved WiFi profile, newest-used first.

    One `-t -f UUID,NAME,TYPE,TIMESTAMP` call gets the common fields; SSID and
    security need per-profile reads because they are type-specific.

    Profile NAME is not the SSID and is not unique: on this Zero,
    'ListenerManagedWifi' holds SSID 'Salty Fox', 'asta' holds
    'UpstairsEXT2.4G', and two separate profiles both hold 'Buckley Clan 2'.
    Everything downstream keys on UUID for that reason.
    """
    active_uuids = set()
    try:
        for line in nmcli("-t", "-f", "UUID,DEVICE", "connection",
                          "show", "--active").splitlines():
            if not line.strip():
                continue
            parts = split_terse(line)
            if len(parts) >= 2 and parts[1] == IFNAME:
                active_uuids.add(parts[0])
    except NmcliError:
        pass  # non-fatal: 'active' is cosmetic

    profiles = []
    listing = nmcli("-t", "-f", "UUID,NAME,TYPE,TIMESTAMP", "connection", "show")
    for line in listing.splitlines():
        if not line.strip():
            continue
        parts = split_terse(line)
        if len(parts) < 4 or parts[2] != WIFI_TYPE:
            continue
        uuid, name, _, timestamp = parts[0], parts[1], parts[2], parts[3]
        try:
            props = nmcli_props(
                uuid,
                "802-11-wireless.ssid",
                "802-11-wireless.band",
                "802-11-wireless-security.key-mgmt",
                "802-11-wireless-security.psk-flags",
                "connection.autoconnect",
                "connection.autoconnect-priority",
            )
        except NmcliError as exc:
            print(f"warning: skipping profile {uuid}: {exc}")
            continue
        ssid = props.get("802-11-wireless.ssid", "")
        band = props.get("802-11-wireless.band", "")
        key_mgmt = props.get("802-11-wireless-security.key-mgmt", "")
        psk_flags = props.get("802-11-wireless-security.psk-flags", "")
        autoconnect = props.get("connection.autoconnect", "")
        priority_raw = props.get("connection.autoconnect-priority", "")

        secured = key_mgmt not in ("", "none")
        profiles.append({
            "uuid": uuid,
            "name": name,
            "ssid": ssid,
            # psk-flags 0 means "stored in system settings". It does not prove a
            # secret is present -- proving that needs --show-secrets, which puts
            # the PSK one logging mistake away from the journal. Proxy on
            # purpose; documented imprecision.
            "has_psk": bool(secured and psk_flags in ("0", "")),
            "secured": secured,
            "key_mgmt": key_mgmt,
            "timestamp": int(timestamp) if timestamp.isdigit() else 0,
            "autoconnect": autoconnect == "yes",
            # nmcli prints "--" for an unset band; "a" = 5 GHz, "bg" = 2.4.
            "band": "" if band in ("--", "(null)") else band,
            "priority": int(priority_raw) if priority_raw.lstrip("-").isdigit() else 0,
            "active": uuid in active_uuids,
        })

    profiles.sort(key=lambda p: (-p["timestamp"], p["name"]))
    return profiles


def profile_rank(profile):
    """
    How NetworkManager itself would order candidate profiles for one SSID:
    autoconnect-priority first, then the most recently used, then by name.
    Name is the final tiebreak because several profiles here have timestamp 0
    and nmcli's output order is not guaranteed.
    """
    return (profile["priority"], profile["timestamp"], profile["name"])


def index_by_ssid(profiles):
    """
    SSID -> the profile NM would prefer. With the 5 GHz/fallback pair this API
    creates, that is the band-locked one, which is the point of the pair.
    """
    index = {}
    for profile in profiles:
        if not profile["ssid"]:
            continue
        best = index.get(profile["ssid"])
        if best is None or profile_rank(profile) > profile_rank(best):
            index[profile["ssid"]] = profile
    return index


def profiles_for_ssid(profiles, ssid):
    """Every profile for one SSID -- the band pair, and any older duplicates
    -- in the order NetworkManager would prefer them."""
    matches = [p for p in profiles if p["ssid"] == ssid]
    matches.sort(key=profile_rank, reverse=True)
    return matches


def count_by_ssid(profiles):
    counts = {}
    for profile in profiles:
        if profile["ssid"]:
            counts[profile["ssid"]] = counts.get(profile["ssid"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def band_of(freq):
    """
    nmcli's FREQ field ("2437 MHz") -> "2.4" | "5" | "6", or "" if unparseable.
    5 and 6 GHz are reported separately but share NetworkManager's band lock
    value "a", so callers treat both as "the 5 GHz-ish band".
    """
    head = freq.strip().split(" ")[0] if freq.strip() else ""
    if not head.isdigit():
        return ""
    mhz = int(head)
    if 2400 <= mhz < 2500:
        return "2.4"
    if 4900 <= mhz < 5925:
        return "5"
    if 5925 <= mhz <= 7125:
        return "6"
    return ""


def normalize_security(raw):
    """
    Maps nmcli's SECURITY field to a label plus whether we can join it.
    Values seen in the wild: '', 'WEP', 'WPA1', 'WPA2', 'WPA1 WPA2',
    'WPA2 WPA3', '802.1X'.
    """
    tokens = raw.replace(",", " ").split()
    upper = [t.upper() for t in tokens]
    if any("802.1X" in t for t in upper):
        return "802.1X", False, "enterprise networks are not supported"
    if not upper:
        return "", True, ""
    if any(t == "WEP" for t in upper):
        return "WEP", False, "WEP is not supported"
    has3 = any("WPA3" in t or "SAE" in t for t in upper)
    has2 = any("WPA2" in t for t in upper)
    has1 = any(t in ("WPA", "WPA1") for t in upper)
    if has3 and (has2 or has1):
        return "WPA2/WPA3", True, ""
    if has3:
        return "WPA3", True, ""
    if has2:
        return "WPA2", True, ""
    if has1:
        return "WPA", True, ""
    return raw, True, ""


_scan_rescan_lock = threading.Lock()
_last_rescan = 0.0


def rescan_allowed():
    """True if enough time has passed since the last real rescan."""
    global _last_rescan
    with _scan_rescan_lock:
        now = time.monotonic()
        if now - _last_rescan < SCAN_MIN_INTERVAL:
            return False
        _last_rescan = now
        return True


def scan(rescan=False):
    """
    Visible networks, one entry per SSID.

    Scan output is per-BSSID, so one SSID appears several times (three
    'tellMyWIFILoveHer' rows here, from three APs), and hidden networks appear
    with an empty SSID. Collapse by SSID keeping the strongest signal; drop the
    empty ones, which cannot be joined without manual entry.

    SIGNAL is a 0-100 percentage, not dBm.
    """
    profiles = PROFILES_CACHE.get()[0]
    index = index_by_ssid(profiles)
    counts = count_by_ssid(profiles)

    out = nmcli(
        "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,BSSID,FREQ",
        "device", "wifi", "list",
        # Pin the interface. Both hosts carry a p2p-dev-<ifname> alongside the
        # real one, and on the RP5 wlan0 is the RV's own AP radio.
        "ifname", IFNAME,
        "--rescan", "yes" if rescan else "no",
    )

    merged = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = split_terse(line)
        if len(parts) < 6:
            continue
        in_use, ssid, signal_raw, security_raw, bssid, freq = parts[:6]
        if not ssid:
            continue  # hidden AP
        try:
            signal = int(signal_raw)
        except ValueError:
            signal = 0

        label, joinable, reason = normalize_security(security_raw)
        profile = index.get(ssid)
        blocked = "self" if ssid == SELF_AP_SSID else ""
        if blocked:
            joinable = False
            reason = "this RV's own network"

        entry = merged.get(ssid)
        if entry is None:
            merged[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "security": label,
                "joinable": joinable,
                "unjoinable_reason": reason,
                "blocked": blocked,
                "in_use": in_use.strip() == "*",
                "saved": profile is not None,
                "has_psk": bool(profile and profile["has_psk"]),
                "profile": profile["name"] if profile else "",
                "profile_uuid": profile["uuid"] if profile else "",
                "profile_count": counts.get(ssid, 0),
                "bssid_count": 1,
                "freq": freq,
                # Every band this SSID was seen on, not just the strongest
                # BSS's.  The strongest BSS is routinely the 2.4 GHz one while
                # the 5 GHz BSS is the one worth joining, so "freq" alone
                # cannot answer "is this network dual-band?".
                "_bands": {band_of(freq)} - {""},
                # The frequency of the BSS actually associated, which is NOT
                # "freq" above: that is the strongest BSS, and on a dual-band
                # SSID the strongest is routinely the 2.4 GHz one while the
                # association is on 5 GHz. Reporting "freq" as the live band
                # said 2.4 GHz for a 5 GHz-locked profile.
                "_in_use_freq": freq if in_use.strip() == "*" else "",
                # Same reasoning for signal: "signal" above is the strongest
                # BSS's, which is the right thing to rank the picker list by
                # and the wrong thing to report as the live link's strength.
                "_in_use_signal": signal if in_use.strip() == "*" else 0,
            }
            continue
        # Same SSID from another AP: keep the strongest, and never lose in_use.
        entry["bssid_count"] += 1
        if in_use.strip() == "*":
            entry["in_use"] = True
            entry["_in_use_freq"] = freq
            entry["_in_use_signal"] = signal
        entry["_bands"] |= {band_of(freq)} - {""}
        if signal > entry["signal"]:
            entry["signal"] = signal
            entry["freq"] = freq
        if label and not entry["security"]:
            entry["security"] = label
            entry["joinable"] = joinable
            entry["unjoinable_reason"] = reason

    networks = list(merged.values())
    for network in networks:
        bands = sorted(network.pop("_bands"))
        network["bands"] = bands
        network["has_5g"] = bool({"5", "6"} & set(bands))
        network["in_use_band"] = band_of(network.pop("_in_use_freq"))
        network["in_use_signal"] = network.pop("_in_use_signal")
    # Connected first, then known, then strongest -- what a phone does.
    networks.sort(key=lambda n: (not n["in_use"], not n["saved"],
                                 -n["signal"], n["ssid"].casefold()))
    return networks


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

# Shape kept stable now so the UI can rely on it before Phase 3 fills it in.
_job = {
    "id": "",
    "op": "",
    "ssid": "",
    "state": "idle",      # idle | running | succeeded | failed
    "phase": "",
    "error": "",
    "started": 0,
    "finished": 0,
    "rolled_back": False,
}
_job_lock = threading.Lock()


def job_snapshot():
    """
    The last operation, plus how long ago it finished. age_seconds is computed
    here on purpose: the browser, the RP5 and the Zero each have their own
    clock, so a consumer deciding "is this news or history?" must not have to
    compare an epoch timestamp against its own time.
    """
    with _job_lock:
        snapshot = dict(_job)
    finished = snapshot.get("finished") or 0
    snapshot["age_seconds"] = round(time.time() - finished, 1) if finished else None
    return snapshot


def load_status():
    """IFNAME's state, the AP it is on, its address, and the active profile."""
    state = "unknown"
    connection = ""
    try:
        for line in nmcli("-t", "-f", "DEVICE,STATE,CONNECTION",
                          "device", "status").splitlines():
            parts = split_terse(line)
            if len(parts) >= 3 and parts[0] == IFNAME:
                state, connection = parts[1], parts[2]
                break
    except NmcliError as exc:
        print(f"warning: device status failed: {exc}")

    profile_uuid = ""
    try:
        for line in nmcli("-t", "-f", "UUID,DEVICE", "connection",
                          "show", "--active").splitlines():
            parts = split_terse(line)
            if len(parts) >= 2 and parts[1] == IFNAME:
                profile_uuid = parts[0]
                break
    except NmcliError:
        pass

    ip = gateway = ""
    try:
        ip = nmcli_get("IP4.ADDRESS", "device", "show", IFNAME)
        gateway = nmcli_get("IP4.GATEWAY", "device", "show", IFNAME)
    except NmcliError:
        pass

    # The SSID comes from the active profile, not from the scan: profile NAME
    # is not the SSID, and this endpoint must stay cheap and independent.
    ssid = ""
    if profile_uuid:
        try:
            ssid = nmcli_get("802-11-wireless.ssid", "connection", "show",
                             profile_uuid)
        except NmcliError:
            pass

    # Live signal is best-effort from whatever the scan cache already holds.
    # peek() never waits: get() holds its lock across a load, so a forced
    # rescan would otherwise block this endpoint for the whole scan -- measured
    # at 5.3s, while the UI polls status every 2-5s to follow a connect.
    signal = 0
    band = ""
    scan_age = None
    networks, scan_age = NETWORKS_CACHE.peek()
    if networks:
        for network in networks:
            if network["in_use"] and (not ssid or network["ssid"] == ssid):
                # The associated BSS's signal, falling back to the SSID's
                # strongest if the scan did not mark a specific BSS in use.
                signal = network.get("in_use_signal") or network["signal"]
                # Which band the uplink actually landed on -- from the
                # associated BSS, not the strongest one. Worth surfacing: the
                # measured penalty for being on the congested 2.4 GHz band of
                # a dual-band SSID was 10-40x, and signal strength alone gives
                # no hint of it.
                band = network.get("in_use_band", "")
                if not ssid:
                    ssid = network["ssid"]
                break

    return {
        "wifi": {
            "state": state,
            "ssid": ssid,
            "signal": signal,
            "band": band,
            "profile": connection,
            "profile_uuid": profile_uuid,
            "ip": ip,
            "gateway": gateway,
            "signal_age_seconds": round(scan_age, 1) if scan_age is not None else None,
        },
        "operation": job_snapshot(),
        "service": {
            "version": SCRIPT_VERSION,
            "uptime": round(time.monotonic() - START_MONOTONIC, 1),
        },
    }


PROFILES_CACHE = Cache(PROFILES_TTL, load_profiles)
NETWORKS_CACHE = Cache(NETWORKS_TTL, lambda: scan(rescan=False))
STATUS_CACHE = Cache(STATUS_TTL, load_status)


def networks_payload(want_rescan):
    """
    Serves the scan, forcing a real rescan only if the caller asked AND the
    floor has elapsed. `age_seconds` lets the UI say how stale the list is.
    """
    if want_rescan and rescan_allowed():
        with NETWORKS_CACHE.lock:
            NETWORKS_CACHE.value = scan(rescan=True)
            NETWORKS_CACHE.stamp = time.monotonic()
        networks, age = NETWORKS_CACHE.value, 0.0
        rescanned = True
    else:
        networks, age = NETWORKS_CACHE.get()
        rescanned = False
    return {
        "ok": True,
        "networks": networks,
        "age_seconds": round(age, 1),
        "rescanned": rescanned,
        "self_ap_ssid": SELF_AP_SSID,
    }



# ---------------------------------------------------------------------------
# Mutating operations: connect and forget
# ---------------------------------------------------------------------------
# Only one at a time, and never on the HTTP thread. A connect takes seconds to
# tens of seconds, and the UI polls /api/status throughout to follow it, so the
# request returns 202 as soon as the job is accepted.

_MUTATE_LOCK = threading.Lock()


def active_wifi_uuid():
    """UUID of the profile currently active on the WiFi interface, or ''."""
    try:
        for line in nmcli("-t", "-f", "UUID,DEVICE", "connection",
                          "show", "--active").splitlines():
            parts = split_terse(line)
            if len(parts) >= 2 and parts[1] == IFNAME:
                return parts[0]
    except NmcliError:
        pass
    return ""


def set_job(**fields):
    with _job_lock:
        _job.update(fields)


def classify_activation_failure(stderr):
    """
    Maps nmcli's own words to an error code. Reading stderr beats polling for
    this: a wrong password is reported as "Secrets were required" within
    seconds, where a status poller would just time out after 45.
    """
    low = (stderr or "").lower()
    if "secrets were required" in low or "802-1x" in low:
        return "auth_failed", "the password was rejected"
    if ("no network with ssid" in low or "not available on device" in low
            or "could not be found" in low):
        return "no_ap", "that network is not in range"
    if "timeout" in low or "timed out" in low:
        return "nm_failed", "NetworkManager timed out bringing the connection up"
    return "nm_failed", stderr.strip() or "NetworkManager could not connect"


def key_mgmt_for(ssid, psk):
    """
    Picks key-mgmt from what the scan saw. The old listener hardcoded wpa-psk,
    which is wrong for open networks and for WPA3-only APs.
    """
    networks, _ = NETWORKS_CACHE.peek()
    security = ""
    for network in networks or []:
        if network["ssid"] == ssid:
            security = network["security"]
            break
    if security in ("WPA3",):
        return "sae"
    if security == "" and not psk:
        return ""          # open network: no security section at all
    return "wpa-psk"


def ssid_has_5ghz(ssid):
    """
    Did the last scan see this SSID on 5/6 GHz? Decides whether a band-locked
    profile is worth creating. Falls back to False when the cache is cold,
    which only costs the 5 GHz preference on a first connect with no prior
    scan -- the picker always scans before offering a network.
    """
    networks, _ = NETWORKS_CACHE.peek()
    for network in networks or []:
        if network["ssid"] == ssid:
            return bool(network.get("has_5g"))
    return False


def add_profile(name, ssid, key_mgmt, psk, band=""):
    """
    Creates one profile and returns its uuid. `band` is NetworkManager's
    802-11-wireless.band ("a" to lock to 5/6 GHz, "" for any band).
    """
    args = ["connection", "add", "type", "wifi", "con-name", name,
            "ifname", IFNAME, "ssid", ssid, "--"]
    if key_mgmt:
        args += ["802-11-wireless-security.key-mgmt", key_mgmt]
        if psk:
            args += ["802-11-wireless-security.psk", psk]
    if band:
        args += ["802-11-wireless.band", band,
                 "connection.autoconnect-priority", FIVE_GHZ_PRIORITY]
    args += list(PROFILE_HARDENING)
    nmcli(*args)

    for profile in load_profiles():
        if profile["name"] == name:
            return profile["uuid"]
    raise NmcliFailed(1, f"profile '{name}' was not found after adding it")


def harden_profile(uuid):
    """
    Applies PROFILE_HARDENING to an existing profile. Idempotent, and safe on
    the live connection: `nmcli connection modify` writes the profile without
    reactivating the device, so it never drops the uplink by itself.
    """
    try:
        nmcli("connection", "modify", uuid, *PROFILE_HARDENING)
    except NmcliError as exc:
        # Worth continuing: the join still works, it is just not hardened.
        print(f"connect: WARNING could not apply profile hardening: {exc}")


def unique_profile_name(ssid, profiles):
    """
    A profile named after the SSID, suffixed if that name is taken. NM allows
    duplicate names, and on this Zero the SSID 'Buckley Clan 2' already has a
    profile called exactly that -- adding a second would be indistinguishable.
    """
    taken = {p["name"] for p in profiles}
    # Both names have to be free: the 5 GHz sibling is this name plus the
    # suffix, and a collision there would make the pair indistinguishable too.
    def free(candidate):
        return (candidate not in taken
                and candidate + FIVE_GHZ_SUFFIX not in taken)

    if free(ssid):
        return ssid
    for suffix in range(2, 100):
        candidate = f"{ssid} {suffix}"
        if free(candidate):
            return candidate
    return f"{ssid} {int(time.time())}"


def do_connect(ssid, psk, job_id):
    """
    Runs in its own thread. Holds _MUTATE_LOCK for its whole life; the caller
    acquired it, this releases it.
    """
    added_uuids = []
    five_ghz_uuid = ""
    previous_uuid = active_wifi_uuid()
    try:
        profiles = PROFILES_CACHE.get(force=True)[0]
        existing = index_by_ssid(profiles).get(ssid)

        if existing and not psk:
            # Saved credential: just bring it up.
            target_uuid = existing["uuid"]
            set_job(phase="activating saved profile")
            harden_profile(target_uuid)
        elif existing and psk:
            # Replacing the stored password. Irreversible: the old PSK cannot
            # be read back, so the UI warns before getting here.
            target_uuid = existing["uuid"]
            set_job(phase="updating saved password")
            km = existing["key_mgmt"] or key_mgmt_for(ssid, psk) or "wpa-psk"
            # Every profile for this SSID, not just the preferred one: a band
            # pair shares one password, and leaving the sibling on the old PSK
            # turns the fallback into an auth_failed later, far from the cause.
            for sibling in profiles_for_ssid(profiles, ssid):
                nmcli("connection", "modify", sibling["uuid"],
                      "802-11-wireless-security.key-mgmt", km,
                      "802-11-wireless-security.psk", psk)
                harden_profile(sibling["uuid"])
        else:
            set_job(phase="creating profile")
            name = unique_profile_name(ssid, profiles)
            km = key_mgmt_for(ssid, psk)
            # The unlocked profile first: it is the one that works everywhere,
            # so it is also the fallback if the 5 GHz lock finds no BSS.
            target_uuid = add_profile(name, ssid, km, psk)
            added_uuids.append(target_uuid)
            if ssid_has_5ghz(ssid):
                five_name = name + FIVE_GHZ_SUFFIX
                try:
                    five_ghz_uuid = add_profile(five_name, ssid, km, psk, band="a")
                except NmcliError as exc:
                    # Not fatal: without it the join just loses the band
                    # preference, which is a slow link, not a broken one.
                    print(f"connect: WARNING could not add {five_name!r}: {exc}")
                else:
                    added_uuids.append(five_ghz_uuid)
                    target_uuid = five_ghz_uuid

        set_job(phase="associating")
        # -w blocks until NM finishes, so stderr carries the real reason.
        try:
            nmcli("-w", "50", "connection", "up", "uuid", target_uuid, timeout=60)
        except NmcliError as exc:
            # A band-locked profile fails outright where that band has no BSS,
            # and the scan that said "dual-band" can be seconds stale. Retry on
            # the unlocked profile instead of reporting a failure the user can
            # do nothing about. Only for no_ap: a rejected password would just
            # fail twice and cost another 50 s.
            stderr = getattr(exc, "stderr", "") or str(exc)
            fallback = added_uuids[0] if (five_ghz_uuid
                                          and target_uuid == five_ghz_uuid) else ""
            if not fallback or classify_activation_failure(stderr)[0] != "no_ap":
                raise
            print("connect: no 5 GHz BSS; falling back to the unlocked profile")
            set_job(phase="associating (no 5 GHz; any band)")
            target_uuid = fallback
            nmcli("-w", "50", "connection", "up", "uuid", target_uuid, timeout=60)

        state = nmcli_props(target_uuid, "connection.id").get("connection.id", "")
        ip = ""
        try:
            ip = nmcli_get("IP4.ADDRESS", "device", "show", IFNAME)
        except NmcliError:
            pass
        print(f"connect: joined {ssid!r} via profile {state!r} ip={ip}")
        set_job(state="succeeded", phase="connected", error="",
                finished=int(time.time()))

    except NmcliError as exc:
        stderr = getattr(exc, "stderr", "") or str(exc)
        code, message = classify_activation_failure(stderr)
        print(f"connect: failed for {ssid!r}: {code}: {message}")

        # Profiles created for this attempt must not survive it, or a typo
        # permanently adds a bogus "saved" network.
        for uuid in added_uuids:
            try:
                nmcli("connection", "delete", "uuid", uuid)
                print("connect: removed a profile added for this attempt")
            except NmcliError as cleanup_exc:
                print(f"connect: WARNING could not remove added profile: {cleanup_exc}")

        # Put the uplink back if we moved off it.
        rolled_back = False
        if previous_uuid and previous_uuid not in added_uuids:
            try:
                nmcli("-w", "40", "connection", "up", "uuid", previous_uuid,
                      timeout=50)
                rolled_back = True
                # `connection up` returns once activation starts; DHCP can
                # still be outstanding, and reporting the failure in that gap
                # makes the UI flash "disconnected" before it recovers.
                for _ in range(10):
                    try:
                        if nmcli_get("IP4.ADDRESS", "device", "show", IFNAME):
                            break
                    except NmcliError:
                        pass
                    time.sleep(1)
                print("connect: rolled back to the previous network")
            except NmcliError as rb_exc:
                print(f"connect: WARNING rollback failed: {rb_exc}")

        set_job(state="failed", phase="", error=f"{code}: {message}",
                rolled_back=rolled_back, finished=int(time.time()))
    finally:
        PROFILES_CACHE.invalidate()
        NETWORKS_CACHE.invalidate()
        _MUTATE_LOCK.release()


def validate_connect(ssid, psk):
    """Returns an error message, or '' if the request is worth attempting."""
    if not ssid:
        return "an ssid is required"
    if len(ssid.encode("utf-8")) > 32:
        return "an ssid cannot exceed 32 bytes"
    if ssid == SELF_AP_SSID:
        return (f"{ssid!r} is this RV's own network; joining it would route the "
                "uplink back through the RP5")
    if psk and not 8 <= len(psk) <= 63:
        return "a WPA password must be 8 to 63 characters"
    return ""


def handle_connect(body):
    ssid = (body.get("ssid") or "").strip()
    psk = body.get("psk") or ""
    problem = validate_connect(ssid, psk)
    if problem:
        code = "blocked_self" if ssid and ssid == SELF_AP_SSID else "invalid_request"
        return 400, {"ok": False, "error": code, "message": problem}
    if not _MUTATE_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "error": "busy",
                     "message": "another WiFi operation is already running",
                     "operation": job_snapshot()}
    job_id = f"{int(time.time())}-{ssid[:12]}"
    set_job(id=job_id, op="connect", ssid=ssid, state="running",
            phase="starting", error="", started=int(time.time()), finished=0,
            rolled_back=False)
    # The password is deliberately absent from this log line.
    print(f"connect: requested ssid={ssid!r} psk={'yes' if psk else 'no'}")
    threading.Thread(target=do_connect, args=(ssid, psk, job_id),
                     daemon=True).start()
    return 202, {"ok": True, "job_id": job_id, "ssid": ssid}


def handle_forget(body):
    uuid = (body.get("uuid") or "").strip()
    ssid = (body.get("ssid") or "").strip()
    force = bool(body.get("force"))
    if not uuid and not ssid:
        return 400, {"ok": False, "error": "invalid_request",
                     "message": "a uuid or ssid is required"}

    profiles = PROFILES_CACHE.get(force=True)[0]
    by_uuid = {p["uuid"]: p for p in profiles}

    if uuid:
        target = by_uuid.get(uuid)
        if target is None:
            # Not in our list: either it does not exist, or it is not WiFi.
            # Refusing protects the usb0 (802-3-ethernet) and lo profiles.
            return 404, {"ok": False, "error": "not_found",
                         "message": "no such saved WiFi profile"}
        targets = [target]
    else:
        # By SSID: every profile for it. "Forget this network" has to mean the
        # whole set -- this API creates a band pair, and deleting only the
        # preferred profile would leave the sibling to autoconnect, so the
        # network would still look saved and still be joined. Earlier versions
        # returned 409 "ambiguous" here, which made the pair unforgettable
        # from the picker.
        targets = profiles_for_ssid(profiles, ssid)
        if not targets:
            return 404, {"ok": False, "error": "not_found",
                         "message": f"no saved profile for {ssid!r}"}

    active = [t for t in targets if t["active"]]
    if active and not force:
        return 409, {"ok": False, "error": "active",
                     "message": (f"{active[0]['name']!r} is the network currently "
                                 "providing the connection; pass force to delete it "
                                 "anyway, which will disconnect the RV"),
                     "profile": {"uuid": active[0]["uuid"], "name": active[0]["name"]}}

    if not _MUTATE_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "error": "busy",
                     "message": "another WiFi operation is already running",
                     "operation": job_snapshot()}
    deleted = []
    try:
        # The active profile last, so that dropping the link cannot leave the
        # siblings behind to autoconnect to the network just forgotten.
        for target in sorted(targets, key=lambda t: t["active"]):
            nmcli("connection", "delete", "uuid", target["uuid"])
            print(f"forget: deleted profile {target['name']!r} (ssid {target['ssid']!r})")
            deleted.append({"uuid": target["uuid"], "name": target["name"],
                            "ssid": target["ssid"]})
    except NmcliError as exc:
        return 502, {"ok": False, "error": "nm_failed", "message": str(exc),
                     "deleted_profiles": deleted}
    finally:
        PROFILES_CACHE.invalidate()
        NETWORKS_CACHE.invalidate()
        _MUTATE_LOCK.release()

    # "deleted" stays a single object for the dashboard, which reports
    # deleted.name; the full set is alongside it.
    return 200, {"ok": True, "deleted": deleted[-1],
                 "deleted_profiles": deleted}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"rpzero-wifi-api/{SCRIPT_VERSION}"
    # HTTP/1.1 requires an accurate Content-Length on every response, which
    # _send() always sets; without it urllib on the RP5 hangs waiting for EOF.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}")

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client gave up; nothing useful to do

    def _error(self, status, code, message):
        self._send(status, {"ok": False, "error": code, "message": message})

    def _allowed(self):
        try:
            return ip_address(self.client_address[0]) in ALLOWED_NET
        except ValueError:
            return False

    def do_GET(self):
        if not self._allowed():
            self._error(403, "forbidden", "requests are accepted on the USB link only")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                self._send(200, {
                    "ok": True,
                    "version": SCRIPT_VERSION,
                    "uptime": round(time.monotonic() - START_MONOTONIC, 1),
                    "ifname": IFNAME,
                })
            elif path == "/api/status":
                payload, _ = STATUS_CACHE.get()
                self._send(200, {"ok": True, **payload})
            elif path == "/api/networks":
                want = parse_qs(parsed.query).get("rescan", ["0"])[0]
                self._send(200, networks_payload(want in ("1", "true", "yes")))
            elif path == "/api/profiles":
                # refresh=1 bypasses the 60s cache. Our own connect/forget
                # invalidate it, but a profile added or removed outside this
                # service (nmcli by hand, or NM itself) is otherwise invisible
                # for up to a minute.
                want = parse_qs(parsed.query).get("refresh", ["0"])[0]
                profiles, age = PROFILES_CACHE.get(
                    force=want in ("1", "true", "yes"))
                self._send(200, {"ok": True, "profiles": profiles,
                                 "age_seconds": round(age, 1)})
            else:
                self._error(404, "not_found", f"no such endpoint: {path}")
        except NmcliNotFound as exc:
            self._error(500, "nmcli_missing", str(exc))
        except NmcliTimeout as exc:
            self._error(504, "nmcli_timeout", str(exc))
        except NmcliFailed as exc:
            self._error(502, "nmcli_failed", exc.stderr or str(exc))
        except Exception as exc:  # never leak a traceback to the client
            print(f"ERROR handling {path}: {exc!r}")
            self._error(500, "internal_error", "see the service journal")

    def do_POST(self):
        if not self._allowed():
            self._error(403, "forbidden", "requests are accepted on the USB link only")
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        # Read and discard the body so the connection stays usable under
        # keep-alive, and so a client is not left writing into a closed pipe.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "invalid_request", "Content-Length is not a number")
            return
        if length > MAX_BODY_BYTES:
            self._error(413, "too_large", "request body too large")
            return
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                self._error(400, "invalid_request", "body is not valid JSON")
                return
            if not isinstance(body, dict):
                self._error(400, "invalid_request", "body must be a JSON object")
                return
        try:
            if path == "/api/connect":
                status, payload = handle_connect(body)
            elif path == "/api/forget":
                status, payload = handle_forget(body)
            else:
                self._error(404, "not_found", f"no such endpoint: {path}")
                return
        except NmcliNotFound as exc:
            self._error(500, "nmcli_missing", str(exc))
            return
        except NmcliTimeout as exc:
            self._error(504, "nmcli_timeout", str(exc))
            return
        except NmcliFailed as exc:
            self._error(502, "nmcli_failed", exc.stderr or str(exc))
            return
        except Exception as exc:
            print(f"ERROR handling {path}: {exc!r}")
            self._error(500, "internal_error", "see the service journal")
            return
        self._send(status, payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16
    # ThreadingMixIn spawns a thread per request without limit. This is a
    # 512MB Pi Zero; cap it and shed load instead of swapping.
    _slots = threading.BoundedSemaphore(MAX_WORKERS)

    def process_request_thread(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                body = b'{"ok":false,"error":"busy","message":"too many requests"}'
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve():
    print(f"rpzero_wifi_api.py Version: {SCRIPT_VERSION}")
    if os.geteuid() != 0:
        print("WARNING: not running as root — scanning will silently return "
              "only the connected AP and nmcli changes will fail.")

    # usb0 is brought up by usb0-up.service, which nothing orders this service
    # after reliably; binding 10.10.0.1 before the address exists raises
    # EADDRNOTAVAIL. Retry rather than relying on Restart= to paper over it.
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    httpd = None
    while True:
        try:
            httpd = Server((BIND_ADDR, BIND_PORT), Handler)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                print(f"FATAL: could not bind {BIND_ADDR}:{BIND_PORT}: {exc}")
                raise
            print(f"waiting for {BIND_ADDR} to exist ({exc}); retrying in 2s")
            time.sleep(2)

    httpd.timeout = 15
    print(f"Listening on {BIND_ADDR}:{BIND_PORT} "
          f"(iface={IFNAME}, self-AP={SELF_AP_SSID!r}, allow={ALLOWED_NET})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("interrupted; shutting down")
    finally:
        httpd.server_close()
        print("server closed")


if __name__ == "__main__":
    serve()
