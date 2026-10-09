# sheets_client.py 3
# ============================================================
# Клиент Google Apps Script (для дедупа)
# - Поддержка дат: ответ может быть [{url, date}] или [url]
# - Даты в формате "дд.мм.гггг" (например, 15.04.2026)
# - Правило Т1: URL есть в таблице и дата СВЕЖЕЕ DEDUP_MAX_AGE_DAYS
#   → дубль; СТАРШЕ → НЕ дубль (перепубликация)
# - С retry и обходом CDN-редиректа (404 от script.googleusercontent.com)
# ============================================================

import time
import logging
import requests
from datetime import datetime
from typing import Set, List, Dict

logger = logging.getLogger(__name__)


class SheetsClient:
    def __init__(self, url: str, timeout: int = 180, max_retries: int = 5):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache: Dict[str, str] = {}   # {normalized_url: date_str}
        self._cache_loaded = False
        self._max_age_days = 30            # дефолт, переопределяется извне

    # --------------------------------------------------------
    # НАСТРОЙКА ПОРОГА
    # --------------------------------------------------------

    def set_max_age_days(self, days: int):
        """Установить порог 'старости' записи (в днях)."""
        try:
            self._max_age_days = int(days)
        except (TypeError, ValueError):
            pass

    # --------------------------------------------------------
    # ЗАГРУЗКА ВСЕХ URL + ДАТ
    # --------------------------------------------------------

    def get_all_urls(self, use_cache: bool = True) -> Dict[str, str]:
        """
        Возвращает {normalized_url: date_str}.
        Поддерживает оба формата ответа Apps Script:
          - ["url1", "url2", ...]                          (старый)
          - [{"url": "...", "date": "15.04.2026"}, ...]    (новый)
        """
        if use_cache and self._cache_loaded:
            return self._cache

        if not self.url:
            logger.warning('⚠️ SHEETS_URL не задан, дедуп отключён')
            return {}

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    f'📥 Загрузка ссылок из Google Sheets '
                    f'(попытка {attempt}/{self.max_retries})...'
                )

                resp = requests.get(
                    self.url,
                    params={'action': 'getUrls', 'redirect': 'false'},
                    timeout=self.timeout,
                    verify=False,
                    allow_redirects=True,
                )

                if resp.status_code == 404:
                    logger.warning(f'⚠️ 404 от Google CDN (попытка {attempt})')
                    if attempt < self.max_retries:
                        time.sleep(2)
                        continue
                    logger.error('❌ Все попытки исчерпаны, дедуп не работает')
                    return {}

                if resp.status_code != 200:
                    logger.error(f'❌ Sheets API: HTTP {resp.status_code}')
                    if attempt < self.max_retries:
                        time.sleep(3)
                        continue
                    return {}

                try:
                    data = resp.json()
                except Exception as je:
                    logger.error(f'❌ Ответ не JSON: {je}')
                    logger.error(f'   Тело: {resp.text[:300]}')
                    if attempt < self.max_retries:
                        time.sleep(3)
                        continue
                    return {}

                if not data.get('success'):
                    logger.error(f'❌ Ошибка Sheets: {data}')
                    return {}

                raw = data.get('urls', [])
                self._cache = self._parse_urls(raw)
                self._cache_loaded = True

                with_dates = sum(1 for v in self._cache.values() if v)
                logger.info(
                    f'✅ Загружено {len(self._cache)} ссылок '
                    f'({with_dates} с датами) '
                    f'из {len(data.get("sheets", []))} листов'
                )
                return self._cache

            except requests.exceptions.Timeout:
                logger.error(f'❌ Таймаут Google Sheets (попытка {attempt})')
                if attempt < self.max_retries:
                    time.sleep(3)
                    continue
                return {}

            except Exception as e:
                logger.error(f'❌ Ошибка Google Sheets: {e}')
                if attempt < self.max_retries:
                    time.sleep(3)
                    continue
                return {}

        return {}

    @staticmethod
    def _parse_urls(raw) -> Dict[str, str]:
        """
        Преобразует ответ Apps Script в {normalized_url: date_str}.
        date_str — как пришло из таблицы ("15.04.2026") или ''.
        """
        result: Dict[str, str] = {}
        for item in raw:
            if isinstance(item, str):
                url = item.strip()
                date_str = ''
            elif isinstance(item, dict):
                url = (item.get('url') or '').strip()
                date_str = (item.get('date') or '').strip()
            else:
                continue

            if not url:
                continue

            norm = SheetsClient._normalize(url)
            # Если уже была запись без даты, а сейчас пришла с датой — перезаписываем
            if norm not in result or (date_str and not result.get(norm)):
                result[norm] = date_str

        return result

    # --------------------------------------------------------
    # ПРОВЕРКА ДУБЛЯ (правило Т1)
    # --------------------------------------------------------

    def is_duplicate(self, url: str) -> bool:
        """
        Правило Т1:
          - URL нет в таблице                     → НЕ дубль
          - URL есть, дата СВЕЖЕЕ max_age_days    → дубль
          - URL есть, дата СТАРШЕ max_age_days    → НЕ дубль (перепубликация)
          - URL есть, дата не распарсилась        → дубль (безопасно)
        """
        if not self._cache_loaded:
            self.get_all_urls()

        norm = self._normalize(url)
        if norm not in self._cache:
            return False

        date_str = self._cache.get(norm, '')
        if not date_str:
            # дата отсутствует → считаем дублем (безопасный вариант)
            return True

        parsed = self._parse_date(date_str)
        if parsed is None:
            # не распарсили → считаем дублем
            return True

        age_days = (datetime.now() - parsed).days
        if age_days >= self._max_age_days:
            logger.info(
                f'  🔁 Перепубликация: URL в таблице, '
                f'дата {date_str} ({age_days} дн. назад)'
            )
            return False

        return True

    @staticmethod
    def _parse_date(date_str: str):
        """
        Парсит дату из таблицы.
        Поддерживает форматы:
          - "15.04.2026"           (дд.мм.гггг)
          - "15.04.2026 12:34"
          - "15.04.2026 12:34:56"
          - "2026-04-15"
          - "2026-04-15T12:34:00Z"
        Возвращает datetime (naive) или None.
        """
        if not date_str:
            return None

        s = date_str.strip()

        # ISO: 2026-04-15 или 2026-04-15T12:34:00Z
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            pass

        # дд.мм.гггг [чч:мм[:сс]]
        for fmt in ('%d.%m.%Y %H:%M:%S', '%d.%m.%Y %H:%M', '%d.%m.%Y'):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue

        logger.warning(f'⚠️ Не распарсил дату: "{date_str}"')
        return None

    # --------------------------------------------------------
    # МАССОВАЯ ПРОВЕРКА (POST) — оставлено для совместимости
    # --------------------------------------------------------

    def check_bulk(self, urls: List[str]) -> Dict:
        """Массовая проверка (POST). Не используется в Т1-логике."""
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
                        time.sleep(2)
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

    # --------------------------------------------------------
    # СЛУЖЕБНОЕ
    # --------------------------------------------------------

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
        self._cache = {}
        self._cache_loaded = False
