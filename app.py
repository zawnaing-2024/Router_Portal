import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from flask import Flask, render_template, request, redirect, url_for, flash
from flask import send_from_directory
from flask import jsonify
from flask_sqlalchemy import SQLAlchemy

from models import db, Device, ResourceMetric
from models import PingCheck
from models import PingSample
from models import FiberCheck, FiberSample
from models import AppSetting
from scheduler import scheduler, add_or_update_backup_job, remove_backup_job, start_monitoring_job
from netmiko_utils import perform_manual_backup
from telegram_utils import send_telegram_message, send_telegram_message_with_details
import requests
from snmp_utils import get_interface_status_and_power


def _run_sqlite_migrations() -> None:
    """Best-effort lightweight migrations for SQLite.

    Adds missing columns to existing tables without Alembic.
    """
    engine = db.engine
    try:
        with engine.begin() as conn:
            # Ensure ping_checks has new alerting columns
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(ping_checks)")
                columns = {row[1] for row in result}
                if 'consecutive_failures' not in columns:
                    conn.exec_driver_sql(
                        "ALTER TABLE ping_checks ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
                    )
                if 'alerted' not in columns:
                    conn.exec_driver_sql(
                        "ALTER TABLE ping_checks ADD COLUMN alerted BOOLEAN NOT NULL DEFAULT 0"
                    )
                if 'down_start_at' not in columns:
                    conn.exec_driver_sql(
                        "ALTER TABLE ping_checks ADD COLUMN down_start_at DATETIME"
                    )
            except Exception:
                pass
            # Ensure devices has alert timestamp columns
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(devices)")
                dcols = {row[1] for row in result}
                if 'last_cpu_alert_at' not in dcols:
                    conn.exec_driver_sql(
                        "ALTER TABLE devices ADD COLUMN last_cpu_alert_at DATETIME"
                    )
                if 'last_ram_alert_at' not in dcols:
                    conn.exec_driver_sql(
                        "ALTER TABLE devices ADD COLUMN last_ram_alert_at DATETIME"
                    )
                if 'last_storage_alert_at' not in dcols:
                    conn.exec_driver_sql(
                        "ALTER TABLE devices ADD COLUMN last_storage_alert_at DATETIME"
                    )
            except Exception:
                pass
            # Ensure ping_samples table exists (created by create_all normally)
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS ping_samples (\n"
                "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "    check_id INTEGER NOT NULL,\n"
                "    timestamp DATETIME NOT NULL,\n"
                "    rtt_ms FLOAT,\n"
                "    FOREIGN KEY(check_id) REFERENCES ping_checks(id)\n"
                ")"
            )
            # Ensure devices has SNMP columns
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(devices)")
                dcols = {row[1] for row in result}
                if 'snmp_version' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN snmp_version VARCHAR(8) DEFAULT 'v2c' NOT NULL")
                if 'snmp_community' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN snmp_community VARCHAR(128)")
            except Exception:
                pass
            # Ensure fiber tables
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS fiber_checks (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " name VARCHAR(128) NOT NULL,\n"
                " device_id INTEGER NOT NULL,\n"
                " interface_name VARCHAR(64) NOT NULL,\n"
                " last_rx_dbm FLOAT,\n"
                " last_tx_dbm FLOAT,\n"
                " last_oper_status INTEGER,\n"
                " last_checked_at DATETIME\n"
                ")"
            )
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS fiber_samples (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " check_id INTEGER NOT NULL,\n"
                " timestamp DATETIME NOT NULL,\n"
                " rx_dbm FLOAT,\n"
                " tx_dbm FLOAT,\n"
                " oper_status INTEGER\n"
                ")"
            )
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS app_settings (\n"
                " key VARCHAR(64) PRIMARY KEY,\n"
                " value TEXT\n"
                ")"
            )
    except Exception:
        # Do not block app start if migration fails
        pass


load_dotenv()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///network_tools.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'change-me')
    # Improve SQLite concurrency
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {
            'check_same_thread': False,
            'timeout': 10,
        }
    }

    db.init_app(app)

    # Ensure backups directory exists
    os.makedirs(os.path.join(app.root_path, 'backups'), exist_ok=True)

    with app.app_context():
        db.create_all()
        _run_sqlite_migrations()
        # Set SQLite to WAL mode and reasonable sync and busy timeout
        try:
            eng = db.engine
            with eng.begin() as conn:
                conn.exec_driver_sql('PRAGMA journal_mode=WAL')
                conn.exec_driver_sql('PRAGMA synchronous=NORMAL')
                conn.exec_driver_sql('PRAGMA busy_timeout=5000')
        except Exception:
            pass

    # Start scheduler and jobs
    start_monitoring_job(app)

    # Create/update backup jobs for all existing devices on startup
    with app.app_context():
        for device in Device.query.all():
            add_or_update_backup_job(device)

    @app.template_filter('yangon_time')
    def yangon_time(dt):
        import pytz
        if dt is None:
            return '-'
        # Treat naive as UTC, then convert to Yangon
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        yangon = pytz.timezone('Asia/Yangon')
        return dt.astimezone(yangon).strftime('%Y-%m-%d %H:%M:%S')

    @app.route('/')
    def dashboard():
        devices = Device.query.order_by(Device.name.asc()).all()
        latest_metrics_by_device = {}
        for device in devices:
            latest = (
                ResourceMetric.query
                .filter_by(device_id=device.id)
                .order_by(ResourceMetric.timestamp.desc())
                .first()
            )
            # Fallback: if no stored metrics, try live fetch once
            if latest is None:
                from netmiko_utils import fetch_device_resources
                metrics = fetch_device_resources(device)
                if metrics:
                    latest = ResourceMetric(
                        device_id=device.id,
                        cpu_load_percent=metrics.get('cpu_load_percent'),
                        total_memory_bytes=metrics.get('total_memory_bytes'),
                        free_memory_bytes=metrics.get('free_memory_bytes'),
                        total_storage_bytes=metrics.get('total_storage_bytes'),
                        free_storage_bytes=metrics.get('free_storage_bytes'),
                        timestamp=datetime.now(timezone.utc),
                    )
                    db.session.add(latest)
                    db.session.commit()
            latest_metrics_by_device[device.id] = latest

        # Module last update time: latest ResourceMetric timestamp across all devices
        last_metrics_update = (
            db.session.query(db.func.max(ResourceMetric.timestamp)).scalar()
        )
        return render_template(
            'dashboard.html',
            devices=devices,
            latest_metrics_by_device=latest_metrics_by_device,
            last_metrics_update=last_metrics_update,
        )

    @app.route('/devices', methods=['GET', 'POST'])
    def devices_page():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            host = request.form.get('host', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            port = request.form.get('port', '22').strip()
            schedule = request.form.get('schedule', 'manual')

            if not name or not host or not username or not password:
                flash('All fields except port are required.', 'danger')
                return redirect(url_for('devices_page'))

            try:
                port_int = int(port)
            except ValueError:
                flash('Port must be a number.', 'danger')
                return redirect(url_for('devices_page'))

            device = Device(
                name=name,
                host=host,
                username=username,
                password=password,
                port=port_int,
                schedule=schedule,
                enabled=True,
                snmp_version=request.form.get('snmp_version','v2c'),
                snmp_community=request.form.get('snmp_community') or None,
            )
            db.session.add(device)
            db.session.commit()

            add_or_update_backup_job(device)

            flash('Device added.', 'success')
            return redirect(url_for('devices_page'))

        devices = Device.query.order_by(Device.name.asc()).all()
        return render_template('devices.html', devices=devices)

    @app.route('/devices/delete/<int:device_id>', methods=['POST'])
    def delete_device(device_id: int):
        device = Device.query.get_or_404(device_id)
        remove_backup_job(device_id)
        ResourceMetric.query.filter_by(device_id=device_id).delete()
        db.session.delete(device)
        db.session.commit()
        flash('Device deleted.', 'success')
        return redirect(url_for('devices_page'))

    @app.route('/backup', methods=['GET'])
    def backup_page():
        devices = Device.query.order_by(Device.name.asc()).all()
        return render_template('backup.html', devices=devices)

    @app.route('/backup/manual/<int:device_id>', methods=['POST'])
    def manual_backup(device_id: int):
        device = Device.query.get_or_404(device_id)
        success, message = perform_manual_backup(device)
        device.last_backup_status = 'success' if success else 'fail'
        device.last_backup_time = datetime.now(timezone.utc)
        device.last_backup_message = message[:1000] if message else None
        db.session.commit()
        flash(f'Manual backup for {device.name}: {"success" if success else "failed"}.', 'success' if success else 'danger')
        return redirect(url_for('backup_page'))

    @app.route('/backup/schedule/<int:device_id>', methods=['POST'])
    def update_schedule(device_id: int):
        device = Device.query.get_or_404(device_id)
        schedule = request.form.get('schedule', 'manual')
        if schedule not in {'manual', 'daily', 'weekly', 'monthly'}:
            flash('Invalid schedule.', 'danger')
            return redirect(url_for('backup_page'))
        device.schedule = schedule
        db.session.commit()
        add_or_update_backup_job(device)
        flash('Schedule updated.', 'success')
        return redirect(url_for('backup_page'))

    @app.route('/backup/files')
    def backup_files():
        # List backups grouped by date folder
        backups_root = os.path.join(app.root_path, 'backups')
        date_folders = []
        if os.path.isdir(backups_root):
            for entry in sorted(os.listdir(backups_root)):
                full = os.path.join(backups_root, entry)
                if os.path.isdir(full):
                    files = sorted([f for f in os.listdir(full) if f.endswith('.backup')])
                    date_folders.append({'date': entry, 'files': files})
        return render_template('backup_files.html', date_folders=date_folders)

    @app.route('/backup/download/<date>/<filename>')
    def download_backup(date: str, filename: str):
        backups_root = os.path.join(app.root_path, 'backups')
        directory = os.path.join(backups_root, date)
        return send_from_directory(directory=directory, path=filename, as_attachment=True)

    @app.route('/backup/delete/<date>/<filename>', methods=['POST'])
    def delete_backup_file(date: str, filename: str):
        backups_root = os.path.join(app.root_path, 'backups')
        directory = os.path.join(backups_root, date)
        target = os.path.join(directory, filename)
        if os.path.isfile(target):
            try:
                os.remove(target)
                flash('Backup file deleted.', 'success')
            except Exception as exc:  # noqa: BLE001
                flash(f'Failed to delete: {exc}', 'danger')
        else:
            flash('File not found.', 'warning')
        return redirect(url_for('backup_files'))

    @app.route('/pings', methods=['GET', 'POST'])
    def pings_page():
        devices = Device.query.order_by(Device.name.asc()).all()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            device_id = request.form.get('device_id')
            target_ip = request.form.get('target_ip', '').strip()
            source_ip = request.form.get('source_ip', '').strip() or None
            source_interface = request.form.get('source_interface', '').strip() or None

            if not name or not device_id or not target_ip:
                flash('Name, device and target IP are required.', 'danger')
                return redirect(url_for('pings_page'))
            try:
                device_id_int = int(device_id)
            except ValueError:
                flash('Invalid device selection.', 'danger')
                return redirect(url_for('pings_page'))

            check = PingCheck(
                name=name,
                device_id=device_id_int,
                target_ip=target_ip,
                source_ip=source_ip,
                source_interface=source_interface,
            )
            db.session.add(check)
            db.session.commit()
            flash('Ping monitor added.', 'success')
            return redirect(url_for('pings_page'))

        checks = PingCheck.query.all()
        # Build rows with device name
        device_by_id = {d.id: d for d in devices}
        rows = []
        for c in checks:
            rows.append({
                'id': c.id,
                'name': c.name,
                'device_name': device_by_id.get(c.device_id).name if device_by_id.get(c.device_id) else '-',
                'target_ip': c.target_ip,
                'source': c.source_ip or c.source_interface or '-',
                'last_rtt_ms': c.last_rtt_ms,
                'last_checked_at': c.last_checked_at,
            })
        return render_template('pings.html', devices=devices, rows=rows)

    @app.route('/pings/delete/<int:check_id>', methods=['POST'])
    def delete_ping(check_id: int):
        check = PingCheck.query.get_or_404(check_id)
        db.session.delete(check)
        db.session.commit()
        flash('Ping monitor deleted.', 'success')
        return redirect(url_for('pings_page'))

    @app.route('/pings/notify/<int:check_id>', methods=['POST'])
    def notify_ping(check_id: int):
        check = PingCheck.query.get_or_404(check_id)
        device = Device.query.get(check.device_id)
        status = f"{check.last_rtt_ms:.1f} ms" if check.last_rtt_ms is not None else 'Timeout'
        sent, info = send_telegram_message_with_details(
            text=(
                f"<b>Ping Status</b>\n"
                f"Monitor: {check.name}\n"
                f"Target: {check.target_ip}\n"
                f"Router: {device.name if device else check.device_id}\n"
                f"Result: {status}"
            )
        )
        flash('Sent to Telegram.' if sent else f'Failed to send Telegram message: {info}', 'success' if sent else 'danger')
        return redirect(url_for('pings_page'))

    @app.route('/api/pings')
    def api_pings():
        # Read-only endpoint; scheduler updates values.
        checks = PingCheck.query.all()
        def to_display_time(dt):
            # Reuse same logic as yangon_time filter
            import pytz
            if dt is None:
                return '-'
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            yangon = pytz.timezone('Asia/Yangon')
            return dt.astimezone(yangon).strftime('%Y-%m-%d %H:%M:%S')
        data = [
            {
                'id': c.id,
                'last_rtt_ms': c.last_rtt_ms,
                'last_checked_at': c.last_checked_at.isoformat() if c.last_checked_at else None,
                'last_checked_at_display': to_display_time(c.last_checked_at),
            }
            for c in checks
        ]
        return jsonify(data)

    @app.route('/api/pings/samples')
    def api_ping_samples():
        try:
            check_id = int(request.args.get('check_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'check_id required'}), 400
        # Filters: since (seconds), limit N points
        since_seconds = request.args.get('since_seconds')
        limit = request.args.get('limit')
        q = PingSample.query.filter_by(check_id=check_id).order_by(PingSample.timestamp.desc())
        if since_seconds:
            try:
                seconds = int(since_seconds)
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
                q = q.filter(PingSample.timestamp >= cutoff)
            except ValueError:
                pass
        if limit:
            try:
                n = int(limit)
                q = q.limit(n)
            except ValueError:
                pass
        samples = list(reversed(q.all()))
        return jsonify([
            {
                'ts': s.timestamp.isoformat(),
                'rtt_ms': s.rtt_ms,
            }
            for s in samples
        ])

    def _write_env(updates: dict) -> None:
        env_path = os.path.join(app.root_path, '.env')
        lines = []
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
        # remove old keys
        keys = set(updates.keys())
        filtered = [ln for ln in lines if not any(ln.startswith(k + '=') for k in keys)]
        for k, v in updates.items():
            filtered.append(f"{k}={v}")
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(filtered))
        # also update current process env
        for k, v in updates.items():
            os.environ[k] = v

    @app.route('/settings/telegram', methods=['GET', 'POST'])
    def telegram_settings():
        def get_setting(k: str) -> str:
            rec = AppSetting.query.get(k)
            return rec.value.strip() if rec and rec.value else ''
        token = get_setting('TELEGRAM_BOT_TOKEN') or (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
        chat_id = get_setting('TELEGRAM_CHAT_ID') or (os.environ.get('TELEGRAM_CHAT_ID') or '').strip()
        group_name = get_setting('TELEGRAM_GROUP_NAME') or (os.environ.get('TELEGRAM_GROUP_NAME') or '').strip()

        if request.method == 'POST':
            action = request.form.get('action')
            form_token = request.form.get('token', '').strip()
            form_chat = request.form.get('chat_id', '').strip()
            form_group = request.form.get('group_name', '').strip()

            if action == 'save':
                # Save to DB settings (primary)
                for k, v in {
                    'TELEGRAM_BOT_TOKEN': form_token,
                    'TELEGRAM_CHAT_ID': form_chat,
                    'TELEGRAM_GROUP_NAME': form_group,
                }.items():
                    rec = AppSetting.query.get(k)
                    if rec:
                        rec.value = v
                    else:
                        db.session.add(AppSetting(key=k, value=v))
                db.session.commit()
                # Also mirror to env to avoid restart needs
                for k, v in [('TELEGRAM_BOT_TOKEN', form_token), ('TELEGRAM_CHAT_ID', form_chat), ('TELEGRAM_GROUP_NAME', form_group)]:
                    if v:
                        os.environ[k] = v
                flash('Telegram settings saved.', 'success')
                return redirect(url_for('telegram_settings'))

            if action == 'test':
                # Prefer form values if present
                use_token = (form_token or get_setting('TELEGRAM_BOT_TOKEN') or token)
                use_chat = (form_chat or get_setting('TELEGRAM_CHAT_ID') or chat_id)
                if not use_token or not use_chat:
                    flash('Token and Chat ID are required to send test.', 'danger')
                    return redirect(url_for('telegram_settings'))
                os.environ['TELEGRAM_BOT_TOKEN'] = use_token
                os.environ['TELEGRAM_CHAT_ID'] = use_chat
                ok, info = send_telegram_message_with_details('Router Portal test message ✅')
                flash('Test sent.' if ok else f'Test failed: {info}', 'success' if ok else 'danger')
                return redirect(url_for('telegram_settings'))

            if action == 'detect':
                use_token = form_token or token
                if not use_token:
                    flash('Provide bot token to detect chat id.', 'danger')
                    return redirect(url_for('telegram_settings'))
                try:
                    resp = requests.get(
                        f"https://api.telegram.org/bot{use_token}/getUpdates",
                        timeout=8,
                    )
                    data = resp.json()
                    found = None
                    if data.get('ok'):
                        for item in data.get('result', [])[::-1]:
                            chat = (item.get('message') or {}).get('chat') or {}
                            if chat.get('type') in {'group', 'supergroup'}:
                                found = (str(chat.get('id') or ''), chat.get('title') or '')
                                break
                    if found:
                        chat_id = found[0]
                        group_name = found[1]
                        flash(f'Detected chat id {chat_id} for group {group_name}. Click Save to persist.', 'success')
                    else:
                        flash('No group chat found in bot updates. Add bot to group, disable privacy, send a message, then Detect again.', 'warning')
                except Exception as exc:  # noqa: BLE001
                    flash(f'Detect failed: {exc}', 'danger')
            # fallthrough to render with possibly updated locals
            token = form_token or token
        return render_template('telegram_settings.html', token=token, chat_id=chat_id, group_name=group_name)

    @app.route('/messages', methods=['GET', 'POST'])
    def messages_page():
        if request.method == 'POST':
            text = request.form.get('text', '').strip()
            if not text:
                flash('Message cannot be empty.', 'danger')
                return redirect(url_for('messages_page'))
            ok, info = send_telegram_message_with_details(text)
            flash('Message sent.' if ok else f'Failed to send: {info}', 'success' if ok else 'danger')
            return redirect(url_for('messages_page'))
        return render_template('messages.html')

    @app.route('/fibers', methods=['GET', 'POST'])
    def fibers_page():
        devices = Device.query.order_by(Device.name.asc()).all()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            device_id = request.form.get('device_id')
            if_name = request.form.get('if_name', '').strip()
            if not name or not device_id or not if_name:
                flash('Name, device and interface are required.', 'danger')
                return redirect(url_for('fibers_page'))
            try:
                device_id_int = int(device_id)
            except ValueError:
                flash('Invalid device.', 'danger')
                return redirect(url_for('fibers_page'))
            check = FiberCheck(name=name, device_id=device_id_int, interface_name=if_name)
            db.session.add(check)
            db.session.commit()
            flash('Fiber monitor added.', 'success')
            return redirect(url_for('fibers_page'))

        checks = FiberCheck.query.all()
        device_by_id = {d.id: d for d in devices}
        rows = []
        for c in checks:
            d = device_by_id.get(c.device_id)
            rows.append({
                'id': c.id,
                'name': c.name,
                'device_name': d.name if d else '-',
                'if_name': c.interface_name,
                'rx': c.last_rx_dbm,
                'tx': c.last_tx_dbm,
                'oper': c.last_oper_status,
                'ts': c.last_checked_at,
            })
        return render_template('fibers.html', devices=devices, rows=rows)

    @app.route('/api/fibers/samples')
    def api_fiber_samples():
        try:
            check_id = int(request.args.get('check_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'check_id required'}), 400
        since_seconds = request.args.get('since_seconds')
        q = FiberSample.query.filter_by(check_id=check_id).order_by(FiberSample.timestamp.desc())
        if since_seconds:
            try:
                seconds = int(since_seconds)
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
                q = q.filter(FiberSample.timestamp >= cutoff)
            except ValueError:
                pass
        samples = list(reversed(q.all()))
        return jsonify([
            {
                'ts': s.timestamp.isoformat(),
                'rx': s.rx_dbm,
                'tx': s.tx_dbm,
                'oper': s.oper_status,
            }
            for s in samples
        ])

    @app.route('/fibers/probe/<int:check_id>', methods=['POST'])
    def fiber_probe(check_id: int):
        check = FiberCheck.query.get_or_404(check_id)
        device = Device.query.get(check.device_id)
        if not device or not device.enabled:
            flash('Device not available.', 'danger')
            return redirect(url_for('fibers_page'))
        if device.snmp_version != 'v2c' or not device.snmp_community:
            flash('SNMP v2c/community not configured on device.', 'danger')
            return redirect(url_for('fibers_page'))
        try:
            res = get_interface_status_and_power(
                device.host,
                device.snmp_community,
                check.interface_name,
                device.username,
                device.password,
                device.port or 22,
            )
        except Exception as exc:  # noqa: BLE001
            # Continue to render generic message; detailed errors are noisy here
            res = None
        if res:
            check.last_rx_dbm = res.get('rx_power_dbm')
            check.last_tx_dbm = res.get('tx_power_dbm')
            check.last_oper_status = res.get('if_oper_status')
            check.last_checked_at = datetime.now(timezone.utc)
            db.session.add(FiberSample(
                check_id=check.id,
                timestamp=datetime.now(timezone.utc),
                rx_dbm=check.last_rx_dbm,
                tx_dbm=check.last_tx_dbm,
                oper_status=check.last_oper_status,
            ))
            db.session.commit()
            rx = '-' if check.last_rx_dbm is None else f'{check.last_rx_dbm}'
            tx = '-' if check.last_tx_dbm is None else f'{check.last_tx_dbm}'
            stat = check.last_oper_status
            flash(f'Probe OK: if={check.interface_name} oper={stat} rx={rx} tx={tx}', 'success')
        else:
            flash('Probe returned no data. Verify interface name and SNMP access.', 'warning')
        return redirect(url_for('fibers_page'))

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)


