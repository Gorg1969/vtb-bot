# sheets_client.py
# ============================================================
# Клиент Google Apps Script (для дедупа)
# С retry и обходом CDN-редиректа (404 от script.googleusercontent.com)
# ============================================================

import time
import logging
import requests
from typing import Set, List, Dict

logger = logging.getLogger(__name__)


class SheetsClient:
    def __init__(self, url: str, timeout: int = 180, max_retries: int = 5):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache: Set[str] = set()
        self._cache_loaded = False

    def get_all_urls(self, use_cache: bool = True) -> Set[str]:
        if use_cache and self._cache_loaded:
            return self._cache

        if not self.url:
            logger.warning('⚠️ SHEETS_URL не задан, дедуп отключён')
            return set()

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f'📥 Загрузка ссылок из Google Sheets (попытка {attempt}/{self.max_retries})...')

                resp = requests.get(
                    self.url,
                    params={'action': 'getUrls', 'redirect': 'false'},
                    timeout=self.timeout,
                    verify=False,
                    allow_redirects=True,
                )

                # 404 от CDN (script.googleusercontent.com) — пробуем ещё раз
                if resp.status_code == 404:
                    logger.warning(f'⚠️ 404 от Google CDN (попытка {attempt})')
                    if attempt < self.max_retries:
                        time.sleep(5)
                        continue
                    else:
                        logger.error('❌ Все попытки исчерпаны, дедуп не работает')
                        return set()

                if resp.status_code != 200:
                    logger.error(f'❌ Sheets API: HTTP {resp.status_code}')
                    if attempt < self.max_retries:
                        time.sleep(3)
                        continue
                    return set()

                # Парсим JSON
                try:
                    data = resp.json()
                except Exception as je:
                    logger.error(f'❌ Ответ не JSON: {je}')
                    logger.error(f'   Тело: {resp.text[:300]}')
                    if attempt < self.max_retries:
                        time.sleep(3)
                        continue
                    return set()

                if not data.get('success'):
                    logger.error(f'❌ Ошибка Sheets: {data}')
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
                logger.error(f'❌ Таймаут Google Sheets (попытка {attempt})')
                if attempt < self.max_retries:
                    time.sleep(5)
                    continue
                return set()

            except Exception as e:
                logger.error(f'❌ Ошибка Google Sheets: {e}')
                if attempt < self.max_retries:
                    time.sleep(5)
                    continue
                return set()

        return set()

    def check_bulk(self, urls: List[str]) -> Dict:
        """Массовая проверка (POST)."""
        if not urls:
            return {'duplicates': [], 'new': []}

        if not self.url:
            return {'duplicates': [], 'new': urls}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    self.url,
                    json={'action': 'checkBulk', 'urls': urls},
                    timeout=self.timeout,
                    verify=False,
                )

                if resp.status_code == 404:
                    logger.warning(f'⚠️ 404 checkBulk (попытка {attempt})')
                    if attempt < self.max_retries:
                        time.sleep(5)
                        continue
                    return {'duplicates': [], 'new': urls}

                if resp.status_code != 200:
                    logger.error(f'❌ checkBulk: HTTP {resp.status_code}')
                    return {'duplicates': [], 'new': urls}

                data = resp.json()
                if not data.get('success'):
                    logger.error(f'❌ checkBulk: {data}')
                    return {'duplicates': [], 'new': urls}

                logger.info(
                    f'✅ Bulk: дублей {len(data.get("duplicates", []))}, '
                    f'новых {len(data.get("new", []))}'
                )
                return data

            except Exception as e:
                logger.error(f'❌ Ошибка checkBulk: {e}')
                if attempt < self.max_retries:
                    time.sleep(3)
                    continue
                return {'duplicates': [], 'new': urls}

        return {'duplicates': [], 'new': urls}

    def is_duplicate(self, url: str) -> bool:
        if not self._cache_loaded:
            self.get_all_urls()
        return self._normalize(url) in self._cache

    @staticmethod
    def _normalize(url: str) -> str:
        if not url:
            return ''
        n = url.strip()
        n = ' '.join(n.split())
        n = n.lower().rstrip('/')
        n = n.replace('https://www.', 'https://').replace('http://www.', 'http://')
        n = n.replace('https://', '').replace('http://', '')
        return n

    def invalidate_cache(self):
        self._cache = set()
        self._cache_loaded = False
