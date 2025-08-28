from datetime import datetime, timezone
from sqlalchemy import desc
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
import pytz
import os

from models import db, Device, ResourceMetric
from models import PingCheck
from models import PingSample
from netmiko_utils import backup_device_and_download, fetch_device_resources
from netmiko_utils import run_ping_on_router
from telegram_utils import send_telegram_message
from snmp_utils import get_interface_status_and_power
from models import FiberCheck, FiberSample


scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Yangon'))
_flask_app = None


def _job_id(device_id: int) -> str:
    return f"backup_device_{device_id}"


def add_or_update_backup_job(device: Device) -> None:
    job_id = _job_id(device.id)

    # Remove existing job if any
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    if not device.enabled or device.schedule == 'manual':
        return

    # Schedule based on device.schedule
    if device.schedule == 'daily':
        trigger = CronTrigger(hour=2, minute=0)  # 02:00 daily
    elif device.schedule == 'weekly':
        trigger = CronTrigger(day_of_week='sun', hour=3, minute=0)  # Sunday 03:00
    elif device.schedule == 'monthly':
        trigger = CronTrigger(day=1, hour=4, minute=0)  # 1st of month 04:00
    else:
        return

    scheduler.add_job(
        func=_scheduled_backup_job,
        trigger=trigger,
        id=job_id,
        args=[device.id],
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )


def remove_backup_job(device_id: int) -> None:
    job_id = _job_id(device_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def _scheduled_backup_job(device_id: int) -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        device = Device.query.get(device_id)
        if not device or not device.enabled:
            return
        success, message = backup_device_and_download(device)
        device.last_backup_status = 'success' if success else 'fail'
        device.last_backup_time = datetime.now(timezone.utc)
        device.last_backup_message = message[:1000] if message else None
        db.session.commit()


def _scheduled_monitoring_job() -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        devices = Device.query.filter_by(enabled=True).all()
        for device in devices:
            metrics = fetch_device_resources(device)
            if metrics is None:
                continue
            metric = ResourceMetric(
                device_id=device.id,
                cpu_load_percent=metrics.get('cpu_load_percent'),
                total_memory_bytes=metrics.get('total_memory_bytes'),
                free_memory_bytes=metrics.get('free_memory_bytes'),
                total_storage_bytes=metrics.get('total_storage_bytes'),
                free_storage_bytes=metrics.get('free_storage_bytes'),
            )
            db.session.add(metric)

            # Immediate threshold alerts (80% default)
            threshold = float(os.environ.get('THRESHOLD_PERCENT', '80'))
            alerts = []
            if metric.cpu_load_percent is not None and metric.cpu_load_percent >= threshold:
                alerts.append('CPU')
                device.last_cpu_alert_at = datetime.now(timezone.utc)
            if (
                metric.total_memory_bytes and metric.free_memory_bytes is not None
                and (metric.total_memory_bytes - metric.free_memory_bytes) / metric.total_memory_bytes * 100.0 >= threshold
            ):
                alerts.append('RAM')
                device.last_ram_alert_at = datetime.now(timezone.utc)
            if (
                metric.total_storage_bytes and metric.free_storage_bytes is not None
                and (metric.total_storage_bytes - metric.free_storage_bytes) / metric.total_storage_bytes * 100.0 >= threshold
            ):
                alerts.append('Storage')
                device.last_storage_alert_at = datetime.now(timezone.utc)
            if alerts:
                send_telegram_message(
                    text=(
                        f"<b>Resource Alert</b>\n"
                        f"Device: {device.name} ({device.host})\n"
                        f"Exceeded: {', '.join(alerts)} >= {threshold}%"
                    )
                )
        db.session.commit()


def _scheduled_ping_job() -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        checks = PingCheck.query.all()
        for check in checks:
            device = Device.query.get(check.device_id)
            if not device or not device.enabled:
                continue
            rtt = run_ping_on_router(
                device,
                target_ip=check.target_ip,
                source_ip=check.source_ip,
                source_interface=check.source_interface,
            )
            # Write in a separate transaction per check to avoid database locked
            try:
                check.last_rtt_ms = rtt if rtt is not None else None
                check.last_checked_at = datetime.now(timezone.utc)
                sample = PingSample(check_id=check.id, timestamp=datetime.now(timezone.utc), rtt_ms=rtt)
                db.session.add(sample)
                # Failure tracking and alerting
                if rtt is None:
                    # start outage timer if first failure
                    if not check.down_start_at:
                        check.down_start_at = datetime.now(timezone.utc)
                    check.consecutive_failures = (check.consecutive_failures or 0) + 1
                    if check.consecutive_failures >= 5 and not check.alerted:
                        started = check.down_start_at.strftime('%Y-%m-%d %H:%M:%S UTC') if check.down_start_at else 'now'
                        sent = send_telegram_message(
                            text=(
                                f"<b>Ping DOWN</b>\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"Router Check ID: {check.device_id}\n"
                                f"Down since: {started}\n"
                                f"Consecutive timeouts: {check.consecutive_failures}"
                            )
                        )
                        if sent:
                            check.alerted = True
                else:
                    # restore
                    if check.alerted or check.consecutive_failures >= 5:
                        started = check.down_start_at
                        duration = ''
                        if started:
                            delta = datetime.now(timezone.utc) - started
                            mins = int(delta.total_seconds() // 60)
                            secs = int(delta.total_seconds() % 60)
                            duration = f" (duration {mins}m {secs}s)"
                        send_telegram_message(
                            text=(
                                f"<b>Ping RESTORED</b>\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"Router Check ID: {check.device_id}\n"
                                f"RTT: {rtt:.1f} ms{duration}"
                            )
                        )
                    check.consecutive_failures = 0
                    check.alerted = False
                    check.down_start_at = None
                db.session.commit()
            except Exception:
                db.session.rollback()


def start_monitoring_job(app) -> None:
    global _flask_app
    _flask_app = app
    # 5-minute interval monitoring
    if not scheduler.running:
        scheduler.start(paused=True)
    scheduler.add_job(
        func=_scheduled_monitoring_job,
        trigger=CronTrigger(minute='*/5'),
        id='monitor_resources',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # 1-second ping checks
    scheduler.add_job(
        func=_scheduled_ping_job,
        trigger=IntervalTrigger(seconds=1),
        id='ping_checks_1s',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1,
    )
    scheduler.add_job(
        func=_scheduled_fiber_job,
        trigger=IntervalTrigger(minutes=1),
        id='fiber_checks_1m',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=10,
    )
    # Hourly performance report at minute 0 (configurable via HOURLY_REPORT_MINUTE)
    try:
        minute = int(os.environ.get('HOURLY_REPORT_MINUTE', '0'))
    except ValueError:
        minute = 0
    scheduler.add_job(
        func=_scheduled_hourly_report,
        trigger=CronTrigger(minute=minute),
        id='hourly_performance_report',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.resume()


def _scheduled_hourly_report() -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        devices = Device.query.order_by(Device.name.asc()).all()
        lines = ["<b>Hourly Performance Report</b>"]
        for d in devices:
            latest = (
                ResourceMetric.query.filter_by(device_id=d.id)
                .order_by(desc(ResourceMetric.timestamp))
                .first()
            )
            cpu = f"{latest.cpu_load_percent:.0f}%" if latest and latest.cpu_load_percent is not None else "-"
            if latest and latest.total_memory_bytes and latest.free_memory_bytes is not None:
                used_mem = latest.total_memory_bytes - latest.free_memory_bytes
                mem_pct = (used_mem / latest.total_memory_bytes) * 100.0
                mem_str = f"{mem_pct:.0f}%"
            else:
                mem_str = "-"
            if latest and latest.total_storage_bytes and latest.free_storage_bytes is not None:
                used_st = latest.total_storage_bytes - latest.free_storage_bytes
                st_pct = (used_st / latest.total_storage_bytes) * 100.0
                st_str = f"{st_pct:.0f}%"
            else:
                st_str = "-"

            check = PingCheck.query.filter_by(device_id=d.id).first()
            rtt = f"{check.last_rtt_ms:.1f} ms" if check and check.last_rtt_ms is not None else "-"
            lines.append(f"\n<b>{d.name}</b> ({d.host})\nCPU: {cpu} | RAM used: {mem_str} | Storage used: {st_str} | RTT: {rtt}")

        send_telegram_message("\n".join(lines))


def _scheduled_fiber_job() -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        checks = FiberCheck.query.all()
        for c in checks:
            d = Device.query.get(c.device_id)
            if not d or not d.enabled or d.snmp_version != 'v2c' or not d.snmp_community:
                continue
            try:
                res = get_interface_status_and_power(
                    d.host,
                    d.snmp_community,
                    c.interface_name,
                    d.username,
                    d.password,
                    d.port or 22,
                )
            except Exception:
                res = None
            try:
                c.last_rx_dbm = None if not res else res.get('rx_power_dbm')
                c.last_tx_dbm = None if not res else res.get('tx_power_dbm')
                c.last_oper_status = None if not res else res.get('if_oper_status')
                c.last_checked_at = datetime.now(timezone.utc)
                db.session.add(FiberSample(
                    check_id=c.id,
                    timestamp=datetime.now(timezone.utc),
                    rx_dbm=c.last_rx_dbm,
                    tx_dbm=c.last_tx_dbm,
                    oper_status=c.last_oper_status,
                ))
                db.session.commit()
            except Exception:
                db.session.rollback()


