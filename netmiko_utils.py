import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import paramiko
import pytz

from flask import current_app


def _open_ssh_client(device) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=device.host,
        port=device.port or 22,
        username=device.username,
        password=device.password,
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )
    return client


def _backup_filename(device_name: str) -> Tuple[str, str]:
    # Format: device_name_DDMMYYHH.backup (Yangon local time)
    yangon = pytz.timezone('Asia/Yangon')
    ts = datetime.now(timezone.utc).astimezone(yangon).strftime('%d%m%y%H')
    base_name = f"{device_name}_{ts}"
    filename = f"{base_name}.backup"
    return base_name, filename


def backup_device_and_download(device) -> Tuple[bool, str]:
    """Create a RouterOS .backup on device and download it to backups/ directory.

    Returns (success, message)
    """
    base_name, filename = _backup_filename(device.name)

    try:
        client = _open_ssh_client(device)
        # Create backup on device (RouterOS will append .backup)
        stdin, stdout, stderr = client.exec_command(f"/system backup save name={base_name}")
        _ = stdout.read().decode(errors='ignore')
        err = stderr.read().decode(errors='ignore')
        if err and 'failure' in err.lower():
            client.close()
            return False, f'RouterOS backup command error: {err.strip()}'

        # Wait briefly to ensure file creation
        time.sleep(2)

        # Download via SFTP using Paramiko
        transport = paramiko.Transport((device.host, device.port or 22))
        transport.connect(username=device.username, password=device.password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        remote_path = f"/{filename}"
        # Save under date-based folder in Yangon time: backups/YYYY-MM-DD/
        yangon = pytz.timezone('Asia/Yangon')
        date_folder = datetime.now(timezone.utc).astimezone(yangon).strftime('%Y-%m-%d')
        local_dir = os.path.join(current_app.root_path, 'backups', date_folder)
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, filename)

        # Try multiple attempts in case the file isn't immediately ready
        for _ in range(10):
            try:
                sftp.get(remote_path, local_path)
                break
            except FileNotFoundError:
                time.sleep(1)
        else:
            sftp.close()
            transport.close()
            client.close()
            return False, 'Backup file not found on device after waiting.'

        sftp.close()
        transport.close()
        client.close()
        return True, f'Backup saved to {local_path}'
    except Exception as exc:  # noqa: BLE001
        return False, f'Backup error: {exc}'


def fetch_device_resources(device) -> Optional[Dict[str, float]]:
    """Fetch CPU, RAM, and storage metrics.

    - For Mikrotik: use RouterOS commands
    - For Linux: read /proc and df via SSH
    """
    if getattr(device, 'device_type', 'mikrotik') == 'linux':
        try:
            client = _open_ssh_client(device)
            # CPU from /proc/stat (locale-independent): two samples to compute usage
            cpu_cmd = "cat /proc/stat; sleep 0.5; cat /proc/stat"
            mem_cmd = "cat /proc/meminfo"
            disk_cmds = [
                "df -B1 / | tail -1",
            ]
            # Execute commands
            _, cpu_out, _ = client.exec_command(cpu_cmd)
            cpu_out = cpu_out.read().decode(errors='ignore')
            _, mem_out, _ = client.exec_command(mem_cmd)
            mem_out = mem_out.read().decode(errors='ignore')
            disk_out = ''
            for dcmd in disk_cmds:
                try:
                    _, dout, _ = client.exec_command(dcmd)
                    tmp = dout.read().decode(errors='ignore')
                    if tmp.strip():
                        disk_out = tmp
                        break
                except Exception:
                    continue
            client.close()
        except Exception:
            return None

        metrics: Dict[str, float] = {}

        # CPU usage calc from /proc/stat
        def parse_cpu(line: str) -> Optional[tuple[int, int]]:
            if not line.startswith('cpu '):
                return None
            parts = line.split()
            try:
                # user nice system idle iowait irq softirq steal guest guest_nice
                vals = list(map(int, parts[1:]))
            except ValueError:
                return None
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return idle, total

        cpu_lines = [ln for ln in cpu_out.splitlines() if ln.startswith('cpu ')]
        if len(cpu_lines) >= 2:
            p1 = parse_cpu(cpu_lines[0])
            p2 = parse_cpu(cpu_lines[1])
            if p1 and p2:
                idle1, total1 = p1
                idle2, total2 = p2
                dt_idle = max(0, idle2 - idle1)
                dt_total = max(1, total2 - total1)
                usage = (1.0 - (dt_idle / dt_total)) * 100.0
                metrics['cpu_load_percent'] = max(0.0, min(100.0, usage))

        # Memory from /proc/meminfo
        def get_kb(key: str) -> Optional[int]:
            mm = re.search(rf"^{key}:\s+(\d+)\s+kB", mem_out, re.MULTILINE)
            return int(mm.group(1)) * 1024 if mm else None
        mem_total = get_kb('MemTotal')
        mem_free = get_kb('MemAvailable') or get_kb('MemFree')
        if mem_total:
            metrics['total_memory_bytes'] = mem_total
        if mem_free is not None:
            metrics['free_memory_bytes'] = mem_free

        # Disk from df output like: Filesystem Size Used Avail Use% Mounted
        parts = disk_out.split()
        # Expect at least: FS, Size, Used, Avail, Use%, Mount
        if len(parts) >= 6:
            try:
                total = int(parts[1])
                avail = int(parts[3])
                metrics['total_storage_bytes'] = total
                metrics['free_storage_bytes'] = avail
            except ValueError:
                pass

        return metrics if metrics else None

    # Mikrotik path
    try:
        client = _open_ssh_client(device)
        stdin, stdout, stderr = client.exec_command("/system resource print without-paging")
        output = stdout.read().decode(errors='ignore')
        _ = stderr.read().decode(errors='ignore')
        client.close()
    except Exception:  # noqa: BLE001
        return None

    # Parse output like RouterOS resource print
    def parse_size_to_bytes(value: str) -> Optional[int]:
        match = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|GiB|TiB|kB|MB|GB|TB)?", value)
        if not match:
            return None
        num = float(match.group(1))
        unit = (match.group(2) or '').lower()
        factor = 1
        if unit in ('kib', 'kb'):
            factor = 1024
        elif unit in ('mib', 'mb'):
            factor = 1024 ** 2
        elif unit in ('gib', 'gb'):
            factor = 1024 ** 3
        elif unit in ('tib', 'tb'):
            factor = 1024 ** 4
        return int(num * factor)

    metrics: Dict[str, float] = {}
    for line in output.splitlines():
        if ':' not in line:
            continue
        key, val = [p.strip() for p in line.split(':', 1)]
        if key == 'cpu-load':
            try:
                metrics['cpu_load_percent'] = float(val.strip().rstrip('%'))
            except ValueError:
                pass
        elif key == 'free-memory':
            b = parse_size_to_bytes(val)
            if b is not None:
                metrics['free_memory_bytes'] = b
        elif key == 'total-memory':
            b = parse_size_to_bytes(val)
            if b is not None:
                metrics['total_memory_bytes'] = b
        elif key == 'free-hdd-space':
            b = parse_size_to_bytes(val)
            if b is not None:
                metrics['free_storage_bytes'] = b
        elif key == 'total-hdd-space':
            b = parse_size_to_bytes(val)
            if b is not None:
                metrics['total_storage_bytes'] = b

    # Basic validation
    if not metrics:
        return None
    return metrics


def perform_manual_backup(device) -> Tuple[bool, str]:
    return backup_device_and_download(device)


def run_ping_on_router(device, target_ip: str, source_ip: Optional[str] = None, source_interface: Optional[str] = None) -> Optional[float]:
    """Run ping from Mikrotik and return average RTT in ms, or None on failure.

    Uses RouterOS ping command without-paging and parses sent/received and round-trip times.
    """
    # Try multiple command variants for RouterOS v6/v7
    params_positional = f"count=1"  # one probe
    if source_ip:
        params_positional += f" src-address={source_ip}"
    if source_interface:
        params_positional += f" interface={source_interface}"

    params_named = f"address={target_ip} count=1"
    if source_ip:
        params_named += f" src-address={source_ip}"
    if source_interface:
        params_named += f" interface={source_interface}"

    ping_cmds = [
        f"/tool/ping {target_ip} {params_positional}",
        f"/tool ping {target_ip} {params_positional}",
        f"/ping {target_ip} {params_positional}",
        f"/tool/ping {params_named}",
        f"/tool ping {params_named}",
        f"/ping {params_named}",
    ]

    output = ''
    try:
        client = _open_ssh_client(device)
        for cmd in ping_cmds:
            try:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=2)
                out = stdout.read().decode(errors='ignore')
                err = stderr.read().decode(errors='ignore')
                if (out and out.strip()) or (err and err.strip()):
                    output = out + "\n" + err
                    break
            except Exception:
                continue
        client.close()
    except Exception:
        return None
    if not output:
        return None

    # Try summary first
    m = re.search(r"round-trip\s+min/avg/max\s*=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms", output)
    if m:
        try:
            return float(m.group(2))
        except ValueError:
            pass

    # Fallback: parse single 'time=' value lines, e.g., "time=8ms" or "time=8.3 ms"
    times = []
    for line in output.splitlines():
        mt = re.search(r"time\s*=\s*([0-9.]+)\s*ms", line, re.IGNORECASE)
        if mt:
            try:
                times.append(float(mt.group(1)))
            except ValueError:
                continue
        # v7 sometimes prints 'avg = Xms' in summary
        mt2 = re.search(r"avg\s*=\s*([0-9.]+)\s*ms", line, re.IGNORECASE)
        if mt2:
            try:
                times.append(float(mt2.group(1)))
            except ValueError:
                continue
        mt3 = re.search(r"avg-?rtt\s*=\s*([0-9.]+)\s*ms", line, re.IGNORECASE)
        if mt3:
            try:
                times.append(float(mt3.group(1)))
            except ValueError:
                continue
    # If packet loss is 100%, return None
    if re.search(r"received\s*=\s*0|packet\s+loss\s*=\s*100%", output, re.IGNORECASE):
        return None
    if times:
        return sum(times) / len(times)
    return None


def list_interfaces(device) -> Optional[list[str]]:
    """Return list of interface names on the device.

    - Mikrotik: parse /interface print detail for name= fields
    - Linux: list /sys/class/net entries (excluding lo)
    """
    if getattr(device, 'device_type', 'mikrotik') == 'linux':
        try:
            client = _open_ssh_client(device)
            _, out, _ = client.exec_command("ls -1 /sys/class/net | grep -v '^lo$'")
            data = out.read().decode(errors='ignore')
            client.close()
            names = [ln.strip() for ln in data.splitlines() if ln.strip()]
            return names
        except Exception:
            return None
    # Mikrotik
    try:
        client = _open_ssh_client(device)
        _, out, _ = client.exec_command("/interface print detail without-paging")
        data = out.read().decode(errors='ignore')
        client.close()
    except Exception:
        return None
    names: list[str] = []
    for line in data.splitlines():
        m = re.search(r"\bname=([^\s]+)", line)
        if m:
            names.append(m.group(1))
    # de-duplicate while preserving order
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def get_interface_rates(device, interface_name: str) -> Optional[Dict[str, float]]:
    """Return instantaneous rx/tx rates in bits per second.

    - Mikrotik: use /interface monitor-traffic ... once
    - Linux: not available directly; return None (computed in app using byte counters)
    """
    if getattr(device, 'device_type', 'mikrotik') == 'linux':
        return None
    # Try multiple command variants and quoting; handle spaced digits
    data = ''
    try:
        client = _open_ssh_client(device)
        variants = [
            f"/interface monitor-traffic interface=\"{interface_name}\" once without-paging",
            f"/interface monitor-traffic interface={interface_name} once without-paging",
            f"/interface/monitor-traffic interface=\"{interface_name}\" once without-paging",
            f"/tool/monitor-traffic interface=\"{interface_name}\" once without-paging",
            # as-value variants often produce key=value which are easier to parse
            f"/interface monitor-traffic interface=\"{interface_name}\" once as-value",
            f"/interface monitor-traffic interface={interface_name} once as-value",
        ]
        for cmd in variants:
            try:
                print(f"[BW] running cmd: {cmd}")
                _, out, _ = client.exec_command(cmd)
                tmp = out.read().decode(errors='ignore')
                print(f"[BW] cmd output: {tmp.strip()[:200]}")
                if tmp and tmp.strip():
                    data = tmp
                    break
            except Exception:
                continue
        client.close()
    except Exception:
        return None
    if not data:
        data = ''
    rx = None
    tx = None
    for line in data.splitlines():
        print(f"[BW] parse line: {line.strip()}")
        # Support both ": value" and "=value" forms, allow spaces and decimals within digits
        mrx = re.search(r"rx-bits-per-second\s*[:=]\s*([0-9. ][0-9. ]*)", line, re.IGNORECASE)
        if mrx:
            try:
                rx = float(mrx.group(1).replace(' ', ''))
            except ValueError:
                pass
        mtx = re.search(r"tx-bits-per-second\s*[:=]\s*([0-9. ][0-9. ]*)", line, re.IGNORECASE)
        if mtx:
            try:
                tx = float(mtx.group(1).replace(' ', ''))
            except ValueError:
                pass
        if rx is None:
            mrx2 = re.search(r"rx-rate\s*[:=]\s*([0-9. ][0-9. ]*)\s*bps", line, re.IGNORECASE)
            if mrx2:
                try:
                    rx = float(mrx2.group(1).replace(' ', ''))
                except ValueError:
                    pass
        if tx is None:
            mtx2 = re.search(r"tx-rate\s*[:=]\s*([0-9. ][0-9. ]*)\s*bps", line, re.IGNORECASE)
            if mtx2:
                try:
                    tx = float(mtx2.group(1).replace(' ', ''))
                except ValueError:
                    pass
    # If not available or zero, try computing from byte counters over ~1s
    if (rx is None and tx is None) or ((rx or 0.0) == 0.0 and (tx or 0.0) == 0.0):
        try:
            client = _open_ssh_client(device)
            # First sample
            cmd_rx1 = f"/interface get [find name=\"{interface_name}\"] rx-byte"
            cmd_tx1 = f"/interface get [find name=\"{interface_name}\"] tx-byte"
            print(f"[BW] bytes first sample: {cmd_rx1} | {cmd_tx1}")
            _, out1, _ = client.exec_command(cmd_rx1)
            _, out2, _ = client.exec_command(cmd_tx1)
            s1 = out1.read().decode(errors='ignore')
            s2 = out2.read().decode(errors='ignore')
            print(f"[BW] bytes first output: rx={s1.strip()} tx={s2.strip()}")
            m1 = re.search(r"([0-9]+)", s1 or '')
            m2 = re.search(r"([0-9]+)", s2 or '')
            if not (m1 and m2):
                client.close()
                return {"rx_bps": rx or 0.0, "tx_bps": tx or 0.0} if (rx is not None or tx is not None) else None
            rx1 = int(m1.group(1)); tx1 = int(m2.group(1))
            time.sleep(1.0)
            # Second sample
            _, out3, _ = client.exec_command(cmd_rx1)
            _, out4, _ = client.exec_command(cmd_tx1)
            s3 = out3.read().decode(errors='ignore')
            s4 = out4.read().decode(errors='ignore')
            print(f"[BW] bytes second output: rx={s3.strip()} tx={s4.strip()}")
            client.close()
            m3 = re.search(r"([0-9]+)", s3 or '')
            m4 = re.search(r"([0-9]+)", s4 or '')
            if m3 and m4:
                rx2 = int(m3.group(1)); tx2 = int(m4.group(1))
                rx = max(0.0, float(rx2 - rx1)) * 8.0  # bytes->bits per ~1s
                tx = max(0.0, float(tx2 - tx1)) * 8.0
        except Exception:
            pass
    if rx is None and tx is None:
        return None
    return {"rx_bps": rx or 0.0, "tx_bps": tx or 0.0}


def read_linux_interface_bytes(device, interface_name: str) -> Optional[Tuple[int, int]]:
    """Read rx/tx byte counters for a Linux interface."""
    try:
        client = _open_ssh_client(device)
        cmd = (
            f"cat /sys/class/net/{interface_name}/statistics/rx_bytes;"
            f"echo -n ' ';"
            f"cat /sys/class/net/{interface_name}/statistics/tx_bytes"
        )
        _, out, _ = client.exec_command(cmd)
        data = out.read().decode(errors='ignore').strip()
        client.close()
        parts = data.split()
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
        return None
    except Exception:
        return None


def read_routeros_interface_bytes(device, interface_name: str) -> Optional[Tuple[int, int]]:
    """Read rx/tx byte counters for a RouterOS interface.

    Returns a tuple (rx_bytes, tx_bytes) or None on failure.
    """
    try:
        client = _open_ssh_client(device)
        # Try ethernet monitor first, then print stats - these give actual counters
        cmds = [
            f"/interface ethernet monitor {interface_name} once",
            f"/interface monitor {interface_name} once",
            f"/interface print where name=\"{interface_name}\" stats detail",
            f"/interface print where name=\"{interface_name}\" stats",
        ]
        rx_val = None
        tx_val = None
        for cmd in cmds:
            try:
                print(f"[BW] counter cmd: {cmd}")
                _, out, _ = client.exec_command(cmd)
                data = out.read().decode(errors='ignore')
                print(f"[BW] counter output: {data[:400]}")
                # Parse from ethernet monitor format
                rx_match = re.search(r"rx-byte\s*:\s*([0-9]+)", data, re.MULTILINE)
                if rx_match:
                    rx_val = int(rx_match.group(1))
                tx_match = re.search(r"tx-byte\s*:\s*([0-9]+)", data, re.MULTILINE)
                if tx_match:
                    tx_val = int(tx_match.group(1))
                # Also try rx-bytes/tx-bytes
                if rx_val is None:
                    rx_match = re.search(r"rx-bytes\s*:\s*([0-9]+)", data, re.MULTILINE)
                    if rx_match:
                        rx_val = int(rx_match.group(1))
                if tx_val is None:
                    tx_match = re.search(r"tx-bytes\s*:\s*([0-9]+)", data, re.MULTILINE)
                    if tx_match:
                        tx_val = int(tx_match.group(1))
                if rx_val is not None and tx_val is not None:
                    break
            except Exception:
                continue
        client.close()
        if rx_val is not None and tx_val is not None:
            print(f"[BW] ssh bytes parsed: rx={rx_val} tx={tx_val}")
            return rx_val, tx_val
        print("[BW] ssh bytes unavailable")
        return None
    except Exception:
        return None


