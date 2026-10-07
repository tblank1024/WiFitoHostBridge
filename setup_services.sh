#!/bin/bash
#
# Installs the WiFi bridge services on the Pi Zero 2W.
#
# Usage (run ON the Zero, with sudo):
#   sudo ./setup_services.sh listener   # the legacy socket listener (port 12345)
#   sudo ./setup_services.sh api        # the HTTP JSON API (port 12346)
#   sudo ./setup_services.sh both       # both (default)
#
# The two services use different ports and can run side by side; that is how
# the cutover is meant to happen. Restarting either one does NOT disturb the
# uplink -- NetworkManager owns wlan0 independently of these processes, and
# usb0 is static and NM-unmanaged, so the RP5 never loses contact with the Zero.

set -u

SCRIPT_DEST_DIR="/usr/local/sbin"
SYSTEMD_DEST="/etc/systemd/system"

LISTENER_SCRIPT="RPZero2WListener.py"
LISTENER_UNIT="wifi-bridge-listener.service"
API_SCRIPT="rpzero_wifi_api.py"
API_UNIT="wifi-bridge-api.service"

MODE="${1:-both}"
case "$MODE" in
  listener|api|both) ;;
  *) echo "Usage: sudo $0 [listener|api|both]" >&2; exit 2 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: this script must be run with sudo." >&2
  exit 1
fi

# install_service <script> <unit>
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

RC=0
if [ "$MODE" = "listener" ] || [ "$MODE" = "both" ]; then
  install_service "$LISTENER_SCRIPT" "$LISTENER_UNIT" || RC=1
fi
if [ "$MODE" = "api" ] || [ "$MODE" = "both" ]; then
  install_service "$API_SCRIPT" "$API_UNIT" || RC=1
fi

if [ "$RC" -ne 0 ]; then
  echo ""
  echo "--- FAILED --- see the errors above. The previous script version, if"
  echo "any, is at $SCRIPT_DEST_DIR/<script>.prev"
  exit "$RC"
fi

echo ""
echo "--- Done ---"
echo "Verify from the RP5 (not from here):"
echo "  curl -s http://10.10.0.1:12346/api/health"
echo "  curl -s 'http://10.10.0.1:12346/api/networks?rescan=1'"
echo "Logs:   journalctl -u $API_UNIT -f"
exit 0
