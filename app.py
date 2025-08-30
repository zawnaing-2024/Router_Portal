import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from flask import Flask, render_template, request, redirect, url_for, flash
from flask import send_from_directory
from flask import jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, Device, ResourceMetric
from models import PingCheck
from models import PingSample
from models import FiberCheck, FiberSample
from models import AppSetting, User, Company, UserCompany, CompanyTelegramSetting
from scheduler import scheduler, add_or_update_backup_job, remove_backup_job, start_monitoring_job
from scheduler import _chunk_text_for_telegram
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
                if 'device_type' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN device_type VARCHAR(16) NOT NULL DEFAULT 'mikrotik'")
                if 'snmp_version' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN snmp_version VARCHAR(8) DEFAULT 'v2c' NOT NULL")
                if 'snmp_community' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN snmp_community VARCHAR(128)")
            except Exception:
                pass
            # Ensure devices has company_id (multi-tenant)
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(devices)")
                dcols = {row[1] for row in result}
                if 'company_id' not in dcols:
                    conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN company_id INTEGER")
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
            # Ensure multi-tenant/auth tables
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS companies (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " name VARCHAR(128) UNIQUE NOT NULL,\n"
                " notes TEXT\n"
                ")"
            )
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS users (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " email VARCHAR(255) UNIQUE NOT NULL,\n"
                " password_hash VARCHAR(255) NOT NULL,\n"
                " is_superadmin BOOLEAN NOT NULL DEFAULT 0\n"
                ")"
            )
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS user_companies (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " user_id INTEGER NOT NULL,\n"
                " company_id INTEGER NOT NULL,\n"
                " role VARCHAR(32) NOT NULL DEFAULT 'viewer'\n"
                ")"
            )
            # Company-specific Telegram settings
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS company_telegram_settings (\n"
                " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                " company_id INTEGER NOT NULL UNIQUE,\n"
                " bot_token VARCHAR(255),\n"
                " chat_id VARCHAR(64),\n"
                " group_name VARCHAR(128),\n"
                " enabled BOOLEAN NOT NULL DEFAULT 1,\n"
                " ping_down_alerts BOOLEAN NOT NULL DEFAULT 1,\n"
                " fiber_down_alerts BOOLEAN NOT NULL DEFAULT 1,\n"
                " high_ping_alerts BOOLEAN NOT NULL DEFAULT 1,\n"
                " high_ping_threshold_ms INTEGER NOT NULL DEFAULT 90,\n"
                " report_interval_minutes INTEGER NOT NULL DEFAULT 60,\n"
                " last_report_sent_at DATETIME,\n"
                " FOREIGN KEY(company_id) REFERENCES companies(id)\n"
                ")"
            )
            # Ensure new columns on existing company_telegram_settings
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(company_telegram_settings)")
                ccols = {row[1] for row in result}
                if 'report_interval_minutes' not in ccols:
                    conn.exec_driver_sql(
                        "ALTER TABLE company_telegram_settings ADD COLUMN report_interval_minutes INTEGER NOT NULL DEFAULT 60"
                    )
                if 'last_report_sent_at' not in ccols:
                    conn.exec_driver_sql(
                        "ALTER TABLE company_telegram_settings ADD COLUMN last_report_sent_at DATETIME"
                    )
            except Exception:
                pass
            # Ensure fiber_checks has alert tracking fields
            try:
                result = conn.exec_driver_sql("PRAGMA table_info(fiber_checks)")
                fcols = {row[1] for row in result}
                if 'alerted_down' not in fcols:
                    conn.exec_driver_sql("ALTER TABLE fiber_checks ADD COLUMN alerted_down BOOLEAN NOT NULL DEFAULT 0")
                if 'down_start_at' not in fcols:
                    conn.exec_driver_sql("ALTER TABLE fiber_checks ADD COLUMN down_start_at DATETIME")
            except Exception:
                pass

            # Ensure ping_checks and fiber_checks have company_id through device relationship
            # (This is handled via joins in queries, no additional columns needed)
    except Exception:
        # Do not block app start if migration fails
        pass


load_dotenv()


def get_user_company_ids(user_id: int) -> list:
    """Get list of company IDs that a user has access to."""
    if User.query.get(user_id).is_superadmin:
        # Super admins can access all companies
        return [c.id for c in Company.query.all()]

    # Regular users get their assigned companies
    user_companies = UserCompany.query.filter_by(user_id=user_id).all()
    return [uc.company_id for uc in user_companies]

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

    # Auth setup
    login_manager = LoginManager()
    login_manager.login_view = 'login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

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

    # Create/update backup jobs for all existing devices on startup (after migrations)
    with app.app_context():
        try:
            devices = Device.query.all()
        except Exception:
            devices = []
        for device in devices:
            add_or_update_backup_job(device)

    # Start scheduler and jobs (after preparing device jobs)
    start_monitoring_job(app)

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
    @login_required
    def dashboard():
        # Get user's company IDs
        user_company_ids = get_user_company_ids(current_user.id)

        # Filter devices by user's companies
        devices = Device.query.filter(Device.company_id.in_(user_company_ids)).order_by(Device.name.asc()).all()
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

        # Module last update time: latest ResourceMetric timestamp across user's devices
        last_metrics_update = (
            db.session.query(db.func.max(ResourceMetric.timestamp))
            .join(Device, ResourceMetric.device_id == Device.id)
            .filter(Device.company_id.in_(user_company_ids))
            .scalar()
        )
        return render_template(
            'dashboard.html',
            devices=devices,
            latest_metrics_by_device=latest_metrics_by_device,
            last_metrics_update=last_metrics_update,
        )

    @app.route('/devices', methods=['GET', 'POST'])
    @login_required
    def devices_page():
        # Get user's company IDs
        user_company_ids = get_user_company_ids(current_user.id)

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            host = request.form.get('host', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            port = request.form.get('port', '22').strip()
            schedule = request.form.get('schedule', 'manual')
            company_id = request.form.get('company_id')

            if not name or not host or not username or not password:
                flash('All fields except port are required.', 'danger')
                return redirect(url_for('devices_page'))

            if not company_id:
                flash('Please select a company.', 'danger')
                return redirect(url_for('devices_page'))

            # Check if user has access to selected company
            if int(company_id) not in user_company_ids:
                flash('You do not have access to the selected company.', 'danger')
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
                company_id=int(company_id),
                device_type=request.form.get('device_type','mikrotik'),
            )
            db.session.add(device)
            db.session.commit()

            add_or_update_backup_job(device)

            flash('Device added.', 'success')
            return redirect(url_for('devices_page'))

        # Filter devices by user's companies
        devices = Device.query.filter(Device.company_id.in_(user_company_ids)).order_by(Device.name.asc()).all()

        # Get companies user can access for device creation
        user_companies = Company.query.filter(Company.id.in_(user_company_ids)).order_by(Company.name.asc()).all()

        return render_template('devices.html', devices=devices, companies=user_companies)

    @app.route('/devices/edit/<int:device_id>', methods=['GET', 'POST'])
    @login_required
    def edit_device(device_id: int):
        device = Device.query.get_or_404(device_id)

        # Check if user has access to the device's company
        user_company_ids = get_user_company_ids(current_user.id)
        if device.company_id not in user_company_ids:
            flash('You do not have access to this device.', 'danger')
            return redirect(url_for('devices_page'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            host = request.form.get('host', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            port = request.form.get('port', '22').strip()
            schedule = request.form.get('schedule', 'manual')
            snmp_version = request.form.get('snmp_version', 'v2c')
            snmp_community = request.form.get('snmp_community') or None

            if not name or not host or not username:
                flash('Name, host and username are required.', 'danger')
                return redirect(url_for('edit_device', device_id=device_id))

            try:
                port_int = int(port)
            except ValueError:
                flash('Port must be a number.', 'danger')
                return redirect(url_for('edit_device', device_id=device_id))

            # Update device
            device.name = name
            device.host = host
            device.username = username
            if password:  # Only update password if provided
                device.password = password
            device.port = port_int
            device.schedule = schedule
            device.snmp_version = snmp_version
            device.snmp_community = snmp_community
            # Update device type if provided
            dt = request.form.get('device_type')
            if dt in ('mikrotik','linux'):
                device.device_type = dt

            db.session.commit()

            # Update backup job if schedule changed
            add_or_update_backup_job(device)

            flash('Device updated.', 'success')
            return redirect(url_for('devices_page'))

        # Get companies user can access for device reassignment
        user_companies = Company.query.filter(Company.id.in_(user_company_ids)).order_by(Company.name.asc()).all()

        return render_template('edit_device.html', device=device, companies=user_companies)

    @app.route('/devices/delete/<int:device_id>', methods=['POST'])
    @login_required
    def delete_device(device_id: int):
        device = Device.query.get_or_404(device_id)

        # Check if user has access to the device's company
        user_company_ids = get_user_company_ids(current_user.id)
        if device.company_id not in user_company_ids:
            flash('You do not have access to this device.', 'danger')
            return redirect(url_for('devices_page'))

        remove_backup_job(device_id)
        ResourceMetric.query.filter_by(device_id=device_id).delete()
        db.session.delete(device)
        db.session.commit()
        flash('Device deleted.', 'success')
        return redirect(url_for('devices_page'))

    @app.route('/backup', methods=['GET'])
    @login_required
    def backup_page():
        # Get user's company IDs
        user_company_ids = get_user_company_ids(current_user.id)
        devices = Device.query.filter(Device.company_id.in_(user_company_ids)).order_by(Device.name.asc()).all()
        return render_template('backup.html', devices=devices)

    @app.route('/backup/manual/<int:device_id>', methods=['POST'])
    @login_required
    def manual_backup(device_id: int):
        device = Device.query.get_or_404(device_id)

        # Check if user has access to the device's company
        user_company_ids = get_user_company_ids(current_user.id)
        if device.company_id not in user_company_ids:
            flash('You do not have access to this device.', 'danger')
            return redirect(url_for('backup_page'))

        success, message = perform_manual_backup(device)
        device.last_backup_status = 'success' if success else 'fail'
        device.last_backup_time = datetime.now(timezone.utc)
        device.last_backup_message = message[:1000] if message else None
        db.session.commit()
        flash(f'Manual backup for {device.name}: {"success" if success else "failed"}.', 'success' if success else 'danger')
        return redirect(url_for('backup_page'))

    @app.route('/backup/schedule/<int:device_id>', methods=['POST'])
    @login_required
    def update_schedule(device_id: int):
        device = Device.query.get_or_404(device_id)

        # Check if user has access to the device's company
        user_company_ids = get_user_company_ids(current_user.id)
        if device.company_id not in user_company_ids:
            flash('You do not have access to this device.', 'danger')
            return redirect(url_for('backup_page'))

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
    @login_required
    def backup_files():
        # Get user's accessible device names
        user_company_ids = get_user_company_ids(current_user.id)
        user_devices = Device.query.filter(Device.company_id.in_(user_company_ids)).all()
        user_device_names = {device.name for device in user_devices}

        # List backups grouped by date folder, filtered by user's devices
        backups_root = os.path.join(app.root_path, 'backups')
        date_folders = []
        if os.path.isdir(backups_root):
            for entry in sorted(os.listdir(backups_root)):
                full = os.path.join(backups_root, entry)
                if os.path.isdir(full):
                    # Filter files to only show backups for user's devices
                    all_files = sorted([f for f in os.listdir(full) if f.endswith('.backup')])
                    user_files = []
                    for file in all_files:
                        # Extract device name from filename (format: device_name_DDMMYYHH.backup)
                        device_name = '_'.join(file.split('_')[:-1])  # Remove the date part
                        if device_name in user_device_names:
                            user_files.append(file)
                    if user_files:  # Only include date folders that have files for user's devices
                        date_folders.append({'date': entry, 'files': user_files})
        return render_template('backup_files.html', date_folders=date_folders)

    @app.route('/backup/download/<date>/<filename>')
    @login_required
    def download_backup(date: str, filename: str):
        # Check if user has access to this backup file
        user_company_ids = get_user_company_ids(current_user.id)
        user_devices = Device.query.filter(Device.company_id.in_(user_company_ids)).all()
        user_device_names = {device.name for device in user_devices}

        # Extract device name from filename
        device_name = '_'.join(filename.split('_')[:-1])
        if device_name not in user_device_names:
            flash('You do not have access to this backup file.', 'danger')
            return redirect(url_for('backup_files'))

        backups_root = os.path.join(app.root_path, 'backups')
        directory = os.path.join(backups_root, date)
        return send_from_directory(directory=directory, path=filename, as_attachment=True)

    @app.route('/backup/delete/<date>/<filename>', methods=['POST'])
    @login_required
    def delete_backup_file(date: str, filename: str):
        # Check if user has access to this backup file
        user_company_ids = get_user_company_ids(current_user.id)
        user_devices = Device.query.filter(Device.company_id.in_(user_company_ids)).all()
        user_device_names = {device.name for device in user_devices}

        # Extract device name from filename
        device_name = '_'.join(filename.split('_')[:-1])
        if device_name not in user_device_names:
            flash('You do not have access to this backup file.', 'danger')
            return redirect(url_for('backup_files'))

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
    @login_required
    def pings_page():
        # Get user's company IDs
        user_company_ids = get_user_company_ids(current_user.id)
        devices = Device.query.filter(Device.company_id.in_(user_company_ids)).order_by(Device.name.asc()).all()

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

            # Check if user has access to the selected device
            device = Device.query.get(device_id_int)
            if not device or device.company_id not in user_company_ids:
                flash('You do not have access to the selected device.', 'danger')
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

        # Filter ping checks by user's companies through device relationship
        checks = PingCheck.query.join(Device).filter(Device.company_id.in_(user_company_ids)).all()
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

    @app.route('/pings/edit/<int:check_id>', methods=['GET', 'POST'])
    @login_required
    def edit_ping(check_id: int):
        check = PingCheck.query.get_or_404(check_id)

        # Check if user has access to the device's company
        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this ping monitor.', 'danger')
            return redirect(url_for('pings_page'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            target_ip = request.form.get('target_ip', '').strip()
            source_ip = request.form.get('source_ip', '').strip() or None
            source_interface = request.form.get('source_interface', '').strip() or None

            if not name or not target_ip:
                flash('Name and target IP are required.', 'danger')
                return redirect(url_for('edit_ping', check_id=check_id))

            # Update ping check
            check.name = name
            check.target_ip = target_ip
            check.source_ip = source_ip
            check.source_interface = source_interface

            db.session.commit()
            flash('Ping monitor updated.', 'success')
            return redirect(url_for('pings_page'))

        return render_template('edit_ping.html', check=check, device=device)

    @app.route('/pings/delete/<int:check_id>', methods=['POST'])
    @login_required
    def delete_ping(check_id: int):
        check = PingCheck.query.get_or_404(check_id)

        # Check if user has access to the device's company
        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this ping monitor.', 'danger')
            return redirect(url_for('pings_page'))

        db.session.delete(check)
        db.session.commit()
        flash('Ping monitor deleted.', 'success')
        return redirect(url_for('pings_page'))

    @app.route('/pings/notify/<int:check_id>', methods=['POST'])
    @login_required
    def notify_ping(check_id: int):
        check = PingCheck.query.get_or_404(check_id)

        # Check if user has access to the device's company
        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this ping monitor.', 'danger')
            return redirect(url_for('pings_page'))

        # Send using the router's company-specific Telegram settings
        from telegram_utils import send_company_telegram_message_with_details
        status = f"{check.last_rtt_ms:.1f} ms" if check.last_rtt_ms is not None else 'Timeout'
        sent, info = send_company_telegram_message_with_details(
            device.company_id,
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
    @login_required
    def api_pings():
        # Read-only endpoint; scheduler updates values.
        user_company_ids = get_user_company_ids(current_user.id)
        checks = PingCheck.query.join(Device).filter(Device.company_id.in_(user_company_ids)).all()
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
    @login_required
    def api_ping_samples():
        try:
            check_id = int(request.args.get('check_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'check_id required'}), 400

        # Check if user has access to this ping check
        check = PingCheck.query.get(check_id)
        if not check:
            return jsonify({'error': 'ping check not found'}), 404

        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            return jsonify({'error': 'access denied'}), 403

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

        # Convert timestamps to Asia/Yangon timezone for consistency
        import pytz
        yangon_tz = pytz.timezone('Asia/Yangon')

        return jsonify([
            {
                'ts': (s.timestamp.astimezone(yangon_tz) if s.timestamp.tzinfo else
                       pytz.utc.localize(s.timestamp).astimezone(yangon_tz)).isoformat(),
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

    @app.route('/admin/telegram/<int:company_id>', methods=['GET', 'POST'])
    @login_required
    def company_telegram_settings(company_id):
        # Only super admins can manage telegram settings
        if not current_user.is_superadmin:
            flash('Access denied. Only super administrators can manage Telegram settings.', 'danger')
            return redirect(url_for('dashboard'))

        company = Company.query.get_or_404(company_id)

        # Get or create telegram settings for this company
        telegram_settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
        if not telegram_settings:
            telegram_settings = CompanyTelegramSetting(company_id=company_id)
            db.session.add(telegram_settings)
            db.session.commit()

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'save':
                # Update settings
                telegram_settings.bot_token = request.form.get('bot_token', '').strip()
                telegram_settings.chat_id = request.form.get('chat_id', '').strip()
                telegram_settings.group_name = request.form.get('group_name', '').strip()
                telegram_settings.enabled = request.form.get('enabled') == 'on'
                telegram_settings.ping_down_alerts = request.form.get('ping_down_alerts') == 'on'
                telegram_settings.fiber_down_alerts = request.form.get('fiber_down_alerts') == 'on'
                telegram_settings.high_ping_alerts = request.form.get('high_ping_alerts') == 'on'
                try:
                    telegram_settings.high_ping_threshold_ms = int(request.form.get('high_ping_threshold_ms', '90'))
                except ValueError:
                    telegram_settings.high_ping_threshold_ms = 90

                # Reporting interval
                try:
                    telegram_settings.report_interval_minutes = int(request.form.get('report_interval_minutes', '60'))
                    if telegram_settings.report_interval_minutes not in (15, 30, 60):
                        telegram_settings.report_interval_minutes = 60
                except ValueError:
                    telegram_settings.report_interval_minutes = 60

                db.session.commit()
                flash(f'Telegram settings for {company.name} saved.', 'success')
                return redirect(url_for('company_telegram_settings', company_id=company_id))

            elif action == 'test':
                if not telegram_settings.bot_token or not telegram_settings.chat_id:
                    flash('Bot token and Chat ID are required to send test message.', 'danger')
                    return redirect(url_for('company_telegram_settings', company_id=company_id))

                # Send via company-specific settings
                from telegram_utils import send_company_telegram_message_with_details
                ok, info = send_company_telegram_message_with_details(company_id, f'🧪 Test message from {company.name} ✅')

                flash('Test sent to Telegram.' if ok else f'Test failed: {info}', 'success' if ok else 'danger')
                return redirect(url_for('company_telegram_settings', company_id=company_id))

            elif action == 'detect':
                if not telegram_settings.bot_token:
                    flash('Provide bot token to detect chat ID.', 'danger')
                    return redirect(url_for('company_telegram_settings', company_id=company_id))

                try:
                    resp = requests.get(
                        f"https://api.telegram.org/bot{telegram_settings.bot_token}/getUpdates",
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
                        telegram_settings.chat_id = found[0]
                        telegram_settings.group_name = found[1]
                        db.session.commit()
                        flash(f'Detected chat ID {found[0]} for group "{found[1]}". Settings saved.', 'success')
                    else:
                        flash('No group chat found in bot updates. Add bot to group, disable privacy, send a message, then try again.', 'warning')
                except Exception as exc:
                    flash(f'Detect failed: {exc}', 'danger')
                return redirect(url_for('company_telegram_settings', company_id=company_id))

        return render_template('company_telegram_settings.html', company=company, settings=telegram_settings)

    @app.route('/admin/telegram/test/<int:company_id>')
    @login_required
    def test_company_telegram(company_id):
        """Test endpoint to check Telegram configuration"""
        if not current_user.is_superadmin:
            return jsonify({'success': False, 'error': 'Access denied'})

        from telegram_utils import send_company_telegram_message_with_details

        test_message = f"🧪 Test message from Router Portal\nCompany: {company_id}\nTime: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

        success, message = send_company_telegram_message_with_details(company_id, test_message)

        return jsonify({
            'success': success,
            'message': message,
            'company_id': company_id
        })

    @app.route('/admin/report/status')
    @login_required
    def admin_report_status():
        if not current_user.is_superadmin:
            return jsonify({'success': False, 'error': 'Access denied'}), 403

        now = datetime.now(timezone.utc)
        companies = Company.query.order_by(Company.id.asc()).all()
        out = []
        for c in companies:
            settings = CompanyTelegramSetting.query.filter_by(company_id=c.id).first()
            devices = Device.query.filter_by(company_id=c.id).all()
            last_sent = settings.last_report_sent_at if settings else None
            if last_sent is not None and last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            interval = (settings.report_interval_minutes if settings else 60) or 60
            should_send = False
            if settings and settings.enabled:
                should_send = last_sent is None or (now - last_sent).total_seconds() >= interval * 60
            out.append({
                'company_id': c.id,
                'company_name': c.name,
                'enabled': bool(settings.enabled) if settings else False,
                'interval_minutes': interval,
                'devices_count': len(devices),
                'last_report_sent_at': last_sent.isoformat() if last_sent else None,
                'should_send_now': should_send,
            })
        return jsonify({'success': True, 'now': now.isoformat(), 'companies': out})

    @app.route('/admin/report/send/<int:company_id>', methods=['POST'])
    @login_required
    def admin_report_send(company_id: int):
        if not current_user.is_superadmin:
            return jsonify({'success': False, 'error': 'Access denied'}), 403

        settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
        if not settings or not settings.enabled:
            return jsonify({'success': False, 'error': 'Settings missing or disabled'})

        # Build report text similarly to scheduler
        lines = ["<b>Performance Report</b>"]
        devices = Device.query.filter_by(company_id=company_id).order_by(Device.name.asc()).all()
        for d in devices:
            latest = (
                ResourceMetric.query.filter_by(device_id=d.id)
                .order_by(ResourceMetric.timestamp.desc())
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
            fiber_checks = FiberCheck.query.filter_by(device_id=d.id).all()
            fiber_lines = []
            for fc in fiber_checks:
                rx = f"{fc.last_rx_dbm:.2f} dBm" if fc.last_rx_dbm is not None else "-"
                tx = f"{fc.last_tx_dbm:.2f} dBm" if fc.last_tx_dbm is not None else "-"
                fiber_lines.append(f"Fiber {fc.name} ({fc.interface_name}): RX {rx}, TX {tx}")
            fiber_str = ("\n" + "\n".join(fiber_lines)) if fiber_lines else ""
            lines.append(
                f"\n<b>Device:</b> {d.name}\nCPU: {cpu} | RAM used: {mem_str} | Storage used: {st_str} | Latency: {rtt}{fiber_str}"
            )

        if len(lines) <= 1:
            return jsonify({'success': False, 'error': 'No devices to report'})

        text = "\n".join(lines)
        parts = _chunk_text_for_telegram(text)
        from telegram_utils import send_company_telegram_message
        all_ok = True
        statuses = []
        for idx, part in enumerate(parts, start=1):
            prefix = f"Part {idx}/{len(parts)}\n" if len(parts) > 1 else ""
            ok = send_company_telegram_message(company_id, prefix + part)
            statuses.append({'part': idx, 'ok': ok})
            if not ok:
                all_ok = False
                break
        if all_ok:
            settings.last_report_sent_at = datetime.now(timezone.utc)
            db.session.commit()
        return jsonify({'success': all_ok, 'parts': statuses})

    @app.route('/messages', methods=['GET', 'POST'])
    @login_required
    def messages_page():
        # Determine which companies the user can message
        if current_user.is_superadmin:
            accessible_company_ids = [c.id for c in Company.query.order_by(Company.name.asc()).all()]
            companies = Company.query.order_by(Company.name.asc()).all()
        else:
            accessible_company_ids = get_user_company_ids(current_user.id)
            companies = Company.query.filter(Company.id.in_(accessible_company_ids)).order_by(Company.name.asc()).all()

        if request.method == 'POST':
            text = request.form.get('text', '').strip()
            if not text:
                flash('Message cannot be empty.', 'danger')
                return redirect(url_for('messages_page'))

            # Optional company selection; default = all accessible
            company_id_str = request.form.get('company_id')
            target_company_ids = accessible_company_ids
            if company_id_str and company_id_str != 'all':
                try:
                    cid = int(company_id_str)
                    if cid in accessible_company_ids:
                        target_company_ids = [cid]
                    else:
                        flash('You do not have access to the selected company.', 'danger')
                        return redirect(url_for('messages_page'))
                except ValueError:
                    pass

            from telegram_utils import send_company_telegram_message_with_details
            failures = []
            for cid in target_company_ids:
                ok, info = send_company_telegram_message_with_details(cid, text)
                if not ok:
                    failures.append((cid, info))

            if not failures:
                flash('Message sent to Telegram.', 'success')
            else:
                fail_str = '; '.join([f'company {cid}: {info}' for cid, info in failures])
                flash(f'Failed for {fail_str}', 'danger')
            return redirect(url_for('messages_page'))

        return render_template('messages.html', companies=companies)

    @app.route('/fibers', methods=['GET', 'POST'])
    @login_required
    def fibers_page():
        # Get user's company IDs
        user_company_ids = get_user_company_ids(current_user.id)
        devices = Device.query.filter(Device.company_id.in_(user_company_ids)).order_by(Device.name.asc()).all()

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

            # Check if user has access to the selected device
            device = Device.query.get(device_id_int)
            if not device or device.company_id not in user_company_ids:
                flash('You do not have access to the selected device.', 'danger')
                return redirect(url_for('fibers_page'))

            check = FiberCheck(name=name, device_id=device_id_int, interface_name=if_name)
            db.session.add(check)
            db.session.commit()
            flash('Fiber monitor added.', 'success')
            return redirect(url_for('fibers_page'))

        # Filter fiber checks by user's companies through device relationship
        checks = FiberCheck.query.join(Device).filter(Device.company_id.in_(user_company_ids)).all()
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

    @app.route('/fibers/edit/<int:check_id>', methods=['GET', 'POST'])
    @login_required
    def edit_fiber(check_id: int):
        check = FiberCheck.query.get_or_404(check_id)

        # Check if user has access to the device's company
        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this fiber monitor.', 'danger')
            return redirect(url_for('fibers_page'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            interface_name = request.form.get('interface_name', '').strip()

            if not name or not interface_name:
                flash('Name and interface name are required.', 'danger')
                return redirect(url_for('edit_fiber', check_id=check_id))

            # Update fiber check
            check.name = name
            check.interface_name = interface_name

            db.session.commit()
            flash('Fiber monitor updated.', 'success')
            return redirect(url_for('fibers_page'))

        return render_template('edit_fiber.html', check=check, device=device)

    @app.route('/fibers/delete/<int:check_id>', methods=['POST'])
    @login_required
    def delete_fiber(check_id: int):
        check = FiberCheck.query.get_or_404(check_id)

        # Check if user has access to the device's company
        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this fiber monitor.', 'danger')
            return redirect(url_for('fibers_page'))

        # Delete associated fiber samples
        FiberSample.query.filter_by(check_id=check_id).delete()
        db.session.delete(check)
        db.session.commit()
        flash('Fiber monitor deleted.', 'success')
        return redirect(url_for('fibers_page'))

    @app.route('/api/fibers/samples')
    @login_required
    def api_fiber_samples():
        try:
            check_id = int(request.args.get('check_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'check_id required'}), 400

        # Check if user has access to this fiber check
        check = FiberCheck.query.get(check_id)
        if not check:
            return jsonify({'error': 'fiber check not found'}), 404

        device = Device.query.get(check.device_id)
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            return jsonify({'error': 'access denied'}), 403

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

        # Convert timestamps to Asia/Yangon timezone for consistency
        import pytz
        yangon_tz = pytz.timezone('Asia/Yangon')

        return jsonify([
            {
                'ts': (s.timestamp.astimezone(yangon_tz) if s.timestamp.tzinfo else
                       pytz.utc.localize(s.timestamp).astimezone(yangon_tz)).isoformat(),
                'rx': s.rx_dbm,
                'tx': s.tx_dbm,
                'oper': s.oper_status,
            }
            for s in samples
        ])

    @app.route('/fibers/probe/<int:check_id>', methods=['POST'])
    @login_required
    def fiber_probe(check_id: int):
        check = FiberCheck.query.get_or_404(check_id)
        device = Device.query.get(check.device_id)

        # Check if user has access to the device's company
        user_company_ids = get_user_company_ids(current_user.id)
        if not device or device.company_id not in user_company_ids:
            flash('You do not have access to this fiber monitor.', 'danger')
            return redirect(url_for('fibers_page'))

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

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()

            if not email or not password:
                flash('Email and password are required.', 'danger')
                return redirect(url_for('login'))

            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password_hash, password):
                login_user(user)
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password.', 'danger')

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('login'))

    @app.route('/init', methods=['GET', 'POST'])
    def init():
        # Only allow if no users exist
        if User.query.count() > 0:
            return redirect(url_for('login'))

        if request.method == 'POST':
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()

            if not email or not password:
                flash('Email and password are required.', 'danger')
                return redirect(url_for('init'))

            if len(password) < 6:
                flash('Password must be at least 6 characters.', 'danger')
                return redirect(url_for('init'))

            # Create superadmin user
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                is_superadmin=True
            )
            db.session.add(user)
            db.session.commit()

            flash('Superadmin account created. You can now log in.', 'success')
            return redirect(url_for('login'))

        return render_template('init.html')

    @app.route('/admin/companies', methods=['GET', 'POST'])
    @login_required
    def admin_companies():
        if not current_user.is_superadmin:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            notes = request.form.get('notes', '').strip()

            if not name:
                flash('Company name is required.', 'danger')
                return redirect(url_for('admin_companies'))

            company = Company(name=name, notes=notes)
            db.session.add(company)
            db.session.commit()

            flash('Company created.', 'success')
            return redirect(url_for('admin_companies'))

        companies = Company.query.order_by(Company.name.asc()).all()
        return render_template('admin_companies.html', companies=companies)

    @app.route('/admin/company/<int:company_id>', methods=['GET', 'POST'])
    @login_required
    def admin_company(company_id):
        if not current_user.is_superadmin:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        company = Company.query.get_or_404(company_id)

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'update':
                name = request.form.get('name', '').strip()
                notes = request.form.get('notes', '').strip()

                if not name:
                    flash('Company name is required.', 'danger')
                    return redirect(url_for('admin_company', company_id=company_id))

                company.name = name
                company.notes = notes
                db.session.commit()
                flash('Company updated.', 'success')

            elif action == 'delete':
                # Check if company has devices
                device_count = Device.query.filter_by(company_id=company_id).count()
                if device_count > 0:
                    flash(f'Cannot delete company with {device_count} devices.', 'danger')
                    return redirect(url_for('admin_company', company_id=company_id))

                # Delete user-company relationships
                UserCompany.query.filter_by(company_id=company_id).delete()
                db.session.delete(company)
                db.session.commit()
                flash('Company deleted.', 'success')
                return redirect(url_for('admin_companies'))

            return redirect(url_for('admin_company', company_id=company_id))

        # Get users assigned to this company
        user_companies = UserCompany.query.filter_by(company_id=company_id).all()
        assigned_users = []
        for uc in user_companies:
            user = User.query.get(uc.user_id)
            if user:
                assigned_users.append({
                    'user': user,
                    'role': uc.role
                })

        return render_template('admin_company.html', company=company, assigned_users=assigned_users)

    @app.route('/admin/users', methods=['GET', 'POST'])
    @login_required
    def admin_users():
        if not current_user.is_superadmin:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()
            is_superadmin = request.form.get('is_superadmin') == 'on'

            if not email or not password:
                flash('Email and password are required.', 'danger')
                return redirect(url_for('admin_users'))

            if len(password) < 6:
                flash('Password must be at least 6 characters.', 'danger')
                return redirect(url_for('admin_users'))

            # Check if user already exists
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('User with this email already exists.', 'danger')
                return redirect(url_for('admin_users'))

            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                is_superadmin=is_superadmin
            )
            db.session.add(user)
            db.session.commit()

            flash('User created.', 'success')
            return redirect(url_for('admin_users'))

        users = User.query.order_by(User.email.asc()).all()
        return render_template('admin_users.html', users=users)

    @app.route('/admin/user/<int:user_id>', methods=['GET', 'POST'])
    @login_required
    def admin_user(user_id):
        if not current_user.is_superadmin:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        user = User.query.get_or_404(user_id)

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'update':
                email = request.form.get('email', '').strip()
                is_superadmin = request.form.get('is_superadmin') == 'on'

                if not email:
                    flash('Email is required.', 'danger')
                    return redirect(url_for('admin_user', user_id=user_id))

                # Check if email is taken by another user
                existing_user = User.query.filter_by(email=email).first()
                if existing_user and existing_user.id != user_id:
                    flash('Email already taken.', 'danger')
                    return redirect(url_for('admin_user', user_id=user_id))

                user.email = email
                user.is_superadmin = is_superadmin
                db.session.commit()
                flash('User updated.', 'success')

            elif action == 'assign_company':
                company_id = request.form.get('company_id')
                role = request.form.get('role', 'viewer')

                if company_id:
                    # Remove existing assignment
                    UserCompany.query.filter_by(user_id=user_id, company_id=company_id).delete()

                    # Add new assignment
                    uc = UserCompany(user_id=user_id, company_id=company_id, role=role)
                    db.session.add(uc)
                    db.session.commit()
                    flash('Company assignment updated.', 'success')

            elif action == 'remove_company':
                company_id = request.form.get('company_id')
                if company_id:
                    UserCompany.query.filter_by(user_id=user_id, company_id=int(company_id)).delete()
                    db.session.commit()
                    flash('Company assignment removed.', 'success')

            elif action == 'delete':
                if user_id == current_user.id:
                    flash('Cannot delete your own account.', 'danger')
                    return redirect(url_for('admin_user', user_id=user_id))

                # Delete user-company relationships
                UserCompany.query.filter_by(user_id=user_id).delete()
                db.session.delete(user)
                db.session.commit()
                flash('User deleted.', 'success')
                return redirect(url_for('admin_users'))

            return redirect(url_for('admin_user', user_id=user_id))

        # Get all companies for assignment
        companies = Company.query.order_by(Company.name.asc()).all()

        # Get user's current company assignments
        user_companies = UserCompany.query.filter_by(user_id=user_id).all()
        assigned_companies = []
        for uc in user_companies:
            company = Company.query.get(uc.company_id)
            if company:
                assigned_companies.append({
                    'company': company,
                    'role': uc.role
                })

        return render_template('admin_user.html', user=user, companies=companies, assigned_companies=assigned_companies)

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)


