# config.py
# ============================================================
# Общие настройки vtb-bot
# Все секреты — из переменных окружения Bothost
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

# === Google Apps Script (для дедупа) ===
SHEETS_URL = os.environ.get("SHEETS_URL", "")

# === Пути ===
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/app/VTB_Объявления")
DB_PATH = os.environ.get("DB_PATH", "/app/data/vtb_parser.db")
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# === Разделы сайта VTB → группы MAX ===
# Эти значения по умолчанию.
# В админке их можно переопределить (хранятся в БД).
SECTIONS = [
    {
        'name': 'car',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/car/',
        'chat_id': '-73112487086609',  # тестовый, заменить в админке
        'key_in_title': None,
        'enabled': True,
    },
    {
        'name': 'truck_samosval',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/truck/',
        'chat_id': '-73112596204049',
        'key_in_title': 'самосвал',
        'enabled': True,
    },
    {
        'name': 'truck_sedelny',
        'url': 'https://www.vtb-leasing.ru/auto-market/sale/truck/',
        'chat_id': '-69959827081745',
        'key_in_title': 'седельный тягач',
        'enabled': True,
    },
]

# === Лимиты парсинга ===
INITIAL_LIMIT = 300           # первый запуск (набрать)
DAILY_LIMIT = 150             # в день на публикацию
MAX_PHOTOS_PER_AD = 5         # фото на объявление
MAX_PAGES = 200               # защита от бесконечного цикла

# === Флаги публикации (CSS-классы на карточке VTB) ===
FLAG_IN_STOCK = 't-in_stock'         # "В наличии" / "Лизинг"
FLAG_LEASING = 't-leasing'           # "Доступно в лизинг"
FLAG_BUY_AVAILABLE = 't-buy-available'  # "Доступно для покупки"
FLAG_REPAIR = 't-repair'             # "Требует ремонта" → НЕ публикуем

# === Расписание публикаций (МСК) ===
SCHEDULE_START = "06:00"      # начало
SCHEDULE_END = "20:00"        # конец

# === Таймауты парсера ===
PAGE_TIMEOUT = 60000          # 60 сек
CARD_DELAY = 0.5              # пауза между карточками
