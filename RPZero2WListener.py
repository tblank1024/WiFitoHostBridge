"""
How It Works: (NetworkManager Version for Bookworm+ - Simplified Fixed Profile)
-------------
This program runs on the Pi Zero W and listens for WiFi configuration commands.
It uses NetworkManager (nmcli) to manage connections, operating on a single,
fixed profile name for listener-managed connections.
- It binds to a specified IP address and port on the Pi Zero W's *existing* network.
- It listens for incoming TCP connections.
- When a packet "SET_WIFI,SSID,PASSWORD" is received:
    1. It attempts to delete any existing NetworkManager connection profile with the
       fixed name "ListenerManagedWifi".
    2. It adds a new NetworkManager connection profile using the provided SSID and password,
       but always naming the profile "ListenerManagedWifi".
    3. It attempts to activate (bring up) the "ListenerManagedWifi" connection profile.
    4. It checks the connection status for up to `CONNECTION_TIMEOUT` seconds
       by verifying the device state, active connection (checking if "ListenerManagedWifi"
       is active and connected to the *target* SSID), and IP address acquisition.
    5. It sends a response back to the client indicating actual connection success or failure.

Usage:
------
1. Ensure the Pi Zero W has an initial network connection (e.g., Ethernet over USB) so this script
   can listen for commands.
2. Ensure NetworkManager is managing the network interfaces (default on Bookworm).
3. Pre-configure any permanent WiFi networks using `nmcli device wifi connect ... name YourPermanentName`.
4. Use the setup_services.sh script to install this script to /usr/local/sbin and set up
   the systemd service (wifi-bridge-listener.service) to run it automatically on boot as root.
5. From the RP5 (or another machine), run the client script (RP5toRPZero2WControl.py) to send
   the desired WiFi SSID and password to the IP address and port this script is listening on.

Expected Output (on Pi Zero W console):
---------------------------------------
Listening on <HOST>:<PORT>...
Connection from <Client IP Address>
Received data: SET_WIFI,<SSID>,<PASSWORD>
Attempting to configure WiFi via NetworkManager: SSID=<SSID> using profile 'ListenerManagedWifi'
Attempting to delete existing connection profile: ListenerManagedWifi...
Adding/Modifying connection profile 'ListenerManagedWifi' for SSID: <SSID>...
Attempting to activate connection: ListenerManagedWifi...
Checking connection status for target SSID: <SSID> via profile 'ListenerManagedWifi' (timeout=45s)...
Connection check attempt 1: Device State=connecting, Active Profile=None, Active SSID=None, IP=Not found
Connection check attempt 2: Device State=connected, Active Profile=ListenerManagedWifi, Active SSID=<SSID>, IP=192.168.1.123
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
LISTENER_PROFILE_NAME = "ListenerManagedWifi" # Fixed profile name for this script
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
            print(f"Command stderr:\n{result.stderr.strip()}")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"ERROR running command '{' '.join(command)}'. Return code: {e.returncode}")
        print(f"--> Stdout: {e.stdout.strip()}")
        print(f"--> Stderr: {e.stderr.strip()}")
        raise
    except subprocess.TimeoutExpired as e:
        print(f"Timeout running command '{' '.join(command)}'")
        if e.stdout:
            print(f"--> Stdout (on timeout): {e.stdout.strip()}")
        if e.stderr:
            print(f"--> Stderr (on timeout): {e.stderr.strip()}")
        raise
    except Exception as e:
        print(f"Unexpected error running command '{' '.join(command)}': {e}")
        raise

def delete_nm_connection(profile_name):
    """Attempts to delete an existing NetworkManager connection by profile name."""
    print(f"Attempting to delete existing connection profile: {profile_name}...")
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "UUID,NAME", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=10
        )
        uuid_to_delete = None
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line: continue
                try:
                    uuid, name = line.split(':', 1)
                    if name == profile_name:
                        uuid_to_delete = uuid
                        break
                except ValueError:
                    print(f"Warning: Could not parse nmcli output line: {line}")
                    continue

        if not uuid_to_delete:
            print(f"No existing connection profile found with name '{profile_name}'.")
            return True

        print(f"Deleting connection UUID: {uuid_to_delete} (Name: {profile_name})")
        run_command(["nmcli", "connection", "delete", uuid_to_delete], suppress_stderr=True)
        print(f"Successfully deleted existing connection profile: {profile_name}.")
        return True

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Error deleting connection profile {profile_name}: {e}")
        return False

def add_nm_wifi_connection(profile_name, ssid, password):
    """Adds or modifies a NetworkManager WiFi connection profile with a fixed name."""
    print(f"Adding/Modifying connection profile '{profile_name}' for SSID: {ssid}...")
    command = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "con-name", profile_name,
        "ifname", WIFI_INTERFACE,
        "ssid", ssid,
        "--",
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password
    ]
    try:
        run_command(command)
        print(f"Successfully added/modified connection profile: {profile_name}")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Failed to add/modify connection profile '{profile_name}' for SSID '{ssid}'.")
        return False

def activate_nm_connection(profile_name):
    """Attempts to activate (bring up) a NetworkManager connection by profile name."""
    print(f"Attempting to activate connection: {profile_name}...")
    command = ["nmcli", "connection", "up", profile_name]
    try:
        run_command(command, suppress_stderr=True)
        print(f"Successfully initiated connection activation for: {profile_name}")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as e:
        print(f"Failed to activate connection {profile_name}: {e}")
        return False

def check_nm_connection_status(target_ssid, profile_name_to_check, timeout):
    """Checks if the WiFi interface is connected via NetworkManager using the specific profile and to the target SSID."""
    print(f"Checking connection status for target SSID: '{target_ssid}' via profile '{profile_name_to_check}' (timeout={timeout}s)...")
    start_time = time.time()
    attempt = 0
    connected = False
    ip_address = "Not found"

    while time.time() - start_time < timeout:
        attempt += 1
        device_state = "unknown"
        active_profile_name = "None"
        active_ssid = "None"
        ip_address = "Not found"

        try:
            dev_status_result = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"],
                capture_output=True, text=True, check=False, timeout=5
            )
            if dev_status_result.returncode == 0:
                 for line in dev_status_result.stdout.strip().split('\n'):
                    if line.startswith(f"{WIFI_INTERFACE}:"):
                        device_state = line.split(':')[1]
                        break

            active_conn_result = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,DEVICE,ACTIVE-SSID", "connection", "show", "--active"],
                 capture_output=True, text=True, check=False, timeout=5
            )
            if active_conn_result.returncode == 0:
                for line in active_conn_result.stdout.strip().split('\n'):
                     if not line: continue
                     try:
                         parts = line.split(':')
                         if len(parts) >= 2 and parts[1] == WIFI_INTERFACE:
                             active_profile_name = parts[0]
                             if len(parts) >= 3:
                                 active_ssid = parts[2]
                             break
                     except ValueError:
                         continue

            if device_state == "connected" and active_profile_name == profile_name_to_check and active_ssid == target_ssid:
                ip_output_result = subprocess.run(
                    ["ip", "-4", "addr", "show", WIFI_INTERFACE],
                    capture_output=True, text=True, check=False, timeout=5
                )
                if ip_output_result.returncode == 0:
                    ip_match = re.search(r"inet (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", ip_output_result.stdout)
                    if ip_match:
                        ip_address = ip_match.group(1)

            print(f"Connection check attempt {attempt}: Device State={device_state}, Active Profile='{active_profile_name}', Active SSID='{active_ssid}', IP={ip_address}")

            if device_state == "connected" and active_profile_name == profile_name_to_check and active_ssid == target_ssid and ip_address != "Not found":
                print(f"Success criteria met: State={device_state}, Profile matches '{profile_name_to_check}', SSID matches '{target_ssid}', IP found ({ip_address})")
                connected = True
                break
            elif device_state == "disconnected":
                 print("Device is disconnected. Waiting...")
            elif device_state == "connecting":
                 print("Device is connecting. Waiting...")
            elif device_state == "connected" and active_profile_name != profile_name_to_check:
                 print(f"Device connected but wrong profile active ('{active_profile_name}'). Waiting for '{profile_name_to_check}'...")
            elif device_state == "connected" and active_profile_name == profile_name_to_check and active_ssid != target_ssid:
                 print(f"Device connected with correct profile but wrong SSID ('{active_ssid}'). Waiting for '{target_ssid}'...")

        except Exception as e:
            print(f"Error during connection check attempt {attempt}: {e}")

        time.sleep(3)

    return connected, ip_address

def start_listener(host, port):
    """Starts listener, uses NetworkManager (nmcli) for WiFi config with a fixed or custom profile name."""
    server_socket = None
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(1)
        print(f"Listening on {host}:{port} (NetworkManager mode - Default Profile '{LISTENER_PROFILE_NAME}')...")
        print("Ensure this script is run with 'sudo'.")

        while True:
            client_socket = None
            addr = None
            try:
                client_socket, addr = server_socket.accept()
                print(f"\nConnection from {addr}")

                data = client_socket.recv(1024).decode('utf-8').strip()
                print(f"Received data: {data}")

                response_message = b"Unknown error occurred"
                final_status = "failed: unknown reason"
                profile_to_use = LISTENER_PROFILE_NAME # Default

                try:
                    if data.startswith("SET_WIFI_PROFILE,"):
                        _, ssid, password, custom_profile_name = data.split(",", 3)
                        profile_to_use = custom_profile_name.strip()
                        if not profile_to_use: # Ensure custom profile name is not empty
                            raise ValueError("Custom profile name cannot be empty.")
                        print(f"Attempting to configure WiFi via NetworkManager: SSID={ssid} using CUSTOM profile '{profile_to_use}'")
                    elif data.startswith("SET_WIFI,"):
                        _, ssid, password = data.split(",", 2)
                        # profile_to_use is already LISTENER_PROFILE_NAME
                        print(f"Attempting to configure WiFi via NetworkManager: SSID={ssid} using DEFAULT profile '{profile_to_use}'")
                    else:
                        raise ValueError("Invalid command prefix")

                    # 1. Delete existing connection for the determined profile (best effort)
                    delete_nm_connection(profile_to_use)
                    time.sleep(1) # Small delay after delete

                    # 2. Add/Modify connection with the determined profile name
                    if not add_nm_wifi_connection(profile_to_use, ssid, password):
                        response_message = b"Error: Failed to add/modify NM connection profile"
                        final_status = f"failed: could not add/modify profile '{profile_to_use}'"
                        raise ConnectionAbortedError("NM add/modify failed")

                    time.sleep(1) # Small delay before activating

                    # 3. Activate the determined connection profile
                    if not activate_nm_connection(profile_to_use):
                        response_message = b"Error: Failed to activate NM connection (check password?)"
                        final_status = f"failed: activation command rejected for '{profile_to_use}'"
                        raise ConnectionAbortedError("NM activate failed")

                    # 4. Check connection status (wait for async activation, check correct SSID)
                    connected, ip_addr = check_nm_connection_status(ssid, profile_to_use, CONNECTION_TIMEOUT)

                    if connected:
                        print(f"WiFi connection successful for profile '{profile_to_use}'. IP Address: {ip_addr}")
                        response_message = b"WiFi connection successful"
                        final_status = f"successful (IP: {ip_addr})"
                    else:
                        print(f"WiFi connection failed or timed out for profile '{profile_to_use}'.")
                        response_message = b"WiFi connection failed: Timeout or connection error"
                        final_status = "failed: timeout or connection error after activation attempt"

                except ValueError as ve:
                    print(f"Invalid packet format or value error: {ve}. Data: '{data}'")
                    response_message = b"Invalid packet format or value"
                    final_status = f"failed: invalid packet ({ve})"
                except ConnectionAbortedError as cae:
                    print(f"Aborting connection attempt for profile '{profile_to_use}': {cae}")
                    # response_message and final_status already set in the blocks above
                except Exception as e:
                    print(f"Error processing command for profile '{profile_to_use}': {e}")
                    response_message = b"Error processing command on server"
                    final_status = f"failed: server error ({e})"
                finally:
                    print(f"Final connection status for {addr} (Profile: '{profile_to_use}'): {final_status}")
                    if client_socket:
                        try:
                            client_socket.sendall(response_message)
                        except socket.error as sock_err:
                            print(f"Failed to send response to client: {sock_err}")

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

    start_listener(HOST, PORT)