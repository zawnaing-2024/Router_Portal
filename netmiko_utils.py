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
    """Fetch CPU, RAM, and storage metrics from Mikrotik RouterOS.

    Returns a dict or None on failure.
    """
    try:
        client = _open_ssh_client(device)
        stdin, stdout, stderr = client.exec_command("/system resource print without-paging")
        output = stdout.read().decode(errors='ignore')
        _ = stderr.read().decode(errors='ignore')
        client.close()
    except Exception:  # noqa: BLE001
        return None

    # Parse output like:
    # cpu-load: 5%
    # free-memory: 123.4MiB
    # total-memory: 256.0MiB
    # free-hdd-space: 200.0MiB
    # total-hdd-space: 512.0MiB
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


