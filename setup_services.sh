#!/bin/bash
#
# Installs the WiFi API service, on either host that can run it.
#
# Usage (run ON the target host, with sudo):
#   sudo ./setup_services.sh rp5     # the RP5 "Sophie" + Alfa adapter (wlan1)
#   sudo ./setup_services.sh zero    # the Pi Zero 2W bridge (wlan0)  [legacy]
#
# Both install the same rpzero_wifi_api.py; the unit's Environment= block is
# the whole difference. The RP5 deployment replaces the Zero -- run one or the
# other, and point the dashboard's WIFI_BRIDGE_HOST at whichever is live
# (127.0.0.1 for rp5, 10.10.0.1 for zero).
#
# Restarting the service does NOT disturb the uplink: NetworkManager owns the
# radio independently of this process.

set -u

SCRIPT_DEST_DIR="/usr/local/sbin"
SYSTEMD_DEST="/etc/systemd/system"

API_SCRIPT="rpzero_wifi_api.py"

MODE="${1:-}"
case "$MODE" in
  rp5)  UNIT="wifi-api-rp5.service";     CHECK_HOST="127.0.0.1" ;;
  zero) UNIT="wifi-bridge-api.service";  CHECK_HOST="10.10.0.1" ;;
  *) echo "Usage: sudo $0 rp5|zero" >&2; exit 2 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: this script must be run with sudo." >&2
  exit 1
fi

# The two units would both serve port 12346 and both drive nmcli. Running them
# together means two services fighting over one NetworkManager.
OTHER_UNIT="wifi-bridge-api.service"
[ "$MODE" = zero ] && OTHER_UNIT="wifi-api-rp5.service"
if systemctl is-enabled --quiet "$OTHER_UNIT" 2>/dev/null || \
   systemctl is-active --quiet "$OTHER_UNIT" 2>/dev/null; then
  echo "Error: $OTHER_UNIT is installed on this host and would collide on port" >&2
  echo "       12346. Run: sudo systemctl disable --now $OTHER_UNIT" >&2
  exit 1
fi

install_service() {
  local script="$1" unit="$2"

  for f in "$script" "$unit"; do
    if [ ! -f "$f" ]; then
      echo "Error: '$f' not found in the current directory." >&2
      return 1
    fi
  done

  echo "--- Installing $unit ---"

  # Keep a copy of whatever is being replaced; a rollback must not require
  # the internet, which may be exactly what is broken.
  if [ -f "$SCRIPT_DEST_DIR/$script" ] && \
     ! cmp -s "$script" "$SCRIPT_DEST_DIR/$script"; then
    cp "$SCRIPT_DEST_DIR/$script" "$SCRIPT_DEST_DIR/$script.prev" || return 1
    echo "  previous version saved as $SCRIPT_DEST_DIR/$script.prev"
  fi

  install -m 755 -o root -g root "$script" "$SCRIPT_DEST_DIR/$script" || return 1
  install -m 644 -o root -g root "$unit" "$SYSTEMD_DEST/$unit" || return 1

  systemctl daemon-reload || return 1
  systemctl enable "$unit" >/dev/null 2>&1 || return 1
  systemctl restart "$unit" || return 1

  sleep 2
  if ! systemctl is-active --quiet "$unit"; then
    echo "  ERROR: $unit is not active after restart." >&2
    systemctl status "$unit" --no-pager | head -12 >&2
    return 1
  fi

  echo "  active. Version banner from the journal:"
  journalctl -u "$unit" -n 20 --no-pager 2>/dev/null \
    | grep -iE "version|listening" | tail -3 | sed 's/^/    /'
  return 0
}

if ! install_service "$API_SCRIPT" "$UNIT"; then
  echo ""
  echo "--- FAILED --- see the errors above. The previous script version, if"
  echo "any, is at $SCRIPT_DEST_DIR/$API_SCRIPT.prev"
  exit 1
fi

echo ""
echo "--- Done ---"
echo "Verify:"
echo "  curl -s http://$CHECK_HOST:12346/api/health"
echo "  curl -s 'http://$CHECK_HOST:12346/api/networks?rescan=1'"
echo "Logs:   journalctl -u $UNIT -f"
exit 0
