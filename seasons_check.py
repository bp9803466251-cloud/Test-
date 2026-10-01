#!/usr/bin/env python3
"""
seasons_check.py — быстрая проверка загруженных сезонов в Redis.
Не тянет значения полей, только ключи — работает в 10-20 раз быстрее
полной диагностики. HLEN + HSCAN с COUNT=50 + retry.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse

# ---------------------------------------------------------------------------
# Подключение к Redis (redis_hub → Upstash REST)
# ---------------------------------------------------------------------------

_REDIS_MODE = None
_REDIS_URL = None
_REDIS_TOKEN = None
HASH_NAME = "GatekeeperAI"


def _p(msg):
    print(msg, flush=True)


def init_redis():
    """Инициализация: redis_hub (локально) → Upstash REST (CI)."""
    global _REDIS_MODE, _REDIS_URL, _REDIS_TOKEN

    try:
        import redis_hub
        redis_hub._execute_upstash_cmd(["PING"])
        _REDIS_MODE = "redis_hub"
        _p("[DIAG] Redis backend: redis_hub")
        return True
    except Exception:
        pass

    _REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[DIAG] Redis backend: Upstash REST API")
        return True

    _REDIS_URL = os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN", "")
    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[DIAG] Redis backend: shared Upstash REST API")
        return True

    _REDIS_MODE = None
    _p("[ERROR] Redis не найден")
    return False


def _exec_upstash(cmd, timeout=30):
    url = _REDIS_URL.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(str(c), safe="") for c in cmd
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_REDIS_TOKEN}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body).get("result")


def _exec(cmd, timeout=30):
    try:
        if _REDIS_MODE == "redis_hub":
            import redis_hub
            return redis_hub._execute_upstash_cmd(cmd, timeout=timeout)
        elif _REDIS_MODE == "upstash":
            return _exec_upstash(cmd, timeout=timeout)
        else:
            _p("[ERROR] Redis не инициализирован")
            return None
    except Exception as e:
        _p(f"[REDIS ERROR] {e}")
        return None


def _exec_with_retry(cmd, max_retries=3, timeout=30):
    """Выполнить команду с retry при таймауте."""
    for attempt in range(max_retries):
        if _REDIS_MODE == "redis_hub":
            try:
                import redis_hub
                redis_hub.reset_circuit_breaker()
            except Exception:
                pass
        result = _exec(cmd, timeout=timeout)
        if result is not None:
            return result
        if attempt < max_retries - 1:
            delay = 3 * (attempt + 1)
            _p(f"  [RETRY] Таймаут, ждём {delay}с и повторяем (попытка {attempt + 2}/{max_retries})...")
            time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Сканирование ключей (только ключи, без значений — быстро)
# ---------------------------------------------------------------------------

def scan_all_keys():
    """HSCAN с COUNT=50 и retry. Возвращает список ключей."""
    cursor = "0"
    all_keys = []
    iteration = 0
    consecutive_failures = 0

    while True:
        cmd = ["HSCAN", HASH_NAME, str(cursor), "COUNT", "50"]
        result = _exec_with_retry(cmd, max_retries=3, timeout=30)

        if result is None:
            consecutive_failures += 1
            _p(f"  [FAIL] HSCAN не прошёл (подряд: {consecutive_failures})")
            if consecutive_failures >= 5:
                _p("  [STOP] 5 неудач подряд — останавливаемся")
                break
            time.sleep(5 * consecutive_failures)
            continue

        consecutive_failures = 0

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                break

        if not isinstance(result, list) or len(result) < 2:
            break

        next_cursor = str(result[0])
        fields = result[1]

        if isinstance(fields, list):
            keys_batch = fields[::2]
            all_keys.extend(keys_batch)

        iteration += 1
        if iteration % 20 == 0:
            _p(f"  ...просканировано {len(all_keys)} ключей (итерация {iteration})")

        if next_cursor == "0":
            break
        cursor = next_cursor
        time.sleep(0.1)

    return all_keys


# ---------------------------------------------------------------------------
# Разбор ключей по сезонам и категориям
# ---------------------------------------------------------------------------

def get_season_from_date(date_str):
    if not date_str or len(date_str) < 4:
        return "?"
    try:
        y = int(date_str[:4])
        m = int(date_str[5:7]) if len(date_str) >= 7 else 0
        if m >= 7:
            return f"{y}/{y + 1}"
        else:
            return f"{y - 1}/{y}"
    except Exception:
        return "?"


def classify_key(key):
    """Возвращает (категория, сезон, дата) для ключа."""
    k = str(key)

    if k.startswith("match:"):
        return "match", None, None

    if k.startswith("search:results:"):
        return "search", None, None

    if k.startswith("history:index:"):
        date_part = k.replace("history:index:", "")
        if len(date_part) == 8 and date_part.isdigit():
            season = get_season_from_date(
                f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
            )
            return "history:index", season, date_part
        return "history:index", "?", None

    if k.startswith("history:"):
        parts = k.split("__")
        if len(parts) >= 3:
            date_part = parts[-1]
            if len(date_part) == 8 and date_part.isdigit():
                season = get_season_from_date(
                    f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
                )
                return "history:match", season, date_part
        return "history:match", "?", None

    return "other", None, None


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def main():
    _p("=" * 60)
    _p("  ПРОВЕРКА СЕЗОНОВ В REDIS")
    _p("=" * 60)

    if not init_redis():
        sys.exit(1)

    # 1. HLEN — мгновенное общее количество
    _p("\n--- HLEN (общее количество полей) ---")
    hlen = _exec(["HLEN", HASH_NAME])
    if hlen is None:
        _p("[ERROR] HLEN не выполнен — выходим")
        sys.exit(1)
    hlen_val = int(hlen) if hlen else 0
    _p(f"  Всего полей: {hlen_val}")

    if hlen_val == 0:
        _p("[WARNING] Хеш пуст — нет данных для проверки")
        sys.exit(0)

    # 2. HSCAN — собираем ключи
    _p(f"\n--- HSCAN (COUNT=50, читаем ключи) ---")
    keys = scan_all_keys()

    _p(f"\n  Просканировано ключей: {len(keys)}")
    if hlen_val and len(keys) < hlen_val:
        missing = hlen_val - len(keys)
        _p(f"  [WARNING] Недосканировано: {missing} ключей (HLEN={hlen_val}, скан={len(keys)})")

    # 3. Разбор по категориям и сезонам
    categories = {}
    seasons = {}

    for key in keys:
        cat, season, _ = classify_key(key)
        categories[cat] = categories.get(cat, 0) + 1
        if season:
            seasons[season] = seasons.get(season, 0) + 1

    # 4. Вывод категорий
    _p("\n--- Категории ключей ---")
    for cat in sorted(categories.keys()):
        _p(f"  {cat}: {categories[cat]}")

    # 5. Вывод сезонов
    _p("\n--- Сезоны (history + history:index) ---")
    total_season_matches = 0
    for season in sorted(seasons.keys()):
        count = seasons[season]
        total_season_matches += count
        _p(f"  {season}: {count}")

    _p(f"\n  Итого сезонов: {len(seasons)}")
    _p(f"  Итого по сезонам: {total_season_matches}")

    # 6. Сводка
    _p("\n" + "=" * 60)
    _p("  СВОДКА")
    _p("=" * 60)
    _p(f"  HLEN:          {hlen_val}")
    _p(f"  Просканировано: {len(keys)}")
    _p(f"  Категорий:     {len(categories)}")
    _p(f"  Сезонов:       {len(seasons)}")
    if hlen_val and len(keys) == hlen_val:
        _p("  [OK] Все поля просканированы")
    else:
        _p(f"  [WARNING] Просканировано не всё ({len(keys)}/{hlen_val})")
    _p("=" * 60)


if __name__ == "__main__":
    main()
