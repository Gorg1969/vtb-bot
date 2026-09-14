# sheets_client.py
# ============================================================
# Клиент Google Apps Script (для дедупа)
# Читает все ссылки из таблицы через Web App
# ============================================================

import logging
import requests
from typing import Set, List, Dict

logger = logging.getLogger(__name__)


class SheetsClient:
    def __init__(self, url: str, timeout: int = 120):
        self.url = url
        self.timeout = timeout
        self._cache: Set[str] = set()
        self._cache_loaded = False

    # --------------------------------------------------------
    # Загрузка всех ссылок (для дедупа в парсере)
    # --------------------------------------------------------
    def get_all_urls(self, use_cache: bool = True) -> Set[str]:
        if use_cache and self._cache_loaded:
            return self._cache

        if not self.url:
            logger.warning('⚠️ SHEETS_URL не задан, дедуп отключён')
            return set()

        try:
            logger.info('📥 Загрузка ссылок из Google Sheets...')
            resp = requests.get(
                self.url,
                params={'action': 'getUrls'},
                timeout=self.timeout,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get('success'):
                logger.error(f'❌ Sheets: {data}')
                return set()

            urls = data.get('urls', [])
            self._cache = {self._normalize(u) for u in urls if u}
            self._cache_loaded = True
            logger.info(
                f'✅ Загружено {len(urls)} ссылок '
                f'из {len(data.get("sheets", []))} листов'
            )
            return self._cache

        except requests.exceptions.Timeout:
            logger.error('❌ Таймаут Google Sheets')
            return set()
        except Exception as e:
            logger.error(f'❌ Ошибка Google Sheets: {e}')
            return set()

    # --------------------------------------------------------
    # Массовая проверка (быстрее, чем по одной)
    # --------------------------------------------------------
    def check_bulk(self, urls: List[str]) -> Dict:
        """
        Проверяет список ссылок разом.
        Возвращает {'duplicates': [...], 'new': [...]}.
        """
        if not urls:
            return {'duplicates': [], 'new': []}

        if not self.url:
            logger.warning('⚠️ SHEETS_URL не задан, все ссылки считаются новыми')
            return {'duplicates': [], 'new': urls}

        try:
            resp = requests.post(
                self.url,
                json={'action': 'checkBulk', 'urls': urls},
                timeout=self.timeout,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get('success'):
                logger.error(f'❌ checkBulk: {data}')
                return {'duplicates': [], 'new': urls}

            logger.info(
                f'✅ Bulk-проверка: дублей {len(data.get("duplicates", []))}, '
                f'новых {len(data.get("new", []))}'
            )
            return data

        except Exception as e:
            logger.error(f'❌ Ошибка checkBulk: {e}')
            return {'duplicates': [], 'new': urls}

    # --------------------------------------------------------
    # Проверка одной ссылки (через локальный кэш)
    # --------------------------------------------------------
    def is_duplicate(self, url: str) -> bool:
        if not self._cache_loaded:
            self.get_all_urls()
        return self._normalize(url) in self._cache

    # --------------------------------------------------------
    # Нормализация URL (приведение к единому виду для сравнения)
    # --------------------------------------------------------
    @staticmethod
    def _normalize(url: str) -> str:
        if not url:
            return ''
        n = url.strip()
        n = ' '.join(n.split())
        n = n.lower().rstrip('/')
        # Убираем www.
        n = n.replace('https://www.', 'https://').replace('http://www.', 'http://')
        # Убираем протокол (для унификации http/https)
        n = n.replace('https://', '').replace('http://', '')
        return n

    # --------------------------------------------------------
    # Сброс кэша (если надо перечитать таблицу)
    # --------------------------------------------------------
    def invalidate_cache(self):
        self._cache = set()
        self._cache_loaded = False
        logger.info('🔄 Кэш Google Sheets сброшен')
