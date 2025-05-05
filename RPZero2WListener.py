"""
How It Works: (NetworkManager Version for Bookworm+)
-------------
This program runs on the Pi Zero W and listens for WiFi configuration commands.
It uses NetworkManager (nmcli) to manage connections.
- It binds to a specified IP address and port on the Pi Zero W's *existing* network
  (e.g., Ethernet or a temporary hotspot).
- It listens for incoming TCP connections.
- When a packet "SET_WIFI,SSID,PASSWORD" is received:
    1. It attempts to delete any existing NetworkManager connection profile for that SSID.
    2. It adds a new NetworkManager connection profile using the provided SSID and password.
       (Requires sudo permissions).
    3. It attempts to activate (bring up) the new connection profile using `nmcli connection up`.
       (Requires sudo permissions).
    4. It checks the connection status for up to `CONNECTION_TIMEOUT` seconds
       by verifying the device state, active connection, and IP address acquisition.
    5. It sends a response back to the client indicating actual connection success or failure.

Usage:
------
1. Ensure the Pi Zero W has an initial network connection (e.g., Ethernet) so this script
   can listen for commands.
2. Ensure NetworkManager is managing the network interfaces (default on Bookworm).
3. Run the script on the Pi Zero W using sudo:
   sudo python3 host-bridge.py
4. From the RP5 (or another machine), run the client script (pi2w-bridge.py) to send
   the desired WiFi SSID and password to the IP address and port this script is listening on.

Expected Output (on Pi Zero W console):
---------------------------------------
Listening on <HOST>:<PORT>...
Connection from <Client IP Address>
Received data: SET_WIFI,<SSID>,<PASSWORD>
Attempting to configure WiFi via NetworkManager: SSID=<SSID>
Attempting to delete existing connection for SSID: <SSID>...
Adding new connection profile: <SSID>...
Attempting to activate connection: <SSID>...
Checking connection status for SSID: <SSID> (timeout=30s)...
Connection check attempt 1: Device State=connecting, Active SSID=None, IP=Not found
Connection check attempt 2: Device State=connected, Active SSID=<SSID>, IP=192.168.1.123
WiFi connection successful.

Expected Response Sent to Client:
---------------------------------
b'WiFi connection successful' or b'WiFi connection failed: <reason>'
"""

import socket
import subprocess
import time
import os
import re

# --- Configuration ---
HOST = "10.10.0.1"  # Listen only on this specific IP address
PORT = 12345
WIFI_INTERFACE = "wlan0" # Ensure this matches your WiFi interface name
CONNECTION_TIMEOUT = 45 # Increased timeout for NetworkManager
# --- End Configuration ---

def run_command(command, suppress_stderr=False):
    """
    Runs a shell command and returns its stdout. Raises exception on failure.
    """
    try:
        print(f"Running command: {' '.join(command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True, # Raise CalledProcessError on non-zero exit code
            timeout=20  # Add a timeout for safety
        )
        print(f"Command stdout:\n{result.stdout.strip()}")
        if result.stderr and not suppress_stderr:
            # nmcli often prints status messages to stderr, only show if not suppressed
            print(f"Command stderr:\n{result.stderr.strip()}")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        # Log detailed error including stdout/stderr from the failed command
        print(f"Error running command '{' '.join(command)}'. Return code: {e.returncode}")
        print(f"Stdout: {e.stdout.strip()}")
        print(f"Stderr: {e.stderr.strip()}")
        # Re-raise the error to be caught by the calling function
        raise
    except subprocess.TimeoutExpired:
        print(f"Timeout running command '{' '.join(command)}'")
        raise
    except Exception as e:
        print(f"Unexpected error running command '{' '.join(command)}': {e}")
        raise

def delete_nm_connection(ssid):
    """Attempts to delete an existing NetworkManager connection by SSID."""
    print(f"Attempting to delete existing connection for SSID: {ssid}...")
    try:
        # Find connection UUID(s) by SSID. Use --fields UUID,NAME for clarity.
        # Use check=False as the connection might not exist, which is fine.
        result = subprocess.run(
            ["nmcli", "-t", "-f", "UUID,NAME", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=10
        )
        connections_to_delete = []
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line: continue
                try:
                    uuid, name = line.split(':', 1)
                    # Use the name (which nmcli often sets based on SSID) for matching
                    if name == ssid:
                        connections_to_delete.append(uuid)
                except ValueError:
                    print(f"Warning: Could not parse nmcli output line: {line}")
                    continue # Skip malformed lines

        if not connections_to_delete:
            print(f"No existing connection profile found with name '{ssid}'.")
            return True # Nothing to delete is considered success

        for uuid in connections_to_delete:
             print(f"Deleting connection UUID: {uuid} (Name: {ssid})")
             # Use suppress_stderr=True as nmcli delete might print to stderr on success
             run_command(["nmcli", "connection", "delete", uuid], suppress_stderr=True)
        print(f"Successfully deleted existing connection(s) for SSID: {ssid}.")
        return True

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Error deleting connection for SSID {ssid}: {e}")
        # Don't treat failure to delete as fatal, maybe it didn't exist
        # or we can overwrite it anyway. Consider this non-critical.
        return False # Indicate deletion didn't fully succeed, but proceed

def add_nm_wifi_connection(ssid, password):
    """Adds a new NetworkManager WiFi connection profile."""
    print(f"Adding new connection profile: {ssid}...")
    # Use 'con-name' to explicitly set the profile name to the SSID
    # Use 'ifname' to bind it to the specific wlan interface if desired (optional but good practice)
    command = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "con-name", ssid, # Set connection name to SSID
        "ifname", WIFI_INTERFACE, # Bind to specific interface
        "ssid", ssid,     # Set the actual SSID
        "--",             # Separator for password section
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password
    ]
    try:
        run_command(command)
        print(f"Successfully added connection profile: {ssid}")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Failed to add connection profile {ssid}: {e}")
        return False

def activate_nm_connection(ssid):
    """Attempts to activate (bring up) a NetworkManager connection."""
    print(f"Attempting to activate connection: {ssid}...")
    # Use 'nmcli connection up' with the connection name (which we set to the SSID)
    command = ["nmcli", "connection", "up", ssid]
    try:
        # Use suppress_stderr=True as 'connection up' often prints status to stderr
        run_command(command, suppress_stderr=True)
        print(f"Successfully initiated connection activation for: {ssid}")
        # Activation is asynchronous, success here just means the command was accepted
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Failed to activate connection {ssid}: {e}")
        # Check stderr for common specific errors if possible
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            if "secrets were required" in e.stderr.lower():
                print("Activation failed likely due to incorrect password.")
            elif "connection profile is not valid" in e.stderr.lower():
                 print("Activation failed likely due to invalid profile configuration.")
        return False

def check_nm_connection_status(target_ssid, timeout):
    """Checks if the WiFi interface is connected via NetworkManager."""
    print(f"Checking connection status for target SSID: '{target_ssid}' (timeout={timeout}s)...")
    start_time = time.time()
    attempt = 0
    connected = False

    while time.time() - start_time < timeout:
        attempt += 1
        device_state = "unknown"
        active_ssid = "None"
        ip_address = "Not found"

        try:
            # 1. Check Device State
            # Use check=False as device might be temporarily unavailable during transition
            dev_status_result = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"],
                capture_output=True, text=True, check=False, timeout=5
            )
            if dev_status_result.returncode == 0:
                 for line in dev_status_result.stdout.strip().split('\n'):
                    if line.startswith(f"{WIFI_INTERFACE}:"):
                        device_state = line.split(':')[1]
                        break

            # 2. Check Active Connection SSID (more reliable than device wifi list)
            # Use check=False as no connection might be active yet
            active_conn_result = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                 capture_output=True, text=True, check=False, timeout=5
            )
            if active_conn_result.returncode == 0:
                for line in active_conn_result.stdout.strip().split('\n'):
                     if not line: continue
                     try:
                         name, device = line.split(':')
                         if device == WIFI_INTERFACE:
                             active_ssid = name # The active connection name on wlan0
                             break
                     except ValueError:
                         continue # Ignore malformed lines

            # 3. Check for IP address (only if device looks connected to the right SSID)
            if device_state == "connected" and active_ssid == target_ssid:
                # Use check=False, ip command might fail if interface is resetting
                ip_output_result = subprocess.run(
                    ["ip", "-4", "addr", "show", WIFI_INTERFACE], # Get IPv4 only
                    capture_output=True, text=True, check=False, timeout=5
                )
                if ip_output_result.returncode == 0:
                    ip_match = re.search(r"inet (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", ip_output_result.stdout)
                    if ip_match:
                        ip_address = ip_match.group(1)

            print(f"Connection check attempt {attempt}: Device State={device_state}, Active SSID='{active_ssid}', IP={ip_address}")

            # Check for success conditions
            if device_state == "connected" and active_ssid == target_ssid and ip_address != "Not found":
                print(f"Success criteria met: State={device_state}, SSID matches '{target_ssid}', IP found ({ip_address})")
                connected = True
                break # Exit loop on success
            elif device_state == "disconnected":
                 print("Device is disconnected. Waiting...")
            elif device_state == "connecting":
                 print("Device is connecting. Waiting...")
            elif device_state == "connected" and active_ssid != target_ssid:
                 print(f"Device connected but to wrong SSID ('{active_ssid}'). Waiting for switch...")


        except Exception as e:
            print(f"Error during connection check attempt {attempt}: {e}")
            # Continue checking until timeout

        # Prevent busy-waiting
        time.sleep(3) # Wait longer between checks for NM

    return connected, ip_address # Return IP as well for potential future use

def start_listener(host, port):
    """Starts listener, uses NetworkManager (nmcli) for WiFi config."""
    server_socket = None
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(1)
        print(f"Listening on {host}:{port} (NetworkManager mode)...")
        print("Ensure this script is run with 'sudo'.")

        while True:
            client_socket = None
            addr = None
            try:
                client_socket, addr = server_socket.accept()
                print(f"\nConnection from {addr}")

                data = client_socket.recv(1024).decode('utf-8').strip()
                print(f"Received data: {data}")

                if data.startswith("SET_WIFI"):
                    response_message = b"Unknown error occurred"
                    final_status = "failed: unknown reason"
                    try:
                        _, ssid, password = data.split(",", 2)
                        print(f"Attempting to configure WiFi via NetworkManager: SSID={ssid}")

                        # 1. Delete existing connection (best effort)
                        delete_nm_connection(ssid)
                        time.sleep(1) # Small delay after delete

                        # 2. Add new connection
                        if not add_nm_wifi_connection(ssid, password):
                            response_message = b"Error: Failed to add NM connection profile"
                            final_status = "failed: could not add profile"
                            raise ConnectionAbortedError("NM add failed")

                        time.sleep(1) # Small delay before activating

                        # 3. Activate connection
                        if not activate_nm_connection(ssid):
                            # Activation command failed immediately
                            response_message = b"Error: Failed to activate NM connection (check password?)"
                            final_status = "failed: activation command rejected"
                            raise ConnectionAbortedError("NM activate failed")

                        # 4. Check connection status (wait for async activation)
                        connected, ip_addr = check_nm_connection_status(ssid, CONNECTION_TIMEOUT)

                        if connected:
                            print(f"WiFi connection successful. IP Address: {ip_addr}")
                            response_message = b"WiFi connection successful"
                            final_status = f"successful (IP: {ip_addr})"
                        else:
                            print("WiFi connection failed or timed out.")
                            response_message = b"WiFi connection failed: Timeout or connection error"
                            final_status = "failed: timeout or connection error after activation attempt"

                    except ValueError:
                        print("Invalid SET_WIFI packet format. Expected SET_WIFI,SSID,PASSWORD")
                        response_message = b"Invalid packet format"
                        final_status = "failed: invalid packet format"
                    except ConnectionAbortedError as cae:
                        print(f"Aborting connection attempt: {cae}")
                        # response_message and final_status already set in the blocks above
                    except Exception as e:
                        print(f"Error processing SET_WIFI command: {e}")
                        response_message = b"Error processing command on server"
                        final_status = f"failed: server error ({e})"
                    finally:
                        print(f"Final connection status for {addr}: {final_status}")
                        if client_socket:
                            try:
                                client_socket.sendall(response_message)
                            except socket.error as sock_err:
                                print(f"Failed to send response to client: {sock_err}")

                else:
                    print("Invalid command received.")
                    if client_socket:
                        client_socket.sendall(b"Invalid command")

            except socket.timeout:
                print("Socket accept timed out.")
            except Exception as e:
                print(f"Error handling client connection: {e}")
            finally:
                if client_socket:
                    client_socket.close()
                    print("Client socket closed.")

    except PermissionError:
         print(f"Error: Permission denied binding to {host}:{port}. Try running with sudo.")
    except Exception as e:
        print(f"FATAL Error starting listener: {e}")
    finally:
        if server_socket:
            server_socket.close()
            print("Server socket closed.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Warning: Script not running as root (sudo). NetworkManager commands will fail.")
        # import sys
        # print("Please run this script using 'sudo python3 host-bridge.py'")
        # sys.exit(1)

    start_listener(HOST, PORT)