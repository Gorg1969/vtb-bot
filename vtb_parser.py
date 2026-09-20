# vtb_parser.py
# ============================================================
# Парсер VTB-лизинга
# - Дедуп ВСЕГДА включён (Google Sheets + БД)
# - Сжатие фото: max 1080px, JPEG quality=85
# - Фильтр по цене: MIN_PRICE <= цена
# - Категория по URL раздела
# - Название обрезается перед годом
# - Пагинация: <base>/ для стр.1, <base>/?PAGEN_1=N для N>=2
# - info.txt = для публикации, report.txt = для отчёта
# - ПОСТРАНИЧНЫЙ ОБХОД: стр.1 всех категорий → стр.2 всех → ...
# - Стоп: когда ВСЕ категории вернули пустую страницу
# - ПЕРЕЗАПУСК БРАУЗЕРА каждые 20 карточек (борьба с OOM)
# - gc.collect() после каждой карточки
# - ОТКЛОНЯЕМ объявления БЕЗ пробега И БЕЗ моточасов
# - Парсим моточасы (для спецтехники)
# ============================================================

import os
import re
import gc
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
    MASK_PLATES, PLATE_MODEL_PATH, PLATE_CONFIDENCE, PLATE_PADDING,
)
from sheets_client import SheetsClient
from db import BotDB

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

try:
    from plate_mask import mask_plate
    MASK_AVAILABLE = True
except ImportError:
    MASK_AVAILABLE = False
    logger.warning('⚠️ plate_mask недоступен — закраска отключена')

    def mask_plate(image_bytes, **kwargs):
        return image_bytes

MAX_IMAGE_SIZE = 1080
JPEG_QUALITY = 85

# === Перезапуск браузера ===
CARDS_BEFORE_RESTART = 20


# ============================================================
# Утилиты
# ============================================================

def normalize_text(s: str) -> str:
    if not s:
        return ''
    s = s.replace('\u00a0', ' ').replace('\xa0', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def strip_year_from_title(raw_title: str) -> str:
    if not raw_title:
        return ''
    match = re.search(r'\s+(19|20)\d{2}\s*г\.', raw_title)
    if match:
        return raw_title[:match.start()].strip()
    return raw_title.strip()


def format_price(price_str: str) -> str:
    if not price_str:
        return ''
    digits = re.sub(r'[^\d\s]', '', price_str)
    return normalize_text(digits)


def price_to_int(price_str: str) -> int:
    if not price_str:
        return 0
    digits = re.sub(r'[^\d]', '', str(price_str))
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def compress_image(input_path: str,
                    max_size: int = MAX_IMAGE_SIZE,
                    quality: int = JPEG_QUALITY) -> Optional[str]:
    """
    Сжимает изображение до max_size px и сохраняет как JPEG.
    Возвращает путь к сжатому файлу или None при ошибке.
    ВСЕГДА удаляет исходный файл (input_path), если он отличается от результата.
    """
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

        img.close()

        if jpeg_path != input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass

        new_size = os.path.getsize(jpeg_path)
        logger.info(f'  🗜️ Сжато → {os.path.basename(jpeg_path)} ({new_size // 1024} КБ)')
        return jpeg_path

    except Exception as e:
        logger.warning(f'⚠️ Ошибка сжатия {input_path}: {e}')
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
        except Exception:
            pass
        return None


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
        self.skipped_price = 0
        self.skipped_no_mileage = 0
        self.errors = 0

    # --------------------------------------------------------
    # СБОР URL С ОДНОЙ СТРАНИЦЫ
    # --------------------------------------------------------

    def collect_urls_from_one_page(self, page: Page, section: dict,
                                    page_url: str) -> List[str]:
        """
        Собирает URL карточек ТОЛЬКО с одной страницы.
        """
        try:
            page.goto(page_url, wait_until='domcontentloaded', timeout=90000)
        except PlaywrightTimeout:
            logger.warning(f'⚠️ Таймаут на {page_url}')
            return []
        except Exception as e:
            logger.error(f'❌ Ошибка загрузки {page_url}: {e}')
            return []

        try:
            page.wait_for_selector('a.t-market-item-slider-item', timeout=15000)
        except PlaywrightTimeout:
            logger.info(f'⏹️ Карточек нет на {page_url}')
            return []

        page.wait_for_timeout(1000)

        urls = []
        cards = page.query_selector_all('a.t-market-item-slider-item')
        for card in cards:
            href = card.get_attribute('href')
            if href:
                if href.startswith('/'):
                    href = 'https://www.vtb-leasing.ru' + href
                if href not in urls:
                    urls.append(href)

        return urls

    # --------------------------------------------------------
    # ДЕДУП
    # --------------------------------------------------------

    def is_duplicate(self, url: str) -> bool:
        if self.sheets.is_duplicate(url):
            return True
        if self.db.is_parsed(url):
            return True
        return False

    # --------------------------------------------------------
    # ПАРСИНГ ОДНОЙ КАРТОЧКИ
    # --------------------------------------------------------

    def parse_card(self, page: Page, url: str) -> Optional[Dict]:
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=90000)
            page.wait_for_timeout(2000)

            title = ''
            raw_title = ''
            for sel in ['div.t-auto-card-title h1', 'h1.t-auto-card-title', 'h1']:
                el = page.query_selector(sel)
                if el:
                    raw_title = normalize_text(el.inner_text())
                    if raw_title:
                        break

            if raw_title:
                title = strip_year_from_title(raw_title)
                if title != raw_title:
                    logger.info(f'  📝 Название (обрезано): "{title}"')
                else:
                    logger.info(f'  📝 Название: "{title}"')

            code = ''
            for sel in ['.js-auto-card-title-code',
                        'span.js-auto-card-title-code',
                        'span[class*="js-auto-card-title-code"]']:
                el = page.query_selector(sel)
                if el:
                    code = normalize_text(el.inner_text())
                    if code:
                        break

            price_raw = ''
            try:
                page.wait_for_selector(
                    'div.t-auto-card-price, div.t-calculator-card-price',
                    timeout=10000,
                    state='attached'
                )
                page.wait_for_timeout(1500)
            except PlaywrightTimeout:
                logger.warning('  ⚠️ Таймаут ожидания цены (10 сек)')

            for sel in ['div.t-auto-card-price',
                        'div.t-calculator-card-price',
                        'div.t-auto-card-prices__head div',
                        '[class*="card-price"]']:
                el = page.query_selector(sel)
                if not el:
                    continue
                text = normalize_text(el.inner_text())
                if text and re.search(r'\d', text):
                    price_raw = text
                    logger.info(f'  💵 Цена через "{sel}": {price_raw}')
                    break

            price = format_price(price_raw)
            if not price:
                logger.warning('  ⚠️ Цена не найдена')

            # === ПАРСИМ ГОРОД, ГОД, ПРОБЕГ, МОТОЧАСЫ ===
            city = year = mileage = motohours = ''
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
                    elif 'моточас' in label and not motohours:
                        motohours = value
                except Exception:
                    continue

            # === ФЛАГИ ===
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

            # === ФОТО ===
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
                'motohours': motohours,
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

    # --------------------------------------------------------
    # СОХРАНЕНИЕ МЕДИА
    # --------------------------------------------------------

    def save_ad_media(self, ad: Dict, index: int, section: dict) -> Optional[str]:
        cat_clean = section['name'].replace('truck_', '')
        folder_name = f'{index}_{cat_clean}'
        folder_path = os.path.join(self.output_dir, folder_name)

        counter = 1
        while os.path.exists(folder_path):
            folder_path = os.path.join(self.output_dir, f'{folder_name}_{counter}')
            counter += 1
        os.makedirs(folder_path, exist_ok=True)

        with open(os.path.join(folder_path, 'info.txt'), 'w', encoding='utf-8') as f:
            f.write(self._build_info_text(ad))

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
            final_path = os.path.join(folder_path, f'photo_{i}.jpg')

            ok = self._download_file(photo_url, temp_path, min_size=10000)
            if not ok:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                logger.warning(f'  ⚠️ Фото {i} не скачалось')
                continue

            jpeg_path = compress_image(temp_path)
            if not jpeg_path:
                logger.warning(f'  ⚠️ Фото {i} не сжалось')
                continue

            try:
                if jpeg_path != final_path:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(jpeg_path, final_path)
            except Exception as e:
                logger.warning(f'  ⚠️ Не удалось переместить {jpeg_path}: {e}')
                continue

            if MASK_PLATES and MASK_AVAILABLE:
                try:
                    with open(final_path, 'rb') as f:
                        original = f.read()
                    masked = mask_plate(
                        original,
                        model_path=PLATE_MODEL_PATH,
                        confidence=PLATE_CONFIDENCE,
                        padding=PLATE_PADDING,
                    )
                    with open(final_path, 'wb') as f:
                        f.write(masked)
                    del original
                    del masked
                except Exception as e:
                    logger.warning(f'⚠️ Ошибка закраски {final_path}: {e}')

            downloaded += 1
            gc.collect()
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
        """
        Собирает текст поста для публикации.
        Пробег — если есть.
        Моточасы — если есть.
        Если ничего нет — блок с пробегом/моточасами пропускается
        (но такие объявления отсеиваются РАНЬШЕ, в _process_one_url).
        """
        # Формируем блок "Пробег / Моточасы"
        mileage = ad.get('mileage') or ''
        motohours = ad.get('motohours') or ''

        spec_lines = []
        if mileage:
            spec_lines.append(f"Пробег: {mileage} км.")
        if motohours:
            spec_lines.append(f"Моточасы: {motohours}")

        spec_block = '\n'.join(spec_lines)

        return f"""**{ad['title']}**

**Цена в лизинг: {ad['price']} руб с НДС**

*возможна скидка после осмотра*
Изъятая техника ЛК -  ✅

{spec_block}
Год: {ad['year']}
Место нахождения: {ad['city']}

**За покупкой и согласованием скидки обращайтесь в личные сообщения ⏩️ [Евгений](https://max.ru/u/f9LHodD0cOL4IXTfONTL9Ju-y7ShR5IHNPGNZ1MFYHPHImA10EgOQcxQWto)
Если не отвечаю в течение 30 мин, обратитесь к [Надежда](https://max.ru/u/f9LHodD0cOIcl8J8friWk-iFWzi9jy7lJql-IMInGvvX_hM8s1w4klx-F0k)**

*ПОДБОР ТЕХНИКИ ПОД ВАШУ ЗАДАЧУ С ОПЛАТОЙ ЗА РЕЗУЛЬТАТ*

#изъятая #изъятка #конфискат"""

    def _build_report_text(self, ad: Dict) -> str:
        mileage = ad.get('mileage') or ''
        motohours = ad.get('motohours') or ''

        spec_lines = []
        if mileage:
            spec_lines.append(f"Пробег: {mileage}")
        if motohours:
            spec_lines.append(f"Моточасы: {motohours}")
        spec_block = '\n'.join(spec_lines)

        return f"""Название: {ad['title']}
Ссылка: {ad['source_url']}
Код предложения: {ad['code']}
Город: {ad['city']}
Год: {ad['year']}
{spec_block}
Цена: {ad['price']} руб"""

    # --------------------------------------------------------
    # ОБРАБОТКА ОДНОЙ КАРТОЧКИ
    # --------------------------------------------------------

    def _process_one_url(self, page: Page, url: str, section: dict,
                          saved_count: int, limit: int) -> Tuple[bool, int]:
        logger.info(f'\n[{saved_count + 1}/{limit}] {url}')

        if self.is_duplicate(url):
            logger.info('  ⏭️ Дубль')
            self.skipped_dup += 1
            return False, saved_count

        ad = self.parse_card(page, url)
        if not ad:
            self.errors += 1
            return False, saved_count

        # === НОВОЕ: проверка наличия пробега ИЛИ моточасов ===
        mileage = (ad.get('mileage') or '').strip()
        motohours = (ad.get('motohours') or '').strip()

        if not mileage and not motohours:
            logger.info('  ⏭️ Нет ни пробега, ни моточасов — пропуск')
            self.skipped_no_mileage += 1
            return False, saved_count

        if mileage:
            logger.info(f'  🛣️ Пробег: {mileage} км')
        if motohours:
            logger.info(f'  ⏱️ Моточасы: {motohours}')

        # === Флаги ===
        ok, reason = self.check_flags(ad['flags'])
        if not ok:
            logger.info(f'  ⏭️ Флаги: {reason} ({ad["flags"]})')
            self.skipped_flags += 1
            return False, saved_count
        logger.info(f'  ✅ Флаги ОК: {ad["flags"]}')

        # === Цена ===
        price_num = price_to_int(ad['price'])
        if MIN_PRICE > 0 and price_num < MIN_PRICE:
            logger.info(f'  ⏭️ Цена {price_num:,} < {MIN_PRICE:,} — пропуск'.replace(',', ' '))
            self.skipped_price += 1
            return False, saved_count
        if MAX_PRICE > 0 and price_num > MAX_PRICE:
            logger.info(f'  ⏭️ Цена {price_num:,} > {MAX_PRICE:,} — пропуск'.replace(',', ' '))
            self.skipped_price += 1
            return False, saved_count
        logger.info(f'  💰 Цена ОК: {price_num:,} ₽'.replace(',', ' '))

        logger.info(f'  📝 Название: "{ad["title"][:80]}"')
        logger.info(f'  📋 Код: "{ad["code"]}"')

        ad['category'] = section['name']
        ad['chat_id'] = section['chat_id']
        logger.info(f'  📂 {section["name"]} → {section["chat_id"]}')

        self.processed += 1
        folder = self.save_ad_media(ad, self.processed, section)
        if not folder:
            self.errors += 1
            return False, saved_count

        ad['folder_name'] = os.path.basename(folder)
        ad['media_path'] = folder
        inserted = self.db.add_parsed_ad(ad)

        if inserted:
            saved_count += 1
            logger.info(f'  ✅ В очередь ({saved_count}/{limit})')
            return True, saved_count
        else:
            logger.info(f'  ⚠️ Уже был в БД')
            return False, saved_count

    # --------------------------------------------------------
    # ГЛАВНЫЙ МЕТОД: ПОСТРАНИЧНЫЙ ОБХОД + ПЕРЕЗАПУСК БРАУЗЕРА
    # --------------------------------------------------------

    def _create_browser(self, p):
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
        return browser, context, page

    def _close_browser(self, browser, context, page):
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        gc.collect()

    def run(self, limit: int = INITIAL_LIMIT):
        logger.info('=' * 60)
        logger.info(f'🚀 СТАРТ ПАРСИНГА (лимит: {limit})')
        logger.info(f'   Сжатие: max {MAX_IMAGE_SIZE}px, JPEG q={JPEG_QUALITY}')
        logger.info(f'   Фильтр цены: MIN={MIN_PRICE:,} MAX={MAX_PRICE or "∞"}'.replace(',', ' '))
        logger.info(f'   Закраска номеров: {"ВКЛ" if MASK_PLATES and MASK_AVAILABLE else "ВЫКЛ"}')
        logger.info(f'   Пагинация: <base>/ для стр.1, <base>/?PAGEN_1=N для N>=2')
        logger.info(f'   Режим: ПОСТРАНИЧНЫЙ (стр.1 всех категорий → стр.2 всех → ...)')
        logger.info(f'   Перезапуск браузера: каждые {CARDS_BEFORE_RESTART} карточек')
        logger.info(f'   Отклоняем: без пробега И без моточасов')
        logger.info(f'   Стоп: когда ВСЕ категории вернули пустую страницу')
        logger.info('=' * 60)

        self.sheets.get_all_urls()
        sections = [s for s in get_sections_from_db(self.db)
                    if s.get('enabled', True)]

        if not sections:
            logger.warning('⚠️ Нет активных разделов')
            return 0

        logger.info(f'📊 Категорий: {len(sections)}')

        saved_count = 0

        with sync_playwright() as p:
            browser, context, page = self._create_browser(p)
            logger.info('✅ Браузер запущен')

            cards_since_restart = 0

            try:
                pagen = 1

                while saved_count < limit and pagen <= MAX_PAGES:
                    logger.info(f'\n{"=" * 60}')
                    logger.info(f'📄 СТРАНИЦА {pagen} ПО ВСЕМ КАТЕГОРИЯМ')
                    logger.info(f'{"=" * 60}')

                    any_cards_on_page = False

                    for section in sections:
                        if saved_count >= limit:
                            break

                        name = section['name']
                        title = section.get('title', name)
                        base_url = section['url']
                        if not base_url.endswith('/'):
                            base_url += '/'

                        if pagen == 1:
                            page_url = base_url
                        else:
                            page_url = f"{base_url}?PAGEN_1={pagen}"

                        logger.info(f'\n📂 {name} ({title}): {page_url}')

                        urls_on_page = self.collect_urls_from_one_page(
                            page, section, page_url
                        )

                        if not urls_on_page:
                            logger.info(f'  ⏹️ {name}: страница пуста — конец категории')
                            continue

                        any_cards_on_page = True

                        logger.info(f'  📋 {len(urls_on_page)} карточек')

                        for url in urls_on_page:
                            if saved_count >= limit:
                                break

                            # === ПЕРЕЗАПУСК БРАУЗЕРА ===
                            if cards_since_restart >= CARDS_BEFORE_RESTART:
                                logger.info(
                                    f'\n♻️ Перезапуск браузера '
                                    f'(обработано {cards_since_restart} карточек с прошлого раза)'
                                )
                                self._close_browser(browser, context, page)

                                browser, context, page = self._create_browser(p)
                                cards_since_restart = 0
                                logger.info('✅ Браузер перезапущен, память освобождена')

                            success, saved_count = self._process_one_url(
                                page, url, section, saved_count, limit
                            )
                            cards_since_restart += 1

                            gc.collect()

                    if not any_cards_on_page:
                        logger.info('\n⏹️ Все категории вернули пустую страницу — стоп')
                        break

                    logger.info(
                        f'\n📊 Итог стр. {pagen}: в очередь {saved_count}/{limit}'
                    )
                    pagen += 1

            finally:
                self._close_browser(browser, context, page)
                logger.info('🛑 Браузер закрыт')

        logger.info('\n' + '=' * 60)
        logger.info('📊 ИТОГИ ПАРСИНГА:')
        logger.info(f'  ✅ Обработано: {self.processed}')
        logger.info(f'  ⏭️ Дублей: {self.skipped_dup}')
        logger.info(f'  🚫 Флаги: {self.skipped_flags}')
        logger.info(f'  💰 Цена не подошла: {self.skipped_price}')
        logger.info(f'  🛣️ Без пробега/моточасов: {self.skipped_no_mileage}')
        logger.info(f'  📂 Категория не определена: {self.skipped_category}')
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
