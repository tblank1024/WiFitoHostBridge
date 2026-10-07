# Zero (`RP2W`) system configuration — captured reference

These files are copies of configuration that lives **only on the Zero's SD card** and was in no
repository. They are the rest of the bridge: without them, `RPZero2WListener.py` has nothing to
listen on. Captured 2026-10-06 from `RP2W`.

They are **reference copies, not a deployment source** — nothing here is installed by
`setup_services.sh`. Re-capture after changing anything on the Zero.

| File | Installed at | Enabled | Purpose |
|---|---|---|---|
| `usb0-up.service` | `/etc/systemd/system/` | enabled | `ip link set usb0 up` + `ip addr add 10.10.0.1/24 dev usb0` |
| `iptables.service` | `/etc/systemd/system/` | alias (active) | `netfilter-persistent start` — loads the `MASQUERADE` rule out `wlan0` |
| `ip6tables.service` | `/etc/systemd/system/` | alias (active) | IPv6 counterpart |
| `dhcpd.conf` | `/etc/dhcp/` | — | Hands the RP5 its `10.10.0.17` lease on `usb0` |
| `host-bridge.service` | `/etc/systemd/system/` | **disabled** | **Dead and corrupt** — see below |

Not captured: `/etc/iptables/rules.v4` (needs root to read). Capture it with
`ssh -t zero 'sudo cat /etc/iptables/rules.v4'` next time you have a sudo prompt open — it holds
the actual NAT rule and is the one file here with no other copy.

## Why this matters

`usb0` is **unmanaged by NetworkManager** (`managed=false` in
`/etc/NetworkManager/NetworkManager.conf`) and statically addressed by `usb0-up.service`. That is
the single most useful property of this system: **the RP5 ↔ Zero control path never traverses
`wlan0` or NetworkManager.** Losing the AP, restarting NetworkManager, or restarting the listener
costs you *internet* but never *control* — `10.10.0.1` stays reachable, so you can always fix the
WiFi remotely. Every recovery procedure depends on it.

## Known problems (verified 2026-10-06, not yet fixed)

**1. `dhcpd` is running unsupervised.** `isc-dhcp-server.service` is a `systemd-sysv-generator`
wrapper around `/etc/init.d/isc-dhcp-server`. Its state is:

```
× isc-dhcp-server.service - LSB: DHCP server
     Active: failed (Result: exit-code) since Sun 2026-10-04 09:45:18 PDT
     CGroup: └─686 /usr/sbin/dhcpd -4 -q -cf /etc/dhcp/dhcpd.conf
```

The unit is `failed` while the daemon it manages is alive (pid 686, PPID 1, started later than the
recorded failure). Consequence: **systemd will not restart `dhcpd` if it dies**, and the RP5's
address and default route are a lease from it (`default via 10.10.0.1 dev usb0 proto dhcp src
10.10.0.17`). If that process exits, the RP5 loses the address *and* the route — including the
means to fix it over the network.

Fix either way, ideally both:
- Give the RP5 a **static fallback address** on `usb0` so it never depends on the lease.
- Replace the sysv wrapper with a native unit (`mask` the generated one), or at least get the
  existing one back to `active` so `Restart=` means something.

**2. `host-bridge.service` is corrupt and should be deleted.** Its body is duplicated three times
in one file and `ExecStart`/`WorkingDirectory` are literal placeholders
(`/path/to/your/script/host-bridge.py`). It is `disabled` and inactive, so it does nothing today.
Remove with `ssh -t zero 'sudo rm /etc/systemd/system/host-bridge.service && sudo systemctl
daemon-reload'`.

**3. `usb0-up.service` ordering is implicit.** It is `After=network.target` only, and nothing
orders the listener after it. `wifi-bridge-listener.service` binds `10.10.0.1` and survives an
early start only because `Restart=on-failure` retries. New services that bind `10.10.0.1` should
declare `After=usb0-up.service` / `Wants=usb0-up.service` explicitly. Note also that
`ExecStart=/sbin/ip addr add ...` fails if the address already exists, so a restart of this oneshot
reports failure even though the interface is fine.
