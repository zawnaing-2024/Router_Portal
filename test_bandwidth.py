#!/usr/bin/env python3
"""Test bandwidth monitoring functions directly"""

from netmiko_utils import get_interface_rates, _open_ssh_client
import paramiko

# Test device configuration
class TestDevice:
    def __init__(self):
        self.host = "103.133.243.27"
        self.username = "admin"
        self.password = "One@2024"
        self.port = 19822
        self.device_type = "mikrotik"
        self.name = "Test Router"

def test_direct_call():
    """Test get_interface_rates function directly"""
    print("=== Testing get_interface_rates function directly ===")

    device = TestDevice()
    interface_name = "sfp-sfpplus2"

    try:
        result = get_interface_rates(device, interface_name)
        print(f"Result: {result}")

        if result:
            rx_mbps = result.get('rx_bps', 0) / 1_000_000
            tx_mbps = result.get('tx_bps', 0) / 1_000_000
            print(f"✅ SUCCESS: RX={rx_mbps:.1f} Mbps, TX={tx_mbps:.1f} Mbps")
        else:
            print("❌ FAILED: Function returned None")

    except Exception as e:
        print(f"❌ EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_direct_call()
