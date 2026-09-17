# app.py 2
# ============================================================
# vtb-bot — Flask-сервер (проект 1)
# + автопубликация по расписанию
# + парсер в отдельном процессе (экономия RAM)
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
                timeout=180, verify=False,
            )
            logger.info(f'📨 Шаг 2: HTTP {ur.status_code}')

            if ur.status_code != 200:
                logger.error(f'❌ Шаг 2: {ur.status_code}')
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

            if token:
                logger.info(f'✅ Токен: {str(token)[:30]}...')
            else:
                logger.error(f'❌ Токен не найден в ответе: {result}')

            return token

        except Exception as e:
            logger.exception(f'❌ upload_file упал: {e}')
            return None

    def send_post(self, chat_id, text, media_tokens):
        if not self.token:
            return False, None
        try:
            attachments = []
            for token in media_tokens[:10]:
                attachments.append({
                    "type": "image",
                    "payload": {"token": token},
                })

            payload = {"text": text, "format": "markdown"}
            if attachments:
                payload["attachments"] = attachments

            chat_id_str = str(chat_id)
            chat_id_for_api = chat_id_str if chat_id_str.startswith('-') else f"-{chat_id_str}"

            logger.info(f'📤 Отправка в {chat_id_for_api}, медиа: {len(attachments)}')

            r = requests.post(
                f"{self.base_url}/messages",
                headers={"Authorization": self.token, "Content-Type": "application/json"},
                params={"chat_id": chat_id_for_api},
                json=payload, timeout=60, verify=False,
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

    media_tokens = []
    photo_files = sorted([
        f for f in os.listdir(media_path)
        if f.startswith('photo_') and f.lower().endswith(('.jpg', '.jpeg', '.png'))
    ])
    logger.info(f'📷 Фото: {len(photo_files)}')

    for photo_file in photo_files[:10]:
        file_path = os.path.join(media_path, photo_file)
        try:
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            token = api.upload_file(file_bytes, photo_file, 'image')
            if token:
                media_tokens.append(token)
            time.sleep(0.5)
        except Exception as e:
            logger.error(f'❌ Ошибка загрузки {photo_file}: {e}')

    if not media_tokens:
        return False, 'Не удалось загрузить ни одно фото', None

    success, post_link = api.send_post(chat_id, text, media_tokens)
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
# Автопубликация по расписанию (фоновый поток)
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
    # Защита от нулевого/отрицательного сна (баг 05:59:59)
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

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🛠 Админка</h1>
        <a href="/" class="btn">← На главную</a>
        <a href="/admin/settings" class="btn">⚙️ Настройки</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/queue" class="btn">📋 Очередь</a>
        <a href="/admin/parser_status" class="btn">🚀 Статус парсера</a>
        <a href="/setup_webhook" class="btn btn-gray">🔄 Вебхук</a>
    </div>

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
    </div>

    <div class="card">
        <h2>📤 Ручная публикация</h2>
        <p>В очереди: <b>{pending}</b></p>
        <a href="/admin/publish_one" class="btn btn-orange" onclick="return confirm('Опубликовать 1?')">📤 Опубликовать 1</a>
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
# АДМИНКА — статус парсера (НОВОЕ)
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
    </div>
    <div class="card">
        <h2>📜 Последние 200 строк лога</h2>
        <pre class="log">{log_tail}</pre>
    </div>
    <script>
        // Автообновление каждые 10 сек, если парсер работает
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
        src_cell = (f'<a href="{p["source_url"]}" target="_blank">🔗 VTB</a>'
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
                            ? `<a href="${{escapeHtml(p.source_url)}}" target="_blank">🔗 VTB</a>`
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
# АДМИНКА — очередь, пути, папки, очистка, run_parser
# ============================================================

@app.route('/admin/queue')
@require_admin
def admin_queue():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM parsed_ads ORDER BY id DESC LIMIT 100')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    for r in rows:
        mp = r.get('media_path')
        if mp:
            r['path_exists'] = os.path.exists(mp)
            try:
                r['path_files'] = ', '.join(os.listdir(mp)[:10]) if r['path_exists'] else '—'
            except Exception:
                r['path_files'] = 'ошибка'
        else:
            r['path_exists'] = False
            r['path_files'] = '—'

    table_rows = ""
    for r in rows:
        exists_icon = '✅' if r['path_exists'] else '❌'
        table_rows += f"""
        <tr>
            <td>{r.get('id')}</td>
            <td>{r.get('folder_name', '—')}</td>
            <td style="font-size:11px;color:#666">{r.get('media_path', '—')}</td>
            <td>{exists_icon}</td>
            <td style="font-size:11px">{r.get('path_files', '—')}</td>
            <td>{r.get('category', '—')}</td>
            <td>{r.get('status', '—')}</td>
        </tr>
        """

    return BASE_STYLE + f"""
    <div class="card">
        <h1>📋 Очередь парсинга ({len(rows)})</h1>
        <a href="/admin" class="btn">← Назад</a>
        <a href="/admin/queue" class="btn btn-gray">🔄 Обновить</a>
        <table>
            <tr>
                <th>ID</th><th>Папка</th><th>media_path</th>
                <th>Есть?</th><th>Файлы</th>
                <th>Категория</th><th>Статус</th>
            </tr>
            {table_rows}
        </table>
    </div>
    """


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

    try:
        if os.path.exists(OUTPUT_DIR):
            for item in os.listdir(OUTPUT_DIR):
                item_path = os.path.join(OUTPUT_DIR, item)
                if os.path.isdir(item_path):
                    try:
                        shutil.rmtree(item_path)
                        result['folders_deleted'] += 1
                    except Exception as e:
                        result['folder_errors'] += 1
                        logger.warning(f'⚠️ Не удалить {item_path}: {e}')
    except Exception as e:
        logger.exception(f'❌ Ошибка очистки папок: {e}')

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

_PARSER_PROC = [None]   # храним объект Popen, а не PID


def parser_is_running() -> bool:
    """
    Жив ли процесс парсера, запущенный ИМЕННО этим Flask-процессом.
    Используем proc.poll() — надёжнее, чем os.kill(pid, 0),
    потому что исключает случай с переиспользованием PID.
    """
    proc = _PARSER_PROC[0]
    if proc is None:
        return False
    if proc.poll() is not None:
        # процесс завершился — сбрасываем
        _PARSER_PROC[0] = None
        return False
    return True


@app.route('/admin/run_parser')
@require_admin
def admin_run_parser():
    """
    Запуск парсера в ОТДЕЛЬНОМ процессе.
    После завершения память полностью освобождается ОС.
    """
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

        # Закрываем log_file, когда процесс завершится (в отдельном потоке)
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
            <a href="/admin/queue" class="btn">📋 Проверить очередь</a>
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
            <a href="/admin/run_parser?limit=5" class="btn btn-green">🚀 Запустить парсер (5)</a>
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
                    f"📅 **Опубликовано:**\n{PUBLIC_URL}/admin/today\n\n"
                    f"⚙️ **Настройки:**\n{PUBLIC_URL}/admin/settings\n\n"
                    f"📋 **Очередь:**\n{PUBLIC_URL}/admin/queue\n\n"
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

    init_autopublish()

    app.run(host='0.0.0.0', port=PORT, threaded=True)
