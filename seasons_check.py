#!/usr/bin/env python3
"""
seasons_check.py — быстрая проверка сезонов в Redis.
Читает только ключи (без значений) — в 10-20x быстрее полной диагностики.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime


def _p(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Redis backend init (redis_hub → Upstash REST → shared Upstash)
# ---------------------------------------------------------------------------
_REDIS_MODE = None
_REDIS_URL = None
_REDIS_TOKEN = None


def _init_redis():
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
    _p("[DIAG] No Redis backend available")
    return False


def _exec_upstash(cmd):
    url = _REDIS_URL.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(str(c), safe="") for c in cmd
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_REDIS_TOKEN}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body).get("result")


def _exec(cmd):
    try:
        if _REDIS_MODE == "redis_hub":
            import redis_hub
            return redis_hub._execute_upstash_cmd(cmd)
        elif _REDIS_MODE == "upstash":
            return _exec_upstash(cmd)
        else:
            _p("[ERROR] No Redis backend initialised")
            return None
    except Exception as e:
        _p(f"[REDIS ERROR] {e}")
        return None


def _reset_breaker():
    """Сброс circuit breaker (если redis_hub)."""
    if _REDIS_MODE == "redis_hub":
        try:
            import redis_hub
            redis_hub.reset_circuit_breaker()
        except Exception:
            pass


def _hscan_keys_only(hash_name, count=50):
    """
    HSCAN, читаем только ключи (значения не тянет).
    COUNT=50, retry 3x, consecutive_failures=5.
    """
    cursor = "0"
    all_keys = []
    consecutive_failures = 0
    max_consecutive_failures = 5
    iterations = 0

    while True:
        iterations += 1

        cmd = ["HSCAN", hash_name, str(cursor), "COUNT", str(count)]
        result = None
        for retry_attempt in range(3):
            result = _exec(cmd)
            if result is not None:
                break
            if retry_attempt < 2:
                wait_sec = 3 * (retry_attempt + 1)
                _p(f"  [HSCAN] retry {retry_attempt + 1}/3 через {wait_sec}s (cursor={cursor})")
                _reset_breaker()
                time.sleep(wait_sec)

        if result is None:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                _p(f"  [HSCAN] {consecutive_failures} неудач подряд, остановка")
                break
            wait_sec = 5 * consecutive_failures
            _p(f"  [HSCAN] таймаут, ждём {wait_sec}s (попытка {consecutive_failures}/{max_consecutive_failures})")
            _reset_breaker()
            time.sleep(wait_sec)
            continue

        consecutive_failures = 0

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                break
        if not isinstance(result, list) or len(result) < 2:
            break

        cursor = str(result[0])
        kv = result[1]
        if isinstance(kv, list):
            # Только ключи (чётные элементы), значения не парсим
            field_names = kv[::2]
            all_keys.extend(field_names)

        if iterations % 50 == 0:
            _p(f"  ...просканировано {len(all_keys)} ключей (итерация {iterations})")

        if cursor == "0" or cursor == 0:
            break
        time.sleep(0.1)

    return all_keys


def _get_season_from_key(key):
    """Определяем сезон по дате в ключе."""
    # history:match:xxx__YYYYMMDD
    # history:index:YYYYMMDD
    # match:xxx__YYYYMMDD
    parts = key.split("__")
    if len(parts) >= 3:
        date_part = parts[-1]
        if len(date_part) == 8 and date_part.isdigit():
            y = int(date_part[:4])
            m = int(date_part[4:6])
            if m >= 7:
                return f"{y}/{y + 1}"
            else:
                return f"{y - 1}/{y}"
    # history:index:YYYYMMDD
    if key.startswith("history:index:"):
        date_part = key.replace("history:index:", "")
        if len(date_part) == 8 and date_part.isdigit():
            y = int(date_part[:4])
            m = int(date_part[4:6])
            if m >= 7:
                return f"{y}/{y + 1}"
            else:
                return f"{y - 1}/{y}"
    return "?"


def main():
    _p("=" * 60)
    _p("  ПРОВЕРКА СЕЗОНОВ В REDIS")
    _p("=" * 60)

    if not _init_redis():
        _p("[FATAL] No Redis backend available")
        sys.exit(1)

    # --- HLEN ---
    _p("\n--- HLEN ---")
    hlen_res = _exec(["HLEN", "GatekeeperAI"])
    total_expected = 0
    if hlen_res is not None:
        try:
            total_expected = int(hlen_res)
        except (TypeError, ValueError):
            pass
    _p(f"  Всего полей: {total_expected}")

    if total_expected == 0:
        _p("[WARN] Хэш пуст")
        return

    # --- HSCAN (ключи только) ---
    _p(f"\n--- HSCAN (COUNT=50, читаем ключи) ---")
    keys = _hscan_keys_only("GatekeeperAI", count=50)
    _p(f"\n  Просканировано ключей: {len(keys)}")

    # --- Категории ---
    _p("\n--- Категории ключей ---")
    cat_history_index = [k for k in keys if k.startswith("history:index:")]
    cat_history_match = [k for k in keys if k.startswith("history:match:")]
    cat_match = [k for k in keys if k.startswith("match:")
                 and not k.startswith("history:")
                 and not k.startswith("match:index:")]
    cat_match_index = [k for k in keys if k.startswith("match:index:")]
    cat_other = [k for k in keys
                 if not k.startswith("history:")
                 and not k.startswith("match:")]

    _p(f"  history:index: {len(cat_history_index)}")
    _p(f"  history:match: {len(cat_history_match)}")
    _p(f"  match:*:       {len(cat_match)}")
    _p(f"  match:index:*: {len(cat_match_index)}")
    _p(f"  other:         {len(cat_other)}")

    if cat_other[:10]:
        _p(f"\n  Other keys sample (первые 10):")
        for k in cat_other[:10]:
            _p(f"    • {k}")

    # --- Сезоны ---
    _p("\n--- Сезоны ---")
    seasons = {}
    for key in keys:
        season = _get_season_from_key(key)
        seasons[season] = seasons.get(season, 0) + 1

    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    total_by_season = sum(v for k, v in seasons.items() if k != "?")
    _p(f"\n  Итого сезонов: {len([k for k in seasons if k != '?'])}")
    _p(f"  Итого по сезонам: {total_by_season}")
    if "?" in seasons:
        _p(f"  Без сезона (?: {seasons['?']}")

    # --- Сверка ---
    _p("\n" + "=" * 60)
    _p("  СВОДКА")
    _p("=" * 60)
    _p(f"  HLEN:          {total_expected}")
    _p(f"  Просканировано: {len(keys)}")
    if total_expected > 0 and len(keys) == total_expected:
        _p("  [OK] Все поля просканированы")
    elif total_expected > 0 and len(keys) < total_expected:
        _p(f"  [WARN] Недосканировано: {total_expected - len(keys)} полей")
    else:
        _p("  [INFO] HLEN недоступен, сверка невозможна")
    _p("=" * 60)


if __name__ == "__main__":
    main()
