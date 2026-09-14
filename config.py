# config.py
# ============================================================
# Общие настройки vtb-bot
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()

# === MAX Bot ===
TOKEN = os.environ.get("MAX_TOKEN") or os.environ.get("MAX_BOT_TOKEN") or os.environ.get("TOKEN")
BASE_URL = "https://platform-api2.max.ru"

# === Flask ===
SECRET_KEY = os.environ.get("SECRET_KEY", "dev_secret_key_change_me")
PORT = int(os.environ.get("PORT", 3000))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://vtb.bothost.tech")

# === Google Apps Script ===
SHEETS_URL = os.environ.get("SHEETS_URL", "")

# === Пути ===
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/app/data/VTB_Объявления")
DB_PATH = os.environ.get("DB_PATH", "/app/data/vtb_parser.db")
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# === Разделы сайта VTB (значения по умолчанию) ===
# chat_id можно переопределить в админке (хранятся в БД)
SECTIONS = [
    {
        'name': 'car',
        'title': 'Легковые',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/car/',
        'chat_id': '-73112487086609',
        'key_in_title': None,
        'enabled': True,
    },
    {
        'name': 'truck_samosval',
        'title': 'Самосвалы',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/truck/',
        'chat_id': '-73112596204049',
        'key_in_title': 'самосвал',
        'enabled': True,
    },
    {
        'name': 'truck_sedelny',
        'title': 'Седельные тягачи',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/truck/',
        'chat_id': '-69959827081745',
        'key_in_title': 'седельный тягач',
        'enabled': True,
    },
]

# === Лимиты ===
INITIAL_LIMIT = 300
MAX_PHOTOS_PER_AD = 5
MAX_PAGES = 200

# === Флаги (CSS-классы на карточке VTB) ===
FLAG_IN_STOCK = 't-in_stock'
FLAG_LEASING = 't-leasing'
FLAG_BUY_AVAILABLE = 't-buy-available'
FLAG_REPAIR = 't-repair'

# === Расписание (значения по умолчанию) ===
SCHEDULE_START = "06:00"
SCHEDULE_END = "20:00"
DAILY_LIMIT = 150

# === Таймауты ===
PAGE_TIMEOUT = 60000
CARD_DELAY = 0.5


# ============================================================
# Получение разделов с подстановкой chat_id из БД
# ============================================================

def get_sections_from_db(db=None):
    """
    Возвращает SECTIONS с chat_id, переопределёнными из БД.
    Если db не передан или значение не задано — берём из config.py.
    """
    sections = [dict(s) for s in SECTIONS]  # копия

    if db is None:
        return sections

    for s in sections:
        key = f"chat_id_{s['name']}"
        db_value = db.get_setting(key)
        if db_value:
            s['chat_id'] = db_value

    return sections


def get_schedule_from_db(db=None):
    """Расписание + лимит из БД, fallback — из config."""
    result = {
        'start': SCHEDULE_START,
        'end': SCHEDULE_END,
        'daily_limit': DAILY_LIMIT,
    }
    if db is None:
        return result

    start = db.get_setting('schedule_start')
    end = db.get_setting('schedule_end')
    limit = db.get_setting('daily_limit')

    if start:
        result['start'] = start
    if end:
        result['end'] = end
    if limit:
        try:
            result['daily_limit'] = int(limit)
        except ValueError:
            pass

    return result
