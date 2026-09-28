# import_folder.py
# ============================================================
# Импорт папок с объявлениями из ZIP-архива.
#
# Логика:
#   - Пользователь загружает ZIP с ОДНОЙ головной папкой.
#   - Внутри головной папки — подпапки.
#   - Имя подпапки: "<префикс>_<chat_id>", где chat_id начинается с '-'.
#     Например: "74_-69959827081745".
#   - В каждой подпапке:
#       * info.txt — обязательно (текст объявления)
#       * либо ОДНО видео (.mp4), либо несколько фото (.jpg/.png/...)
#
# Что делает:
#   - Распаковывает ZIP во временную папку.
#   - Находит головную папку и все её подпапки.
#   - Для каждой подпапки:
#       * парсит chat_id из имени
#       * проверяет info.txt
#       * проверяет наличие фото ИЛИ видео
#       * парсит info.txt → пост + метаданные для отчёта
#       * копирует подпапку в IMPORT_DIR
#       * добавляет запись в parsed_ads (status='pending')
#   - Возвращает отчёт.
# ============================================================

import os
import re
import shutil
import zipfile
import logging
import tempfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Куда копировать импортированные объявления
IMPORT_DIR = '/app/data/import_ads'

# Расширения
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm'}

# Категория для импорта (не входит в CATEGORY_GROUPS → в конец очереди)
IMPORT_CATEGORY = 'import'


# ============================================================
# Парсинг info.txt
# ============================================================

def parse_info_txt(text: str) -> Dict:
    """
    Разбирает info.txt на:
      - post_text: всё до строки "#изъятая"
      - title: первая строка (вместе с **)
      - source_url: значение "Ссылка: ..." (после #изъятая)
      - code: значение "Код предложения: ..." (после #изъятая)
    """
    result = {
        'post_text': '',
        'title': '',
        'source_url': '',
        'code': '',
    }

    if not text:
        return result

    # Заголовок = первая непустая строка
    for line in text.splitlines():
        s = line.strip()
        if s:
            result['title'] = s
            break

    # Отделяем основную часть от служебной
    # Служебная начинается с "#изъятая #изъятка #конфискат"
    # (или похожего набора хэштегов)
    marker = '#изъятая'
    idx = text.find(marker)
    if idx == -1:
        # Нет маркера → всё считаем постом
        result['post_text'] = text.strip()
        return result

    # Основная часть — до маркера, но перед ним обычно пустая строка
    main_part = text[:idx].rstrip()
    result['post_text'] = main_part

    # Служебная часть — после маркера
    service_part = text[idx:]

    # "Ссылка: ..."
    m = re.search(r'Ссылка:\s*(\S+)', service_part, re.IGNORECASE)
    if m:
        result['source_url'] = m.group(1).strip()

    # "Код предложения: ..."
    m = re.search(r'Код предложения:\s*(.+)', service_part, re.IGNORECASE)
    if m:
        result['code'] = m.group(1).strip()

    return result


# ============================================================
# Парсинг chat_id из имени подпапки
# ============================================================

def extract_chat_id(folder_name: str) -> Optional[str]:
    """
    Имя папки: "<префикс>_<chat_id>", chat_id начинается с '-'.
    Например: "74_-69959827081745" → "-69959827081745".
    Возвращает None, если chat_id не найден.
    """
    if not folder_name or '_' not in folder_name:
        return None

    # Часть после последнего "_"
    tail = folder_name.rsplit('_', 1)[-1].strip()

    # Должна начинаться с '-' и содержать только цифры после
    if not tail.startswith('-'):
        return None
    if not tail[1:].isdigit():
        return None

    return tail


def extract_prefix(folder_name: str) -> Optional[int]:
    """
    Префикс — число до первого '_'.
    "74_-69959827081745" → 74.
    Если не число — None.
    """
    if not folder_name or '_' not in folder_name:
        return None
    head = folder_name.split('_', 1)[0].strip()
    if head.isdigit():
        return int(head)
    return None


# ============================================================
# Анализ содержимого подпапки
# ============================================================

def analyze_subfolder(folder_path: str) -> Dict:
    """
    Проверяет содержимое подпапки.
    Возвращает:
      {
        'ok': bool,
        'reason': str,
        'info_path': str,
        'photos': [str, ...],   # имена файлов
        'videos': [str, ...],
        'media_type': 'image' | 'video' | None,
      }
    """
    result = {
        'ok': False,
        'reason': '',
        'info_path': '',
        'photos': [],
        'videos': [],
        'media_type': None,
    }

    if not os.path.isdir(folder_path):
        result['reason'] = 'не папка'
        return result

    try:
        files = os.listdir(folder_path)
    except Exception as e:
        result['reason'] = f'ошибка чтения: {e}'
        return result

    info_files = [f for f in files if f.lower() == 'info.txt']
    if not info_files:
        result['reason'] = 'нет info.txt'
        return result
    result['info_path'] = os.path.join(folder_path, info_files[0])

    photos = []
    videos = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in PHOTO_EXTS:
            photos.append(f)
        elif ext in VIDEO_EXTS:
            videos.append(f)

    photos.sort()
    videos.sort()

    result['photos'] = photos
    result['videos'] = videos

    if videos and photos:
        # Смешанные — приоритет видео, но помечаем
        result['media_type'] = 'video'
        result['reason'] = 'смешанные фото+видео (берём видео)'
        result['ok'] = True
    elif videos:
        if len(videos) > 1:
            result['media_type'] = 'video'
            result['reason'] = f'несколько видео ({len(videos)}) — берём первое'
            result['ok'] = True
        else:
            result['media_type'] = 'video'
            result['ok'] = True
    elif photos:
        result['media_type'] = 'image'
        result['ok'] = True
    else:
        result['reason'] = 'нет ни фото, ни видео'
        return result

    return result


# ============================================================
# Основная функция импорта
# ============================================================

def import_zip(zip_path: str, bot_db, output_dir: str = IMPORT_DIR) -> Dict:
    """
    Импортирует ZIP с одной головной папкой.
    Возвращает отчёт:
      {
        'total': int,             # всего подпапок найдено
        'imported': int,          # добавлено в очередь
        'skipped': int,
        'errors': int,
        'details': [
            {'folder': str, 'status': 'ok'|'skip'|'error', 'reason': str},
            ...
        ]
      }
    """
    report = {
        'total': 0,
        'imported': 0,
        'skipped': 0,
        'errors': 0,
        'details': [],
    }

    os.makedirs(output_dir, exist_ok=True)

    # Временная папка для распаковки
    tmp_dir = tempfile.mkdtemp(prefix='import_zip_')

    try:
        # === 1. Распаковка ===
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(tmp_dir)
        except Exception as e:
            logger.error(f'❌ Ошибка распаковки: {e}')
            report['errors'] += 1
            report['details'].append({
                'folder': '(архив)',
                'status': 'error',
                'reason': f'ошибка распаковки: {e}',
            })
            return report

        # === 2. Поиск головной папки ===
        # Головная — единственная папка верхнего уровня.
        # Если файлы лежат прямо в корне zip — берём корень.
        top_entries = os.listdir(tmp_dir)
        top_dirs = [
            d for d in top_entries
            if os.path.isdir(os.path.join(tmp_dir, d))
        ]
        top_files = [
            f for f in top_entries
            if os.path.isfile(os.path.join(tmp_dir, f))
        ]

        # Пропускаем служебные файлы macOS
        top_dirs = [d for d in top_dirs if not d.startswith('__MACOSX')]

        if len(top_dirs) == 1 and not top_files:
            head_dir = os.path.join(tmp_dir, top_dirs[0])
            logger.info(f'📁 Головная папка: {top_dirs[0]}')
        elif len(top_dirs) == 0 and top_files:
            # Файлы в корне — считаем корень головной
            head_dir = tmp_dir
            logger.info('📁 Головная папка: (корень архива)')
        elif len(top_dirs) >= 1:
            # Несколько папок — берём первую, но предупреждаем
            head_dir = os.path.join(tmp_dir, top_dirs[0])
            logger.warning(
                f'⚠️ В архиве несколько папок верхнего уровня, '
                f'берём первую: {top_dirs[0]}'
            )
        else:
            report['details'].append({
                'folder': '(архив)',
                'status': 'error',
                'reason': 'пустой архив',
            })
            report['errors'] += 1
            return report

        # === 3. Обход подпапок головной папки ===
        try:
            subfolders = [
                d for d in os.listdir(head_dir)
                if os.path.isdir(os.path.join(head_dir, d))
                and not d.startswith('__MACOSX')
                and not d.startswith('.')
            ]
        except Exception as e:
            report['details'].append({
                'folder': '(головная)',
                'status': 'error',
                'reason': f'ошибка чтения: {e}',
            })
            report['errors'] += 1
            return report

        if not subfolders:
            report['details'].append({
                'folder': os.path.basename(head_dir),
                'status': 'error',
                'reason': 'в головной папке нет подпапок',
            })
            report['errors'] += 1
            return report

        # Сортируем по префиксу (числовая сортировка)
        def sort_key(name):
            p = extract_prefix(name)
            if p is None:
                return (1, name)  # без префикса — в конец
            return (0, p, name)

        subfolders.sort(key=sort_key)

        report['total'] = len(subfolders)
        logger.info(f'📊 Найдено подпапок: {len(subfolders)}')

        # === 4. Обработка каждой подпапки ===
        for sub_name in subfolders:
            sub_path = os.path.join(head_dir, sub_name)

            # --- chat_id ---
            chat_id = extract_chat_id(sub_name)
            if not chat_id:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'skip',
                    'reason': 'некорректное имя (нужно: <префикс>_<-chat_id>)',
                })
                continue

            # --- содержимое ---
            analysis = analyze_subfolder(sub_path)
            if not analysis['ok']:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'skip',
                    'reason': analysis['reason'],
                })
                continue

            # --- info.txt ---
            try:
                with open(analysis['info_path'], 'r', encoding='utf-8') as f:
                    info_text = f.read()
            except Exception as e:
                report['errors'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'error',
                    'reason': f'не читается info.txt: {e}',
                })
                continue

            parsed = parse_info_txt(info_text)
            if not parsed['post_text'] or not parsed['title']:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'skip',
                    'reason': 'info.txt пустой или без заголовка',
                })
                continue

            # --- копирование в IMPORT_DIR ---
            target_name = sub_name
            target_path = os.path.join(output_dir, target_name)
            counter = 1
            while os.path.exists(target_path):
                target_name = f'{sub_name}_{counter}'
                target_path = os.path.join(output_dir, target_name)
                counter += 1

            try:
                shutil.copytree(sub_path, target_path)
            except Exception as e:
                report['errors'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'error',
                    'reason': f'ошибка копирования: {e}',
                })
                continue

            # --- добавляем в БД ---
            ad_data = {
                'source_url': f'import://{target_name}',
                'title': parsed['title'],
                'code': parsed['code'],
                'price': '',
                'city': '',
                'year': '',
                'mileage': '',
                'category': IMPORT_CATEGORY,
                'chat_id': chat_id,
                'folder_name': target_name,
                'media_path': target_path,
            }

            try:
                inserted = bot_db.add_parsed_ad(ad_data)
            except Exception as e:
                # Если не вставилось — чистим скопированное
                shutil.rmtree(target_path, ignore_errors=True)
                report['errors'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'error',
                    'reason': f'ошибка БД: {e}',
                })
                continue

            if not inserted:
                # Уже была такая запись — чистим
                shutil.rmtree(target_path, ignore_errors=True)
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name,
                    'status': 'skip',
                    'reason': 'уже есть в БД',
                })
                continue

            report['imported'] += 1
            report['details'].append({
                'folder': sub_name,
                'status': 'ok',
                'reason': f"{analysis['media_type']} → {chat_id}",
            })

        logger.info(
            f'✅ Импорт завершён: '
            f'импортировано {report["imported"]}, '
            f'пропущено {report["skipped"]}, '
            f'ошибок {report["errors"]}'
        )

        return report

    finally:
        # === 5. Чистим временную папку ===
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
