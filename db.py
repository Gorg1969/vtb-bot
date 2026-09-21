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
                sort_order INTEGER DEFAULT 0,
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

        # === Миграция: добавить sort_order, если её нет ===
        c.execute("PRAGMA table_info(parsed_ads)")
        columns = [row[1] for row in c.fetchall()]
        if 'sort_order' not in columns:
            c.execute('ALTER TABLE parsed_ads ADD COLUMN sort_order INTEGER DEFAULT 0')
            c.execute('UPDATE parsed_ads SET sort_order = id WHERE sort_order = 0')
            logger.info('✅ Добавлена колонка sort_order в parsed_ads')

        conn.commit()
        conn.close()
        logger.info(f'✅ БД инициализирована: {self.db_path}')

    # --------------------------------------------------------
    # ПАРСЕР: очередь
    # --------------------------------------------------------

    def add_parsed_ad(self, ad: Dict) -> bool:
        """
        Добавляет объявление в очередь.
        sort_order выставляется в конец очереди (max + 1).
        """
        try:
            conn = self._connect()
            c = conn.cursor()

            c.execute("SELECT COALESCE(MAX(sort_order), 0) FROM parsed_ads")
            max_order = c.fetchone()[0]
            new_order = max_order + 1

            c.execute('''
                INSERT OR IGNORE INTO parsed_ads
                (source_url, title, code, price, city, year, mileage,
                 category, chat_id, folder_name, media_path, status, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
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
                new_order,
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
        """
        Возвращает pending-объявления в порядке sort_order ASC, created_at ASC.
        Именно в этом порядке автопубликация берёт объявления.
        """
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM parsed_ads
            WHERE status = 'pending'
            ORDER BY sort_order ASC, created_at ASC
            LIMIT ?
        ''', (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_all_pending_ads(self) -> List[Dict]:
        """Все pending-объявления в порядке очереди."""
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM parsed_ads
            WHERE status = 'pending'
            ORDER BY sort_order ASC, created_at ASC
        ''')
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_ad_by_id(self, ad_id: int) -> Optional[Dict]:
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT * FROM parsed_ads WHERE id = ?', (ad_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

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

    def delete_ad(self, ad_id: int, delete_files: bool = True) -> bool:
        """
        Удаляет объявление из БД. Если delete_files=True — удаляет папку с медиа.
        Возвращает True, если запись найдена и удалена.
        """
        import shutil
        import os

        ad = self.get_ad_by_id(ad_id)
        if not ad:
            return False

        media_path = ad.get('media_path')

        if delete_files and media_path and os.path.exists(media_path):
            try:
                shutil.rmtree(media_path)
                logger.info(f'🗑️ Папка удалена: {media_path}')
            except Exception as e:
                logger.warning(f'⚠️ Не удалить {media_path}: {e}')

        conn = self._connect()
        c = conn.cursor()
        c.execute('DELETE FROM parsed_ads WHERE id = ?', (ad_id,))
        conn.commit()
        conn.close()

        logger.info(f'🗑️ Объявление #{ad_id} удалено из БД')
        return True

    def move_ad(self, ad_id: int, direction: str) -> bool:
        """
        Меняет порядок публикации.
        direction='up'   — поменять sort_order с предыдущим pending
        direction='down' — поменять sort_order со следующим pending
        """
        if direction not in ('up', 'down'):
            return False

        conn = self._connect()
        c = conn.cursor()

        c.execute('SELECT id, sort_order FROM parsed_ads WHERE id = ?', (ad_id,))
        current = c.fetchone()
        if not current:
            conn.close()
            return False

        current_order = current[1]

        if direction == 'up':
            c.execute('''
                SELECT id, sort_order FROM parsed_ads
                WHERE status = 'pending' AND sort_order < ?
                ORDER BY sort_order DESC LIMIT 1
            ''', (current_order,))
        else:
            c.execute('''
                SELECT id, sort_order FROM parsed_ads
                WHERE status = 'pending' AND sort_order > ?
                ORDER BY sort_order ASC LIMIT 1
            ''', (current_order,))

        neighbor = c.fetchone()
        if not neighbor:
            conn.close()
            return False

        neighbor_id, neighbor_order = neighbor

        c.execute('UPDATE parsed_ads SET sort_order = ? WHERE id = ?',
                  (neighbor_order, ad_id))
        c.execute('UPDATE parsed_ads SET sort_order = ? WHERE id = ?',
                  (current_order, neighbor_id))

        conn.commit()
        conn.close()
        return True

    # --------------------------------------------------------
    # ПЕРЕМЕЖЕНИЕ КАТЕГОРИЙ В ОЧЕРЕДИ
    # --------------------------------------------------------

    # Порядок групп категорий для перемежения.
    # Внутри одной группы (например, "дорожная техника") —
    # несколько категорий, которые идут как одна "полка".
    CATEGORY_GROUPS = [
        ['truck_samosval'],                                  # 1. Самосвалы
        ['truck_sedelny'],                                   # 2. Седельные тягачи
        ['buldozer', 'excavator', 'grader'],                 # 3. Дорожная техника
        ['trailer'],                                         # 4. Прицепы
        ['car'],                                             # 5. Легковые
    ]

    def resort_queue_by_categories(self) -> int:
        """
        Пересчитывает sort_order у всех pending-объявлений так,
        чтобы категории шли ПО ОЧЕРЕДИ (перемежались).

        Логика:
          - Группируем все pending по "полкам" (CATEGORY_GROUPS).
          - Внутри каждой полки объявления сортируются по created_at.
          - Затем "каруселью" берём по одному из каждой полки по кругу:
              полка1[0], полка2[0], полка3[0], полка4[0], полка5[0],
              полка1[1], полка2[1], ...
          - Присваиваем sort_order 1, 2, 3, ...

        Возвращает количество обновлённых записей.
        """
        conn = self._connect()
        c = conn.cursor()

        c.execute('''
            SELECT id, category FROM parsed_ads
            WHERE status = 'pending'
            ORDER BY created_at ASC, id ASC
        ''')
        rows = c.fetchall()

        if not rows:
            conn.close()
            return 0

        # Раскладываем по полкам
        buckets = {i: [] for i in range(len(self.CATEGORY_GROUPS))}
        uncategorized = []

        for row in rows:
            ad_id = row[0]
            cat = row[1] or ''
            placed = False
            for i, group in enumerate(self.CATEGORY_GROUPS):
                if cat in group:
                    buckets[i].append(ad_id)
                    placed = True
                    break
            if not placed:
                uncategorized.append(ad_id)

        # Крутим "карусель"
        ordered_ids = []
        max_len = max((len(b) for b in buckets.values()), default=0)

        for idx in range(max_len):
            for bucket_idx in range(len(self.CATEGORY_GROUPS)):
                bucket = buckets[bucket_idx]
                if idx < len(bucket):
                    ordered_ids.append(bucket[idx])

        # В конец — всё, что не попало в группы
        ordered_ids.extend(uncategorized)

        # Присваиваем sort_order 1..N
        for new_order, ad_id in enumerate(ordered_ids, start=1):
            c.execute(
                'UPDATE parsed_ads SET sort_order = ? WHERE id = ?',
                (new_order, ad_id)
            )

        conn.commit()
        conn.close()

        logger.info(f'🔀 Очередь перемежена: {len(ordered_ids)} записей, '
                    f'полок: {len(self.CATEGORY_GROUPS)}')
        return len(ordered_ids)

    def get_queue_category_summary(self) -> Dict:
        """
        Возвращает сводку: сколько pending-объявлений в каждой категории.
        """
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            SELECT category, COUNT(*) FROM parsed_ads
            WHERE status = 'pending'
            GROUP BY category
            ORDER BY COUNT(*) DESC
        ''')
        result = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return result

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
