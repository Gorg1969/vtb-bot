# import_folder.py
# ============================================================
# Импорт папок с объявлениями из ZIP-архива.
# ============================================================

import os
import re
import json
import shutil
import zipfile
import logging
import tempfile
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

IMPORT_DIR = '/app/data/import_ads'

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm'}

IMPORT_CATEGORY = 'import'

CHAT_ID_RE = re.compile(r'(-\d{10,})\s*$')


def split_info_txt(text: str) -> Dict:
    """
    Разбирает info.txt на:
      - post_text:   всё до маркера "#изъятая" (без хвоста)
      - title:       первая непустая строка
      - source_url:  значение "Ссылка: ..." (после #изъятая)
      - code:        значение "Код предложения: ..." (после #изъятая)
    """
    result = {'post_text': '', 'title': '', 'source_url': '', 'code': ''}
    if not text:
        return result

    for line in text.splitlines():
        s = line.strip()
        if s:
            result['title'] = s
            break

    marker = '#изъятая'
    idx = text.find(marker)
    if idx == -1:
        result['post_text'] = text.rstrip()
        return result

    result['post_text'] = text[:idx].rstrip()
    service_part = text[idx:]

    m = re.search(r'Ссылка:\s*(\S+)', service_part, re.IGNORECASE)
    if m:
        result['source_url'] = m.group(1).strip()

    m = re.search(r'Код предложения:\s*(.+)', service_part, re.IGNORECASE)
    if m:
        result['code'] = m.group(1).strip()

    return result


def extract_chat_id(folder_name: str) -> Optional[str]:
    """Последнее вхождение -<10+ цифр> в конце имени."""
    if not folder_name:
        return None
    m = CHAT_ID_RE.search(folder_name.strip())
    return m.group(1) if m else None


def extract_prefix(folder_name: str) -> Optional[int]:
    """Число в начале имени."""
    if not folder_name:
        return None
    m = re.match(r'^(\d+)', folder_name.strip())
    return int(m.group(1)) if m else None


def analyze_subfolder(folder_path: str) -> Dict:
    result = {'ok': False, 'reason': '', 'info_path': '',
              'photos': [], 'videos': [], 'media_type': None}

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

    photos, videos = [], []
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

    if videos:
        result['media_type'] = 'video'
        result['ok'] = True
    elif photos:
        result['media_type'] = 'image'
        result['ok'] = True
    else:
        result['reason'] = 'нет ни фото, ни видео'

    return result


def import_zip(zip_path: str, bot_db, output_dir: str = IMPORT_DIR) -> Dict:
    report = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0, 'details': []}
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix='import_zip_')

    try:
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(tmp_dir)
        except Exception as e:
            report['errors'] += 1
            report['details'].append({
                'folder': '(архив)', 'status': 'error',
                'reason': f'ошибка распаковки: {e}'})
            return report

        top_entries = os.listdir(tmp_dir)
        top_dirs = [d for d in top_entries if os.path.isdir(os.path.join(tmp_dir, d))]
        top_files = [f for f in top_entries if os.path.isfile(os.path.join(tmp_dir, f))]
        top_dirs = [d for d in top_dirs if not d.startswith('__MACOSX')]

        if len(top_dirs) == 1 and not top_files:
            head_dir = os.path.join(tmp_dir, top_dirs[0])
        elif len(top_dirs) == 0 and top_files:
            head_dir = tmp_dir
        elif len(top_dirs) >= 1:
            head_dir = os.path.join(tmp_dir, top_dirs[0])
        else:
            report['errors'] += 1
            report['details'].append({
                'folder': '(архив)', 'status': 'error', 'reason': 'пустой архив'})
            return report

        try:
            subfolders = [d for d in os.listdir(head_dir)
                          if os.path.isdir(os.path.join(head_dir, d))
                          and not d.startswith('__MACOSX')
                          and not d.startswith('.')]
        except Exception as e:
            report['errors'] += 1
            report['details'].append({
                'folder': '(головная)', 'status': 'error',
                'reason': f'ошибка чтения: {e}'})
            return report

        if not subfolders:
            report['errors'] += 1
            report['details'].append({
                'folder': os.path.basename(head_dir), 'status': 'error',
                'reason': 'в головной папке нет подпапок'})
            return report

        subfolders.sort(key=lambda n: (extract_prefix(n) is None,
                                       extract_prefix(n) or 0, n))
        report['total'] = len(subfolders)

        for sub_name in subfolders:
            sub_path = os.path.join(head_dir, sub_name)

            chat_id = extract_chat_id(sub_name)
            if not chat_id:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'skip',
                    'reason': 'некорректное имя'})
                continue

            analysis = analyze_subfolder(sub_path)
            if not analysis['ok']:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'skip',
                    'reason': analysis['reason']})
                continue

            try:
                with open(analysis['info_path'], 'r', encoding='utf-8') as f:
                    info_text = f.read()
            except Exception as e:
                report['errors'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'error',
                    'reason': f'не читается info.txt: {e}'})
                continue

            parsed = split_info_txt(info_text)
            if not parsed['post_text'] or not parsed['title']:
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'skip',
                    'reason': 'info.txt пустой'})
                continue

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
                    'folder': sub_name, 'status': 'error',
                    'reason': f'ошибка копирования: {e}'})
                continue

            # report_data.json
            try:
                with open(os.path.join(target_path, 'report_data.json'),
                          'w', encoding='utf-8') as f:
                    json.dump({
                        'title': parsed['title'],
                        'source_url': parsed['source_url'],
                        'code': parsed['code'],
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f'⚠️ Не сохранить report_data.json: {e}')

            ad_data = {
                'source_url': parsed['source_url'] or f'import://{target_name}',
                'title': parsed['title'],
                'code': parsed['code'],
                'price': '', 'city': '', 'year': '', 'mileage': '',
                'category': IMPORT_CATEGORY,
                'chat_id': chat_id,
                'folder_name': target_name,
                'media_path': target_path,
            }

            try:
                inserted = bot_db.add_parsed_ad(ad_data)
            except Exception as e:
                shutil.rmtree(target_path, ignore_errors=True)
                report['errors'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'error',
                    'reason': f'ошибка БД: {e}'})
                continue

            if not inserted:
                shutil.rmtree(target_path, ignore_errors=True)
                report['skipped'] += 1
                report['details'].append({
                    'folder': sub_name, 'status': 'skip',
                    'reason': 'уже есть в БД'})
                continue

            report['imported'] += 1
            report['details'].append({
                'folder': sub_name, 'status': 'ok',
                'reason': f"{analysis['media_type']} → {chat_id}"})

        return report

    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
