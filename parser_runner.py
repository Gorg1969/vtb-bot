# parser_runner.py
# ============================================================
# Обёртка для запуска парсера.
# Используется:
#   - вручную: python parser_runner.py --limit 300
#   - из админки: как подпроцесс
#   - по расписанию: cron / APScheduler
# ============================================================

import os
import sys
import logging
import argparse
from datetime import datetime

# Убеждаемся, что TZ=Europe/Moscow
os.environ.setdefault('TZ', 'Europe/Moscow')

try:
    import time
    time.tzset()
except AttributeError:
    pass  # Windows

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def run_parser(limit: int = 300, no_sheets: bool = False) -> int:
    """
    Запускает парсер с указанным лимитом.
    Возвращает количество новых объявлений в очереди.
    """
    from config import SHEETS_URL, DB_PATH, OUTPUT_DIR
    from sheets_client import SheetsClient
    from db import BotDB
    from vtb_parser import VTBParser

    logger.info('=' * 60)
    logger.info(f'🚀 ЗАПУСК ПАРСЕРА  (лимит: {limit})')
    logger.info(f'   Время: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'   Sheets: {"выкл" if no_sheets else "вкл"}')
    logger.info('=' * 60)

    sheets = SheetsClient(url='' if no_sheets else SHEETS_URL)
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
    ap.add_argument('--no-sheets', action='store_true',
                    help='Не использовать Google Sheets')
    args = ap.parse_args()

    saved = run_parser(limit=args.limit, no_sheets=args.no_sheets)
    sys.exit(0 if saved >= 0 else 1)


if __name__ == '__main__':
    main()
