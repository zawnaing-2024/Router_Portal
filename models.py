from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin


db = SQLAlchemy()


class Device(db.Model):
    __tablename__ = 'devices'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    host = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(128), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=22)
    # Device type: 'mikrotik' or 'linux'
    device_type = db.Column(db.String(16), nullable=False, default='mikrotik')
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    # SNMP configuration
    snmp_version = db.Column(db.String(8), nullable=False, default='v2c')
    snmp_community = db.Column(db.String(128))

    # Multi-tenant ownership
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'))

    # Scheduling: manual, daily, weekly, monthly
    schedule = db.Column(db.String(16), nullable=False, default='manual')

    # Backup status tracking
    last_backup_status = db.Column(db.String(16))  # success | fail | None
    last_backup_time = db.Column(db.DateTime(timezone=True))
    last_backup_message = db.Column(db.Text)

    metrics = db.relationship('ResourceMetric', backref='device', cascade='all, delete-orphan')

    # Alert cooldown timestamps
    last_cpu_alert_at = db.Column(db.DateTime(timezone=True))
    last_ram_alert_at = db.Column(db.DateTime(timezone=True))
    last_storage_alert_at = db.Column(db.DateTime(timezone=True))


class ResourceMetric(db.Model):
    __tablename__ = 'resource_metrics'
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey('devices.id'), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    cpu_load_percent = db.Column(db.Float)
    total_memory_bytes = db.Column(db.BigInteger)
    free_memory_bytes = db.Column(db.BigInteger)
    total_storage_bytes = db.Column(db.BigInteger)
    free_storage_bytes = db.Column(db.BigInteger)


class PingCheck(db.Model):
    __tablename__ = 'ping_checks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey('devices.id'), nullable=False)
    target_ip = db.Column(db.String(64), nullable=False)
    source_ip = db.Column(db.String(64))  # optional
    source_interface = db.Column(db.String(64))  # optional

    last_rtt_ms = db.Column(db.Float)  # last ping result in ms
    last_checked_at = db.Column(db.DateTime(timezone=True))
    consecutive_failures = db.Column(db.Integer, nullable=False, default=0)
    alerted = db.Column(db.Boolean, nullable=False, default=False)
    down_start_at = db.Column(db.DateTime(timezone=True))  # when outages began


class PingSample(db.Model):
    __tablename__ = 'ping_samples'
    id = db.Column(db.Integer, primary_key=True)
    check_id = db.Column(db.Integer, db.ForeignKey('ping_checks.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    rtt_ms = db.Column(db.Float)


class FiberCheck(db.Model):
    __tablename__ = 'fiber_checks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey('devices.id'), nullable=False)
    interface_name = db.Column(db.String(64), nullable=False)  # e.g., sfp1

    last_rx_dbm = db.Column(db.Float)
    last_tx_dbm = db.Column(db.Float)
    last_oper_status = db.Column(db.Integer)  # 1 up, 2 down
    last_checked_at = db.Column(db.DateTime(timezone=True))

    # Alert tracking
    alerted_down = db.Column(db.Boolean, nullable=False, default=False)
    down_start_at = db.Column(db.DateTime(timezone=True))  # when fiber went down


class FiberSample(db.Model):
    __tablename__ = 'fiber_samples'
    id = db.Column(db.Integer, primary_key=True)
    check_id = db.Column(db.Integer, db.ForeignKey('fiber_checks.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    rx_dbm = db.Column(db.Float)
    tx_dbm = db.Column(db.Float)
    oper_status = db.Column(db.Integer)


class AppSetting(db.Model):
    __tablename__ = 'app_settings'
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)


class CompanyTelegramSetting(db.Model):
    __tablename__ = 'company_telegram_settings'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False, unique=True)
    bot_token = db.Column(db.String(255))
    chat_id = db.Column(db.String(64))
    group_name = db.Column(db.String(128))
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    ping_down_alerts = db.Column(db.Boolean, nullable=False, default=True)
    fiber_down_alerts = db.Column(db.Boolean, nullable=False, default=True)
    high_ping_alerts = db.Column(db.Boolean, nullable=False, default=True)
    high_ping_threshold_ms = db.Column(db.Integer, nullable=False, default=90)
    # Scheduled report
    report_interval_minutes = db.Column(db.Integer, nullable=False, default=60)
    last_report_sent_at = db.Column(db.DateTime(timezone=True))


class Company(db.Model):
    __tablename__ = 'companies'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    notes = db.Column(db.Text)

    devices = db.relationship('Device', backref='company', cascade='all, delete-orphan')
    telegram_settings = db.relationship('CompanyTelegramSetting', backref='company', cascade='all, delete-orphan', uselist=False)


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_superadmin = db.Column(db.Boolean, nullable=False, default=False)


class UserCompany(db.Model):
    __tablename__ = 'user_companies'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    role = db.Column(db.String(32), nullable=False, default='viewer')  # viewer|admin

