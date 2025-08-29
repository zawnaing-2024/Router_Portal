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
            if not settings:
                print(f"[TELEGRAM] No settings found for company {company_id}")
                return False

            if not settings.enabled:
                print(f"[TELEGRAM] Telegram disabled for company {company_id}")
                return False

            if not settings.bot_token or not settings.chat_id:
                print(f"[TELEGRAM] Missing bot_token or chat_id for company {company_id}")
                return False

            url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
            print(f"[TELEGRAM] Sending to company {company_id}: {url}")

            resp = requests.post(url, json={
                'chat_id': settings.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=10)

            if resp.ok:
                print(f"[TELEGRAM] Message sent successfully to company {company_id}")
                return True
            else:
                print(f"[TELEGRAM] Failed to send to company {company_id}: {resp.status_code} - {resp.text}")
                return False

    except Exception as e:
        print(f"[TELEGRAM] Exception sending to company {company_id}: {e}")
        return False


def send_company_telegram_message_with_details(company_id: int, text: str) -> tuple[bool, str]:
    """Send Telegram message using company-specific settings with error details"""
    try:
        from app import create_app
        from models import CompanyTelegramSetting, db
        app = create_app()
        with app.app_context():
            settings = CompanyTelegramSetting.query.filter_by(company_id=company_id).first()
            if not settings:
                return False, f'No Telegram settings found for company {company_id}'

            if not settings.enabled:
                return False, f'Telegram disabled for company {company_id}'

            if not settings.bot_token or not settings.chat_id:
                return False, f'Missing bot token or chat ID for company {company_id}'

            url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
            print(f"[TELEGRAM] Testing message to company {company_id}")

            resp = requests.post(url, json={
                'chat_id': settings.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=10)

            if resp.ok:
                print(f"[TELEGRAM] Test message sent successfully to company {company_id}")
                return True, 'Message sent successfully'
            else:
                error_msg = f"HTTP {resp.status_code}: {resp.text}"
                print(f"[TELEGRAM] Failed to send test message to company {company_id}: {error_msg}")
                return False, error_msg

    except Exception as exc:
        error_msg = f'Exception: {exc}'
        print(f"[TELEGRAM] Exception testing message to company {company_id}: {error_msg}")
        return False, error_msg


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


