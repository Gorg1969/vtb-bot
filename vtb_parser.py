# vtb_parser.py
# ============================================================
# Парсер VTB-лизинга
# - Дедуп ВСЕГДА включён (Google Sheets + БД)
# - Сжатие фото: max 1080px, JPEG quality=85
# - Парсинг кода предложения из span.js-auto-card-title-code
# - info.txt = только для публикации (обрезан по #изъятая)
# - report.txt = полные данные для отчёта
# ============================================================

import os
import re
import json
import time
import shutil
import logging
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import requests
from PIL import Image
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from config import (
    INITIAL_LIMIT, MAX_PHOTOS_PER_AD, MAX_PAGES,
    OUTPUT_DIR, FLAG_IN_STOCK, FLAG_LEASING, FLAG_REPAIR,
    FLAG_BUY_AVAILABLE, PAGE_TIMEOUT, CARD_DELAY, SHEETS_URL,
    get_sections_from_db,
    MIN_PRICE, MAX_PRICE,
)
from sheets_client import SheetsClient
from db import BotDB

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 1080
JPEG_QUALITY = 85


# ============================================================
# Утилиты
# ============================================================

def normalize_text(s: str) -> str:
    if not s:
        return ''
    s = s.replace('\u00a0', ' ').replace('\xa0', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def format_price(price_str: str) -> str:
    if not price_str:
        return ''
    digits = re.sub(r'[^\d\s]', '', price_str)
    return normalize_text(digits)


def detect_category(title: str, section: dict) -> Optional[dict]:
    if section['key_in_title'] is None:
        return section
    title_norm = ' '.join(title.lower().split())
    key_norm = ' '.join(section['key_in_title'].lower().split())
    if key_norm in title_norm:
        return section
    return None


def compress_image(input_path: str,
                    max_size: int = MAX_IMAGE_SIZE,
                    quality: int = JPEG_QUALITY) -> str:
    """Ресайз + JPEG. Возвращает путь к сжатому файлу."""
    try:
        img = Image.open(input_path)

        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        w, h = img.size
        if max(w, h) > max_size:
            if w > h:
                new_w = max_size
                new_h = int(h * max_size / w)
            else:
                new_h = max_size
                new_w = int(w * max_size / h)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info(f'  📐 Ресайз {w}x{h} → {new_w}x{new_h}')

        jpeg_path = input_path.rsplit('.', 1)[0] + '.jpg'
        img.save(jpeg_path, 'JPEG', quality=quality, optimize=True)

        if jpeg_path != input_path and os.path.exists(input_path):
            os.remove(input_path)

        new_size = os.path.getsize(jpeg_path)
        logger.info(f'  🗜️ Сжато → {os.path.basename(jpeg_path)} ({new_size // 1024} КБ)')
        return jpeg_path

    except Exception as e:
        logger.warning(f'⚠️ Ошибка сжатия {input_path}: {e}')
        return input_path


# ============================================================
# Парсер
# ============================================================

class VTBParser:
    def __init__(self, sheets: SheetsClient, db: BotDB, output_dir: str):
        self.sheets = sheets
        self.db = db
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.processed = 0
        self.skipped_dup = 0
        self.skipped_flags = 0
        self.skipped_category = 0
        self.errors = 0

    def collect_urls_from_section(self, page: Page, section: dict,
                                   limit: int) -> List[str]:
        urls = []
        pagen = 1
        max_needed = limit * 3

        while pagen <= MAX_PAGES and len(urls) < max_needed:
            page_url = f"{section['url']}?sort=dateDesc&PAGEN_1={pagen}"
            logger.info(f'📄 Страница {pagen}')

            try:
                page.goto(page_url, wait_until='networkidle', timeout=PAGE_TIMEOUT)
            except PlaywrightTimeout:
                logger.warning(f'⚠️ Таймаут на стр. {pagen}')
                break

            cards = page.query_selector_all('a.t-market-item-slider-item')
            if not cards:
                logger.info(f'⏹️ Стр. {pagen} пустая')
                break

            for card in cards:
                href = card.get_attribute('href')
                if href:
                    if href.startswith('/'):
                        href = 'https://www.vtb-leasing.ru' + href
                    urls.append(href)

            logger.info(f'  → {len(cards)} карточек (всего: {len(urls)})')
            pagen += 1

        return urls

    def is_duplicate(self, url: str) -> bool:
        if self.sheets.is_duplicate(url):
            return True
        if self.db.is_parsed(url):
            return True
        return False

    def parse_card(self, page: Page, url: str) -> Optional[Dict]:
        try:
            page.goto(url, wait_until='networkidle', timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(2000)

            # --- Название ---
            title = ''
            for sel in ['div.t-auto-card-title h1', 'h1.t-auto-card-title', 'h1']:
                el = page.query_selector(sel)
                if el:
                    title = normalize_text(el.inner_text())
                    if title:
                        break

            # --- Код предложения (span с классом БЕЗ -text) ---
            code = ''
            for sel in ['.js-auto-card-title-code',
                        'span.js-auto-card-title-code',
                        'span[class*="js-auto-card-title-code"]']:
                el = page.query_selector(sel)
                if el:
                    code = normalize_text(el.inner_text())
                    if code:
                        break

            # --- Цена ---
            price_el = page.query_selector('div.t-auto-card-price')
            price_raw = normalize_text(price_el.inner_text()) if price_el else ''
            price = format_price(price_raw)

            # --- Характеристики ---
            city = year = mileage = ''
            items = page.query_selector_all(
                'div.t-tab-content.active div.t-tab-content-column-item'
            )
            for item in items:
                try:
                    label_el = item.query_selector('div:first-child span')
                    value_el = item.query_selector('div:last-child')
                    if not label_el or not value_el:
                        continue
                    label = normalize_text(label_el.inner_text()).lower()
                    value = normalize_text(value_el.inner_text())

                    if 'город' in label and not city:
                        city = value
                    elif 'год' in label and not year:
                        year = value
                    elif 'пробег' in label and not mileage:
                        mileage = value
                except Exception:
                    continue

            # --- Флаги ---
            flags = set()
            for el in page.query_selector_all('div.t-market-item-flags-item'):
                cls = el.get_attribute('class') or ''
                if FLAG_IN_STOCK in cls:
                    flags.add('in_stock')
                if FLAG_LEASING in cls:
                    flags.add('leasing')
                if FLAG_BUY_AVAILABLE in cls:
                    flags.add('buy_available')
                if FLAG_REPAIR in cls:
                    flags.add('repair')

            # --- Фото ---
            photos = []
            for slider in page.query_selector_all('div.t-main-slider-slide[data-images]'):
                data_images = slider.get_attribute('data-images')
                if not data_images:
                    continue
                try:
                    data_images = data_images.replace('&quot;', '"')
                    for p in json.loads(data_images):
                        full = ('https://www.vtb-leasing.ru' + p
                                if p.startswith('/') else p)
                        if full not in photos:
                            photos.append(full)
                except Exception as e:
                    logger.warning(f'⚠️ data-images: {e}')

            return {
                'source_url': url,
                'title': title,
                'code': code,
                'price': price,
                'city': city,
                'year': year,
                'mileage': mileage,
                'flags': list(flags),
                'photos': photos[:MAX_PHOTOS_PER_AD],
            }

        except Exception as e:
            logger.error(f'❌ Ошибка парсинга {url}: {e}')
            return None

    @staticmethod
    def check_flags(flags: List[str]) -> Tuple[bool, str]:
        if 'repair' in flags:
            return False, 'есть "требует ремонта"'
        if 'leasing' not in flags:
            return False, 'нет "доступно в лизинг"'
        if 'in_stock' not in flags:
            return False, 'нет "в наличии"'
        return True, 'OK'

    def save_ad_media(self, ad: Dict, index: int, section: dict) -> Optional[str]:
        cat_clean = section['name'].replace('truck_', '')
        folder_name = f'{index}_{cat_clean}'
        folder_path = os.path.join(self.output_dir, folder_name)

        counter = 1
        while os.path.exists(folder_path):
            folder_path = os.path.join(self.output_dir, f'{folder_name}_{counter}')
            counter += 1
        os.makedirs(folder_path, exist_ok=True)

        # info.txt — для публикации в MAX
        with open(os.path.join(folder_path, 'info.txt'), 'w', encoding='utf-8') as f:
            f.write(self._build_info_text(ad))

        # report.txt — полные данные для отчёта
        with open(os.path.join(folder_path, 'report.txt'), 'w', encoding='utf-8') as f:
            f.write(self._build_report_text(ad))

        downloaded = 0
        for i, photo_url in enumerate(ad['photos'], 1):
            ext = Path(photo_url).suffix or '.webp'
            if '?' in ext:
                ext = ext.split('?')[0]
            if not ext or len(ext) > 5:
                ext = '.webp'

            temp_path = os.path.join(folder_path, f'photo_{i}_temp{ext}')

            if self._download_file(photo_url, temp_path, min_size=10000):
                jpeg_path = compress_image(temp_path)
                final_path = os.path.join(folder_path, f'photo_{i}.jpg')
                if jpeg_path != final_path:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(jpeg_path, final_path)
                downloaded += 1
            else:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                logger.warning(f'  ⚠️ Фото {i} не скачалось')

            time.sleep(0.3)

        if downloaded == 0:
            shutil.rmtree(folder_path)
            logger.warning(f'⚠️ Нет фото, папка {folder_name} удалена')
            return None

        logger.info(f'  💾 {folder_name} ({downloaded} фото)')
        return folder_path

    def _download_file(self, url: str, filepath: str, min_size: int = 0) -> bool:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.vtb-leasing.ru/',
            }
            resp = requests.get(url, headers=headers, timeout=60,
                                stream=True, verify=False)
            if resp.status_code != 200:
                return False
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            if min_size and os.path.getsize(filepath) < min_size:
                os.remove(filepath)
                return False
            return True
        except Exception as e:
            logger.warning(f'⚠️ Скачивание {url[:60]}: {e}')
            return False

    def _build_info_text(self, ad: Dict) -> str:
        """Текст для публикации в MAX. Обрезан на #изъятая — без служебных данных."""
        return f"""**{ad['title']}**

**Цена в лизинг: {ad['price']} руб с НДС**

*возможна скидка после осмотра*
Изъятая техника ЛК -  ✅

Пробег: {ad['mileage']} км.
Год: {ad['year']}
Место нахождения: {ad['city']}

**За покупкой и согласованием скидки обращайтесь в личные сообщения ⏩️ [Евгений](https://max.ru/u/f9LHodD0cOL4IXTfONTL9Ju-y7ShR5IHNPGNZ1MFYHPHImA10EgOQcxQWto)
Если не отвечаю в течение 30 мин, обратитесь к [Надежда](https://max.ru/u/f9LHodD0cOIcl8J8friWk-iFWzi9jy7lJql-IMInGvvX_hM8s1w4klx-F0k)**

*ПОМОЖЕМ В ПОДБОРЕ ПО ВАШИМ ПОЖЕЛАНИЯМ*

#изъятая #изъятка #конфискат"""

    def _build_report_text(self, ad: Dict) -> str:
        """Полные данные для отчёта."""
        return f"""Название: {ad['title']}
Ссылка: {ad['source_url']}
Код предложения: {ad['code']}
Город: {ad['city']}
Год: {ad['year']}
Пробег: {ad['mileage']}
Цена: {ad['price']} руб"""

    def run(self, limit: int = INITIAL_LIMIT):
        logger.info('=' * 60)
        logger.info(f'🚀 СТАРТ ПАРСИНГА (лимит: {limit})')
        logger.info(f'   Сжатие: max {MAX_IMAGE_SIZE}px, JPEG q={JPEG_QUALITY}')
        logger.info('=' * 60)

        self.sheets.get_all_urls()
        sections = get_sections_from_db(self.db)
        saved_count = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox',
                      '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = context.new_page()

            try:
                for section in sections:
                    if not section.get('enabled', True):
                        continue
                    if saved_count >= limit:
                        break

                    logger.info(f'\n{"=" * 60}')
                    logger.info(f'📂 Раздел: {section["name"]}')
                    logger.info(f'{"=" * 60}')

                    remaining = limit - saved_count
                    urls = self.collect_urls_from_section(page, section, remaining)
                    logger.info(f'📋 Собрано {len(urls)} ссылок')

                    for i, url in enumerate(urls, 1):
                        if saved_count >= limit:
                            logger.info(f'⏹️ Лимит {limit} достигнут')
                            break

                        logger.info(f'\n[{i}/{len(urls)}] {url}')

                        if self.is_duplicate(url):
                            logger.info('  ⏭️ Дубль')
                            self.skipped_dup += 1
                            continue

                        ad = self.parse_card(page, url)
                        if not ad:
                            self.errors += 1
                            continue

                        ok, reason = self.check_flags(ad['flags'])
                        if not ok:
                            logger.info(f'  ⏭️ Флаги: {reason} ({ad["flags"]})')
                            self.skipped_flags += 1
                            continue
                        logger.info(f'  ✅ Флаги ОК: {ad["flags"]}')

                        logger.info(f'  📝 Название: "{ad["title"][:80]}"')
                        logger.info(f'  📋 Код: "{ad["code"]}"')

                        detected = detect_category(ad['title'], section)
                        if not detected:
                            logger.info(f'  ⏭️ Не подходит под "{section["key_in_title"]}"')
                            self.skipped_category += 1
                            continue

                        ad['category'] = detected['name']
                        ad['chat_id'] = detected['chat_id']
                        logger.info(f'  📂 {detected["name"]} → {detected["chat_id"]}')

                        self.processed += 1
                        folder = self.save_ad_media(ad, self.processed, detected)
                        if not folder:
                            self.errors += 1
                            continue

                        ad['folder_name'] = os.path.basename(folder)
                        ad['media_path'] = folder
                        inserted = self.db.add_parsed_ad(ad)

                        if inserted:
                            saved_count += 1
                            logger.info(f'  ✅ В очередь ({saved_count}/{limit})')
                        else:
                            logger.info(f'  ⚠️ Уже был в БД')

                        time.sleep(CARD_DELAY)

            finally:
                browser.close()

        logger.info('\n' + '=' * 60)
        logger.info('📊 ИТОГИ ПАРСИНГА:')
        logger.info(f'  ✅ Обработано: {self.processed}')
        logger.info(f'  ⏭️ Дублей: {self.skipped_dup}')
        logger.info(f'  🚫 Флаги: {self.skipped_flags}')
        logger.info(f'  📂 Категория: {self.skipped_category}')
        logger.info(f'  ❌ Ошибок: {self.errors}')
        logger.info(f'  💾 В очередь: {saved_count}')
        logger.info('=' * 60)

        stats = self.db.count_by_status()
        logger.info(f'📊 Очередь: {stats}')
        return saved_count


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description='VTB Parser')
    ap.add_argument('--limit', type=int, default=INITIAL_LIMIT)
    args = ap.parse_args()

    sheets = SheetsClient(url=SHEETS_URL)
    from config import DB_PATH
    db = BotDB(DB_PATH)

    parser = VTBParser(sheets, db, OUTPUT_DIR)
    saved = parser.run(limit=args.limit)
    logger.info(f'🎯 Готово. Новых: {saved}')


if __name__ == '__main__':
    main()
