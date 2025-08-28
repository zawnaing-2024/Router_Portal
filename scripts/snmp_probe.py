import sqlite3
from typing import Any
from puresnmp import Client


def to_str(val: Any) -> str:
    if isinstance(val, (bytes, bytearray)):
        return val.decode('utf-8', errors='ignore')
    return str(val)


def num(val: Any):
    try:
        s = to_str(val)
        return float(s)
    except Exception:
        return None


def main():
    conn = sqlite3.connect('network_tools.db')
    cur = conn.cursor()
    cur.execute(
        """
        SELECT f.id, f.name, f.interface_name, d.host, d.snmp_community
        FROM fiber_checks f
        JOIN devices d ON d.id = f.device_id
        LIMIT 5
        """
    )
    rows = cur.fetchall()
    if not rows:
        print('No fiber_checks found. Add a monitor first.')
        return
    for fid, name, ifname, host, comm in rows:
        print(f"\nProbe check_id={fid} name={name} if={ifname} host={host}")
        try:
            c = Client(host, community=comm, timeout=1)
            # map ifName -> index
            name_to_idx = {}
            for vb in c.walk('1.3.6.1.2.1.31.1.1.1.1'):
                idx = int(str(vb.oid).split('.')[-1])
                name_to_idx[to_str(vb.value)] = idx
            idx = name_to_idx.get(ifname)
            print('ifIndex=', idx)
            if not idx:
                continue
            oper = int(c.get(f'1.3.6.1.2.1.2.2.1.8.{idx}'))
            rx = c.get(f'1.3.6.1.4.1.14988.1.1.6.1.1.4.{idx}')
            tx = c.get(f'1.3.6.1.4.1.14988.1.1.6.1.1.3.{idx}')
            print('oper=', oper, 'raw_rx=', rx, 'raw_tx=', tx, 'parsed_rx=', num(rx), 'parsed_tx=', num(tx))
        except Exception as exc:
            print('Probe error:', exc)


if __name__ == '__main__':
    main()


