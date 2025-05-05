#!/bin/bash

# === Configuration ===
# Service file name (assuming it is in the current directory)
LISTENER_SERVICE_FILE="wifi-bridge-listener.service"

# Python script name (used in instructions)
LISTENER_SCRIPT_NAME="RPZero2WListener.py"

# Destination for systemd service files
SYSTEMD_DEST="/etc/systemd/system/"

# === Script Logic ===

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "Error: This script must be run with sudo." >&2
  exit 1
fi

# Check if listener service file exists in the current directory
if [ ! -f "$LISTENER_SERVICE_FILE" ]; then
    echo "Error: Listener service file '$LISTENER_SERVICE_FILE' not found in the current directory." >&2
    exit 1
fi

echo "--- Setting up WiFi Bridge Listener Service ---"

# --- Listener Service Setup ---
echo "1. Copying Listener service file ($LISTENER_SERVICE_FILE) to $SYSTEMD_DEST..."
cp "$LISTENER_SERVICE_FILE" "$SYSTEMD_DEST"
if [ $? -ne 0 ]; then echo "Error copying listener service file. Aborting."; exit 1; fi

echo "2. Setting permissions for $SYSTEMD_DEST$LISTENER_SERVICE_FILE..."
chmod 644 "$SYSTEMD_DEST$LISTENER_SERVICE_FILE"
if [ $? -ne 0 ]; then echo "Error setting permissions for listener service file. Aborting."; exit 1; fi

# --- Systemd Configuration ---
echo "3. Reloading systemd daemon..."
systemctl daemon-reload
if [ $? -ne 0 ]; then echo "Error reloading systemd daemon. Aborting."; exit 1; fi

echo "4. Enabling Listener service ($LISTENER_SERVICE_FILE) to start on boot..."
systemctl enable "${LISTENER_SERVICE_FILE%.service}" # Use filename without .service extension
if [ $? -ne 0 ]; then echo "Error enabling listener service. Aborting."; exit 1; fi

echo "--- Setup Complete ---"
echo ""
echo "IMPORTANT NEXT STEPS:"
echo "1. EDIT the listener service file in $SYSTEMD_DEST:"
echo "   - sudo nano $SYSTEMD_DEST$LISTENER_SERVICE_FILE"
echo "   - Verify 'WorkingDirectory' and 'ExecStart' paths point to where $LISTENER_SCRIPT_NAME is located."
echo "   - Verify the 'User' is correct (should be 'root' or a user with sudo rights for nmcli)."
echo "2. After editing, reload the daemon again:"
echo "   - sudo systemctl daemon-reload"
echo "3. You can now start the listener service manually to test:"
echo "   - sudo systemctl start ${LISTENER_SERVICE_FILE%.service}"
echo "4. Check the status:"
echo "   - sudo systemctl status ${LISTENER_SERVICE_FILE%.service}"
echo "5. View logs:"
echo "   - journalctl -u ${LISTENER_SERVICE_FILE%.service} -f"
echo "The listener service is now enabled and will start automatically on the next boot (after you've correctly edited the file)."
echo "Remember to run the $CLIENT_SCRIPT_NAME script from a *different* machine to send commands."

exit 0
