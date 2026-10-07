# WiFitoHostBridge

A Pi Zero 2W program (the "Zero") and a host program (the RP5) that let the Zero bridge WiFi
traffic to the RP5 over a private subnet. The RP5 can ask the Zero to join a new SSID with a
supplied password and is told whether it connected.

## Architecture

```
 Internet <-- WiFi (wlan0) -- [Pi Zero 2W] -- USB/ethernet 10.10.0.x -- [RP5 / "Sophie"] -- RV LAN
                                  ^  listener on 10.10.0.1:12345
                                  |  TCP, one text packet per request
 RP5 web UI (Internet page) -> server.py /api/wifi-config -> RP5toRPZero2WControl.py
```

| File | Runs on | Purpose |
|------|---------|---------|
| `RPZero2WListener.py` | Zero (as root, systemd `wifi-bridge-listener.service`, installed at `/usr/local/sbin/`) | Listens on 10.10.0.1:12345, drives `nmcli` |
| `RP5toRPZero2WControl.py` | RP5 / laptop | Client: sends `SET_WIFI,ssid,pw` or `SET_WIFI_PROFILE,ssid,pw,profile` |
| `diagnose_routing.sh`, `fix_interface_routing.sh` | RP5 | RaspAP routing diagnosis/repair |

**Two copies of the client exist:** this one and `rv/webserver/server/RP5toRPZero2WControl.py`
(copied into the Docker image). Keep them identical; edit here, then copy.

## Protocol / exit codes

Listener is single-threaded and handles one request at a time. A request takes up to ~70 s
(delete + add profile, `nmcli up`, then up to 45 s waiting for an IP).

| Exit | Meaning | What to do |
|------|---------|-----------|
| 0 | Connected, profile saved | Done |
| 1 | General failure; the message printed just before says which (unreachable, no answer, nmcli add/activate failed, bad packet) | See Troubleshooting |
| 100 | Password rejected (profile saved) | Resend with correct password |
| 101 | Profile saved but no IP before timeout | Check SSID spelling, range, 2.4 GHz only, DHCP |

Timeouts (client): 10 s to connect, 90 s waiting for the answer. The web server
(`server.py` `/api/wifi-config`) allows 120 s. These must stay larger than the listener's worst case;
the old 30 s web limit caused "timed out after 30 sec" even when the Zero was fine.

## Using it

From the RV web UI: Internet page -> WiFi Configuration. Expect up to ~70 s of waiting.

From a shell on the RP5 (or the Docker container):
```
python RP5toRPZero2WControl.py                      # interactive
python RP5toRPZero2WControl.py "My SSID" "pw"       # default profile ListenerManagedWifi
python RP5toRPZero2WControl.py "My SSID" "pw" Name  # named (permanent) profile
```
Host/port override: env `WIFI_BRIDGE_HOST`, `WIFI_BRIDGE_PORT` (defaults 10.10.0.1 / 12345).

## Troubleshooting (in order)

1. **Can the RP5 reach the Zero?** `ping 10.10.0.1` and `nc -vz 10.10.0.1 12345` from the RP5.
   Fails -> Zero off/not booted, link down, or service not running.
2. **Is the listener running?** On the Zero: `sudo systemctl status wifi-bridge-listener`
   (restart: `sudo systemctl restart wifi-bridge-listener`).
3. **What did the listener do?** `sudo journalctl -u wifi-bridge-listener -n 100`. It logs every
   nmcli command and each status poll. Look for the last "Final connection status" line.
4. **Does nmcli work by hand?** On the Zero: `nmcli device wifi list` (is the SSID visible? Zero 2W is
   2.4 GHz only), `nmcli connection show`, `sudo nmcli connection up ListenerManagedWifi`.
5. **Bad profile left behind?** `sudo nmcli connection delete ListenerManagedWifi` and retry.
6. Password must be 8-63 chars for WPA-PSK; open networks are not supported (listener always sets wpa-psk).

Note: the client does not resend after the packet is delivered. Earlier versions retried on a
10 s read timeout, which queued duplicate requests behind the still-running first one.

## Development workflow

The problem: Claude Code needs internet; the RP5/Zero are on the private RV network.
Pick one:

**A. Edit on the laptop (internet), run on the RP5 over SSH (recommended).**
Laptop stays on normal internet. Reach the RP5 at `192.168.2.196` when home/ethernet-connected
(see `~/.ssh/config`). Edit files locally with Claude, then `scp` or `git push` / `git pull` on
the RP5, restart the service/container. Use SSH from the RP5 to the Zero (`ssh zero`, i.e. tblank@10.10.0.1) to
see the listener logs. The RP5 is the jump host; the laptop never needs to be on 10.10.0.x.
From the RP5, `ssh zero` is configured in `~/.ssh/config` (user `tblank`, key
`~/.ssh/id_ed25519_zero`); the account is `tblank`, not `pi`.

**B. Samba mount of the RP5 code** (see `~/dotfiles/claude/CLAUDE-laptop.md`, "Remote Machine
Access"): edit live files natively from the laptop while it stays on internet.

**Avoid** joining the laptop to the Zero's/RV network just to test: you lose internet for Claude.
Test through the RP5 instead.

Deploying changes:
- Listener (on Zero): `scp RPZero2WListener.py zero:/tmp/` then
  `ssh -t zero 'sudo cp /tmp/RPZero2WListener.py /usr/local/sbin/ && sudo systemctl restart wifi-bridge-listener'`
  (`-t`: sudo on the Zero prompts for a password).
  Restarting the listener does not disturb the uplink -- it only drops the control
  socket; NetworkManager keeps wlan0 and the USB link up. Sending SET_WIFI *does*
  risk the uplink, so do not test a reconfiguration from a session that depends on it.
  Bump `SCRIPT_VERSION`; it is logged at start-up so you can confirm the new code is running.
- Client / web server (on RP5): `cd rv/docker && docker compose up -d --build webserver`.
- Service unit: `wifi-bridge-listener.service` -> `/etc/systemd/system/`, then
  `sudo systemctl daemon-reload && sudo systemctl enable --now wifi-bridge-listener`.
