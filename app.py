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

from flask import (
    Flask, request, jsonify, render_template_string,
    send_file, redirect, Response
)

from modules import Database, FileManager, Publisher, ReportGenerator
from config import (
    TOKEN, BASE_URL, SECRET_KEY, PORT, DATA_DIR, PUBLIC_URL,
    SHEETS_URL, OUTPUT_DIR, DB_PATH,
    SECTIONS, get_sections_from_db, get_schedule_from_db,
)
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

ALLOWED_ADMIN_IDS = [
    int(x) for x in (os.environ.get('ADMIN_IDS') or '').split(',')
    if x.strip().isdigit()
]

MOSCOW_TZ = pytz.timezone('Europe/Moscow')


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not ADMIN_PASS:
            return f(*args, **kwargs)
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return (
                '🔒 Требуется авторизация',
                401,
                {'WWW-Authenticate': 'Basic realm="VTB Admin"'},
            )
        return f(*args, **kwargs)
    return decorated


def is_allowed_user(user_id):
    if not ALLOWED_ADMIN_IDS:
        return True
    return int(user_id) in ALLOWED_ADMIN_IDS


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
                json=payload, timeout=30, verify=False,
            )
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

            try:
                data = r.json()
            except Exception as je:
                logger.error(f'❌ Шаг 1: не JSON: {je}')
                logger.error(f'❌ Шаг 1: тело: {r.text[:500]}')
                return None

            upload_url = data.get('url')
            if not upload_url:
                logger.error(f'❌ Шаг 1: нет url в ответе: {data}')
                return None

            logger.info(f'📤 Шаг 2: загрузка на {upload_url[:100]}')
            ur = requests.post(
                upload_url,
                files={'data': (filename, file_bytes)},
                timeout=300, verify=False,
            )
            logger.info(f'📨 Шаг 2: HTTP {ur.status_code}')

            if ur.status_code != 200:
                logger.error(f'❌ Шаг 2: {ur.status_code} - {ur.text[:200]}')
                return None

            try:
                result = ur.json()
            except Exception as je:
                logger.error(f'❌ Шаг 2: не JSON: {je}')
                logger.error(f'❌ Шаг 2: тело: {ur.text[:500]}')
                return None

            token = result.get('token')
            if not token and isinstance(result, dict):
                if 'data' in result and isinstance(result['data'], dict):
                    token = result['data'].get('token')
                if not token and 'photos' in result and isinstance(result['photos'], dict):
                    for v in result['photos'].values():
                        if isinstance(v, dict) and 'token' in v:
                            token = v['token']
                            break
                if not token and 'videos' in result and isinstance(result['videos'], dict):
                    for v in result['videos'].values():
                        if isinstance(v, dict) and 'token' in v:
                            token = v['token']
                            break

            if token:
                logger.info(f'✅ Токен: {str(token)[:30]}...')
            else:
                logger.error(f'❌ Токен не найден в ответе: {result}')

            return token

        except Exception as e:
            logger.exception(f'❌ upload_file упал: {e}')
            return None

    def send_post(self, chat_id, text, media_tokens, media_types=None):
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
                attachments.append({
                    "type": mtype,
                    "payload": {"token": token},
                })

            payload = {"text": text, "format": "markdown"}
            if attachments:
                payload["attachments"] = attachments

            chat_id_str = str(chat_id)
            chat_id_for_api = chat_id_str if chat_id_str.startswith('-') else f"-{chat_id_str}"

            logger.info(f'📤 Отправка в {chat_id_for_api}, медиа: {len(attachments)} ({media_types})')

            r = requests.post(
                f"{self.base_url}/messages",
                headers={"Authorization": self.token, "Content-Type": "application/json"},
                params={"chat_id": chat_id_for_api},
                json=payload, timeout=120, verify=False,
            )

            logger.info(f'📨 Ответ: {r.status_code}')
            if r.status_code != 200:
                logger.error(f'❌ send_post: {r.status_code} - {r.text[:300]}')
                return False, None

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


def publish_one_ad(ad: dict) -> tuple:
    ad_id = ad['id']
    folder_name = ad.get('folder_name')
    chat_id = ad.get('chat_id')
    media_path = ad.get('media_path')

    if not media_path:
        return False, 'Нет media_path', None

    if not os.path.exists(media_path):
        return False, f'Папка не найдена: {media_path}', None

    info_path = os.path.join(media_path, 'info.txt')
    if not os.path.exists(info_path):
        return False, f'Нет info.txt в {media_path}', None

    with open(info_path, 'r', encoding='utf-8') as f:
        text = f.read()

    all_files = sorted(os.listdir(media_path))

    photos = []
    videos = []
    for f in all_files:
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

    for fname, ftype in media_files:
        fpath = os.path.join(media_path, fname)
        try:
            with open(fpath, 'rb') as f:
                file_bytes = f.read()

            if ftype == 'video':
                token = api.upload_file(file_bytes, fname, 'video')
            else:
                token = api.upload_file(file_bytes, fname, 'image')

            if token:
                media_tokens.append(token)
                media_types.append(ftype)

            time.sleep(0.5)
        except Exception as e:
            logger.error(f'❌ Ошибка загрузки {fname}: {e}')

    if not media_tokens:
        return False, 'Не удалось загрузить ни одно медиа', None

    success, post_link = api.send_post(chat_id, text, media_tokens, media_types)
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
        c.execute('''
            SELECT COUNT(*) FROM publications
            WHERE DATE(published_at) = DATE('now', 'localtime')
              AND status = 'success'
        ''')
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
                logger.info(f'⏹ Дневной лимит {limit} достигнут. Ждём до 06:00')
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
        msg = '⏹ Выключена'
        next_in = None
    elif not in_window:
        secs = seconds_until_window_start()
        mins = secs // 60
        msg = f'⏰ Ждёт окна (до 06:00, ~{mins} мин)'
        next_in = secs
    elif today_count >= limit:
        msg = f'⏹ Лимит {limit} достигнут'
        next_in = None
    else:
        min_i, max_i = calc_interval_seconds(limit)
        msg = f'✅ Работает ({today_count}/{limit})'
        next_in = max_i

    return {
        'enabled': enabled,
        'in_window': in_window,
        'today_count': today_count,
        'daily_limit': limit,
        'status_message': msg,
        'next_in_seconds': next_in,
        'window': f'{sh:02d}:{sm:02d} – {eh:02d}:{em:02d} МСК',
    }


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
    .btn:hover { opacity: 0.9; }
    .stats { display: flex; gap: 20px; flex-wrap: wrap; }
    .stat { background: #f8f9fa; padding: 15px 20px; border-radius: 5px; }
    .stat .num { font-size: 24px; font-weight: bold; color: #007bff; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; font-size: 13px; }
    th { background: #f8f9fa; }
    input[type="text"] { padding: 8px 12px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; width: 100%; max-width: 320px; }
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
    .photo-del {
        position: absolute; top: 8px; right: 8px;
        background: rgba(220,53,69,0.9); color: white;
        padding: 4px 8px; border-radius: 4px;
        font-size: 12px; text-decoration: none;
        cursor: pointer; font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        line-height: 1;
    }
    .photo-del:hover { background: #dc3545; color: white; }
    .photo-del-locked {
        position: absolute; top: 8px; right: 8px;
        background: rgba(108,117,125,0.7); color: white;
        padding: 4px 8px; border-radius: 4px;
        font-size: 12px; font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        line-height: 1;
    }
    .file-drop {
        border: 2px dashed #007bff;
        border-radius: 8px;
        padding: 30px;
        text-align: center;
        background: #f8f9fa;
        margin: 15px 0;
        cursor: pointer;
    }
    .file-drop:hover { background: #e9ecef; }
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


@app.route('/health')
def health():
    return {"status": "ok", "token_set": bool(TOKEN)}


@app.route('/status')
def status():
    return {"status": "running", "queue": bot_db.count_by_status()}


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
                    requests.delete(
                        f"{BASE_URL}/subscriptions",
                        headers=headers,
                        params={"url": old_url},
                        timeout=30, verify=False,
                    )
    except Exception as e:
        logger.warning(f'⚠️ {e}')
    try:
        r = requests.post(
            f"{BASE_URL}/subscriptions",
            headers=headers,
            json={"url": webhook_url, "update_types": ["message_created", "bot_started", "bot_stopped"]},
            timeout=30, verify=False,
        )
        if r.status_code == 200:
            return f"✅ Вебхук зарегистрирован: {webhook_url}"
        return f"❌ Ошибка: {r.status_code} - {r.text}"
    except Exception as e:
        return f"❌ Ошибка: {e}"


@app.route('/ingest_ads', methods=['POST'])
def ingest_ads():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400
        ads = data.get('ads', [])
        if not ads:
            return jsonify({'success': False, 'error': 'Empty ads'}), 400
        added = skipped = 0
        for ad in ads:
            try:
                if bot_db.add_parsed_ad(ad):
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f'❌ {e}')
                skipped += 1
        if added > 0:
            try:
                bot_db.resort_queue_by_categories()
            except Exception as e:
                logger.warning(f'⚠️ resort: {e}')
        return jsonify({'success': True, 'added': added, 'skipped': skipped})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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

    rows = ""
    for s in sections:
        rows += f"""
        <tr>
            <td>{s.get('title', s['name'])}</td>
            <td>{s['url']}</td>
            <td>{s.get('key_in_title') or '—'}</td>
            <td>{s['chat_id']}</td>
            <td>{'✅' if s.get('enabled', True) else '❌'}</td>
        </tr>
        """

    stats_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in stats.items()
    ) or "<tr><td colspan='2'>Пусто</td></tr>"

    pending = stats.get('pending', 0)

    ap_class = 'status-on' if ap_status['enabled'] else 'status-off'
    ap_next = ''
    if ap_status.get('next_in_seconds'):
        mins = ap_status['next_in_seconds'] // 60
        ap_next = f' (следующий пост через ~{mins} мин)'

    import_warn = ''
    if not IMPORT_FOLDER_AVAILABLE:
        import_warn = '<div class="warn">⚠️ Файл <code>import_folder.py</code> не найден. Импорт папок не работает.</div>'

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
    </div>

    {import_warn}

    <div class="card">
        <h2>🕒 Автопубликация</h2>
        <p>Статус: <span class="{ap_class}">{ap_status['status_message']}</span>{ap_next}</p>
        <p>Окно: <b>{ap_status['window']}</b> · Сегодня: <b>{ap_status['today_count']}/{ap_status['daily_limit']}</b></p>
        <a href="/admin/toggle_autopublish" class="btn {'btn-red' if ap_status['enabled'] else 'btn-green'}" onclick="return confirm('Переключить автопубликацию?')">
            {'⏹ Выключить автопубликацию' if ap_status['enabled'] else '▶ Включить автопубликацию'}
        </a>
        <a href="/admin" class="btn btn-gray">🔄 Обновить статус</a>
    </div>

    <div class="card">
        <h2>🚀 Парсинг (дедуп ВКЛ)</h2>
        <a href="/admin/run_parser?limit=5" class="btn btn-green">Тест (5)</a>
        <a href="/admin/run_parser?limit=50" class="btn btn-green">50</a>
        <a href="/admin/run_parser?limit=300" class="btn btn-green">300</a>
        <p class="hint">Парсер запускается в отдельном процессе — после завершения память освобождается</p>
        <p class="hint">После парсинга зайдите в <a href="/admin/queue">📋 Очередь</a>, чтобы проверить, отредактировать и упорядочить объявления перед публикацией.</p>
    </div>

    <div class="card">
        <h2>📥 Импорт готовых папок</h2>
        <p>Загрузите ZIP с одной головной папкой, внутри — подпапки с именами <code>&lt;префикс&gt;_&lt;chat_id&gt;</code>.</p>
        <a href="/admin/import_folder" class="btn">📥 Перейти к импорту</a>
    </div>

    <div class="card">
        <h2>📤 Ручная публикация</h2>
        <p>В очереди: <b>{pending}</b></p>
        <a href="/admin/publish_one" class="btn btn-orange" onclick="return confirm('Опубликовать 1 (первое по очереди)?')">📤 Опубликовать 1 (первое)</a>
        <a href="/admin/queue" class="btn">📋 Открыть очередь</a>
    </div>

    <div class="card">
        <h2>🧹 Очистка</h2>
        <a href="/admin/cleanup_orphans" class="btn btn-red" onclick="return confirm('Удалить записи без папок на диске?')">🧹 Очистить «мёртвые» записи</a>
        <a href="/admin/clear_all" class="btn btn-red" onclick="return confirm('⚠️ ПОЛНАЯ ОЧИСТКА! Удалить: всю очередь, журнал публикаций, все папки, все настройки. Продолжить?')">🗑️ ПОЛНАЯ ОЧИСТКА</a>
    </div>

    <div class="card">
        <h2>⚙️ Разделы и группы MAX</h2>
        <p class="hint">Редактировать chat_id → <a href="/admin/settings">Настройки</a></p>
        <table>
            <tr><th>Категория</th><th>URL</th><th>Ключ</th><th>chat_id</th><th>Вкл</th></tr>
            {rows}
        </table>
    </div>

    <div class="card">
        <h2>⏰ Расписание</h2>
        <p>Начало: <b>{schedule['start']}</b> · Конец: <b>{schedule['end']}</b> МСК · Лимит: <b>{schedule['daily_limit']}/день</b></p>
    </div>

    <div class="card">
        <h2>📊 Очередь парсинга</h2>
        <table>
            <tr><th>Статус</th><th>Количество</th></tr>
            {stats_rows}
        </table>
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
# АДМИНКА — Импорт папок (ZIP)
# ============================================================

@app.route('/admin/import_folder', methods=['GET', 'POST'])
@require_admin
def admin_import_folder():
    if not IMPORT_FOLDER_AVAILABLE:
        return BASE_STYLE + """
        <div class="card">
            <h1>❌ Модуль импорта недоступен</h1>
            <div class="error-msg">
                Файл <code>import_folder.py</code> не найден рядом с <code>app.py</code>.<br>
                Загрузите его и перезапустите контейнер.
            </div>
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
            report = {
                'total': 0, 'imported': 0, 'skipped': 0, 'errors': 1,
                'details': [{'folder': '(архив)', 'status': 'error', 'reason': str(e)}],
            }
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        details_html = ""
        for d in report['details']:
            folder = d.get('folder', '')
            status = d.get('status', '')
            reason = d.get('reason', '')

            if status == 'ok':
                icon = '✅'
                color = '#28a745'
            elif status == 'skip':
                icon = '⏭️'
                color = '#fd7e14'
            else:
                icon = '❌'
                color = '#dc3545'

            details_html += f"""
            <tr>
                <td style="color:{color};font-weight:bold">{icon}</td>
                <td><code>{folder}</code></td>
                <td style="font-size:12px">{reason}</td>
            </tr>
            """

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
                <table>
                    <thead>
                        <tr><th>Статус</th><th>Папка</th><th>Причина</th></tr>
                    </thead>
                    <tbody>
                        {details_html}
                    </tbody>
                </table>
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

    imported_count = 0
    try:
        imported_count = bot_db.count_by_status().get('pending', 0)
    except Exception:
        pass

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
            <li>Заархивируйте <b>одну головную папку</b> в ZIP.
                <div class="hint">Например: <code>ОБЪЯВЛЕНИЯ.zip</code>, внутри — папка <code>ОБЪЯВЛЕНИЯ 2</code>.</div>
            </li>
            <li>Внутри головной папки — <b>подпапки</b> с объявлениями.</li>
            <li>Имя подпапки: <b><code>&lt;префикс&gt;_&lt;chat_id&gt;</code></b>, где chat_id начинается с <code>-</code>.
                <div class="hint">Например: <code>74_-69959827081745</code></div>
            </li>
            <li>В каждой подпапке:
                <ul>
                    <li><b><code>info.txt</code></b> — обязательно (текст объявления)</li>
                    <li>Либо <b>одно видео</b> (<code>.mp4</code>), либо <b>несколько фото</b> (<code>.jpg</code>, <code>.png</code>…)</li>
                </ul>
            </li>
            <li>Нажмите «Загрузить и распределить».</li>
            <li>Все корректные подпапки попадут в <a href="/admin/queue">📋 Очередь</a> со статусом <b>pending</b>.</li>
        </ol>
    </div>

    <div class="card">
        <h2>📤 Загрузка</h2>
        <form method="POST" enctype="multipart/form-data">
            <div class="file-drop" onclick="document.getElementById('zipInput').click()">
                <div style="font-size:48px">📦</div>
                <div style="margin-top:10px;font-size:16px" id="fileName">
                    Нажмите, чтобы выбрать ZIP-архив
                </div>
                <div class="hint">или перетащите файл сюда</div>
            </div>
            <input type="file" name="zip_file" id="zipInput" accept=".zip" style="display:none"
                   onchange="document.getElementById('fileName').textContent = this.files[0] ? this.files[0].name : 'Нажмите, чтобы выбрать ZIP-архив'">

            <button type="submit" class="btn btn-green" style="font-size:16px;padding:12px 30px">
                🚀 Загрузить и распределить
            </button>
            <p class="hint">Импорт может занять время (копирование файлов). Не закрывайте страницу.</p>
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

        ['dragenter', 'dragover'].forEach(ev =>
            drop.addEventListener(ev, e => {{
                e.preventDefault();
                drop.style.background = '#e3f2fd';
            }})
        );
        ['dragleave', 'drop'].forEach(ev =>
            drop.addEventListener(ev, e => {{
                e.preventDefault();
                drop.style.background = '#f8f9fa';
            }})
        );
        drop.addEventListener('drop', e => {{
            const files = e.dataTransfer.files;
            if (files.length > 0) {{
                input.files = files;
                document.getElementById('fileName').textContent = files[0].name;
            }}
        }});
    </script>
    """
    # ============================================================
# АДМИНКА — статус парсера
# ============================================================

@app.route('/admin/parser_status')
@require_admin
def admin_parser_status():
    log_path = '/tmp/parser_subprocess.log'
    running = parser_is_running()

    log_tail = ''
    log_size = 0
    if os.path.exists(log_path):
        try:
            log_size = os.path.getsize(log_path)
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                log_tail = ''.join(lines[-200:])
        except Exception as e:
            log_tail = f'⚠️ Не удалось прочитать лог: {e}'

    if not log_tail:
        log_tail = '(лог пуст)'

    status_class = 'status-on' if running else 'status-off'
    status_text = '🟢 РАБОТАЕТ' if running else '🔴 ОСТАНОВЛЕН'

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🚀 Статус парсера</h1>
        <p>Состояние: <span class="{status_class}">{status_text}</span></p>
        <p>Лог: <code>{log_path}</code> ({log_size} байт)</p>
        <a href="/admin" class="btn btn-gray">← В админку</a>
        <a href="/admin/parser_status" class="btn">🔄 Обновить</a>
        <a href="/admin/run_parser?limit=300" class="btn btn-green" onclick="return confirm('Запустить парсер (300)?')">🚀 Запустить парсер</a>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
    </div>
    <div class="card">
        <h2>📜 Последние 200 строк лога</h2>
        <pre class="log">{log_tail}</pre>
    </div>
    <script>
        const running = {str(running).lower()};
        if (running) {{
            setTimeout(() => location.reload(), 10000);
        }}
    </script>
    """


# ============================================================
# АДМИНКА — настройки
# ============================================================

@app.route('/admin/settings', methods=['GET', 'POST'])
@require_admin
def admin_settings():
    saved = False
    if request.method == 'POST':
        for s in SECTIONS:
            key = f"chat_id_{s['name']}"
            value = (request.form.get(key) or '').strip()
            bot_db.set_setting(key, value)
        bot_db.set_setting('schedule_start', (request.form.get('schedule_start') or '06:00').strip())
        bot_db.set_setting('schedule_end', (request.form.get('schedule_end') or '20:00').strip())
        daily_limit = (request.form.get('daily_limit') or '150').strip()
        try:
            int(daily_limit)
        except ValueError:
            daily_limit = '150'
        bot_db.set_setting('daily_limit', daily_limit)
        saved = True

    sections = get_sections_from_db(bot_db)
    schedule = get_schedule_from_db(bot_db)

    chat_fields = ""
    for s in sections:
        chat_fields += f"""
        <div class="form-row">
            <label>{s.get('title', s['name'])}:</label>
            <input type="text" name="chat_id_{s['name']}" value="{s['chat_id']}" placeholder="-00000000000000">
        </div>
        """

    saved_msg = '<div class="success-msg">✅ Настройки сохранены</div>' if saved else ''

    return BASE_STYLE + f"""
    <div class="card">
        <h1>⚙️ Настройки</h1>
        <a href="/admin" class="btn">← Назад</a>
    </div>
    <form method="POST">
        <div class="card">
            <h2>📢 chat_id групп MAX</h2>
            {chat_fields}
        </div>
        <div class="card">
            <h2>⏰ Расписание (МСК)</h2>
            <div class="form-row"><label>Начало:</label><input type="text" name="schedule_start" value="{schedule['start']}"></div>
            <div class="form-row"><label>Конец:</label><input type="text" name="schedule_end" value="{schedule['end']}"></div>
            <div class="form-row"><label>Лимит/день:</label><input type="text" name="daily_limit" value="{schedule['daily_limit']}"></div>
            <p class="hint">Интервал между постами = (конец − начало) / лимит, со случайным разбросом ±30%</p>
        </div>
        {saved_msg}
        <div class="card">
            <button type="submit" class="btn btn-green">💾 Сохранить</button>
            <a href="/admin" class="btn btn-gray">Отмена</a>
        </div>
    </form>
    """


# ============================================================
# АДМИНКА — Опубликовано сегодня
# ============================================================

@app.route('/admin/today')
@require_admin
def admin_today():
    pubs = bot_db.get_publications_today_full()

    rows_html = ""
    for i, p in enumerate(pubs, 1):
        max_cell = (f'<a href="{p["max_post_url"]}" target="_blank">🔗 MAX</a>'
                    if p.get('max_post_url') else '—')
        src_cell = (f'<a href="{p["source_url"]}" target="_blank">🔗 Источник</a>'
                    if p.get('source_url') and str(p['source_url']).startswith('http') else '—')
        rows_html += f"""
        <tr>
            <td>{i}</td>
            <td>{p.get('date','')}</td>
            <td>{p.get('time','')}</td>
            <td>{max_cell}</td>
            <td>{src_cell}</td>
            <td>{p.get('title','')}</td>
            <td>{p.get('code','')}</td>
            <td>{p.get('price','') or ''}</td>
        </tr>
        """

    if not rows_html:
        rows_html = "<tr><td colspan='8' style='text-align:center;color:#999'>Пока ничего не опубликовано</td></tr>"

    return BASE_STYLE + f"""
    <div class="card">
        <h1>📅 Опубликовано сегодня</h1>
        <p>Всего: <span class="counter-big" id="totalCount">{len(pubs)}</span></p>
        <div style="margin-top:15px">
            <a href="/admin" class="btn btn-gray">← Назад</a>
            <button class="btn" onclick="refreshTable()">🔄 Обновить</button>
            <a href="/api/today/export" class="btn btn-green">📥 Скачать Excel</a>
            <button class="btn btn-orange" onclick="copyMaxLinks()">📋 Копировать ссылки MAX</button>
        </div>
        <p class="hint" id="lastUpdate">Автообновление каждые 15 сек</p>
    </div>

    <div class="card">
        <div style="max-height: 600px; overflow-y: auto;">
            <table>
                <thead>
                    <tr>
                        <th>№</th>
                        <th>Дата</th>
                        <th>Время (МСК)</th>
                        <th>Ссылка на пост</th>
                        <th>Ссылка-источник</th>
                        <th>Название</th>
                        <th>Код</th>
                        <th>Цена</th>
                    </tr>
                </thead>
                <tbody id="todayTable">
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        let lastMaxLinks = [];

        function escapeHtml(s) {{
            if (!s) return '';
            return String(s).replace(/[&<>"']/g, c => ({{
                '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
            }})[c]);
        }}

        async function refreshTable() {{
            try {{
                const resp = await fetch('/api/today?_=' + Date.now());
                const data = await resp.json();
                if (!data.success) return;

                const tbody = document.getElementById('todayTable');
                const pubs = data.publications || [];
                lastMaxLinks = pubs.map(p => p.max_post_url).filter(Boolean);

                if (pubs.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#999">Пока ничего не опубликовано</td></tr>';
                }} else {{
                    tbody.innerHTML = pubs.map((p, i) => {{
                        const maxCell = p.max_post_url
                            ? `<a href="${{escapeHtml(p.max_post_url)}}" target="_blank">🔗 MAX</a>`
                            : '—';
                        const srcCell = (p.source_url && p.source_url.startsWith('http'))
                            ? `<a href="${{escapeHtml(p.source_url)}}" target="_blank">🔗 Источник</a>`
                            : '—';
                        return `<tr>
                            <td>${{i+1}}</td>
                            <td>${{escapeHtml(p.date)}}</td>
                            <td>${{escapeHtml(p.time)}}</td>
                            <td>${{maxCell}}</td>
                            <td>${{srcCell}}</td>
                            <td>${{escapeHtml(p.title)}}</td>
                            <td>${{escapeHtml(p.code)}}</td>
                            <td>${{escapeHtml(p.price)}}</td>
                        </tr>`;
                    }}).join('');
                }}

                document.getElementById('totalCount').textContent = pubs.length;
                document.getElementById('lastUpdate').textContent =
                    'Обновлено: ' + new Date().toLocaleTimeString('ru-RU') +
                    ' (автообновление каждые 15 сек)';
            }} catch (e) {{
                console.error(e);
            }}
        }}

        function copyMaxLinks() {{
            if (lastMaxLinks.length === 0) {{
                alert('Нет ссылок для копирования');
                return;
            }}
            const text = lastMaxLinks.join('\\n');
            navigator.clipboard.writeText(text).then(
                () => alert('✅ Скопировано ' + lastMaxLinks.length + ' ссылок'),
                () => alert('❌ Не удалось скопировать')
            );
        }}

        setInterval(refreshTable, 15000);
    </script>
    """


@app.route('/api/today')
@require_admin
def api_today():
    pubs = bot_db.get_publications_today_full()
    resp = jsonify({'success': True, 'publications': pubs, 'count': len(pubs)})
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    return resp


@app.route('/api/today/export')
@require_admin
def api_today_export():
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    pubs = bot_db.get_publications_today_full()
    today = datetime.now().strftime('%Y%m%d_%H%M%S')

    wb = Workbook()
    ws = wb.active
    ws.title = "Отчет"

    headers = ['№', 'Дата', 'Время (МСК)', 'Ссылка на пост',
               'Ссылка (источник)', 'Название', 'Код предложения', 'Цена в лизинге']

    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Border(
        left=Side(style="thin", color="D0D0D0"),
        right=Side(style="thin", color="D0D0D0"),
        top=Side(style="thin", color="D0D0D0"),
        bottom=Side(style="thin", color="D0D0D0"),
    )

    title = ws.cell(row=1, column=1, value="Отчет по публикациям")
    title.font = Font(bold=True, size=14)
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells('A1:H1')
    ws.row_dimensions[1].height = 30

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin
    ws.row_dimensions[2].height = 25

    link_font = Font(color="0563C1", underline="single", size=10)
    text_font = Font(size=10)
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for i, p in enumerate(pubs, 1):
        r = i + 2
        vals = [
            i,
            p.get('date', ''),
            p.get('time', ''),
            p.get('max_post_url', ''),
            p.get('source_url', ''),
            p.get('title', ''),
            p.get('code', ''),
            p.get('price', ''),
        ]
        for col, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=col, value=val)
            cell.font = text_font
            cell.border = thin
            cell.alignment = center if col in (1, 2, 3) else left
            if col in (4, 5) and val and str(val).startswith('http'):
                cell.font = link_font
                cell.hyperlink = val

    widths = {'A': 5, 'B': 12, 'C': 12, 'D': 45, 'E': 55, 'F': 35, 'G': 22, 'H': 18}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A3'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"Отчет_{today}.xlsx"
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


# ============================================================
# АДМИНКА — ОЧЕРЕДЬ
# ============================================================

@app.route('/admin/queue')
@require_admin
def admin_queue():
    ads = bot_db.get_all_pending_ads()

    cards_html = ""
    for i, ad in enumerate(ads, 1):
        ad_id = ad['id']
        media_path = ad.get('media_path') or ''
        title = ad.get('title') or '—'
        category = ad.get('category') or '—'
        chat_id = ad.get('chat_id') or '—'
        price = ad.get('price') or '—'
        code = ad.get('code') or '—'
        city = ad.get('city') or '—'
        year = ad.get('year') or '—'
        mileage = ad.get('mileage') or '—'
        source_url = ad.get('source_url') or ''
        created = ad.get('created_at') or ''

        photos_html = ""
        if media_path and os.path.exists(media_path):
            try:
                files = sorted(os.listdir(media_path))
                photos = []
                videos = []
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in IMAGE_EXTS:
                        photos.append(f)
                    elif ext in VIDEO_EXTS:
                        videos.append(f)

                if videos:
                    for v in videos[:1]:
                        photos_html += f'''
                            <div style="position:relative;display:inline-block;margin:2px">
                                <div style="width:120px;height:90px;background:#000;
                                            border-radius:4px;display:flex;
                                            align-items:center;justify-content:center;
                                            color:white;font-size:32px">🎬</div>
                                <a href="/admin/queue_photo_delete/{ad_id}/{v}"
                                   style="position:absolute;top:2px;right:2px;
                                          background:rgba(220,53,69,0.9);color:white;
                                          padding:2px 6px;border-radius:3px;
                                          font-size:11px;text-decoration:none;
                                          cursor:pointer;font-weight:bold;
                                          line-height:1"
                                   onclick="event.stopPropagation();return confirm('Удалить видео?');"
                                   title="Удалить видео">✕</a>
                            </div>
                        '''
                elif photos:
                    for pf in photos[:5]:
                        photos_html += f'''
                            <div style="position:relative;display:inline-block;margin:2px">
                                <img src="/admin/queue_photo/{ad_id}/{pf}"
                                     style="width:120px;height:90px;object-fit:cover;
                                            border-radius:4px;cursor:pointer;display:block"
                                     onclick="window.open(this.src, '_blank')">
                                <a href="/admin/queue_photo_delete/{ad_id}/{pf}"
                                   style="position:absolute;top:2px;right:2px;
                                          background:rgba(220,53,69,0.9);color:white;
                                          padding:2px 6px;border-radius:3px;
                                          font-size:11px;text-decoration:none;
                                          cursor:pointer;font-weight:bold;
                                          line-height:1"
                                   onclick="event.stopPropagation();return confirm('Удалить это фото?');"
                                   title="Удалить фото">✕</a>
                            </div>
                        '''
            except Exception as e:
                photos_html = f'<span style="color:#999">Ошибка: {e}</span>'
        else:
            photos_html = '<span style="color:#dc3545">❌ Папка не найдена</span>'

        text_preview = ''
        if media_path:
            info_path = os.path.join(media_path, 'info.txt')
            if os.path.exists(info_path):
                try:
                    with open(info_path, 'r', encoding='utf-8') as f:
                        text_preview = f.read()[:300]
                    text_preview = text_preview.replace('\n', '<br>')
                except Exception:
                    text_preview = '⚠️ Не читается'
            else:
                text_preview = '⚠️ Нет info.txt'

        up_disabled = (i == 1)
        down_disabled = (i == len(ads))

        up_style = 'font-size:13px;padding:6px 10px;text-align:center'
        down_style = 'font-size:13px;padding:6px 10px;text-align:center'
        if up_disabled:
            up_style += ';pointer-events:none;opacity:0.4'
        if down_disabled:
            down_style += ';pointer-events:none;opacity:0.4'

        cards_html += f'''
        <div class="card" id="ad_{ad_id}" style="padding:15px">
            <div style="display:flex;gap:15px;flex-wrap:wrap">
                <div style="flex:0 0 140px">
                    <div style="background:#f8f9fa;padding:8px;border-radius:5px;
                                text-align:center;font-weight:bold;font-size:20px">
                        #{i}
                    </div>
                    <div style="font-size:11px;color:#666;margin-top:5px;text-align:center">
                        ID {ad_id}
                    </div>
                </div>

                <div style="flex:0 0 400px">
                    {photos_html}
                </div>

                <div style="flex:1;min-width:300px">
                    <p style="margin:0 0 8px 0;font-size:16px;font-weight:bold">
                        {title}
                    </p>
                    <p style="margin:0 0 4px 0;font-size:13px;color:#555">
                        📂 <b>{category}</b> → <code>{chat_id}</code>
                    </p>
                    <p style="margin:0 0 4px 0;font-size:13px;color:#555">
                        💰 {price} ₽ · 📋 {code} · 📍 {city} · 📅 {year} · 🛣️ {mileage} км
                    </p>
                    <p style="margin:8px 0 4px 0;font-size:12px;color:#666">
                        <a href="{source_url}" target="_blank">🔗 Источник</a>
                        · <a href="/admin/ad_detail/{ad_id}">👁️ Детали</a>
                        · <span style="color:#999">создано: {created}</span>
                    </p>
                    <details style="margin-top:8px">
                        <summary style="cursor:pointer;font-size:13px;color:#007bff">
                            Показать текст поста
                        </summary>
                        <div style="background:#f8f9fa;padding:10px;border-radius:5px;
                                    margin-top:5px;font-size:12px;line-height:1.5;
                                    max-height:200px;overflow-y:auto">
                            {text_preview}
                        </div>
                    </details>
                </div>

                <div style="flex:0 0 180px;display:flex;flex-direction:column;gap:5px">
                    <a href="/admin/queue_move/{ad_id}/up"
                       class="btn" style="{up_style}">
                        ⬆️ Вверх
                    </a>
                    <a href="/admin/queue_move/{ad_id}/down"
                       class="btn" style="{down_style}">
                        ⬇️ Вниз
                    </a>
                    <a href="/admin/publish_ad/{ad_id}"
                       class="btn btn-orange"
                       style="font-size:13px;padding:6px 10px;text-align:center"
                       onclick="return confirm('Опубликовать это объявление СЕЙЧАС?')">
                        📤 Опубликовать
                    </a>
                    <a href="/admin/queue_delete/{ad_id}"
                       class="btn btn-red"
                       style="font-size:13px;padding:6px 10px;text-align:center"
                       onclick="return confirm('Удалить объявление #{i} ({title})? Папка с медиа тоже будет удалена.')">
                        🗑️ Удалить
                    </a>
                </div>
            </div>
        </div>
        '''

    if not cards_html:
        cards_html = '''
        <div class="card">
            <p style="text-align:center;color:#999;font-size:16px">
                Очередь пуста. Запустите парсер или импортируйте папки.
            </p>
            <div style="text-align:center;margin-top:15px">
                <a href="/admin/run_parser?limit=5" class="btn btn-green">🚀 Парсер (5)</a>
                <a href="/admin/import_folder" class="btn btn-green">📥 Импорт папок</a>
            </div>
        </div>
        '''

    summary = bot_db.get_queue_category_summary()
    summary_html = ""
    if summary:
        summary_items = " · ".join(
            f"<b>{cat}</b>: {cnt}" for cat, cnt in summary.items()
        )
        summary_html = f'<p class="hint">📊 По категориям: {summary_items}</p>'

    return BASE_STYLE + f"""
    <div class="card">
        <h1>📋 Очередь на публикацию ({len(ads)})</h1>
        <p class="hint">
            Порядок публикации соответствует порядку карточек (сверху вниз).
            Меняйте кнопками ⬆️/⬇️. Автопубликация берёт <b>самое верхнее</b> объявление.
        </p>
        {summary_html}
        <a href="/admin" class="btn">← Назад</a>
        <a href="/admin/queue" class="btn btn-gray">🔄 Обновить</a>
        <a href="/admin/queue_resort" class="btn"
           onclick="return confirm('Пересчитать порядок: перемешать категории? Текущий ручной порядок будет сброшен.')">
            🔀 Перемешать по категориям
        </a>
        <a href="/admin/import_folder" class="btn btn-green">📥 Импорт папок</a>
        <a href="/admin/parser_status" class="btn">🚀 Статус парсера</a>
        <a href="/admin/queue_clear" class="btn btn-red"
           onclick="return confirm('Удалить ВСЮ очередь и все папки? Отменить нельзя.')">
            🗑️ Очистить всю очередь
        </a>
    </div>
    {cards_html}
    """


@app.route('/admin/queue_photo/<int:ad_id>/<path:filename>')
@require_admin
def admin_queue_photo(ad_id, filename):
    ad = bot_db.get_ad_by_id(ad_id)
    if not ad:
        return 'Not found', 404

    media_path = ad.get('media_path')
    if not media_path or not os.path.exists(media_path):
        return 'Not found', 404

    safe_name = os.path.basename(filename)
    file_path = os.path.join(media_path, safe_name)

    if not os.path.exists(file_path):
        return 'Not found', 404

    ext = os.path.splitext(safe_name)[1].lower()
    if ext == '.mp4':
        mimetype = 'video/mp4'
    elif ext == '.mov':
        mimetype = 'video/quicktime'
    elif ext == '.webm':
        mimetype = 'video/webm'
    elif ext == '.png':
        mimetype = 'image/png'
    elif ext == '.gif':
        mimetype = 'image/gif'
    elif ext == '.webp':
        mimetype = 'image/webp'
    else:
        mimetype = 'image/jpeg'

    return send_file(file_path, mimetype=mimetype)


@app.route('/admin/queue_photo_delete/<int:ad_id>/<path:filename>')
@require_admin
def admin_queue_photo_delete(ad_id, filename):
    ok, message, remaining = bot_db.delete_ad_photo(ad_id, filename, keep_min=1)

    if ok:
        logger.info(f'🗑️ Файл удалён: {filename} (осталось {remaining})')
    else:
        logger.warning(f'⚠️ Не удалось удалить {filename}: {message}')

    ref = request.referrer or ''
    if f'/admin/ad_detail/{ad_id}' in ref:
        return redirect(f'/admin/ad_detail/{ad_id}')
    return redirect('/admin/queue')


@app.route('/admin/queue_delete/<int:ad_id>')
@require_admin
def admin_queue_delete(ad_id):
    bot_db.delete_ad(ad_id, delete_files=True)
    return redirect('/admin/queue')


@app.route('/admin/queue_move/<int:ad_id>/<direction>')
@require_admin
def admin_queue_move(ad_id, direction):
    bot_db.move_ad(ad_id, direction)
    return redirect('/admin/queue')


@app.route('/admin/queue_resort')
@require_admin
def admin_queue_resort():
    try:
        count = bot_db.resort_queue_by_categories()
        return BASE_STYLE + f"""
        <div class="card">
            <h1>🔀 Очередь перемежена</h1>
            <p>Обработано записей: <b>{count}</b></p>
            <p class="hint">
                Теперь публикация пойдёт по очереди в разные категории:
                <b>Самосвал → Тягач → Дорожная → Прицеп → Легковые → снова...</b>
            </p>
            <a href="/admin/queue" class="btn">📋 К очереди</a>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """
    except Exception as e:
        logger.exception(f'❌ Ошибка перемежения: {e}')
        return BASE_STYLE + f"""
        <div class="card">
            <h1>❌ Ошибка</h1>
            <div class="error-msg">{e}</div>
            <a href="/admin/queue" class="btn">← К очереди</a>
        </div>
        """


@app.route('/admin/queue_clear')
@require_admin
def admin_queue_clear():
    ads = bot_db.get_all_pending_ads()
    deleted_folders = 0
    deleted_records = 0

    for ad in ads:
        media_path = ad.get('media_path')
        if media_path and os.path.exists(media_path):
            try:
                shutil.rmtree(media_path)
                deleted_folders += 1
            except Exception as e:
                logger.warning(f'⚠️ Не удалить {media_path}: {e}')

        bot_db.delete_ad(ad['id'], delete_files=False)
        deleted_records += 1

    logger.info(f'🗑️ Очередь очищена: {deleted_records} записей, {deleted_folders} папок')

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🗑️ Очередь очищена</h1>
        <p>Удалено записей: <b>{deleted_records}</b></p>
        <p>Удалено папок: <b>{deleted_folders}</b></p>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
        <a href="/admin" class="btn btn-gray">← В админку</a>
    </div>
    """


@app.route('/admin/ad_detail/<int:ad_id>')
@require_admin
def admin_ad_detail(ad_id):
    ad = bot_db.get_ad_by_id(ad_id)
    if not ad:
        return BASE_STYLE + '''
        <div class="card"><h1>❌ Не найдено</h1>
        <a href="/admin/queue" class="btn">← К очереди</a></div>
        '''

    media_path = ad.get('media_path') or ''
    text = ''
    photos_html = ''
    photos_count = 0
    videos_count = 0
    can_delete = False

    if media_path and os.path.exists(media_path):
        info_path = os.path.join(media_path, 'info.txt')
        if os.path.exists(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except Exception as e:
                text = f'⚠️ Ошибка чтения: {e}'

        try:
            files = sorted(os.listdir(media_path))
            photos = []
            videos = []
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in IMAGE_EXTS:
                    photos.append(f)
                elif ext in VIDEO_EXTS:
                    videos.append(f)

            photos_count = len(photos)
            videos_count = len(videos)
            total_media = photos_count + videos_count
            can_delete = (total_media > 1)

            for vf in videos:
                if can_delete:
                    badge = f'''
                        <a href="/admin/queue_photo_delete/{ad_id}/{vf}"
                           class="photo-del"
                           onclick="return confirm('Удалить это видео?')"
                           title="Удалить видео">🗑️</a>
                    '''
                else:
                    badge = '<span class="photo-del-locked" title="Нельзя удалить последнее">🔒</span>'

                photos_html += f'''
                    <div class="photo-wrap">
                        <video src="/admin/queue_photo/{ad_id}/{vf}"
                               controls
                               style="width:400px;height:auto;border-radius:6px;display:block"></video>
                        {badge}
                    </div>
                '''

            for pf in photos:
                if can_delete:
                    badge = f'''
                        <a href="/admin/queue_photo_delete/{ad_id}/{pf}"
                           class="photo-del"
                           onclick="return confirm('Удалить это фото?')"
                           title="Удалить это фото">🗑️</a>
                    '''
                else:
                    badge = '<span class="photo-del-locked" title="Нельзя удалить последнее">🔒</span>'

                photos_html += f'''
                    <div class="photo-wrap">
                        <img src="/admin/queue_photo/{ad_id}/{pf}"
                             style="width:300px;height:auto;border-radius:6px;
                                    cursor:pointer;display:block"
                             onclick="window.open(this.src, '_blank')">
                        {badge}
                    </div>
                '''
        except Exception as e:
            photos_html = f'<p style="color:red">Ошибка: {e}</p>'

    return BASE_STYLE + f"""
    <div class="card">
        <h1>👁️ Предпросмотр #{ad_id}</h1>
        <a href="/admin/queue" class="btn">← К очереди</a>
        <a href="/admin/publish_ad/{ad_id}" class="btn btn-orange"
           onclick="return confirm('Опубликовать сейчас?')">📤 Опубликовать сейчас</a>
        <a href="/admin/queue_delete/{ad_id}" class="btn btn-red"
           onclick="return confirm('Удалить всё объявление?')">🗑️ Удалить всё</a>
    </div>

    <div class="card">
        <h2>🎬 Медиа (фото: {photos_count}, видео: {videos_count})</h2>
        <p class="hint">
            Клик по фото — открыть в новой вкладке.
            Клик по 🗑️ — удалить это медиа.
            {'Последнее медиа удалить нельзя.' if not can_delete else ''}
        </p>
        <div>{photos_html or '<p style="color:#999">Нет медиа</p>'}</div>
    </div>

    <div class="card">
        <h2>📝 Текст поста (info.txt)</h2>
        <pre style="background:#f8f9fa;padding:15px;border-radius:5px;
                    white-space:pre-wrap;font-size:13px;line-height:1.6">{text}</pre>
    </div>

    <div class="card">
        <h2>📊 Метаданные</h2>
        <table>
            <tr><td>ID</td><td>{ad_id}</td></tr>
            <tr><td>Категория</td><td>{ad.get('category', '')}</td></tr>
            <tr><td>chat_id</td><td>{ad.get('chat_id', '')}</td></tr>
            <tr><td>Название</td><td>{ad.get('title', '')}</td></tr>
            <tr><td>Код</td><td>{ad.get('code', '')}</td></tr>
            <tr><td>Цена</td><td>{ad.get('price', '')}</td></tr>
            <tr><td>Город</td><td>{ad.get('city', '')}</td></tr>
            <tr><td>Год</td><td>{ad.get('year', '')}</td></tr>
            <tr><td>Пробег</td><td>{ad.get('mileage', '')}</td></tr>
            <tr><td>Папка</td><td><code>{media_path}</code></td></tr>
            <tr><td>Источник</td><td><a href="{ad.get('source_url', '')}" target="_blank">🔗 Открыть</a></td></tr>
            <tr><td>Создано</td><td>{ad.get('created_at', '')}</td></tr>
            <tr><td>sort_order</td><td>{ad.get('sort_order', '')}</td></tr>
        </table>
    </div>
    """


@app.route('/admin/publish_ad/<int:ad_id>')
@require_admin
def admin_publish_ad(ad_id):
    ad = bot_db.get_ad_by_id(ad_id)
    if not ad:
        return BASE_STYLE + '''
        <div class="card"><h1>❌ Не найдено</h1>
        <a href="/admin/queue" class="btn">← К очереди</a></div>
        '''

    if ad.get('status') != 'pending':
        return BASE_STYLE + f'''
        <div class="card">
            <h1>⚠️ Объявление уже не в очереди</h1>
            <p>Статус: <b>{ad.get('status')}</b></p>
            <a href="/admin/queue" class="btn">← К очереди</a>
        </div>
        '''

    logger.info(f'📤 Публикация # {ad_id}: {ad.get("folder_name")}')

    try:
        ok, message, post_link = publish_one_ad(ad)
        if ok:
            link_html = (f'<p>🔗 <a href="{post_link}" target="_blank">{post_link}</a></p>'
                         if post_link else '')
            return BASE_STYLE + f"""
            <div class="card">
                <h1>✅ Опубликовано</h1>
                <p><b>{ad.get('title', '')}</b></p>
                <p>Категория: <code>{ad.get('category', '')}</code></p>
                {link_html}
                <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
                <a href="/admin/queue" class="btn">📋 К очереди</a>
                <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
                <a href="/admin" class="btn btn-gray">← В админку</a>
            </div>
            """
        else:
            bot_db.mark_ad_failed(ad['id'], message)
            return BASE_STYLE + f"""
            <div class="card">
                <h1>❌ Ошибка публикации</h1>
                <p><b>{ad.get('title', '')}</b></p>
                <div class="error-msg">{message}</div>
                <a href="/admin/queue" class="btn">← К очереди</a>
                <a href="/admin/publish_ad/{ad_id}" class="btn btn-orange">Попробовать ещё</a>
            </div>
            """
    except Exception as e:
        logger.exception(f'❌ Ошибка публикации: {e}')
        bot_db.mark_ad_failed(ad['id'], str(e))
        return BASE_STYLE + f"""
        <div class="card">
            <h1>❌ Ошибка</h1>
            <div class="error-msg">{e}</div>
            <a href="/admin/queue" class="btn">← К очереди</a>
        </div>
        """


# ============================================================
# Прочие роуты
# ============================================================

@app.route('/admin/check_paths')
@require_admin
def admin_check_paths():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, folder_name, media_path FROM parsed_ads")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    result = {'total': len(rows), 'exists': 0, 'missing': 0, 'missing_details': []}
    for r in rows:
        mp = r.get('media_path')
        if mp and os.path.exists(mp):
            result['exists'] += 1
        else:
            result['missing'] += 1
            result['missing_details'].append({
                'id': r['id'],
                'folder': r.get('folder_name'),
                'path': mp,
            })
    return jsonify(result)


@app.route('/admin/list_folders')
@require_admin
def admin_list_folders():
    if not os.path.exists(OUTPUT_DIR):
        return jsonify({'error': f'Папка не существует: {OUTPUT_DIR}', 'files': []})
    try:
        files = sorted(os.listdir(OUTPUT_DIR))
    except Exception as e:
        return jsonify({'error': str(e), 'files': []})
    return jsonify({'output_dir': OUTPUT_DIR, 'files': files, 'count': len(files)})


@app.route('/admin/cleanup_orphans')
@require_admin
def admin_cleanup_orphans():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, folder_name, media_path FROM parsed_ads")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    deleted = 0
    for r in rows:
        mp = r.get('media_path')
        if not mp or not os.path.exists(mp):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM parsed_ads WHERE id = ?", (r['id'],))
            conn.commit()
            conn.close()
            deleted += 1

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🧹 Очистка завершена</h1>
        <p>Удалено «мёртвых» записей: <b>{deleted}</b> из <b>{len(rows)}</b></p>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
        <a href="/admin" class="btn btn-gray">← В админку</a>
    </div>
    """


@app.route('/admin/clear_all')
@require_admin
def admin_clear_all():
    result = {
        'parsed_ads_deleted': 0,
        'publications_deleted': 0,
        'settings_deleted': 0,
        'folders_deleted': 0,
        'folder_errors': 0,
    }

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM parsed_ads")
        result['parsed_ads_deleted'] = c.fetchone()[0]
        c.execute("DELETE FROM parsed_ads")

        c.execute("SELECT COUNT(*) FROM publications")
        result['publications_deleted'] = c.fetchone()[0]
        c.execute("DELETE FROM publications")

        c.execute("SELECT COUNT(*) FROM settings")
        result['settings_deleted'] = c.fetchone()[0]
        c.execute("DELETE FROM settings")

        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception(f'❌ Ошибка очистки БД: {e}')

    for folder in [OUTPUT_DIR, IMPORT_DIR]:
        try:
            if os.path.exists(folder):
                for item in os.listdir(folder):
                    item_path = os.path.join(folder, item)
                    if os.path.isdir(item_path):
                        try:
                            shutil.rmtree(item_path)
                            result['folders_deleted'] += 1
                        except Exception as e:
                            result['folder_errors'] += 1
                            logger.warning(f'⚠️ Не удалить {item_path}: {e}')
        except Exception as e:
            logger.exception(f'❌ Ошибка очистки {folder}: {e}')

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🗑️ Полная очистка выполнена</h1>
        <table>
            <tr><th>Что</th><th>Удалено</th></tr>
            <tr><td>Очередь парсинга (<code>parsed_ads</code>)</td><td><b>{result['parsed_ads_deleted']}</b></td></tr>
            <tr><td>Журнал публикаций (<code>publications</code>)</td><td><b>{result['publications_deleted']}</b></td></tr>
            <tr><td>Настройки (<code>settings</code>)</td><td><b>{result['settings_deleted']}</b></td></tr>
            <tr><td>Папки с медиа</td><td><b>{result['folders_deleted']}</b></td></tr>
        </table>
        {f'<div class="error-msg">⚠️ Ошибок при удалении папок: {result["folder_errors"]}</div>' if result['folder_errors'] else ''}
        <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
        <a href="/admin" class="btn btn-gray">← В админку</a>
        <a href="/admin/settings" class="btn">⚙️ Проверить настройки</a>
    </div>
    """


# ============================================================
# Защита от двойного запуска парсера
# ============================================================

_PARSER_PROC = [None]


def parser_is_running() -> bool:
    proc = _PARSER_PROC[0]
    if proc is None:
        return False
    if proc.poll() is not None:
        _PARSER_PROC[0] = None
        return False
    return True


@app.route('/admin/run_parser')
@require_admin
def admin_run_parser():
    limit = int(request.args.get('limit', 50))

    if parser_is_running():
        return BASE_STYLE + """
        <div class="card">
            <h1>⚠️ Парсер уже запущен</h1>
            <p>Дождитесь завершения текущего процесса.</p>
            <a href="/admin" class="btn btn-gray">← В админку</a>
            <a href="/admin/parser_status" class="btn">🚀 Статус парсера</a>
        </div>
        """

    log_path = '/tmp/parser_subprocess.log'

    try:
        log_file = open(log_path, 'a', encoding='utf-8')
        proc = subprocess.Popen(
            [sys.executable, '-u', '-m', 'parser_runner', '--limit', str(limit)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            bufsize=1,
        )
        _PARSER_PROC[0] = proc
        logger.info(f'🚀 Парсер запущен PID={proc.pid}, лимит={limit}, лог={log_path}')

        def _close_log_when_done():
            proc.wait()
            try:
                log_file.close()
            except Exception:
                pass

        threading.Thread(target=_close_log_when_done, daemon=True).start()

        return BASE_STYLE + f"""
        <div class="card">
            <h1>🚀 Парсер запущен (отдельный процесс)</h1>
            <p>PID: <b>{proc.pid}</b></p>
            <p>Лимит: <b>{limit}</b></p>
            <p>Лог: <code>{log_path}</code></p>
            <p class="hint">После завершения процесс умрёт, и вся память вернётся ОС.</p>
            <a href="/admin/parser_status" class="btn">🚀 Смотреть прогресс</a>
            <a href="/admin/queue" class="btn">📋 Очередь</a>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """
    except Exception as e:
        logger.exception(f'❌ Не удалось запустить парсер: {e}')
        return BASE_STYLE + f"""
        <div class="card">
            <h1>❌ Ошибка запуска парсера</h1>
            <div class="error-msg">{e}</div>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """


@app.route('/admin/publish_one')
@require_admin
def admin_publish_one():
    ads = bot_db.get_pending_ads(limit=1)
    if not ads:
        return BASE_STYLE + """
        <div class="card">
            <h1>⚠️ Очередь пуста</h1>
            <a href="/admin/run_parser?limit=5" class="btn btn-green">🚀 Парсер (5)</a>
            <a href="/admin/import_folder" class="btn btn-green">📥 Импорт папок</a>
            <a href="/admin/queue" class="btn">📋 Очередь</a>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """

    ad = ads[0]
    logger.info(f'📤 Публикация: {ad.get("folder_name")}')

    try:
        ok, message, post_link = publish_one_ad(ad)
        if ok:
            link_html = f'<p>🔗 <a href="{post_link}" target="_blank">{post_link}</a></p>' if post_link else ''
            return BASE_STYLE + f"""
            <div class="card">
                <h1>✅ Опубликовано</h1>
                <p><b>{ad.get('title', '')}</b></p>
                <p>Категория: <code>{ad.get('category', '')}</code></p>
                {link_html}
                <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
                <a href="/admin/publish_one" class="btn btn-orange">📤 Опубликовать ещё 1</a>
                <a href="/admin/queue" class="btn">📋 К очереди</a>
                <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
                <a href="/admin" class="btn btn-gray">← В админку</a>
            </div>
            """
        else:
            bot_db.mark_ad_failed(ad['id'], message)
            return BASE_STYLE + f"""
            <div class="card">
                <h1>❌ Ошибка публикации</h1>
                <p><b>{ad.get('title', '')}</b></p>
                <div class="error-msg">{message}</div>
                <a href="/admin/publish_one" class="btn btn-orange">Попробовать ещё</a>
                <a href="/admin/queue" class="btn">← К очереди</a>
                <a href="/admin" class="btn btn-gray">← В админку</a>
            </div>
            """
    except Exception as e:
        logger.exception(f'❌ Ошибка публикации: {e}')
        bot_db.mark_ad_failed(ad['id'], str(e))
        return BASE_STYLE + f"""
        <div class="card">
            <h1>❌ Ошибка</h1>
            <div class="error-msg">{e}</div>
            <a href="/admin" class="btn btn-gray">← В админку</a>
        </div>
        """


# ============================================================
# Webhook MAX
# ============================================================

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        logger.info(f'📩 WEBHOOK: {data}')
        if not data:
            return jsonify({"ok": True}), 200

        if data.get('update_type') == 'message_created':
            message = data.get('message', {})
            sender = message.get('sender', {})
            body = message.get('body', {})
            user_id = sender.get('user_id')
            text = (body.get('text') or '').strip()

            logger.info(f'📨 user_id={user_id}, text={text[:100]}')

            if user_id and not is_allowed_user(user_id):
                logger.warning(f'⛔ Игнор user_id={user_id}')
                return jsonify({"ok": True}), 200

            if user_id and text == '/start':
                api.send_message(
                    user_id,
                    "🏠 **VTB Bot**\n\n"
                    f"🌐 **Админка:**\n{PUBLIC_URL}/admin\n\n"
                    f"📋 **Очередь:**\n{PUBLIC_URL}/admin/queue\n\n"
                    f"📥 **Импорт папок:**\n{PUBLIC_URL}/admin/import_folder\n\n"
                    f"📅 **Опубликовано:**\n{PUBLIC_URL}/admin/today\n\n"
                    f"⚙️ **Настройки:**\n{PUBLIC_URL}/admin/settings\n\n"
                    f"🚀 **Статус парсера:**\n{PUBLIC_URL}/admin/parser_status\n\n"
                    "🔒 Пароль спросит браузер."
                )
                return jsonify({"ok": True}), 200

            if user_id and text == '/status':
                stats = bot_db.count_by_status()
                ap_status = get_autopublish_status()
                api.send_message(
                    user_id,
                    f"📊 **Статус:**\n"
                    f"⏳ В очереди: {stats.get('pending', 0)}\n"
                    f"✅ Опубликовано: {stats.get('published', 0)}\n"
                    f"❌ Ошибок: {stats.get('failed', 0)}\n\n"
                    f"🕒 Автопубликация: {ap_status['status_message']}\n"
                    f"📅 Сегодня: {ap_status['today_count']}/{ap_status['daily_limit']}"
                )
                return jsonify({"ok": True}), 200

            if user_id and text == '/myid':
                api.send_message(user_id, f"Твой user_id: `{user_id}`")
                return jsonify({"ok": True}), 200

            if user_id and text == '/autopublish_on':
                start_auto_publish()
                api.send_message(user_id, "✅ Автопубликация включена")
                return jsonify({"ok": True}), 200

            if user_id and text == '/autopublish_off':
                stop_auto_publish()
                api.send_message(user_id, "🛑 Автопубликация выключена")
                return jsonify({"ok": True}), 200

        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception(f'❌ webhook: {e}')
        return jsonify({"ok": False}), 500


# ============================================================
# Запуск
# ============================================================

def init_autopublish():
    enabled = bot_db.get_setting('autopublish_enabled', '0')
    if enabled == '1':
        logger.info('🔁 Автопубликация была включена — запускаем поток')
        start_auto_publish()
    else:
        logger.info('ℹ️ Автопубликация выключена')


if __name__ == '__main__':
    logger.info(f'🚀 Запуск vtb-bot на порту {PORT}')
    logger.info(f'   TOKEN: {"✅" if TOKEN else "❌"}')
    logger.info(f'   SHEETS_URL: {"✅" if SHEETS_URL else "❌"}')
    logger.info(f'   ADMIN_USER: {ADMIN_USER}')
    logger.info(f'   ADMIN_PASS: {"✅" if ADMIN_PASS else "❌"}')
    logger.info(f'   ADMIN_IDS: {ALLOWED_ADMIN_IDS if ALLOWED_ADMIN_IDS else "❌"}')
    logger.info(f'   IMPORT_FOLDER: {"✅" if IMPORT_FOLDER_AVAILABLE else "❌"}')

    init_autopublish()

    app.run(host='0.0.0.0', port=PORT, threaded=True)
