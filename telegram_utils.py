import os
import requests
from dotenv import load_dotenv, find_dotenv

# Load .env once on import so web app picks up saved vars without manual export
def _refresh_env_if_needed() -> None:
    try:
        load_dotenv(find_dotenv(), override=False)
    except Exception:
        pass


def send_telegram_message(text: str) -> bool:
    _refresh_env_if_needed()
    # Try DB settings first
    try:
        from app import create_app
        from models import AppSetting, db
        app = create_app()
        with app.app_context():
            rec_t = AppSetting.query.get('TELEGRAM_BOT_TOKEN')
            rec_c = AppSetting.query.get('TELEGRAM_CHAT_ID')
            token = (rec_t.value if rec_t else '') or (os.environ.get('TELEGRAM_BOT_TOKEN') or '')
            chat_id = (rec_c.value if rec_c else '') or (os.environ.get('TELEGRAM_CHAT_ID') or '')
    except Exception:
        token = (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
        chat_id = (os.environ.get('TELEGRAM_CHAT_ID') or '').strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=5)
        return resp.ok
    except Exception:
        return False


def send_telegram_message_with_details(text: str) -> tuple[bool, str]:
    _refresh_env_if_needed()
    try:
        from app import create_app
        from models import AppSetting, db
        app = create_app()
        with app.app_context():
            rec_t = AppSetting.query.get('TELEGRAM_BOT_TOKEN')
            rec_c = AppSetting.query.get('TELEGRAM_CHAT_ID')
            token = (rec_t.value if rec_t else '') or (os.environ.get('TELEGRAM_BOT_TOKEN') or '')
            chat_id = (rec_c.value if rec_c else '') or (os.environ.get('TELEGRAM_CHAT_ID') or '')
    except Exception:
        token = (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
        chat_id = (os.environ.get('TELEGRAM_CHAT_ID') or '').strip()
    if not token or not chat_id:
        return False, 'Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars'
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=5)
        if resp.ok:
            return True, 'ok'
        return False, resp.text
    except Exception as exc:
        return False, f'Exception: {exc}'


def send_company_telegram_message(company_id: int, text: str) -> bool:
    """Send Telegram message using company-specific settings"""
    try:
        from app import create_app
        from models import CompanyTelegramSetting, db
        app = create_app()
        with app.app_context():
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            if not settings or not settings.enabled or not settings.bot_token or not settings.chat_id:
                return False

            url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
            resp = requests.post(url, json={
                'chat_id': settings.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=5)
            return resp.ok
    except Exception:
        return False


def send_company_telegram_message_with_details(company_id: int, text: str) -> tuple[bool, str]:
    """Send Telegram message using company-specific settings with error details"""
    try:
        from app import create_app
        from models import CompanyTelegramSetting, db
        app = create_app()
        with app.app_context():
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            if not settings or not settings.enabled:
                return False, 'Telegram not enabled for this company'

            if not settings.bot_token or not settings.chat_id:
                return False, 'Missing bot token or chat ID'

            url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
            resp = requests.post(url, json={
                'chat_id': settings.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=5)

            if resp.ok:
                return True, 'ok'
            return False, resp.text
    except Exception as exc:
        return False, f'Exception: {exc}'


def should_send_company_alert(company_id: int, alert_type: str) -> bool:
    """Check if a specific alert type should be sent for a company"""
    try:
        from app import create_app
        from models import CompanyTelegramSetting, db
        app = create_app()
        with app.app_context():
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            if not settings or not settings.enabled:
                return False

            if alert_type == 'ping_down' and settings.ping_down_alerts:
                return True
            elif alert_type == 'fiber_down' and settings.fiber_down_alerts:
                return True
            elif alert_type == 'high_ping' and settings.high_ping_alerts:
                return True

            return False
    except Exception:
        return False


def get_company_ping_threshold(company_id: int) -> int:
    """Get the high ping threshold for a company"""
    try:
        from app import create_app
        from models import CompanyTelegramSetting, db
        app = create_app()
        with app.app_context():
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            return settings.high_ping_threshold_ms if settings else 90
    except Exception:
        return 90


