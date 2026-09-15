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

# === Разделы сайта VTB ===
SECTIONS = [
    {
        'name': 'truck_samosval',
        'title': 'Самосвалы',
        'url': 'https://www.vtb-leasing.ru/auto-market/f/type-is-2/subtype_truck-is-569a697f8a2174b34fadc4bfcf45dd51/',
        'chat_id': '-73112596204049',
        'enabled': True,
    },
    {
        'name': 'truck_sedelny',
        'title': 'Седельные тягачи',
        'url': 'https://www.vtb-leasing.ru/auto-market/f/type-is-2/subtype_truck-is-0f039d32dc77df2bac4071f3956c09ca/',
        'chat_id': '-69959827081745',
        'enabled': True,
    },
    {
        'name': 'buldozer',
        'title': 'Бульдозеры',
        'url': 'https://www.vtb-leasing.ru/auto-market/f/type-is-6/subtype_special-is-b1e156e60e6bc31171c4089dbfa293eb/',
        'chat_id': '-73112403724817',
        'enabled': True,
    },
    {
        'name': 'excavator',
        'title': 'Экскаваторы',
        'url': 'https://www.vtb-leasing.ru/auto-market/f/type-is-6/subtype_special-is-4b49d2ec4e6a23d1e277c2a3eaf893ee/',
        'chat_id': '-73112403724817',
        'enabled': True,
    },
    {
        'name': 'grader',
        'title': 'Грейдеры',
        'url': 'https://www.vtb-leasing.ru/auto-market/f/type-is-6/subtype_special-is-e063efe897e18a8291c411cf537b1fd1/',
        'chat_id': '-73112403724817',
        'enabled': True,
    },
]

# === Лимиты ===
INITIAL_LIMIT = 300
MAX_PHOTOS_PER_AD = 5
MAX_PAGES = 200

# === Фильтр по цене ===
MIN_PRICE = 2_500_000
MAX_PRICE = 0

# === Флаги ===
FLAG_IN_STOCK = 't-in_stock'
FLAG_LEASING = 't-leasing'
FLAG_BUY_AVAILABLE = 't-buy-available'
FLAG_REPAIR = 't-repair'

# === Расписание ===
SCHEDULE_START = "06:00"
SCHEDULE_END = "20:00"
DAILY_LIMIT = 150

# === Таймауты ===
PAGE_TIMEOUT = 60000
CARD_DELAY = 0.5

# === Закраска номеров ===
MASK_PLATES = True                                      # ← ВКЛЮЧАЕМ
PLATE_MODEL_PATH = '/app/yolov8_plate_fp16.onnx'        # ← наш ONNX-файл
PLATE_CONFIDENCE = 0.4                                  # порог уверенности
PLATE_PADDING = 3                                       # отступ (пиксели)


def get_sections_from_db(db=None):
    sections = [dict(s) for s in SECTIONS]
    if db is None:
        return sections
    for s in sections:
        key = f"chat_id_{s['name']}"
        db_value = db.get_setting(key)
        if db_value:
            s['chat_id'] = db_value
    return sections


def get_schedule_from_db(db=None):
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
