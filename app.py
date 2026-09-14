# app.py
# ============================================================
# vtb-bot — Flask-сервер
#  - Бот MAX (webhook)
#  - Админка (chat_id, расписание, публикация, диагностика)
#  - Парсер + публикатор + удаление после публикации
# ============================================================

import os
os.environ['TZ'] = 'Europe/Moscow'
import time
try:
    time.tzset()
except AttributeError:
    pass

import sqlite3
import logging
import shutil
import urllib3
import threading
import json
import requests
import base64
from functools import wraps
from datetime import datetime

from flask import (
    Flask, request, jsonify, render_template_string,
    send_file, redirect
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


# === Модули ===
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
            if response.status_code != 200:
                logger.error(f"❌ send_message: {response.status_code} - {response.text[:200]}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"❌ send_message: {e}")
            return False

    def upload_file(self, file_bytes, filename='file.bin', file_type='image'):
        if not self.token:
            return None
        try:
            logger.info(f'📤 Загрузка {file_type} {filename} ({len(file_bytes)} байт)')
            r = requests.post(
                f"{self.base_url}/uploads",
                headers={"Authorization": self.token},
                params={"type": file_type},
                timeout=30, verify=False,
            )
            if r.status_code != 200:
                logger.error(f'❌ /uploads: {r.status_code} - {r.text[:200]}')
                return None
            data = r.json()
            upload_url = data.get('url')
            if not upload_url:
                logger.error(f'❌ Нет url: {data}')
                return None

            ur = requests.post(
                upload_url,
                files={'data': (filename, file_bytes)},
                timeout=180, verify=False,
            )
            if ur.status_code != 200:
                logger.error(f'❌ upload: {ur.status_code} - {ur.text[:200]}')
                return None

            result = ur.json()
            token = result.get('token')
            if not token and 'data' in result:
                token = result['data'].get('token')
            if token:
                logger.info(f'✅ Токен: {token[:20]}...')
            return token
        except Exception as e:
            logger.exception(f"❌ upload_file: {e}")
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
                logger.info(f'📨 JSON: {json.dumps(result, ensure_ascii=False)[:300]}')
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
# Публикация одного объявления + удаление папки
# ============================================================

def publish_one_ad(ad: dict) -> tuple:
    ad_id = ad['id']
    folder_name = ad.get('folder_name')
    chat_id = ad.get('chat_id')
    media_path = ad.get('media_path')

    logger.info(f'🔍 Проверка путей: folder_name={folder_name!r}, media_path={media_path!r}')

    if not media_path:
        return False, f'Нет media_path в БД', None

    if not os.path.exists(media_path):
        # Попробуем альтернативные варианты
        alternatives = [
            os.path.join('/app/VTB_Объявления', folder_name or ''),
            os.path.join('/app/data/VTB_Объявления', folder_name or ''),
            os.path.join(OUTPUT_DIR, folder_name or ''),
        ]
        found = None
        for alt in alternatives:
            if alt and os.path.exists(alt):
                found = alt
                logger.info(f'🔄 Найден альтернативный путь: {alt}')
                break

        if not found:
            return False, f'Папка не найдена: {media_path}. Проверено также: {alternatives}', None
        media_path = found

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
    logger.info(f'📷 Фото: {len(photo_files)} → {photo_files}')
    logger.info(f'📝 Текст: {text[:100]}...')

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
        logger.warning(f'⚠️ Не удалось удалить {media_path}: {e}')

    return True, 'Опубликовано', post_link


# ============================================================
# Стили
# ============================================================

BASE_STYLE = """
<style>
    body { font-family: Arial; max-width: 1200px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
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
    admin_ids_status = ', '.join(str(x) for x in ALLOWED_ADMIN_IDS) if ALLOWED_ADMIN_IDS else 'все'
    return BASE_STYLE + f"""
    <div class="card">
        <h1>🤖 VTB Bot</h1>
        <p>Токен MAX: {'✅' if TOKEN else '❌'}</p>
        <p>Бот отвечает: <b>{admin_ids_status}</b></p>
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
        <a href="/admin/check_paths" class="btn">🔍 Проверить пути</a>
        <a href="/admin/list_folders" class="btn">📁 Папки на диске</a>
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
# АДМИНКА
# ============================================================

@app.route('/admin')
@require_admin
def admin_page():
    sections = get_sections_from_db(bot_db)
    stats = bot_db.count_by_status()
    schedule = get_schedule_from_db(bot_db)

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

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🛠 Админка</h1>
        <a href="/" class="btn">← На главную</a>
        <a href="/admin/settings" class="btn">⚙️ Настройки</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/queue" class="btn">📋 Очередь (с путями)</a>
        <a href="/admin/check_paths" class="btn">🔍 Проверить пути</a>
        <a href="/admin/list_folders" class="btn">📁 Папки на диске</a>
        <a href="/setup_webhook" class="btn btn-gray">🔄 Вебхук</a>
    </div>

    <div class="card">
        <h2>🚀 Парсинг (дедуп ВКЛ)</h2>
        <a href="/admin/run_parser?limit=5" class="btn btn-green">Тест (5)</a>
        <a href="/admin/run_parser?limit=50" class="btn btn-green">50</a>
        <a href="/admin/run_parser?limit=300" class="btn btn-green">300</a>
    </div>

    <div class="card">
        <h2>📤 Публикация в MAX</h2>
        <p>В очереди: <b>{pending}</b></p>
        <a href="/admin/publish_one" class="btn btn-orange" onclick="return confirm('Опубликовать 1 объявление?')">📤 Опубликовать 1</a>
        <a href="/admin/publish_all" class="btn btn-orange" onclick="return confirm('Опубликовать ВСЕ? Это займёт время.')">📤 Опубликовать все</a>
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


@app.route('/admin/settings', methods=['GET', 'POST'])
@require_admin
def admin_settings():
    saved = False
    if request.method == 'POST':
        for s in SECTIONS:
            key = f"chat_id_{s['name']}"
            value = (request.form.get(key) or '').strip()
            bot_db.set_setting(key, value)
            logger.info(f'💾 {key} = {value}')
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
        </div>
        {saved_msg}
        <div class="card">
            <button type="submit" class="btn btn-green">💾 Сохранить</button>
            <a href="/admin" class="btn btn-gray">Отмена</a>
        </div>
    </form>
    """


@app.route('/admin/today')
@require_admin
def admin_today():
    pubs = bot_db.get_publications_today()
    rows = ""
    for i, p in enumerate(pubs, 1):
        max_link = f'<a href="{p["max_post_url"]}" target="_blank">MAX</a>' if p.get('max_post_url') else '—'
        src_link = f'<a href="{p["folder_name"]}" target="_blank">VTB</a>' if p.get('folder_name') and str(p['folder_name']).startswith('http') else '—'
        rows += f"""
        <tr>
            <td>{i}</td>
            <td>{p.get('published_at', '—')}</td>
            <td>{p.get('category', '—')}</td>
            <td>{p.get('title', '—')}</td>
            <td>{p.get('code', '—')}</td>
            <td>{max_link}</td>
            <td>{src_link}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='7'>Пока ничего не опубликовано</td></tr>"

    return BASE_STYLE + f"""
    <div class="card">
        <h1>📅 Опубликовано сегодня ({len(pubs)})</h1>
        <a href="/admin" class="btn">← Назад</a>
        <table>
            <tr><th>#</th><th>Время</th><th>Категория</th><th>Название</th><th>Код</th><th>MAX</th><th>Источник</th></tr>
            {rows}
        </table>
    </div>
    """


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
            <td style="font-size:11px">{r.get('source_url', '—')[:60]}</td>
        </tr>
        """

    return BASE_STYLE + f"""
    <div class="card">
        <h1>📋 Очередь парсинга ({len(rows)})</h1>
        <a href="/admin" class="btn">← Назад</a>
        <p class="hint">Проверь пути <b>media_path</b>. Если ❌ — файлы пропали.</p>
        <table>
            <tr>
                <th>ID</th><th>Папка</th><th>media_path</th>
                <th>Есть?</th><th>Файлы</th>
                <th>Категория</th><th>Статус</th><th>Источник</th>
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
    return jsonify({
        'output_dir': OUTPUT_DIR,
        'files': files,
        'count': len(files),
    })


@app.route('/admin/cleanup_orphans')
@require_admin
def admin_cleanup_orphans():
    """Удаляет записи из parsed_ads, у которых папка не найдена на диске."""
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
            logger.info(f'🗑️ Удалена запись id={r["id"]} ({r.get("folder_name")})')

    return jsonify({'success': True, 'deleted': deleted, 'total': len(rows)})


@app.route('/admin/run_parser')
@require_admin
def admin_run_parser():
    limit = int(request.args.get('limit', 50))

    def _run():
        try:
            run_parser(limit=limit)
        except Exception as e:
            logger.exception(f'❌ Парсер упал: {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'message': f'Парсер запущен (лимит={limit})'})


# ============================================================
# Публикация
# ============================================================

@app.route('/admin/publish_one')
@require_admin
def admin_publish_one():
    ads = bot_db.get_pending_ads(limit=1)
    if not ads:
        return jsonify({'success': False, 'message': 'Очередь пуста'})

    ad = ads[0]
    logger.info(f'📤 Публикация: {ad.get("folder_name")} → {ad.get("chat_id")}')
    logger.info(f'   media_path={ad.get("media_path")}')

    try:
        ok, message, post_link = publish_one_ad(ad)
        if ok:
            return jsonify({
                'success': True,
                'message': message,
                'post_link': post_link,
                'title': ad.get('title'),
                'chat_id': ad.get('chat_id'),
            })
        else:
            bot_db.mark_ad_failed(ad['id'], message)
            return jsonify({'success': False, 'message': message})
    except Exception as e:
        logger.exception(f'❌ Ошибка публикации: {e}')
        bot_db.mark_ad_failed(ad['id'], str(e))
        return jsonify({'success': False, 'message': str(e)})


@app.route('/admin/publish_all')
@require_admin
def admin_publish_all():
    def _run():
        pause = 30
        published = 0
        failed = 0
        while True:
            ads = bot_db.get_pending_ads(limit=1)
            if not ads:
                break
            ad = ads[0]
            logger.info(f'📤 [{published+failed+1}] {ad.get("folder_name")}')
            try:
                ok, message, post_link = publish_one_ad(ad)
                if ok:
                    published += 1
                    logger.info(f'  ✅ {message} {post_link or ""}')
                else:
                    failed += 1
                    bot_db.mark_ad_failed(ad['id'], message)
                    logger.warning(f'  ❌ {message}')
            except Exception as e:
                failed += 1
                bot_db.mark_ad_failed(ad['id'], str(e))
                logger.exception(f'  ❌ {e}')
            if bot_db.get_pending_ads(limit=1):
                logger.info(f'  ⏸ Пауза {pause} сек...')
                time.sleep(pause)
        logger.info(f'🏁 Публикация завершена: ✅ {published}, ❌ {failed}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'message': 'Публикация запущена в фоне. Следи в логах.'})


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

            logger.info(f'📨 user_id
