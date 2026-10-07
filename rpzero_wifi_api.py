#!/usr/bin/env python3
"""
WiFi bridge HTTP API — runs on the Pi Zero 2W ("the Zero"), as root.

Why this exists
---------------
The RP5's dashboard needs to show the networks the Zero can see, with signal
strength, and which of them the Zero already has credentials for. Scanning
requires root (as an unprivileged user, `nmcli device wifi list --rescan yes`
silently returns only the connected AP), so the privileged work has to live in a
service like this one.

It replaces the comma-delimited socket protocol of RPZero2WListener.py, which
cannot carry a list: requests were split on commas (so an SSID containing a
comma was unparseable) and both ends did a single unframed recv(1024), which a
12-18 AP scan result overflows.

Exposure
--------
Bound to 10.10.0.1 only — the point-to-point USB link to the RP5 — and requests
from outside 10.10.0.0/24 are refused. This matters: the Zero's wlan0 sits on a
foreign network (campground, marina), and binding 0.0.0.0 would publish a
root-privileged "join this network" API to it. There is no authentication, in
keeping with the RP5 dashboard it serves; the bind address is the control.

Secrets
-------
Passwords are never logged. nmcli argv is redacted before printing, and no
endpoint returns a PSK — `has_psk` is derived from psk-flags, so the secret is
never read in the first place.

Phase 1 implements the read-only endpoints:
    GET /api/health      version and uptime
    GET /api/status      wlan0 state, SSID, signal, IP, active profile
    GET /api/networks    scan results, deduped by SSID   (?rescan=1 to force)
    GET /api/profiles    saved WiFi profiles, with has_psk

POST /api/connect and /api/forget answer 501 until Phase 3.

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

SCRIPT_VERSION = "0.2.1"

# --- Configuration (all overridable from the unit's Environment=) ---
BIND_ADDR = os.environ.get("WIFI_API_BIND", "10.10.0.1")
BIND_PORT = int(os.environ.get("WIFI_API_PORT", "12346"))
IFNAME = os.environ.get("WIFI_IFNAME", "wlan0")
# The RV's own AP, broadcast by the RP5. Joining it would route the Zero's
# uplink back through the RP5 that depends on the Zero. Never offered.
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
                "802-11-wireless-security.key-mgmt",
                "802-11-wireless-security.psk-flags",
                "connection.autoconnect",
            )
        except NmcliError as exc:
            print(f"warning: skipping profile {uuid}: {exc}")
            continue
        ssid = props.get("802-11-wireless.ssid", "")
        key_mgmt = props.get("802-11-wireless-security.key-mgmt", "")
        psk_flags = props.get("802-11-wireless-security.psk-flags", "")
        autoconnect = props.get("connection.autoconnect", "")

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
            "active": uuid in active_uuids,
        })

    profiles.sort(key=lambda p: (-p["timestamp"], p["name"]))
    return profiles


def index_by_ssid(profiles):
    """
    SSID -> best profile. NetworkManager breaks autoconnect-priority ties by
    timestamp, so preferring the newest-used profile matches what NM itself
    would pick. Name is the tiebreak, because several profiles here have
    timestamp 0 and nmcli's output order is not guaranteed.
    """
    index = {}
    for profile in profiles:
        if not profile["ssid"]:
            continue
        best = index.get(profile["ssid"])
        key = (profile["timestamp"], profile["name"])
        if best is None or key > (best["timestamp"], best["name"]):
            index[profile["ssid"]] = profile
    return index


def count_by_ssid(profiles):
    counts = {}
    for profile in profiles:
        if profile["ssid"]:
            counts[profile["ssid"]] = counts.get(profile["ssid"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

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
        # Pin the interface: this Zero also has a p2p-dev-wlan0.
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
            }
            continue
        # Same SSID from another AP: keep the strongest, and never lose in_use.
        entry["bssid_count"] += 1
        entry["in_use"] = entry["in_use"] or in_use.strip() == "*"
        if signal > entry["signal"]:
            entry["signal"] = signal
            entry["freq"] = freq
        if label and not entry["security"]:
            entry["security"] = label
            entry["joinable"] = joinable
            entry["unjoinable_reason"] = reason

    networks = list(merged.values())
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
    with _job_lock:
        return dict(_job)


def load_status():
    """wlan0's state, the AP it is on, its address, and the active profile."""
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
    scan_age = None
    networks, scan_age = NETWORKS_CACHE.peek()
    if networks:
        for network in networks:
            if network["in_use"] and (not ssid or network["ssid"] == ssid):
                signal = network["signal"]
                if not ssid:
                    ssid = network["ssid"]
                break

    return {
        "wifi": {
            "state": state,
            "ssid": ssid,
            "signal": signal,
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
    if "no network with ssid" in low or "not available on device" in low:
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


def unique_profile_name(ssid, profiles):
    """
    A profile named after the SSID, suffixed if that name is taken. NM allows
    duplicate names, and on this Zero the SSID 'Buckley Clan 2' already has a
    profile called exactly that -- adding a second would be indistinguishable.
    """
    taken = {p["name"] for p in profiles}
    if ssid not in taken:
        return ssid
    for suffix in range(2, 100):
        candidate = f"{ssid} {suffix}"
        if candidate not in taken:
            return candidate
    return f"{ssid} {int(time.time())}"


def do_connect(ssid, psk, job_id):
    """
    Runs in its own thread. Holds _MUTATE_LOCK for its whole life; the caller
    acquired it, this releases it.
    """
    added_uuid = ""
    previous_uuid = active_wifi_uuid()
    try:
        profiles = PROFILES_CACHE.get(force=True)[0]
        existing = index_by_ssid(profiles).get(ssid)

        if existing and not psk:
            # Saved credential: just bring it up.
            target_uuid = existing["uuid"]
            set_job(phase="activating saved profile")
        elif existing and psk:
            # Replacing the stored password. Irreversible: the old PSK cannot
            # be read back, so the UI warns before getting here.
            target_uuid = existing["uuid"]
            set_job(phase="updating saved password")
            km = existing["key_mgmt"] or key_mgmt_for(ssid, psk) or "wpa-psk"
            nmcli("connection", "modify", target_uuid,
                  "802-11-wireless-security.key-mgmt", km,
                  "802-11-wireless-security.psk", psk)
        else:
            set_job(phase="creating profile")
            name = unique_profile_name(ssid, profiles)
            km = key_mgmt_for(ssid, psk)
            args = ["connection", "add", "type", "wifi", "con-name", name,
                    "ifname", IFNAME, "ssid", ssid]
            if km:
                args += ["--", "802-11-wireless-security.key-mgmt", km]
                if psk:
                    args += ["802-11-wireless-security.psk", psk]
            nmcli(*args)
            target_uuid = ""
            for profile in load_profiles():
                if profile["name"] == name:
                    target_uuid = profile["uuid"]
                    break
            if not target_uuid:
                raise NmcliFailed(1, f"profile '{name}' was not found after adding it")
            added_uuid = target_uuid

        set_job(phase="associating")
        # -w blocks until NM finishes, so stderr carries the real reason.
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

        # A profile created for this attempt must not survive it, or a typo
        # permanently adds a bogus "saved" network.
        if added_uuid:
            try:
                nmcli("connection", "delete", "uuid", added_uuid)
                print(f"connect: removed the profile added for this attempt")
            except NmcliError as cleanup_exc:
                print(f"connect: WARNING could not remove added profile: {cleanup_exc}")

        # Put the uplink back if we moved off it.
        rolled_back = False
        if previous_uuid and previous_uuid != added_uuid:
            try:
                nmcli("-w", "40", "connection", "up", "uuid", previous_uuid,
                      timeout=50)
                rolled_back = True
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
    else:
        matches = [p for p in profiles if p["ssid"] == ssid]
        if not matches:
            return 404, {"ok": False, "error": "not_found",
                         "message": f"no saved profile for {ssid!r}"}
        if len(matches) > 1:
            return 409, {"ok": False, "error": "ambiguous",
                         "message": (f"{ssid!r} has {len(matches)} saved profiles; "
                                     "pass the uuid of the one to delete"),
                         "candidates": [{"uuid": m["uuid"], "name": m["name"]}
                                        for m in matches]}
        target = matches[0]

    if target["active"] and not force:
        return 409, {"ok": False, "error": "active",
                     "message": (f"{target['name']!r} is the network currently "
                                 "providing the connection; pass force to delete it "
                                 "anyway, which will disconnect the RV"),
                     "profile": {"uuid": target["uuid"], "name": target["name"]}}

    if not _MUTATE_LOCK.acquire(blocking=False):
        return 409, {"ok": False, "error": "busy",
                     "message": "another WiFi operation is already running",
                     "operation": job_snapshot()}
    try:
        nmcli("connection", "delete", "uuid", target["uuid"])
        print(f"forget: deleted profile {target['name']!r} (ssid {target['ssid']!r})")
    except NmcliError as exc:
        return 502, {"ok": False, "error": "nm_failed", "message": str(exc)}
    finally:
        PROFILES_CACHE.invalidate()
        NETWORKS_CACHE.invalidate()
        _MUTATE_LOCK.release()

    return 200, {"ok": True, "deleted": {"uuid": target["uuid"],
                                         "name": target["name"],
                                         "ssid": target["ssid"]}}


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
