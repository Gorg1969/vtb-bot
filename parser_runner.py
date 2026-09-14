# parser_runner.py
# ============================================================
# Обёртка для запуска парсера
# Дедуп ВСЕГДА включён
# ============================================================

import os
import sys
import logging
import argparse
from datetime import datetime

os.environ.setdefault('TZ', 'Europe/Moscow')
try:
    import time
    time.tzset()
except AttributeError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def run_parser(limit: int = 300) -> int:
    """Запускает парсер. Дедуп всегда включён."""
    from config import SHEETS_URL, DB_PATH, OUTPUT_DIR
    from sheets_client import SheetsClient
    from db import BotDB
    from vtb_parser import VTBParser

    logger.info('=' * 60)
    logger.info(f'🚀 ЗАПУСК ПАРСЕРА  (лимит: {limit})')
    logger.info(f'   Время: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'   Дедуп: ВКЛ (Google Sheets + БД)')
    logger.info('=' * 60)

    sheets = SheetsClient(url=SHEETS_URL)
    db = BotDB(DB_PATH)
    parser = VTBParser(sheets, db, OUTPUT_DIR)

    try:
        saved = parser.run(limit=limit)
        logger.info(f'✅ Парсер завершён. Новых: {saved}')
        return saved
    except Exception as e:
        logger.exception(f'❌ Парсер упал: {e}')
        return 0


def main():
    ap = argparse.ArgumentParser(description='Запуск VTB-парсера')
    ap.add_argument('--limit', type=int, default=300,
                    help='Сколько новых объявлений набрать')
    args = ap.parse_args()

    saved = run_parser(limit=args.limit)
    sys.exit(0 if saved >= 0 else 1)


if __name__ == '__main__':
    main()
