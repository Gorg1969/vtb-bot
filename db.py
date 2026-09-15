# db.py
# ============================================================
# Единая SQLite для бота и парсера
# ============================================================

import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict, Set

logger = logging.getLogger(__name__)


class BotDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        c = conn.cursor()

        # === Очередь парсинга ===
        c.execute('''
            CREATE TABLE IF NOT EXISTS parsed_ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT UNIQUE NOT NULL,
                title TEXT,
                code TEXT,
                price TEXT,
                city TEXT,
                year TEXT,
                mileage TEXT,
                category TEXT,
                chat_id TEXT,
                folder_name TEXT,
                media_path TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMP
            )
        ''')

        # === Опубликовано ===
        c.execute('''
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                folder_name TEXT,
                group_id TEXT,
                max_post_url TEXT,
                title TEXT,
                code TEXT,
                city TEXT,
                price TEXT,
                category TEXT,
                status TEXT DEFAULT 'success',
                error TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # === Настройки админки ===
        c.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        logger.info(f'✅ БД инициализирована: {self.db_path}')

    # --------------------------------------------------------
    # ПАРСЕР: очередь
    # --------------------------------------------------------

    def add_parsed_ad(self, ad: Dict) -> bool:
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute('''
                INSERT OR IGNORE INTO parsed_ads
                (source_url, title, code, price, city, year, mileage,
                 category, chat_id, folder_name, media_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (
                ad.get('source_url'),
                ad.get('title'),
                ad.get('code'),
                ad.get('price'),
                ad.get('city'),
                ad.get('year'),
                ad.get('mileage'),
                ad.get('category'),
                ad.get('chat_id'),
                ad.get('folder_name'),
                ad.get('media_path'),
            ))
            conn.commit()
            inserted = c.rowcount > 0
            conn.close()
            return inserted
        except Exception as e:
            logger.error(f'❌ add_parsed_ad: {e}')
            return False

    def get_parsed_urls(self) -> Set[str]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT source_url FROM parsed_ads')
        urls = {row[0] for row in c.fetchall()}
        conn.close()
        return urls

    def is_parsed(self, url: str) -> bool:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT 1 FROM parsed_ads WHERE source_url = ? LIMIT 1', (url,))
        found = c.fetchone() is not None
        conn.close()
        return found

    def get_pending_ads(self, limit: int = 10) -> List[Dict]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM parsed_ads
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
        ''', (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def mark_ad_published(self, ad_id: int, max_post_url: str = None):
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            UPDATE parsed_ads
            SET status = 'published', published_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (ad_id,))
        conn.commit()
        conn.close()

    def mark_ad_failed(self, ad_id: int, error: str):
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            UPDATE parsed_ads
            SET status = 'failed', error = ?
            WHERE id = ?
        ''', (error, ad_id))
        conn.commit()
        conn.close()

    def count_by_status(self) -> Dict:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT status, COUNT(*) FROM parsed_ads GROUP BY status')
        result = dict(c.fetchall())
        conn.close()
        return result

    # --------------------------------------------------------
    # ПУБЛИКАЦИИ
    # --------------------------------------------------------

    def add_publication(self, data: Dict) -> int:
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            INSERT INTO publications
            (user_id, folder_name, group_id, max_post_url, title, code,
             city, price, category, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('user_id'),
            data.get('folder_name'),
            data.get('group_id'),
            data.get('max_post_url'),
            data.get('title'),
            data.get('code'),
            data.get('city'),
            data.get('price'),
            data.get('category'),
            data.get('status', 'success'),
            data.get('error'),
        ))
        pub_id = c.lastrowid
        conn.commit()
        conn.close()
        return pub_id

    def get_publications_today(self) -> List[Dict]:
        """Публикации за сегодня (простые)."""
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM publications
            WHERE DATE(published_at) = DATE('now', 'localtime')
            ORDER BY published_at DESC
        ''')
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_publications_today_full(self) -> List[Dict]:
        """
        Публикации за сегодня (расширенные — с датой, временем МСК, ценой).
        Используется на странице /admin/today и для экспорта в Excel.
        """
        import pytz
        moscow_tz = pytz.timezone('Europe/Moscow')

        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT id, published_at, max_post_url, folder_name,
                   title, code, price, category, group_id
            FROM publications
            WHERE DATE(published_at) = DATE('now', 'localtime')
            ORDER BY published_at ASC
        ''')
        rows = c.fetchall()
        conn.close()

        result = []
        for row in rows:
            pub_dt = row[1]
            if isinstance(pub_dt, str):
                try:
                    pub_dt = datetime.fromisoformat(pub_dt)
                except Exception:
                    pub_dt = datetime.now()
            if pub_dt is None:
                pub_dt = datetime.now()
            if hasattr(pub_dt, 'tzinfo') and pub_dt.tzinfo is None:
                pub_dt = moscow_tz.localize(pub_dt)

            result.append({
                'id': row[0],
                'date': pub_dt.strftime('%d.%m.%Y'),
                'time': pub_dt.strftime('%H:%M'),
                'max_post_url': row[2] or '',
                'source_url': row[3] or '',
                'title': row[4] or '',
                'code': row[5] or '',
                'price': row[6] or '',
                'category': row[7] or '',
                'group_id': row[8] or '',
            })
        return result

    def get_publications(self, limit: int = 100) -> List[Dict]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM publications
            ORDER BY published_at DESC
            LIMIT ?
        ''', (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    # --------------------------------------------------------
    # НАСТРОЙКИ
    # --------------------------------------------------------

    def set_setting(self, key: str, value: str):
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
        ''', (key, value))
        conn.commit()
        conn.close()

    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default

    def get_all_settings(self) -> Dict[str, str]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings')
        result = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return result
