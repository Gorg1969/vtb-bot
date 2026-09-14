# app.py
# ============================================================
# vtb-bot — Flask-сервер
#  - Бот MAX (webhook)
#  - Админка (chat_id, расписание, «Опубликовано сегодня»)
#  - Приём данных от парсера
# ============================================================

import os
os.environ['TZ'] = 'Europe/Moscow'
import time
try:
    time.tzset()
except AttributeError:
    pass

import logging
import shutil
import urllib3
import threading
import json
import requests
from functools import wraps

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

# === Список user_id, кому бот отвечает (через запятую) ===
# Если пусто — отвечает всем
ALLOWED_ADMIN_IDS = [
    int(x) for x in (os.environ.get('ADMIN_IDS') or '').split(',')
    if x.strip().isdigit()
]


def require_admin(f):
    """Декоратор: Basic Auth для админки."""
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
    """Проверяет, отвечать ли этому пользователю."""
    if not ALLOWED_ADMIN_IDS:
        return True  # если список пуст — отвечаем всем
    return int(user_id) in ALLOWED_ADMIN_IDS


# === Модули (совместимость) ===
db = Database()
db.fix_publication_times()
fm = FileManager(DATA_DIR)

# === Новые модули ===
bot_db = BotDB(DB_PATH)
sheets = SheetsClient(url=SHEETS_URL)


# ============================================================
# MAX API Client
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
            r = requests.post(
                f"{self.base_url}/uploads",
                headers={"Authorization": self.token},
                params={"type": file_type},
                timeout=30, verify=False,
            )
            if r.status_code != 200:
                return None
            upload_url = r.json().get('url')
            if not upload_url:
                return None
            ur = requests.post(upload_url, files={'data': (filename, file_bytes)},
                               timeout=180, verify=False)
            if ur.status_code != 200:
                return None
            result = ur.json()
            token = result.get('token')
            if not token and 'data' in result:
                token = result['data'].get('token')
            return token
        except Exception as e:
            logger.error(f"❌ upload_file: {e}")
            return None


api = APIClient()
publisher = Publisher(api, fm, db)
report_gen = ReportGenerator(fm, db)


# ============================================================
# Стили
# ============================================================

BASE_STYLE = """
<style>
    body { font-family: Arial; max-width: 1000px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
    .card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
    h1, h2 { margin-top: 0; }
    a { color: #007bff; text-decoration: none; }
    .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; border-radius: 5px; margin-right: 10px; margin-bottom: 10px; border: none; cursor: pointer; font-size: 14px; }
    .btn-green { background: #28a745; }
    .btn-red { background: #dc3545; }
    .btn-gray { background: #6c757d; }
    .btn:hover { opacity: 0.9; }
    .stats { display: flex; gap: 20px; flex-wrap: wrap; }
    .stat { background: #f8f9fa; padding: 15px 20px; border-radius: 5px; }
    .stat .num { font-size: 24px; font-weight: bold; color: #007bff; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 10px 12px; border-bottom: 1px solid #eee; text-align: left; }
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
    admin_ids_status = ', '.join(str(x) for x in ALLOWED_ADMIN_IDS) if ALLOWED_ADMIN_IDS else 'все (не защищено)'
    warn = '' if ALLOWED_ADMIN_IDS else '<div class="warn">⚠️ ADMIN_IDS не задан — бот отвечает всем. Добавь свой user_id в переменные Bothost.</div>'
    return BASE_STYLE + f"""
    <div class="card">
        <h1>🤖 VTB Bot</h1>
        <p>Токен MAX: {'✅' if TOKEN else '❌'}</p>
        <p>Бот отвечает: <b>{admin_ids_status}</b></p>
        {warn}
    </div>
    <div class="card">
        <h2>📊 Очередь парсинга</h2>
        <div class="stats">
            <div class="stat"><div>В очереди</div><div class="num">{stats.get('pending', 0)}</div></div>
            <div class="stat"><div>Опубликовано</div><div class="num">{stats.get('published', 0)}</div></div>
            <div class="stat"><div>Ошибок</div><div class="num">{stats.get('failed', 0)}</div></div>
        </div>
    </div>
    <div class="card">
        <h2>⚙️ Управление</h2>
        <a href="/admin" class="btn">🛠 Админка</a>
        <a href="/admin/settings" class="btn">⚙️ Настройки</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
    </div>
    """


@app.route('/health')
def health():
    return {"status": "ok", "token_set": bool(TOKEN)}


@app.route('/status')
def status():
    return {"status": "running", "token_set": bool(TOKEN), "queue": bot_db.count_by_status()}


# ============================================================
# Регистрация вебхука
# ============================================================

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
                    logger.info(f'🗑️ Удалена подписка: {old_url}')
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


# ============================================================
# Приём JSON от парсера
# ============================================================

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

        logger.info(f'📥 Принято: добавлено {added}, дублей {skipped}')
        return jsonify({'success': True, 'added': added, 'skipped': skipped, 'total': len(ads)})
    except Exception as e:
        logger.exception(f'❌ ingest_ads: {e}')
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

    return BASE_STYLE + f"""
    <div class="card">
        <h1>🛠 Админка</h1>
        <a href="/" class="btn">← На главную</a>
        <a href="/admin/settings" class="btn">⚙️ Настройки</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/run_parser?limit=5&no_sheets=1" class="btn btn-green">🚀 Тест парсера (5)</a>
        <a href="/admin/run_parser?limit=50" class="btn btn-green">🚀 Парсер (50)</a>
        <a href="/setup_webhook" class="btn btn-gray">🔄 Перерегистрировать вебхук</a>
    </div>

    <div class="card">
        <h2>⚙️ Разделы и группы MAX</h2>
        <p class="hint">Редактировать chat_id → на странице <a href="/admin/settings">Настройки</a></p>
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
            logger.info(f'💾 Сохранено {key} = {value}')

        bot_db.set_setting('schedule_start', (request.form.get('schedule_start') or '06:00').strip())
        bot_db.set_setting('schedule_end', (request.form.get('schedule_end') or '20:00').strip())

        daily_limit = (request.form.get('daily_limit') or '150').strip()
        try:
            int(daily_limit)
        except ValueError:
            daily_limit = '150'
        bot_db.set_setting('daily_limit', daily_limit)

        saved = True
        logger.info('✅ Настройки сохранены')

    sections = get_sections_from_db(bot_db)
    schedule = get_schedule_from_db(bot_db)

    chat_fields = ""
    for s in sections:
        chat_fields += f"""
        <div class="form-row">
            <label>{s.get('title', s['name'])}:</label>
            <input type="text" name="chat_id_{s['name']}" value="{s['chat_id']}" placeholder="-00000000000000">
            <div class="hint">chat_id группы MAX для «{s.get('title', s['name'])}»</div>
        </div>
        """

    saved_msg = '<div class="success-msg">✅ Настройки сохранены</div>' if saved else ''

    return BASE_STYLE + f"""
    <div class="card">
        <h1>⚙️ Настройки</h1>
        <a href="/admin" class="btn">← Назад в админку</a>
    </div>

    <form method="POST">
        <div class="card">
            <h2>📢 chat_id групп MAX</h2>
            <p class="hint">Введи тестовые ID, проверь — потом замени на боевые.</p>
            {chat_fields}
        </div>

        <div class="card">
            <h2>⏰ Расписание публикаций (МСК)</h2>
            <div class="form-row">
                <label>Начало:</label>
                <input type="text" name="schedule_start" value="{schedule['start']}" placeholder="06:00">
            </div>
            <div class="form-row">
                <label>Конец:</label>
                <input type="text" name="schedule_end" value="{schedule['end']}" placeholder="20:00">
            </div>
            <div class="form-row">
                <label>Лимит публикаций в день:</label>
                <input type="text" name="daily_limit" value="{schedule['daily_limit']}" placeholder="150">
            </div>
        </div>

        {saved_msg}

        <div class="card">
            <button type="submit" class="btn btn-green">💾 Сохранить</button>
            <a href="/admin" class="btn btn-gray">Отмена</a>
        </div>
    </form>
    """


# ============================================================
# АДМИНКА — опубликовано сегодня
# ============================================================

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
        <a href="/admin" class="btn">← Назад в админку</a>
        <table>
            <tr>
                <th>#</th><th>Время</th><th>Категория</th><th>Название</th>
                <th>Код</th><th>MAX</th><th>Источник</th>
            </tr>
            {rows}
        </table>
    </div>
    """


# ============================================================
# АДМИНКА — запуск парсера
# ============================================================

@app.route('/admin/run_parser')
@require_admin
def admin_run_parser():
    limit = int(request.args.get('limit', 50))
    no_sheets = request.args.get('no_sheets') == '1'

    def _run():
        try:
            run_parser(limit=limit, no_sheets=no_sheets)
        except Exception as e:
            logger.exception(f'❌ Парсер упал: {e}')

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({
        'success': True,
        'message': f'Парсер запущен (лимит={limit}, no_sheets={no_sheets})',
    })


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

            # === Проверка доступа ===
            if user_id and not is_allowed_user(user_id):
                logger.warning(f'⛔ Игнорируем user_id={user_id} (не в ADMIN_IDS)')
                return jsonify({"ok": True}), 200

            if user_id and text == '/start':
                api.send_message(
                    user_id,
                    "🏠 **VTB Bot**\n\n"
                    f"🌐 **Админка:**\n{PUBLIC_URL}/admin\n\n"
                    f"📅 **Опубликовано сегодня:**\n{PUBLIC_URL}/admin/today\n\n"
                    f"⚙️ **Настройки:**\n{PUBLIC_URL}/admin/settings\n\n"
                    f"🚀 **Запустить парсер:**\n{PUBLIC_URL}/admin/run_parser?limit=50\n\n"
                    "🔒 Пароль спросит браузер."
                )
                return jsonify({"ok": True}), 200

            if user_id and text == '/status':
                stats = bot_db.count_by_status()
                api.send_message(
                    user_id,
                    f"📊 **Статус:**\n"
                    f"⏳ В очереди: {stats.get('pending', 0)}\n"
                    f"✅ Опубликовано: {stats.get('published', 0)}\n"
                    f"❌ Ошибок: {stats.get('failed', 0)}"
                )
                return jsonify({"ok": True}), 200

            if user_id and text == '/myid':
                api.send_message(user_id, f"Твой user_id: `{user_id}`")
                return jsonify({"ok": True}), 200

        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception(f'❌ webhook: {e}')
        return jsonify({"ok": False}), 500


# ============================================================
# Запуск
# ============================================================

if __name__ == '__main__':
    logger.info(f'🚀 Запуск vtb-bot на порту {PORT}')
    logger.info(f'   TOKEN: {"✅" if TOKEN else "❌"}')
    logger.info(f'   SHEETS_URL: {"✅" if SHEETS_URL else "❌"}')
    logger.info(f'   ADMIN_PASS: {"✅" if ADMIN_PASS else "❌ (админка открыта!)"}')
    logger.info(f'   ADMIN_IDS: {ALLOWED_ADMIN_IDS if ALLOWED_ADMIN_IDS else "❌ (все)"}')
    logger.info(f'   PUBLIC_URL: {PUBLIC_URL}')
    app.run(host='0.0.0.0', port=PORT, threaded=True)
