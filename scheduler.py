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
from telegram_utils import send_company_telegram_message, should_send_company_alert, get_company_ping_threshold
from snmp_utils import get_interface_status_and_power
from models import FiberCheck, FiberSample
from models import CompanyTelegramSetting


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
            if alerts and device.company_id:
                send_company_telegram_message(
                    device.company_id,
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
            if not device or not device.enabled or not device.company_id:
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

                # High ping alert (if enabled for company)
                if rtt is not None and should_send_company_alert(device.company_id, 'high_ping'):
                    threshold = get_company_ping_threshold(device.company_id)
                    if rtt > threshold:
                        send_company_telegram_message(
                            device.company_id,
                            text=(
                                f"<b>⚠️ High Ping Alert</b>\n"
                                f"Device: {device.name}\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"RTT: {rtt:.1f} ms (threshold: {threshold} ms)\n"
                                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                            )
                        )

                # Failure tracking and alerting
                if rtt is None:
                    # start outage timer if first failure
                    if not check.down_start_at:
                        check.down_start_at = datetime.now(timezone.utc)
                    check.consecutive_failures = (check.consecutive_failures or 0) + 1

                    # Send down alert if configured and not already alerted
                    if check.consecutive_failures >= 5 and not check.alerted and should_send_company_alert(device.company_id, 'ping_down'):
                        started = check.down_start_at.strftime('%Y-%m-%d %H:%M:%S UTC') if check.down_start_at else 'now'
                        send_company_telegram_message(
                            device.company_id,
                            text=(
                                f"<b>🔴 Ping DOWN</b>\n"
                                f"Device: {device.name}\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"Down since: {started}\n"
                                f"Consecutive timeouts: {check.consecutive_failures}"
                            )
                        )
                        check.alerted = True
                else:
                    # Send restore alert if previously down
                    if check.alerted or check.consecutive_failures >= 5:
                        started = check.down_start_at
                        duration = ''
                        if started:
                            delta = datetime.now(timezone.utc) - started
                            mins = int(delta.total_seconds() // 60)
                            secs = int(delta.total_seconds() % 60)
                            duration = f" (duration {mins}m {secs}s)"

                        send_company_telegram_message(
                            device.company_id,
                            text=(
                                f"<b>🟢 Ping RESTORED</b>\n"
                                f"Device: {device.name}\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"RTT: {rtt:.1f} ms{duration}"
                            )
                        )

                    check.consecutive_failures = 0
                    check.alerted = False
                    check.down_start_at = None

                # Handle continuous down alerts (send reminder every 30 minutes if still down)
                if rtt is None and check.alerted and check.down_start_at and should_send_company_alert(device.company_id, 'ping_down'):
                    # Check if it's been 30 minutes since last alert (using last_checked_at)
                    time_since_alert = datetime.now(timezone.utc) - check.last_checked_at
                    if time_since_alert.total_seconds() >= 1800:  # 30 minutes
                        send_company_telegram_message(
                            device.company_id,
                            text=(
                                f"<b>🔴 Ping STILL DOWN</b>\n"
                                f"Device: {device.name}\n"
                                f"Monitor: {check.name}\n"
                                f"Target: {check.target_ip}\n"
                                f"Down since: {check.down_start_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                                f"Consecutive timeouts: {check.consecutive_failures}\n"
                                f"Duration: {int((datetime.now(timezone.utc) - check.down_start_at).total_seconds() // 60)} minutes"
                            )
                        )

                db.session.commit()
            except Exception as exc:
                print(f"Error in ping job for check {check.id}: {exc}")
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
        # Group report lines per company
        company_to_lines = {}
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

            # Get latest fiber sample per device across its checks
            fiber_checks = FiberCheck.query.filter_by(device_id=d.id).all()
            fiber_lines = []
            for fc in fiber_checks:
                rx = f"{fc.last_rx_dbm:.2f} dBm" if fc.last_rx_dbm is not None else "-"
                tx = f"{fc.last_tx_dbm:.2f} dBm" if fc.last_tx_dbm is not None else "-"
                fiber_lines.append(f"Fiber {fc.name} ({fc.interface_name}): RX {rx}, TX {tx}")
            fiber_str = ("\n" + "\n".join(fiber_lines)) if fiber_lines else ""

            lines = company_to_lines.setdefault(d.company_id, ["<b>Performance Report</b>"])
            lines.append(
                f"\n<b>Device:</b> {d.name}\nCPU: {cpu} | RAM used: {mem_str} | Storage used: {st_str} | Latency: {rtt}{fiber_str}"
            )

        # Send one message per company, respecting report interval
        now = datetime.now(timezone.utc)
        for company_id, lines in company_to_lines.items():
            if not company_id:
                continue
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            if not settings or not settings.enabled:
                continue
            interval = settings.report_interval_minutes or 60
            if settings.last_report_sent_at is None or (now - settings.last_report_sent_at).total_seconds() >= interval * 60:
                ok = send_company_telegram_message(company_id, "\n".join(lines))
                settings.last_report_sent_at = now if ok else settings.last_report_sent_at
        db.session.commit()


def _scheduled_fiber_job() -> None:
    global _flask_app
    app = _flask_app
    if app is None:
        return
    with app.app_context():
        checks = FiberCheck.query.all()
        for c in checks:
            d = Device.query.get(c.device_id)
            if not d or not d.enabled or not d.company_id or d.snmp_version != 'v2c' or not d.snmp_community:
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
                prev_oper_status = c.last_oper_status
                c.last_rx_dbm = None if not res else res.get('rx_power_dbm')
                c.last_tx_dbm = None if not res else res.get('tx_power_dbm')
                c.last_oper_status = None if not res else res.get('if_oper_status')
                c.last_checked_at = datetime.now(timezone.utc)

                # Fiber down alert (if status changed from up to down)
                if prev_oper_status == 1 and c.last_oper_status != 1 and should_send_company_alert(d.company_id, 'fiber_down'):
                    # Set down start time if not already set
                    if not c.down_start_at:
                        c.down_start_at = datetime.now(timezone.utc)

                    send_company_telegram_message(
                        d.company_id,
                        text=(
                            f"<b>🔴 Fiber DOWN</b>\n"
                            f"Device: {d.name}\n"
                            f"Monitor: {c.name}\n"
                            f"Interface: {c.interface_name}\n"
                            f"Status: DOWN\n"
                            f"Down since: {c.down_start_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                    )
                    c.alerted_down = True

                # Fiber restored alert (if status changed from down to up)
                elif prev_oper_status != 1 and c.last_oper_status == 1 and should_send_company_alert(d.company_id, 'fiber_down'):
                    rx_power = f"{c.last_rx_dbm:.2f} dBm" if c.last_rx_dbm is not None else "Unknown"
                    tx_power = f"{c.last_tx_dbm:.2f} dBm" if c.last_tx_dbm is not None else "Unknown"

                    # Calculate outage duration
                    duration_info = ""
                    if c.down_start_at:
                        delta = datetime.now(timezone.utc) - c.down_start_at
                        mins = int(delta.total_seconds() // 60)
                        secs = int(delta.total_seconds() % 60)
                        if mins > 0 or secs > 0:
                            duration_info = f" (outage duration: {mins}m {secs}s)"

                    send_company_telegram_message(
                        d.company_id,
                        text=(
                            f"<b>🟢 Fiber RESTORED</b>\n"
                            f"Device: {d.name}\n"
                            f"Monitor: {c.name}\n"
                            f"Interface: {c.interface_name}\n"
                            f"Status: UP\n"
                            f"Rx Power: {rx_power}\n"
                            f"Tx Power: {tx_power}{duration_info}\n"
                            f"Restored at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                    )

                    # Reset alert tracking
                    c.alerted_down = False
                    c.down_start_at = None

                # Handle continuous down alerts (send reminder every 30 minutes if still down)
                elif c.last_oper_status != 1 and c.alerted_down and c.down_start_at and should_send_company_alert(d.company_id, 'fiber_down'):
                    # Check if it's been 30 minutes since last alert
                    if c.last_checked_at:
                        time_since_alert = datetime.now(timezone.utc) - c.last_checked_at
                        if time_since_alert.total_seconds() >= 1800:  # 30 minutes
                            send_company_telegram_message(
                                d.company_id,
                                text=(
                                    f"<b>🔴 Fiber STILL DOWN</b>\n"
                                    f"Device: {d.name}\n"
                                    f"Monitor: {c.name}\n"
                                    f"Interface: {c.interface_name}\n"
                                    f"Status: DOWN\n"
                                    f"Down since: {c.down_start_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                                    f"Duration: {int((datetime.now(timezone.utc) - c.down_start_at).total_seconds() // 60)} minutes"
                                )
                            )

                db.session.add(FiberSample(
                    check_id=c.id,
                    timestamp=datetime.now(timezone.utc),
                    rx_dbm=c.last_rx_dbm,
                    tx_dbm=c.last_tx_dbm,
                    oper_status=c.last_oper_status,
                ))
                db.session.commit()

            except Exception as exc:
                print(f"Error in fiber job for check {c.id}: {exc}")
                db.session.rollback()


