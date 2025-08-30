#!/usr/bin/env python3
"""
Debug script for RouterOS bandwidth monitoring
Run this directly on your server to test SSH commands
"""

import paramiko
import re
import time

def test_routeros_commands(host, username, password, interface_name):
    """Test various RouterOS commands to find working bandwidth counters"""
    print(f"Testing interface: {interface_name}")
    print(f"Connecting to: {host}")

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=username, password=password, timeout=15)

        # First check if interface exists
        print("\n=== Checking if interface exists ===")
        check_cmd = f"/interface print where name=\"{interface_name}\""
        print(f"Command: {check_cmd}")
        stdin, stdout, stderr = client.exec_command(check_cmd)
        check_output = stdout.read().decode('utf-8', errors='ignore')
        print(f"Output: {check_output.strip()}")
        if not check_output.strip():
            print("❌ Interface not found!")
            client.close()
            return

        # Test different monitor commands
        commands = [
            f"/interface ethernet monitor {interface_name} once",
            f"/interface monitor {interface_name} once",
            f"/interface wireless monitor {interface_name} once",
            f"/interface bridge monitor {interface_name} once",
            f"/interface vlan monitor {interface_name} once",
        ]

        print("\n=== Testing monitor commands ===")
        for cmd in commands:
            print(f"\n--- Command: {cmd} ---")
            try:
                stdin, stdout, stderr = client.exec_command(cmd)
                output = stdout.read().decode('utf-8', errors='ignore')
                err_output = stderr.read().decode('utf-8', errors='ignore')

                if output.strip():
                    print(f"✅ Output:\n{output}")
                    # Try to parse rx/tx values
                    rx_patterns = [
                        r"rx-byte\s*:\s*([0-9]+)",
                        r"rx-bytes\s*:\s*([0-9]+)",
                        r"rx\s*:\s*([0-9]+)",
                    ]
                    tx_patterns = [
                        r"tx-byte\s*:\s*([0-9]+)",
                        r"tx-bytes\s*:\s*([0-9]+)",
                        r"tx\s*:\s*([0-9]+)",
                    ]

                    rx_val = None
                    tx_val = None

                    for pattern in rx_patterns:
                        match = re.search(pattern, output, re.MULTILINE)
                        if match:
                            rx_val = int(match.group(1))
                            break

                    for pattern in tx_patterns:
                        match = re.search(pattern, output, re.MULTILINE)
                        if match:
                            tx_val = int(match.group(1))
                            break

                    if rx_val is not None and tx_val is not None:
                        print(f"✅ Successfully parsed: RX={rx_val}, TX={tx_val}")
                        return rx_val, tx_val
                    else:
                        print("⚠️  Command worked but couldn't parse rx/tx values")
                else:
                    print("❌ No output")

                if err_output.strip():
                    print(f"Error output: {err_output}")

            except Exception as e:
                print(f"❌ Command failed: {e}")

        print("\n=== Testing byte counter commands ===")
        # Test direct byte counter commands
        byte_commands = [
            f"/interface get [find name=\"{interface_name}\"] rx-byte",
            f"/interface get [find name=\"{interface_name}\"] tx-byte",
            f"/interface print where name=\"{interface_name}\" stats detail",
        ]

        for cmd in byte_commands:
            print(f"\n--- Command: {cmd} ---")
            try:
                stdin, stdout, stderr = client.exec_command(cmd)
                output = stdout.read().decode('utf-8', errors='ignore')
                if output.strip():
                    print(f"Output: {output.strip()}")
                else:
                    print("❌ No output")
            except Exception as e:
                print(f"❌ Command failed: {e}")

        client.close()
        print("\n=== Test completed ===")
        print("If no commands worked, the interface may not support monitoring or may have different name.")

    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    # Replace these with your actual values
    HOST = "YOUR_ROUTER_IP"  # e.g., "192.168.1.1"
    USERNAME = "YOUR_USERNAME"  # e.g., "admin"
    PASSWORD = "YOUR_PASSWORD"  # e.g., "password"
    INTERFACE = "YOUR_INTERFACE"  # e.g., "ether1"

    print("RouterOS Bandwidth Debug Tool")
    print("Edit the HOST, USERNAME, PASSWORD, and INTERFACE variables above")
    print("Then run: python3 debug_bandwidth.py")
    print()

    if HOST == "YOUR_ROUTER_IP":
        print("❌ Please edit the script with your actual router details!")
    else:
        test_routeros_commands(HOST, USERNAME, PASSWORD, INTERFACE)
