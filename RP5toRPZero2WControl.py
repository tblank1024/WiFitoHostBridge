import socket
import time
import sys # Import sys module for command-line arguments

def send_wifi_config(host, port, ssid, password, retries=3, delay=5):
    """
    Sends a special packet to the bridge program to configure WiFi.

    Args:
        host (str): The IP address or hostname of the Raspberry Pi.
        port (int): The port number the bridge program is listening on.
        ssid (str): The WiFi SSID to configure.
        password (str): The WiFi password to configure.
        retries (int): Number of retries if the connection fails.
        delay (int): Delay (in seconds) between retries.
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
            packet = f"SET_WIFI,{ssid},{password}"

            # Send the packet
            client_socket.sendall(packet.encode('utf-8'))
            print(f"Sent packet: SET_WIFI,{ssid},<password_hidden>") # Avoid printing password

            # Receive the response
            response = client_socket.recv(1024).decode('utf-8')
            print(f"Received response: {response}")
            return  # Exit function if successful

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

    # Ensure socket is closed if loop finishes without success
    if client_socket:
        client_socket.close()


if __name__ == "__main__":
    # Configuration parameters
    RPI_HOST = '10.10.0.1' # Default host IP
    RPI_PORT = 12345      # Default host port

    # Check for command-line arguments
    if len(sys.argv) == 3:
        # Use arguments: script_name ssid password
        cli_ssid = sys.argv[1]
        cli_password = sys.argv[2]
        print(f"Using command-line arguments: SSID='{cli_ssid}', Password=<hidden>")
        send_wifi_config(RPI_HOST, RPI_PORT, cli_ssid, cli_password)
        print("Command-line execution finished.")
    elif len(sys.argv) == 1:
        # No arguments provided, run the interactive loop
        print("No command-line arguments provided. Starting interactive mode.")
        while True:
            tmp = input("1 for Guest, 2 for Home, 3 to exit: ")
            if tmp == '1':
                print("Decher WiFi configuration selected")
                # Update with correct Guest SSID/Password if needed
                WIFI_SSID = 'Decher&BlankGuests' # Example - Use actual SSID
                WIFI_PASSWORD = 'xxx' # Example - Use actual Password
            elif tmp == '2':
                print("Home WiFi configuration selected")
                WIFI_SSID = 'Buckley Clan 2'
                WIFI_PASSWORD = 'xxx'
            elif tmp == '3':
                print("Exiting interactive mode...")
                break
            else:
                print("Invalid choice. Please enter 1, 2, or 3.")
                continue # Ask again

            # Send the WiFi configuration packet
            print(f"\nSending configuration for SSID: {WIFI_SSID}")
            send_wifi_config(RPI_HOST, RPI_PORT, WIFI_SSID, WIFI_PASSWORD)
            print("-" * 20) # Separator for clarity
        print("Interactive mode finished.")
    else:
        # Incorrect number of arguments
        print("Usage:")
        print(f"  Interactive mode: python {sys.argv[0]}")
        print(f"  Command-line mode: python {sys.argv[0]} <SSID> <Password>")
        print("Note: If SSID or Password contain spaces, enclose them in quotes.")

    print("Done.")

