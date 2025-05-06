import socket
import time
import sys # Import sys module for command-line arguments

def send_wifi_config(host, port, ssid, password, profile_name=None, retries=3, delay=5):
    """
    Sends a special packet to the bridge program to configure WiFi.
    Returns an exit code:
        0: WiFi connected and new SSID/PW installed.
        1: Any general failure (e.g., connection to listener, invalid packet, server error, profile add failure).
        100: Activation failed (likely bad password), but SSID/PW were updated/profile modified.
        101: WiFi didn't connect after activation attempt (e.g. bad/unreachable SSID, timeout),
             but SSID/PW were updated/profile modified.
    """
    client_socket = None # Initialize client_socket
    for attempt in range(1, retries + 1):
        try:
            # Create a TCP socket
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_socket.settimeout(10) # Add a timeout for connection and recv

            # Connect to the bridge program
            print(f"Attempt {attempt}: Connecting to {host}:{port}...")
            client_socket.connect((host, port))
            print(f"Connected to {host}:{port}")

            # Create the special packet
            if profile_name:
                packet = f"SET_WIFI_PROFILE,{ssid},{password},{profile_name}"
                print(f"Sent packet: SET_WIFI_PROFILE,{ssid},<password_hidden>,{profile_name}")
            else:
                packet = f"SET_WIFI,{ssid},{password}"
                print(f"Sent packet: SET_WIFI,{ssid},<password_hidden>")

            # Send the packet
            client_socket.sendall(packet.encode('utf-8'))

            # Receive the response
            response = client_socket.recv(1024).decode('utf-8')
            print(f"Received response: {response}")

            # Determine exit code based on response
            if response == "WiFi connection successful":
                return 0
            elif "Error: Activation failed - bad password?" in response:
                return 100 # Profile updated, but activation failed due to likely bad password
            elif "WiFi connection failed: Timeout or connection error" in response:
                # Profile updated, activation command likely succeeded, but final connection check failed
                return 101
            elif "Error: Failed to activate NM connection command" in response:
                # Profile updated, but activation command itself failed for other reasons
                return 1 # Treat as a more general failure if not specifically bad password
            else:
                # All other errors from listener (e.g., profile add failure, invalid packet), or unexpected responses
                return 1
        except socket.timeout:
             print(f"Attempt {attempt} failed: Connection or receive timed out.")
        except socket.error as e:
            print(f"Attempt {attempt} failed: Socket error - {e}")
        except Exception as e:
            print(f"Attempt {attempt} failed: Unexpected error - {e}")

        # Close socket before retrying or exiting loop
        if client_socket:
            client_socket.close()
            client_socket = None # Reset for next attempt

        # Retry logic
        if attempt < retries:
            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)
        else:
            print("All attempts failed. Please check the connection and try again.")
            return 1 # General failure if all retries fail

    # Ensure socket is closed if loop finishes without success (should be caught by return 1 above)
    if client_socket:
        client_socket.close()
    return 1 # Default to general failure if something unexpected happens


if __name__ == "__main__":
    # Configuration parameters
    RPI_HOST = '10.10.0.1' # Default host IP
    RPI_PORT = 12345      # Default host port
    exit_code = 1         # Default to general failure

    # Check for command-line arguments
    if len(sys.argv) == 3:
        # Use arguments: script_name ssid password (uses default profile on listener)
        cli_ssid = sys.argv[1]
        cli_password = sys.argv[2]
        print(f"Using command-line arguments: SSID='{cli_ssid}', Password=<hidden>, Profile=Default")
        exit_code = send_wifi_config(RPI_HOST, RPI_PORT, cli_ssid, cli_password)
        print("Command-line execution finished.")
    elif len(sys.argv) == 4:
        # Use arguments: script_name ssid password profile_name
        cli_ssid = sys.argv[1]
        cli_password = sys.argv[2]
        cli_profile = sys.argv[3]
        print(f"Using command-line arguments: SSID='{cli_ssid}', Password=<hidden>, Profile='{cli_profile}'")
        exit_code = send_wifi_config(RPI_HOST, RPI_PORT, cli_ssid, cli_password, profile_name=cli_profile)
        print("Command-line execution finished.")
    elif len(sys.argv) == 1:
        # No arguments provided, run the interactive loop
        print("No command-line arguments provided. Starting interactive mode.")
        while True:
            print("\nInteractive Mode: ^C to exit")
            profile_name_interactive = None # Default
            WIFI_SSID = input("Enter SSID: ")
            WIFI_PASSWORD = input("Enter Password: ")
            Profile = input("Enter Profile Name (or leave blank for default): ")
            if Profile.strip():
                profile_name_interactive = Profile.strip()
            # Send the WiFi configuration packet
            if profile_name_interactive:
                print(f"\nSending configuration for SSID: {WIFI_SSID} with Profile: {profile_name_interactive}")
            else:
                print(f"\nSending configuration for SSID: {WIFI_SSID} (Default Profile)")
            current_exit_code = send_wifi_config(RPI_HOST, RPI_PORT, WIFI_SSID, WIFI_PASSWORD, profile_name=profile_name_interactive)
            print(f"Operation resulted in exit code: {current_exit_code}")
            print("-" * 20) # Separator for clarity
            # Loop continues until Ctrl+C
    else:
        # Incorrect number of arguments
        print("Usage:")
        print(f"  Interactive mode: python {sys.argv[0]}")
        print(f"  Command-line (default profile): python {sys.argv[0]} <SSID> <Password>")
        print(f"  Command-line (custom profile):  python {sys.argv[0]} <SSID> <Password> <ProfileName>")
        print("Note: If SSID, Password or ProfileName contain spaces, enclose them in quotes.")
        exit_code = 1 # General failure due to incorrect usage

    print(f"Done. Exiting with code {exit_code}.")
    sys.exit(exit_code)

