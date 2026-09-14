# app.py
# ============================================================
# vtb-bot — основной Flask-сервер
#  - Бот MAX (webhook)
#  - Админка (настройки, расписание, «Опубликовано сегодня»)
#  - Приём данных от парсера
# ============================================================

import os
os.environ['TZ'] = 'Europe/Moscow'
import time
try:
    time.tzset()
except AttributeError:
    pass

from flask import Flask, request, jsonify, render_template_string, send_file, redirect
import logging
import shutil
import urllib3
import threading
import json
import base64
from werkzeug.exceptions import ClientDisconnected

from modules import Database, FileManager, Publisher, ReportGenerator
from config import (
    TOKEN, BASE_URL, SECRET_KEY, PORT, DATA_DIR,
    SHEETS_URL, OUTPUT_DIR, DB_PATH,
    SCHEDULE_START, SCHEDULE_END, DAILY_LIMIT,
)
from db import BotDB
from sheets_client import SheetsClient
from parser_runner import run_parser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TOKEN:
    logger.error("❌ ТОКЕН MAX НЕ НАЙДЕН!")

# === Старые модули (совместимость) ===
db = Database()
db.fix_publication_times()
fm = FileManager(DATA_DIR)

# === Новые модули ===
bot_db = BotDB(DB_PATH)
sheets = SheetsClient(url=SHEETS_URL)


# ============================================================
# MAX API Client (из старого app.py — без изменений)
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
                json=payload,
                timeout=30,
                verify=False,
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"❌ Ошибка отправки: {e}")
            return False

    def upload_file(self, file_bytes, filename='file.bin', file_type='image'):
        """Загрузка файла в MAX (фото/видео). Полная реализация — в старом app.py."""
        if not self.token:
            return None
        try:
            response = requests.post(
                f"{self.base_url}/uploads",
                headers={"Authorization": self.token},
                params={"type": file_type},
                timeout=30,
                verify=False,
            )
            if response.status_code != 200:
                return None
            upload_data = response.json()
            upload_url = upload_data.get('url')
            if not upload_url:
                return None
            upload_response = requests.post(
                upload_url,
                files={'data': (filename, file_bytes)},
                timeout=180,
                verify=False,
            )
            if upload_response.status_code != 200:
                return None
            result = upload_response.json()
            token = result.get('token')
            if not token and 'data' in result:
                token = result['data'].get('token')
            return token
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки: {e}")
            return None


# Нужно импортировать requests до APIClient
import requests
api = APIClient()
publisher = Publisher(api, fm, db)
report_gen = ReportGenerator(fm, db)


# ============================================================
# HTML — страницы (упрощённые, полные версии — позже)
# ============================================================

MAIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>VTB Bot</title>
    <style>
        body { font-family: Arial; max-width: 900px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        .card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        h1 { margin-top: 0; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; border-radius: 5px; margin-right: 10px; }
        .btn-green { background: #28a745; }
        .btn:hover { opacity: 0.9; text-decoration: none; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        .stat { background: #f8f9fa; padding: 15px 20px; border-radius: 5px; }
        .stat .num { font-size: 24px; font-weight: bold; color: #007bff; }
    </style>
</head>
<body>
    <div class="card">
        <h1>🤖 VTB Bot</h1>
        <p>Сервер работает. Токен MAX: {{ '✅' if token_set else '❌' }}</p>
    </div>

    <div class="card">
        <h2>📊 Статистика</h2>
        <div class="stats">
            <div class="stat">
                <div>В очереди</div>
                <div class="num">{{ stats.get('pending', 0) }}</div>
            </div>
            <div class="stat">
                <div>Опубликовано</div>
                <div class="num">{{ stats.get('published', 0) }}</div>
            </div>
            <div class="stat">
                <div>Ошибок</div>
                <div class="num">{{ stats.get('failed', 0) }}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>⚙️ Управление</h2>
        <a href="/admin" class="btn">🛠 Админка</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/run_parser" class="btn btn-green">🚀 Запустить парсер</a>
    </div>
</body>
</html>
"""


# ============================================================
# Маршруты — базовые
# ============================================================

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        return webhook()
    stats = bot_db.count_by_status()
    return render_template_string(MAIN_PAGE, stats=stats, token_set=bool(TOKEN))


@app.route('/health')
def health():
    return {"status": "ok", "token_set": bool(TOKEN)}


@app.route('/status')
def status():
    stats = bot_db.count_by_status()
    return {"status": "running", "token_set": bool(TOKEN), "queue": stats}


# ============================================================
# Приём JSON от парсера (когда парсер на GitHub Actions / отдельно)
# ============================================================

@app.route('/ingest_ads', methods=['POST'])
def ingest_ads():
    """
    Принимает JSON от парсера:
      {"source": "vtb_parser", "ads": [{...}, {...}]}
    Складывает в очередь БД.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        ads = data.get('ads', [])
        if not ads:
            return jsonify({'success': False, 'error': 'Empty ads'}), 400

        added = 0
        skipped = 0
        for ad in ads:
            try:
                if bot_db.add_parsed_ad(ad):
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f'❌ Ошибка добавления: {e}')
                skipped += 1

        logger.info(f'📥 Принято от парсера: добавлено {added}, дублей {skipped}')
        return jsonify({
            'success': True,
            'added': added,
            'skipped': skipped,
            'total': len(ads),
        })
    except Exception as e:
        logger.exception(f'❌ ingest_ads: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# Админка (упрощённая — развернём позже)
# ============================================================

ADMIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Админка VTB Bot</title>
    <style>
        body { font-family: Arial; max-width: 1000px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        .card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        h1 { margin-top: 0; }
        .btn { display: inline-block; padding: 10px 20px; background: #007bff; color: white; border-radius: 5px; text-decoration: none; margin-right: 10px; }
        .btn-green { background: #28a745; }
        .btn-red { background: #dc3545; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 8px 12px; border-bottom: 1px solid #eee; text-align: left; }
        th { background: #f8f9fa; }
    </style>
</head>
<body>
    <div class="card">
        <h1>🛠 Админка</h1>
        <a href="/" class="btn">← На главную</a>
        <a href="/admin/today" class="btn">📅 Опубликовано сегодня</a>
        <a href="/admin/run_parser" class="btn btn-green">🚀 Запустить парсер</a>
    </div>

    <div class="card">
        <h2>⚙️ Разделы и группы MAX</h2>
        <p><em>Полная настройка появится позже. Пока — из config.py.</em></p>
        <table>
            <tr><th>Раздел</th><th>chat_id</th><th>Ключ</th><th>Вкл</th></tr>
            {% for s in sections %}
            <tr>
                <td>{{ s.name }}</td>
                <td>{{ s.chat_id }}</td>
                <td>{{ s.key_in_title or '—' }}</td>
                <td>{{ '✅' if s.get('enabled', True) else '❌' }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>

    <div class="card">
        <h2>📊 Очередь парсинга</h2>
        <table>
            <tr><th>Статус</th><th>Количество</th></tr>
            {% for status, count in stats.items() %}
            <tr><td>{{ status }}</td><td>{{ count }}</td></tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""


@app.route('/admin')
def admin_page():
    from config import SECTIONS
    stats = bot_db.count_by_status()
    return render_template_string(ADMIN_PAGE, sections=SECTIONS, stats=stats)


@app.route('/admin/today')
def admin_today():
    """Простой список опубликованного за сегодня."""
    pubs = bot_db.get_publications_today()
    html = """
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Опубликовано сегодня</title>
    <style>
        body { font-family: Arial; max-width: 1400px; margin: 20px auto; padding: 20px; background: #f5f5f5; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        h1 { margin-top: 0; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { padding: 6px 10px; border-bottom: 1px solid #eee; text-align: left; }
        th { background: #f8f9fa; }
        a { color: #007bff; }
        .btn { display: inline-block; padding: 8px 16px; background: #007bff; color: white; border-radius: 5px; text-decoration: none; margin-right: 10px; }
    </style></head><body>
    <div class="card">
        <h1>📅 Опубликовано сегодня ({{ count }})</h1>
        <a href="/admin" class="btn">← Назад в админку</a>
        <table>
            <tr>
                <th>#</th><th>Время</th><th>Категория</th><th>Название</th>
                <th>Код</th><th>Ссылка MAX</th><th>Источник</th>
            </tr>
            {% for p in pubs %}
            <tr>
                <td>{{ loop.index }}</td>
                <td>{{ p.published_at }}</td>
                <td>{{ p.category or '—' }}</td>
                <td>{{ p.title or '—' }}</td>
                <td>{{ p.code or '—' }}</td>
                <td>{% if p.max_post_url %}<a href="{{ p.max_post_url }}" target="_blank">MAX</a>{% else %}—{% endif %}</td>
                <td><a href="{{ p.folder_name or '#' }}" target="_blank">VTB</a></td>
            </tr>
            {% endfor %}
        </table>
    </div>
    </body></html>
    """
    return render_template_string(html, pubs=pubs, count=len(pubs))


@app.route('/admin/run_parser')
def admin_run_parser():
    """Кнопка ручного запуска парсера (в фоне)."""
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
        'message': f'Парсер запущен в фоне (лимит={limit}, no_sheets={no_sheets})',
    })


# ============================================================
# Webhook MAX (упрощённая версия — полная в старом app.py)
# ============================================================

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        logger.info(f'📩 WEBHOOK: {data}')
        if not data:
            return jsonify({"ok": True}), 200

        update_type = data.get('update_type')

        if update_type == 'message_created':
            message = data.get('message', {})
            sender = message.get('sender', {})
            body = message.get('body', {})
            user_id = sender.get('user_id')
            text = (body.get('text') or '').strip()

            if user_id and text == '/start':
                api.send_message(
                    user_id,
                    "🏠 **VTB Bot**\n\n"
                    "🌐 Админка: https://your-bothost-url/admin\n"
                    "📅 Опубликовано сегодня: /admin/today\n"
                    "🚀 Запустить парсер: /admin/run_parser\n"
                )
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
    logger.info(f'   DB: {DB_PATH}')
    logger.info(f'   OUTPUT_DIR: {OUTPUT_DIR}')
    logger.info(f'   Расписание: {SCHEDULE_START} – {SCHEDULE_END} МСК, {DAILY_LIMIT}/день')
    app.run(host='0.0.0.0', port=PORT, threaded=True)
