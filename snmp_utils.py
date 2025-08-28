from typing import Dict, Optional, Any
import re

# Lazy import puresnmp only when needed to avoid import issues on some envs


IF_NAME = '1.3.6.1.2.1.31.1.1.1.1'  # ifName
IF_OPER_STATUS = '1.3.6.1.2.1.2.2.1.8'  # 1=up,2=down

# MikroTik optical MIB numeric OIDs
MT_RX_POWER = '1.3.6.1.4.1.14988.1.1.6.1.1.4'  # mtxrOpticalRxPower.<ifIndex>
MT_TX_POWER = '1.3.6.1.4.1.14988.1.1.6.1.1.3'  # mtxrOpticalTxPower.<ifIndex>


def walk_if_names(host: str, community: str) -> Dict[str, int]:
    # Local import avoids environment issues if SNMP lib is missing
    from puresnmp import Client  # type: ignore
    names: Dict[str, int] = {}
    client = Client(host, community)
    for vb in client.walk(IF_NAME):
        idx = int(str(vb.oid).split('.')[-1])
        val = vb.value
        if isinstance(val, (bytes, bytearray)):
            name = val.decode('utf-8', errors='ignore')
        else:
            name = str(val)
        names[name] = idx
    return names
def _parse_power(value: Any) -> Optional[float]:
    # Handles numeric, bytes, or strings like '-3.2 dBm' or '-32'
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode('utf-8', errors='ignore')
        if isinstance(value, str):
            m = re.search(r"[-+]?\d+(?:\.\d+)?", value)
            if m:
                return float(m.group(0))
    except Exception:
        return None
    return None


def get_interface_status_and_power(host: str, community: str, if_name: str, username: Optional[str] = None, password: Optional[str] = None, port: int = 22) -> Optional[Dict[str, float]]:
    # Prefer SSH monitor for reliability across RouterOS and SFP variations
    try:
        from netmiko_utils import _open_ssh_client  # lazy import to avoid circular
        client = _open_ssh_client(type('D', (), {'host': host, 'username': username, 'password': password, 'port': port}))
        outputs = []
        cmds = [
            f"/interface/ethernet/monitor {if_name} once",
            f"/interface/ethernet/monitor interface={if_name} once",
            f"/interface/monitor {if_name} once",
        ]
        for cmd in cmds:
            try:
                stdin, stdout, stderr = client.exec_command(cmd)
                out = stdout.read().decode(errors='ignore')
                if out.strip():
                    outputs.append(out)
            except Exception:
                continue
        client.close()
        text = "\n".join(outputs)
        rx = tx = None
        oper = 0
        for raw in text.splitlines():
            line = raw.strip()
            if ':' not in line:
                continue
            k, v = [p.strip() for p in line.split(':', 1)]
            lk = k.lower()
            if lk in ('sfp-rx-power', 'rx-power', 'sfp-rx-power(dbm)'):
                rx = _parse_power(v)
            elif lk in ('sfp-tx-power', 'tx-power', 'sfp-tx-power(dbm)'):
                tx = _parse_power(v)
            elif lk in ('status', 'link', 'link-status', 'link-ok'):
                vv = v.lower()
                oper = 1 if ('ok' in vv or 'yes' in vv or 'up' in vv) else 2
        return {
            'if_index': 0,
            'if_oper_status': oper,
            'rx_power_dbm': rx,
            'tx_power_dbm': tx,
        }
    except Exception:
        # Try SNMP v2c if SSH not available
        try:
            name_to_index = walk_if_names(host, community)
            idx = name_to_index.get(if_name)
            if not idx:
                return None
            from puresnmp import Client  # type: ignore
            client = Client(host, community)
            oper_val = client.get(f'{IF_OPER_STATUS}.{idx}')
            try:
                oper = int(oper_val)
            except Exception:
                oper = int(_parse_power(oper_val) or 0)
            rx = _parse_power(client.get(f'{MT_RX_POWER}.{idx}'))
            tx = _parse_power(client.get(f'{MT_TX_POWER}.{idx}'))
            return {
                'if_index': idx,
                'if_oper_status': oper,
                'rx_power_dbm': rx,
                'tx_power_dbm': tx,
            }
        except Exception:
            return None


