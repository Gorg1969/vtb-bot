# app.py
# ============================================================
# vtb-bot — Flask-сервер
# ============================================================

import os
os.environ['TZ'] = 'Europe/Moscow'
import time
try:
    time.tzset()
except AttributeError:
    pass

import gc
import sqlite3
import logging
import shutil
import subprocess
import sys
import urllib3
import threading
import random
import json
import requests
import base64
import io
from functools import wraps
from datetime import datetime, timedelta
import pytz

from PIL import Image  # для поворота фото

from flask import (Flask, request, jsonify, send_file, redirect)

# === Создание видео (OpenCV) ===
try:
    from video_maker import (process_queue_videos, process_one_folder,
                             MOVIEPY_AVAILABLE,
                             MOVIEPY_IMPORT_ERROR,
                             MOVIEPY_VERSION)
    VIDEO_MAKER_AVAILABLE = MOVIEPY_AVAILABLE
except Exception as e:
    VIDEO_MAKER_AVAILABLE = False
    MOVIEPY_IMPORT_ERROR = f"{type(e).__name__}: {e}"
    MOVIEPY_VERSION = None
    _import_logger = logging.getLogger(__name__)
    _import_logger.warning(f'⚠️ video_maker недоступен: {MOVIEPY_IMPORT_ERROR}')
    def process_queue_videos(*a, **k):
        return {'total': 0, 'ok': 0, 'failed': 1, 'skipped': 0,
                'details': [{'id': None, 'folder': '(все)',
                             'status': 'error',
                             'reason': 'video_maker недоступен'}]}
    def process_one_folder(*a, **k):
        return False, 'video_maker недоступен'

from modules import Database, FileManager, Publisher, ReportGenerator
from config import (TOKEN, BASE_URL, SECRET_KEY, PORT, DATA_DIR, PUBLIC_URL,
                    SHEETS_URL, OUTPUT_DIR, DB_PATH,
                    SECTIONS, get_sections_from_db, get_schedule_from_db)
from db import BotDB
from sheets_client import SheetsClient
from parser_runner import run_parser

try:
    from import_folder import import_zip, IMPORT_DIR
    IMPORT_FOLDER_AVAILABLE = True
except ImportError as e:
    IMPORT_FOLDER_AVAILABLE = False
    IMPORT_DIR = '/app/data/import_ads'
    def import_zip(*args, **kwargs):
        raise RuntimeError(f'import_folder.py не найден: {e}')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TOKEN:
    logger.error("❌ ТОКЕН MAX НЕ НАЙДЕН!")

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '')
ALLOWED_ADMIN_IDS = [int(x) for x in (os.environ.get('ADMIN_IDS') or '').split(',')
                     if x.strip().isdigit()]
MOSCOW_TZ = pytz.timezone('Europe/Moscow')


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not ADMIN_PASS:
            return f(*args, **kwargs)
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return ('🔒 Требуется авторизация', 401,
                    {'WWW-Authenticate': 'Basic realm="VTB Admin"'})
        return f(*args, **kwargs)
    return decorated


def is_allowed_user(user_id):
    if not ALLOWED_ADMIN_IDS:
        return True
    return int(user_id) in ALLOWED_ADMIN_IDS


def log_threads(tag=''):
    try:
        logger.info(f'🧵 Потоков [{tag}]: {threading.active_count()}')
    except Exception:
        pass


db = Database()
db.fix_publication_times()
fm = FileManager(DATA_DIR)
bot_db = BotDB(DB_PATH)
sheets = SheetsClient(url=SHEETS_URL)


# ============================================================
# MAX API
# ============================================================

class APIClient:
    def __init__(self):
        self.token = TOKEN
        self.base_url = BASE_URL

    def send_message(self, user_id, text, attachments=None):
        if not self.token:
            return False
        try:
            payload = {"text": text, "format": "markdown"}
            if attachments:
                payload["attachments"] = attachments
            response = requests.post(
                f"{self.base_url}/messages",
                headers={"Authorization": self.token, "Content-Type": "application/json"},
                params={"user_id": user_id},
                json=payload, timeout=30, verify=False)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"❌ send_message: {e}")
            return False

    def upload_file(self, file_bytes, filename='file.bin', file_type='image'):
        if not self.token:
            logger.error('❌ upload_file: нет токена')
            return None
        try:
            logger.info(f'📤 Шаг 1: /uploads для {file_type} {filename} ({len(file_bytes)} байт)')
            r = requests.post(
                f"{self.base_url}/uploads",
                headers={"Authorization": self.token},
                params={"type": file_type},
                timeout=30, verify=False,
            )
            logger.info(f'📨 Шаг 1: HTTP {r.status_code}')
            if r.status_code != 200:
                logger.error(f'❌ Шаг 1: {r.status_code} - {r.text[:300]}')
                return None

            data = r.json()
            upload_url = data.get('url')
            if not upload_url:
                logger.error(f'❌ Шаг 1: нет url в ответе: {data}')
                return None

            if file_type == 'video':
                token_from_step1 = data.get('token')
                logger.info(f'🎬 Видео: токен из Шага 1: '
                            f'{str(token_from_step1)[:30] + "..." if token_from_step1 else "НЕТ"}')

                logger.info(f'📤 Шаг 2: загрузка видео (multipart) на {upload_url[:100]}')
                ur = requests.post(
                    upload_url,
                    files={'data': (filename, file_bytes, 'application/octet-stream')},
                    headers={'Content-Length': str(len(file_bytes))},
                    timeout=180, verify=False,
                )
                logger.info(f'📨 Шаг 2 (multipart): HTTP {ur.status_code}')
                if ur.status_code not in (200, 201, 204):
                    logger.error(f'❌ Шаг 2 (multipart): {ur.status_code} - {ur.text[:300]}')

                if token_from_step1:
                    logger.info(f'✅ Токен видео (из Шага 1): {str(token_from_step1)[:30]}...')
                    return token_from_step1

                try:
                    result = ur.json()
                    logger.info(f'📨 Ответ загрузки видео: '
                                f'{json.dumps(result, ensure_ascii=False)[:500]}')
                    token = result.get('token')
                    if not token and isinstance(result, dict):
                        for key in ('videos', 'video', 'data', 'payload'):
                            if token:
                                break
                            val = result.get(key)
                            if isinstance(val, dict):
                                if 'token' in val:
                                    token = val['token']
                                else:
                                    for v in val.values():
                                        if isinstance(v, dict) and 'token' in v:
                                            token = v['token']
                                            break
                    if token:
                        logger.info(f'✅ Токен видео (из Шага 2): {str(token)[:30]}...')
                    else:
                        logger.error(f'❌ Токен видео не найден в ответе Шага 2')
                    return token
                except Exception as je:
                    logger.error(f'❌ Ошибка парсинга ответа видео: {je}')
                    logger.error(f'   Тело: {ur.text[:500]}')
                    return None

            logger.info(f'📤 Шаг 2: загрузка на {upload_url[:100]}')
            ur = requests.post(upload_url,
                               files={'data': (filename, file_bytes)},
                               timeout=120, verify=False)
            logger.info(f'📨 Шаг 2: HTTP {ur.status_code}')

            if ur.status_code != 200:
                logger.error(f'❌ Шаг 2: {ur.status_code} - {ur.text[:200]}')
                return None

            result = ur.json()
            logger.info(f'📨 Ответ загрузки: {json.dumps(result, ensure_ascii=False)[:500]}')

            token = result.get('token')
            if not token and isinstance(result, dict):
                for key in ('photos', 'videos', 'data', 'payload'):
                    if token:
                        break
                    val = result.get(key)
                    if isinstance(val, dict):
                        if 'token' in val:
                            token = val['token']
                        else:
                            for v in val.values():
                                if isinstance(v, dict) and 'token' in v:
                                    token = v['token']
                                    break

            if token:
                logger.info(f'✅ Токен: {str(token)[:30]}...')
            else:
                logger.error(f'❌ Токен не найден в ответе')
            return token

        except Exception as e:
            logger.exception(f'❌ upload_file упал: {e}')
            return None

    def send_post(self, chat_id, text, media_tokens, media_types=None,
                  retry_not_ready=True, max_retries=12):
        if not self.token:
            return False, None
        try:
            if media_types is None:
                media_types = ['image'] * len(media_tokens)
            if len(media_types) < len(media_tokens):
                media_types = media_types + ['image'] * (len(media_tokens) - len(media_types))

            attachments = []
            for i, token in enumerate(media_tokens[:10]):
                mtype = media_types[i] if i < len(media_types) else 'image'
                attachments.append({"type": mtype, "payload": {"token": token}})

            payload = {"text": text, "format": "markdown"}
            if attachments:
                payload["attachments"] = attachments

            chat_id_str = str(chat_id)
            chat_id_for_api = chat_id_str if chat_id_str.startswith('-') else f"-{chat_id_str}"

            has_video = 'video' in media_types

            for attempt in range(1, max_retries + 1):
                logger.info(f'📤 Отправка в {chat_id_for_api} (попытка {attempt}/{max_retries}), '
                            f'медиа: {len(attachments)} ({media_types})')

                r = requests.post(
                    f"{self.base_url}/messages",
                    headers={"Authorization": self.token, "Content-Type": "application/json"},
                    params={"chat_id": chat_id_for_api},
                    json=payload, timeout=60, verify=False)

                logger.info(f'📨 Ответ: {r.status_code}')

                if r.status_code == 200:
                    post_link = None
                    try:
                        result = r.json()
                        seq = None
                        if isinstance(result, dict):
                            if 'message' in result and isinstance(result['message'], dict):
                                msg = result['message']
                                if 'body' in msg and isinstance(msg['body'], dict):
                                    seq = msg['body'].get('seq')
                            if not seq and 'seq' in result:
                                seq = result['seq']
                        if seq:
                            seq_bytes = int(seq).to_bytes(8, byteorder='big')
                            encoded = base64.urlsafe_b64encode(seq_bytes).decode('utf-8').rstrip('=')
                            post_link = f"https://max.ru/c/{chat_id_str}/{encoded}"
                            logger.info(f'🔗 Ссылка: {post_link}')
                    except Exception as e:
                        logger.warning(f'⚠️ Ссылка: {e}')
                    return True, post_link

                error_text = r.text.lower()
                if 'attachment.not.ready' in error_text or 'not.processed' in error_text:
                    if retry_not_ready and attempt < max_retries:
                        if has_video:
                            wait = min(5 * (2 ** (attempt - 1)), 180)
                        else:
                            wait = min(3 * attempt, 30)
                        logger.warning(f'⚠️ Медиа ещё не обработано, ждём {wait} сек...')
                        time.sleep(wait)
                        continue
                logger.error(f'❌ send_post: {r.status_code} - {r.text[:300]}')
                return False, None

            return False, None
        except Exception as e:
            logger.exception(f'❌ send_post: {e}')
            return False, None


api = APIClient()
publisher = Publisher(api, fm, db)
report_gen = ReportGenerator(fm, db)


# ============================================================
# Публикация одного объявления
# ============================================================

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm'}


def clean_post_text(raw_text: str) -> str:
    if not raw_text:
        return ''
    marker = '#изъятая'
    idx = raw_text.find(marker)
    if idx == -1:
        return raw_text.rstrip()
    post = raw_text[:idx].rstrip()
    lines = []
    for line in post.splitlines():
        s = line.strip()
        if (s.startswith('Название:') or s.startswith('Ссылка:')
                or s.startswith('Код предложения:')):
            continue
        lines.append(line)
    return '\n'.join(lines).rstrip()


def publish_one_ad(ad: dict) -> tuple:
    ad_id = ad['id']
    chat_id = ad.get('chat_id')
    media_path = ad.get('media_path')

    if not media_path or not os.path.exists(media_path):
        return False, f'Папка не найдена: {media_path}', None

    info_path = os.path.join(media_path, 'info.txt')
    if not os.path.exists(info_path):
        return False, f'Нет info.txt в {media_path}', None

    with open(info_path, 'r', encoding='utf-8') as f:
        raw_text = f.read()
    text = clean_post_text(raw_text)
    if not text:
        return False, 'Пустой текст поста', None

    all_files = sorted(os.listdir(media_path))
    photos, videos = [], []
    for f in all_files:
        if '.orig.' in f:
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in IMAGE_EXTS:
            photos.append(f)
        elif ext in VIDEO_EXTS:
            videos.append(f)

    media_files = []
    if videos:
        media_files.append((videos[0], 'video'))
    elif photos:
        for p in photos[:10]:
            media_files.append((p, 'image'))
    else:
        return False, 'Нет ни фото, ни видео', None

    logger.info(f'📷 Медиа: {len(media_files)} ({[t for _, t in media_files]})')

    media_tokens = []
    media_types = []
    upload_errors = []

    for fname, ftype in media_files:
        fpath = os.path.join(media_path, fname)
        try:
            with open(fpath, 'rb') as f:
                file_bytes = f.read()

            logger.info(f'⬆️ Загрузка {ftype}: {fname} ({len(file_bytes)} байт)')
            token = api.upload_file(file_bytes, fname, ftype)

            if token:
                media_tokens.append(token)
                media_types.append(ftype)
                logger.info(f'✅ Загружено {fname}')
            else:
                upload_errors.append(fname)
                logger.error(f'❌ {fname} — токен не получен')

            if ftype == 'video':
                logger.info('⏳ Ждём 60 сек для обработки видео на сервере MAX...')
                time.sleep(60)
            else:
                time.sleep(0.5)

        except Exception as e:
            upload_errors.append(fname)
            logger.error(f'❌ Ошибка загрузки {fname}: {e}')

    if not media_tokens:
        return False, f'Не удалось загрузить медиа (ошибок: {len(upload_errors)})', None

    success, post_link = api.send_post(chat_id, text, media_tokens, media_types,
                                        retry_not_ready=True, max_retries=12)
    if not success:
        return False, 'Ошибка отправки поста в MAX', None

    bot_db.add_publication({
        'user_id': 0,
        'folder_name': ad.get('source_url'),
        'group_id': chat_id,
        'max_post_url': post_link,
        'title': ad.get('title'),
        'code': ad.get('code'),
        'city': ad.get('city'),
        'price': ad.get('price'),
        'category': ad.get('category'),
        'status': 'success',
    })

    bot_db.mark_ad_published(ad_id, post_link)

    try:
        shutil.rmtree(media_path)
        logger.info(f'🗑️ Папка удалена: {media_path}')
    except Exception as e:
        logger.warning(f'⚠️ Не удалить {media_path}: {e}')

    return True, 'Опубликовано', post_link


# ============================================================
# Автопубликация
# ============================================================

AUTOPUBLISH_ENABLED = [False]
AUTOPUBLISH_THREAD = [None]


def get_schedule_params():
    sched = get_schedule_from_db(bot_db)
    try:
        sh, sm = [int(x) for x in sched.get('start', '06:00').split(':')]
    except Exception:
        sh, sm = 6, 0
    try:
        eh, em = [int(x) for x in sched.get('end', '20:00').split(':')]
    except Exception:
        eh, em = 20, 0
    try:
        limit = int(sched.get('daily_limit', 150))
    except Exception:
        limit = 150
    return sh, sm, eh, em, limit


def count_today_publications() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT COUNT(*) FROM publications
                     WHERE DATE(published_at) = DATE('now', 'localtime')
                       AND status = 'success' ''')
        n = c.fetchone()[0]
        conn.close()
        return n
    except Exception as e:
        logger.error(f'❌ count_today_publications: {e}')
        return 0


def calc_interval_seconds(limit: int):
    sh, sm, eh, em, _ = get_schedule_params()
    window_seconds = (eh * 3600 + em * 60) - (sh * 3600 + sm * 60)
    if window_seconds <= 0:
        window_seconds = 14 * 3600
    if limit <= 0:
        limit = 150
    avg_interval = window_seconds / limit
    min_i = int(avg_interval * 0.7)
    max_i = int(avg_interval * 1.3)
    if min_i < 10:
        min_i = 10
    if max_i < min_i:
        max_i = min_i
    return min_i, max_i


def is_in_schedule_window():
    now_msk = datetime.now(MOSCOW_TZ)
    sh, sm, eh, em, _ = get_schedule_params()
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = now_msk.hour * 60 + now_msk.minute
    return start_min <= now_min < end_min


def seconds_until_window_start():
    now_msk = datetime.now(MOSCOW_TZ)
    sh, sm, eh, em, _ = get_schedule_params()
    today_start = now_msk.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if now_msk >= today_start:
        tomorrow_start = today_start + timedelta(days=1)
        return int((tomorrow_start - now_msk).total_seconds())
    return int((today_start - now_msk).total_seconds())


def _sleep_with_check(seconds: int):
    if seconds is None or seconds <= 0:
        seconds = 1
    elapsed = 0
    while elapsed < seconds and AUTOPUBLISH_ENABLED[0]:
        chunk = min(5, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk


def auto_publish_loop():
    logger.info('🚀 Автопубликация: поток запущен')
    while AUTOPUBLISH_ENABLED[0]:
        try:
            log_threads('auto_publish_loop')

            if not is_in_schedule_window():
                wait_sec = min(seconds_until_window_start(), 3600)
                if wait_sec <= 0:
                    wait_sec = 1
                logger.info(f'⏰ Вне окна расписания. Ждём {wait_sec} сек')
                _sleep_with_check(wait_sec)
                continue

            sh, sm, eh, em, limit = get_schedule_params()
            today_count = count_today_publications()
            if today_count >= limit:
                logger.info(f'⏹ Дневной лимит {limit} достигнут')
                wait_sec = min(seconds_until_window_start(), 3600)
                if wait_sec <= 0:
                    wait_sec = 1
                _sleep_with_check(wait_sec)
                continue

            ads = bot_db.get_pending_ads(limit=1)
            if not ads:
                logger.info('📭 Очередь пуста. Ждём 60 сек')
                _sleep_with_check(60)
                continue

            ad = ads[0]
            logger.info(f'📤 [{today_count+1}/{limit}] Автопубликация: {ad.get("folder_name")}')
            try:
                ok, message, post_link = publish_one_ad(ad)
                if ok:
                    logger.info(f'  ✅ {message} {post_link or ""}')
                    bot_db.set_setting('last_auto_publish_at', datetime.now().isoformat())
                else:
                    logger.warning(f'  ❌ {message}')
                    bot_db.mark_ad_failed(ad['id'], message)
            except Exception as e:
                logger.exception(f'  ❌ Ошибка публикации: {e}')
                bot_db.mark_ad_failed(ad['id'], str(e))

            gc.collect()
            min_i, max_i = calc_interval_seconds(limit)
            pause = random.randint(min_i, max_i)
            logger.info(f'⏸ Пауза {pause} сек до следующего поста')
            _sleep_with_check(pause)
        except Exception as e:
            logger.exception(f'❌ Ошибка в auto_publish_loop: {e}')
            _sleep_with_check(60)
    logger.info('⏹ Автопубликация остановлена')


def start_auto_publish():
    if AUTOPUBLISH_THREAD[0] and AUTOPUBLISH_THREAD[0].is_alive():
        logger.info('ℹ️ Автопубликация уже запущена')
        return
    AUTOPUBLISH_ENABLED[0] = True
    bot_db.set_setting('autopublish_enabled', '1')
    t = threading.Thread(target=auto_publish_loop, daemon=True)
    t.start()
    AUTOPUBLISH_THREAD[0] = t
    logger.info('✅ Автопубликация запущена')


def stop_auto_publish():
    AUTOPUBLISH_ENABLED[0] = False
    bot_db.set_setting('autopublish_enabled', '0')
    logger.info('🛑 Автопубликация остановлена')


def get_autopublish_status():
    enabled = AUTOPUBLISH_ENABLED[0]
    sh, sm, eh, em, limit = get_schedule_params()
    today_count = count_today_publications()
    in_window = is_in_schedule_window()
    if not enabled:
        msg, next_in = '⏹ Выключена', None
    elif not in_window:
        secs = seconds_until_window_start()
        mins = secs // 60
        msg, next_in = f'⏰ Ждёт окна (до 06:00, ~{mins} мин)', secs
    elif today_count >= limit:
        msg, next_in = f'⏹ Лимит {limit} достигнут', None
    else:
        min_i, max_i = calc_interval_seconds(limit)
        msg, next_in = f'✅ Работает ({today_count}/{limit})', max_i
    return {'enabled': enabled, 'in_window': in_window,
            'today_count': today_count, 'daily_limit': limit,
            'status_message': msg, 'next_in_seconds': next_in,
            'window': f'{sh:02d}:{sm:02d} – {eh:02d}:{em:02d} МСК'}


# ============================================================
# Создание видео из фото (фоновый поток + статус)
# ============================================================

VIDEO_MAKER_THREAD = [None]
VIDEO_MAKER_STOP = [False]
VIDEO_MAKER_STATUS = {
    'running': False,
    'total': 0,
    'done': 0,
    'ok': 0,
    'failed': 0,
    'current': None,
    'log': '—',
    'started_at': None,
    'finished_at': None,
    'report': None,
}


def video_maker_is_running() -> bool:
    return VIDEO_MAKER_STATUS.get('running', False)


def _video_progress_cb(state: dict):
    VIDEO_MAKER_STATUS.update({
        'total': state.get('total', 0),
        'done': state.get('done', 0),
        'ok': state.get('ok', 0),
        'failed': state.get('failed', 0),
        'current': state.get('current'),
        'log': state.get('log', '—'),
    })


def _run_video_maker(only_ids=None):
    try:
        VIDEO_MAKER_STATUS.update({
            'running': True,
            'total': 0, 'done': 0, 'ok': 0, 'failed': 0,
            'current': None, 'log': '▶️ Старт',
            'started_at': datetime.now().isoformat(),
            'finished_at': None,
            'report': None,
        })
        report = process_queue_videos(
            bot_db,
            only_ids=only_ids,
            stop_flag=lambda: VIDEO_MAKER_STOP[0],
            progress_cb=_video_progress_cb,
        )
        VIDEO_MAKER_STATUS['report'] = report
        VIDEO_MAKER_STATUS['log'] = (
            f"🏁 Готово. OK: {report.get('ok', 0)}, "
            f"ошибок: {report.get('failed', 0)}, "
            f"пропущено: {report.get('skipped', 0)}"
        )
        logger.info(f'🎬 video_maker завершён: {VIDEO_MAKER_STATUS["log"]}')
    except Exception as e:
        logger.exception(f'❌ Ошибка video_maker: {e}')
        VIDEO_MAKER_STATUS['log'] = f'❌ Ошибка: {e}'
    finally:
        VIDEO_MAKER_STATUS['running'] = False
        VIDEO_MAKER_STATUS['finished_at'] = datetime.now().isoformat()
        VIDEO_MAKER_THREAD[0] = None
        VIDEO_MAKER_STOP[0] = False


def start_video_maker(only_ids=None):
    if video_maker_is_running():
        return False, 'Уже запущено'
    VIDEO_MAKER_STOP[0] = False
    t = threading.Thread(target=_run_video_maker, args=(only_ids,), daemon=True)
    t.start()
    VIDEO_MAKER_THREAD[0] = t
    return True, 'Запущено'


def stop_video_maker():
    if not video_maker_is_running():
        return False
    VIDEO_MAKER_STOP[0] = True
    return True
    # ============================================================
# Стили
# ============================================================

BASE_STYLE = """
<style>
    body { font-family: Arial; max-width: 1400px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
    .card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
    h1, h2 { margin-top: 0; }
    a { color: #007bff; text-decoration: none; }
    .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; border-radius: 5px; margin-right: 10px; margin-bottom: 10px; border: none; cursor: pointer; font-size: 14px; }
    .btn-green { background: #28a745; }
    .btn-orange { background: #fd7e14; }
    .btn-red { background: #dc3545; }
    .btn-gray { background: #6c757d; }
    .btn-purple { background: #6f42c1; }
    .btn:hover { opacity: 0.9; }
    .stats { display: flex; gap: 20px; flex-wrap: wrap; }
    .stat { background: #f8f9fa; padding: 15px 20px; border-radius: 5px; }
    .stat .num { font-size: 24px; font-weight: bold; color: #007bff; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 13px; }
    th { background: #f8f9fa; }
    input[type="text"], input[type="date"] { padding: 8px 12px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; }
    input[type="text"] { width: 100%; max-width: 320px; }
    .form-row { margin-bottom: 15px; }
    .form-row label { display: block; margin-bottom: 5px; font-weight: bold; color: #333; font-size: 14px; }
    .success-msg { background: #d4edda; color: #155724; padding: 12px; border-radius: 5px; margin-bottom: 15px; }
    .hint { color: #666; font-size: 13px; margin-top: 5px; }
    .warn { background: #fff3cd; color: #856404; padding: 12px; border-radius: 5px; margin-bottom: 15px; }
    .error-msg { background: #f8d7da; color: #721c24; padding: 12px; border-radius: 5px; margin-bottom: 15px; }
    .counter-big { font-size: 36px; font-weight: bold; color: #28a745; }
    .status-on { color: #28a745; font-weight: bold; }
    .status-off { color: #dc3545; font-weight: bold; }
    pre.log { background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px;
              overflow-x: auto; font-size: 12px; line-height: 1.4; max-height: 700px;
              overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
    .photo-wrap { position: relative; display: inline-block; margin: 5px; }
    .photo-del { position: absolute; top: 8px; right: 8px; background: rgba(220,53,69,0.9);
                 color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px;
                 text-decoration: none; cursor: pointer; font-weight: bold;
                 box-shadow: 0 2px 4px rgba(0,0,0,0.3); line-height: 1; }
    .photo-del:hover { background: #dc3545; color: white; }
    .photo-del-locked { position: absolute; top: 8px; right: 8px;
                        background: rgba(108,117,125,0.7); color: white;
                        padding: 4px 8px; border-radius: 4px; font-size: 12px;
                        font-weight: bold; box-shadow: 0 2px 4px rgba(0,0,0,0.3); line-height: 1; }
    .photo-rotate { position: absolute; top: 8px; right: 44px;
                    background: rgba(0,123,255,0.9); color: white;
                    padding: 4px 8px; border-radius: 4px; font-size: 12px;
                    text-decoration: none; cursor: pointer; font-weight: bold;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.3); line-height: 1; }
    .photo-rotate:hover { background: #007bff; color: white; }
    .photo-reset { position: absolute; top: 8px; right: 80px;
                   background: rgba(108,117,125,0.9); color: white;
                   padding: 4px 8px; border-radius: 4px; font-size: 12px;
                   text-decoration: none; cursor: pointer; font-weight: bold;
                   box-shadow: 0 2px 4px rgba(0,0,0,0.3); line-height: 1; }
    .photo-reset:hover { background: #6c757d; color: white; }
    .file-drop { border: 2px dashed #007bff; border-radius: 8px; padding: 30px;
                 text-align: center; background: #f8f9fa; margin: 15px 0; cursor: pointer; }
    .file-drop:hover { background: #e9ecef; }
    .period-form { display: flex; gap: 15px; align-items: flex-end; flex-wrap: wrap; margin: 15px 0; }
    .period-form > div { display: flex; flex-direction: column; }
    .period-form label { font-size: 12px; color: #666; margin-bottom: 4px; }
    .quick-links { margin: 10px 0; }
    .quick-links .btn { font-size: 13px; padding: 7px 12px; }
    .progress-bar { width: 100%; height: 24px; background: #e9ecef; border-radius: 6px;
                    overflow: hidden; margin: 10px 0; }
    .progress-bar-inner { height: 100%; background: linear-gradient(90deg, #28a745, #20c997);
                          transition: width 0.3s; color: white; text-align: center;
                          font-size: 13px; font-weight: bold; line-height: 24px; }
</style>
"""


# ============================================================
# Главная
# ============================================================

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        return webhook()
    stats = bot_db.count_by_status()
    ap_status = get_autopublish_status()
    return BASE_STYLE + f"""
    <div class="card">
        <h1>🤖 VTB Bot</h1>
        <p>Токен MAX: {'✅' if TOKEN else '❌'}</p>
        <p>Автопубликация: <span class="{'status-on' if ap_status['enabled'] else 'status-off'}">{ap_status['status_message']}</span></p>
        <p>OUTPUT_DIR: <code>{OUTPUT_DIR}</code></p>
    </div>
    <div class="card">
        <h2>📊 Очередь</h2>
        <div class="stats">
            <div class="stat"><div>В очереди</div><div class="num">{stats.get('pending', 0)}</div></div>
            <div class="stat"><div>Опубликовано</div><div class="num">{stats.get('published', 0)}</div></div>
            <div class="stat"><div>Ошибок</div><div class="num">{stats.get('failed', 0)}</div></div>
        </div>
    </div>
    <div class="card">
        <h2>⚙️ Управление</h2>
        <a href="/admin" class="btn">🛠 Админка</a>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
    </div>
    """


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/health')
def health():
    return {"status": "ok", "token_set": bool(TOKEN),
            "threads": threading.active_count()}


@app.route('/status')
def status():
    return {"status": "running", "queue": bot_db.count_by_status(),
            "threads": threading.active_count()}


@app.route('/setup_webhook')
def setup_webhook():
    token = request.args.get('token') or TOKEN
    if not token:
        return "❌ Нет токена", 400
    webhook_url = f"{PUBLIC_URL}/webhook"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    try:
        r = requests.get(f"{BASE_URL}/subscriptions", headers=headers, timeout=30, verify=False)
        if r.status_code == 200:
            for sub in r.json().get('subscriptions', []):
                old_url = sub.get('url')
                if old_url:
                    requests.delete(f"{BASE_URL}/subscriptions", headers=headers,
                                    params={"url": old_url}, timeout=30, verify=False)
    except Exception as e:
        logger.warning(f'⚠️ {e}')
    try:
        r = requests.post(f"{BASE_URL}/subscriptions", headers=headers,
                          json={"url": webhook_url,
                                "update_types": ["message_created", "bot_started", "bot_stopped"]},
                          timeout=30, verify=False)
        if r.status_code == 200:
            return f"✅ Вебхук зарегистрирован: {webhook_url}"
        return f"❌ Ошибка: {r.status_code} - {r.text}"
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ============================================================
# АДМИНКА — главная
# ============================================================

@app.route('/admin')
@require_admin
def admin_page():
    sections = get_sections_from_db(bot_db)
    stats = bot_db.count_by_status()
    schedule = get_schedule_from_db(bot_db)
    ap_status = get_autopublish_status()
    parser_running = parser_is_running()
    video_running = video_maker_is_running()

    rows = "".join(
        f"<tr><td>{s.get('title', s['name'])}</td><td>{s['url']}</td>"
        f"<td>{s.get('key_in_title') or '—'}</td><td>{s['chat_id']}</td>"
        f"<td>{'✅' if s.get('enabled', True) else '❌'}</td></tr>"
        for s in sections)
    stats_rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in stats.items()) or \
        "<tr><td colspan='2'>Пусто</td></tr>"
    pending = stats.get('pending', 0)
    ap_class = 'status-on' if ap_status['enabled'] else 'status-off'
    ap_next = f" (следующий пост через ~{ap_status['next_in_seconds']//60} мин)" \
              if ap_status.get('next_in_seconds') else ''
    import_warn = '' if IMPORT_FOLDER_AVAILABLE else \
        '<div class="warn">⚠️ Файл <code>import_folder.py</code> не найден.</div>'

    now_msk = datetime.now(MOSCOW_TZ)
    yesterday = (now_msk - timedelta(days=1)).strftime('%Y-%m-%d')
    week_ago = (now_msk - timedelta(days=7)).strftime('%Y-%m-%d')
    month_ago = (now_msk - timedelta(days=30)).strftime('%Y-%m-%d')

    parser_status_html = (
        '<span class="status-on">🟢 РАБОТАЕТ</span>'
        if parser_running else
        '<span class="status-off">🔴 ОСТАНОВЛЕН</span>'
    )
    parser_stop_btn = (
        '<a href="/admin/stop_parser" class="btn btn-red" '
        'onclick="return confirm(\'Остановить парсер?\')">⏹ СТОП парсера</a>'
        if parser_running else
        '<a href="/admin/run_parser?limit=300" class="btn btn-green" '
        'onclick="return confirm(\'Запустить парсер (300)?\')">🚀 Запустить парсер</a>'
    )

    video_status_html = (
        '<span class="status-on">🟢 РАБОТАЕТ</span>'
        if video_running else
        '<span class="status-off">🔴 НЕ ЗАПУЩЕНО</span>'
    )
    if not VIDEO_MAKER_AVAILABLE:
        reason_short = (MOVIEPY_IMPORT_ERROR or 'video_maker недоступен')
        if len(reason_short) > 60:
            reason_short = reason_short[:60] + '…'
        video_btn = (
            f'<a href="/admin/make_videos_status" class="btn btn-gray" '
            f'title="{MOVIEPY_IMPORT_ERROR or "video_maker недоступен"}">'
            f'🎬 Видео недоступно: {reason_short}</a>'
        )
    elif video_running:
        video_btn = '<a href="/admin/make_videos_status" class="btn btn-purple">🎬 К прогрессу</a>'
    else:
        video_btn = (
            '<a href="/admin/make_videos" class="btn btn-purple" '
            'onclick="return confirm(\'Собрать видео из фото для ВСЕЙ очереди? Исходные фото будут удалены.\')">🎬 Создать видео</a>'
        )

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🛠 Админка</h1>
        <a href="/" class="btn">← На главную</a>
        <a href="/admin/settings" class="btn">⚙️ Настройки</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
        <a href="/admin/import_folder" class="btn">📥 Импорт папок (zip)</a>
        <a href="/admin/parser_status" class="btn">🚀 Статус парсера</a>
        <a href="/setup_webhook" class="btn btn-gray">🔄 Вебхук</a>
        {parser_stop_btn}
        {video_btn}
    </div>
    {import_warn}
    <div class="card">
        <h2>📊 Отчёты по публикациям</h2>
        <div class="quick-links">
            <a href="/admin/today" class="btn">📅 Сегодня</a>
            <a href="/admin/today?from={yesterday}&to={yesterday}" class="btn">📅 Вчера</a>
            <a href="/admin/today?from={week_ago}" class="btn">📅 За 7 дней</a>
            <a href="/admin/today?from={month_ago}" class="btn">📅 За 30 дней</a>
            <a href="/admin/today?all=1" class="btn btn-orange">📚 Всё время</a>
        </div>
    </div>
    <div class="card">
        <h2>🕒 Автопубликация</h2>
        <p>Статус: <span class="{ap_class}">{ap_status['status_message']}</span>{ap_next}</p>
        <p>Окно: <b>{ap_status['window']}</b> · Сегодня: <b>{ap_status['today_count']}/{ap_status['daily_limit']}</b></p>
        <a href="/admin/toggle_autopublish" class="btn {'btn-red' if ap_status['enabled'] else 'btn-green'}" onclick="return confirm('Переключить автопубликацию?')">
            {'⏹ Выключить' if ap_status['enabled'] else '▶ Включить'}
        </a>
    </div>
    <div class="card">
        <h2>🚀 Парсинг</h2>
        <p>Статус: {parser_status_html}</p>
        <a href="/admin/run_parser?limit=5" class="btn btn-green">Тест (5)</a>
        <a href="/admin/run_parser?limit=50" class="btn btn-green">50</a>
        <a href="/admin/run_parser?limit=300" class="btn btn-green">300</a>
        {parser_stop_btn}
    </div>
    <div class="card">
        <h2>🎬 Создание видео из фото</h2>
        <p>Статус: {video_status_html}</p>
        <p class="hint">Собирает zoom-видео из всех фото в папках очереди и удаляет исходные фото.</p>
        {video_btn}
        <a href="/admin/queue" class="btn">📋 К очереди</a>
    </div>
    <div class="card">
        <h2>📥 Импорт готовых папок</h2>
        <a href="/admin/import_folder" class="btn">📥 Перейти к импорту</a>
    </div>
    <div class="card">
        <h2>📤 Ручная публикация</h2>
        <p>В очереди: <b>{pending}</b></p>
        <a href="/admin/publish_one" class="btn btn-orange" onclick="return confirm('Опубликовать 1?')">📤 Опубликовать 1</a>
        <a href="/admin/queue" class="btn">📋 Открыть очередь</a>
    </div>
    <div class="card">
        <h2>🧹 Очистка</h2>
        <a href="/admin/cleanup_orphans" class="btn btn-red" onclick="return confirm('Удалить записи без папок?')">🧹 Очистить «мёртвые» записи</a>
        <a href="/admin/clear_all" class="btn btn-red" onclick="return confirm('⚠️ ПОЛНАЯ ОЧИСТКА!')">🗑️ ПОЛНАЯ ОЧИСТКА</a>
    </div>
    <div class="card">
        <h2>⚙️ Разделы и группы MAX</h2>
        <table><tr><th>Категория</th><th>URL</th><th>Ключ</th><th>chat_id</th><th>Вкл</th></tr>{rows}</table>
    </div>
    <div class="card">
        <h2>⏰ Расписание</h2>
        <p>Начало: <b>{schedule['start']}</b> · Конец: <b>{schedule['end']}</b> МСК · Лимит: <b>{schedule['daily_limit']}/день</b></p>
    </div>
    <div class="card">
        <h2>📊 Очередь парсинга</h2>
        <table><tr><th>Статус</th><th>Количество</th></tr>{stats_rows}</table>
    </div>
    """


@app.route('/admin/toggle_autopublish')
@require_admin
def admin_toggle_autopublish():
    if AUTOPUBLISH_ENABLED[0]:
        stop_auto_publish()
    else:
        start_auto_publish()
    return redirect('/admin')


# ============================================================
# АДМИНКА — Импорт папок
# ============================================================

@app.route('/admin/import_folder', methods=['GET', 'POST'])
@require_admin
def admin_import_folder():
    if not IMPORT_FOLDER_AVAILABLE:
        return BASE_STYLE + """
        <div class="card">
            <h1>❌ Модуль импорта недоступен</h1>
            <div class="error-msg">Файл <code>import_folder.py</code> не найден.</div>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """
    if request.method == 'POST':
        if 'zip_file' not in request.files:
            return redirect('/admin/import_folder?error=no_file')
        f = request.files['zip_file']
        if not f or not f.filename:
            return redirect('/admin/import_folder?error=no_file')
        if not f.filename.lower().endswith('.zip'):
            return redirect('/admin/import_folder?error=not_zip')
        tmp_dir = '/tmp/import_upload'
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f'upload_{int(time.time())}.zip')
        try:
            f.save(tmp_path)
        except Exception as e:
            logger.exception(f'❌ Не сохранить zip: {e}')
            return redirect('/admin/import_folder?error=save_failed')
        try:
            report = import_zip(tmp_path, bot_db)
        except Exception as e:
            logger.exception(f'❌ Ошибка импорта: {e}')
            report = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 1,
                      'details': [{'folder': '(архив)', 'status': 'error', 'reason': str(e)}]}
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        details_html = ""
        for d in report['details']:
            status = d.get('status', '')
            icon, color = ('✅', '#28a745') if status == 'ok' else \
                          ('⏭️', '#fd7e14') if status == 'skip' else ('❌', '#dc3545')
            details_html += (f"<tr><td style='color:{color};font-weight:bold'>{icon}</td>"
                             f"<td><code>{d.get('folder','')}</code></td>"
                             f"<td style='font-size:12px'>{d.get('reason','')}</td></tr>")
        if not details_html:
            details_html = "<tr><td colspan='3' style='text-align:center;color:#999'>Нет данных</td></tr>"
        return BASE_STYLE + f"""
        <div class="card">
            <h1>📥 Отчёт импорта</h1>
            <div class="stats" style="margin:15px 0">
                <div class="stat"><div>Всего</div><div class="num">{report['total']}</div></div>
                <div class="stat"><div>Импортировано</div><div class="num" style="color:#28a745">{report['imported']}</div></div>
                <div class="stat"><div>Пропущено</div><div class="num" style="color:#fd7e14">{report['skipped']}</div></div>
                <div class="stat"><div>Ошибок</div><div class="num" style="color:#dc3545">{report['errors']}</div></div>
            </div>
            <a href="/admin/queue" class="btn btn-green">📋 Перейти в очередь</a>
            <a href="/admin/import_folder" class="btn">📥 Загрузить ещё</a>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        <div class="card">
            <h2>📋 Детали</h2>
            <div style="max-height:600px;overflow-y:auto">
                <table><thead><tr><th>Статус</th><th>Папка</th><th>Причина</th></tr></thead>
                <tbody>{details_html}</tbody></table>
            </div>
        </div>
        """
    error = request.args.get('error', '')
    error_html = ''
    if error == 'no_file':
        error_html = '<div class="error-msg">⚠️ Файл не выбран</div>'
    elif error == 'not_zip':
        error_html = '<div class="error-msg">⚠️ Файл должен быть ZIP-архивом</div>'
    elif error == 'save_failed':
        error_html = '<div class="error-msg">⚠️ Не удалось сохранить файл</div>'
    try:
        imported_count = bot_db.count_by_status().get('pending', 0)
    except Exception:
        imported_count = 0
    return BASE_STYLE + f"""
    <div class="card">
        <h1>📥 Импорт папок из ZIP</h1>
        <a href="/admin" class="btn">← В админку</a>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
    </div>
    {error_html}
    <div class="card">
        <h2>📖 Как это работает</h2>
        <ol style="line-height:1.8">
            <li>Заархивируйте <b>одну головную папку</b> в ZIP.</li>
            <li>Внутри головной папки — <b>подпапки</b> с объявлениями.</li>
            <li>Имя подпапки: <b><code>&lt;префикс&gt;_&lt;chat_id&gt;</code></b> или с пробелом.</li>
            <li>В каждой подпапке: <b><code>info.txt</code></b> + либо <b>одно видео</b> (<code>.mp4</code>), либо <b>несколько фото</b>.</li>
            <li>Нажмите «Загрузить и распределить».</li>
        </ol>
    </div>
    <div class="card">
        <h2>📤 Загрузка</h2>
        <form method="POST" enctype="multipart/form-data">
            <div class="file-drop" onclick="document.getElementById('zipInput').click()">
                <div style="font-size:48px">📦</div>
                <div style="margin-top:10px;font-size:16px" id="fileName">Нажмите, чтобы выбрать ZIP-архив</div>
                <div class="hint">или перетащите файл сюда</div>
            </div>
            <input type="file" name="zip_file" id="zipInput" accept=".zip" style="display:none"
                   onchange="document.getElementById('fileName').textContent = this.files[0] ? this.files[0].name : 'Нажмите, чтобы выбрать ZIP-архив'">
            <button type="submit" class="btn btn-green" style="font-size:16px;padding:12px 30px">
                🚀 Загрузить и распределить
            </button>
            <p class="hint">Импорт может занять время (копирование файлов).</p>
        </form>
    </div>
    <div class="card">
        <h2>📊 Состояние</h2>
        <p>Сейчас в очереди: <b>{imported_count}</b> pending-объявлений.</p>
        <a href="/admin/queue" class="btn">📋 Открыть очередь</a>
    </div>
    <script>
        const drop = document.querySelector('.file-drop');
        const input = document.getElementById('zipInput');
        ['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {{ e.preventDefault(); drop.style.background = '#e3f2fd'; }}));
        ['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {{ e.preventDefault(); drop.style.background = '#f8f9fa'; }}));
        drop.addEventListener('drop', e => {{
            const files = e.dataTransfer.files;
            if (files.length > 0) {{
                input.files = files;
                document.getElementById('fileName').textContent = files[0].name;
            }}
        }});
    </script>
    """
