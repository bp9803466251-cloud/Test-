"""
redis_config.py — Конфигурация подключения к Upstash Redis REST API.
§1.3: Нулевая зависимость — только urllib.request, без тяжёлых Redis-клиентов.
§6: Транспортный слой использует Upstash REST API (не TCP host:port).

Переменные окружения (GitHub Actions secrets):
  SHARED_UPSTASH_REDIS_REST_URL   — Base URL Upstash REST API
  SHARED_UPSTASH_REDIS_REST_TOKEN  — Bearer token для аутентификации

Дополнительно (опционально):
  SHARED_REDIS_REST_URL    — Fallback URL (для не-shared окружений)
  SHARED_REDIS_REST_TOKEN  — Fallback token
  REDIS_TIMEOUT            — Таймаут запроса (сек), по умолчанию 30
"""

import os

# ── Upstash REST API URL ────────────────────────────────────
# Приоритет: SHARED_ → fallback → пустая строка (для совместимости)
REDIS_REST_URL = (
    os.getenv("SHARED_UPSTASH_REDIS_REST_URL", "")
    or os.getenv("SHARED_REDIS_REST_URL", "")
    or os.getenv("UPSTASH_REDIS_REST_URL", "")
)

# ── Upstash REST API Token ───────────────────────────────────
REDIS_REST_TOKEN = (
    os.getenv("SHARED_UPSTASH_REDIS_REST_TOKEN", "")
    or os.getenv("SHARED_REDIS_REST_TOKEN", "")
    or os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
)

# ── Таймауты (§6: дифференцированные тайм-ауты) ───────────────
REDIS_TIMEOUT = int(os.getenv("REDIS_TIMEOUT", "30"))

# ── Circuit Breaker параметры (§1.9) ─────────────────────────
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "10"))
CB_RECOVERY_TIMEOUT = int(os.getenv("CB_RECOVERY_TIMEOUT", "60"))

# ── Дополнительные параметры ─────────────────────────────────
REDIS_HASH_NAME = os.getenv("REDIS_HASH_NAME", "GatekeeperAI")
REDIS_MAX_PIPELINE = int(os.getenv("REDIS_MAX_PIPELINE", "10"))

# ── Проверка конфигурации ────────────────────────────────────
def get_redis_config_errors():
    """Возвращает список ошибок конфигурации Redis."""
    errors = []
    if not REDIS_REST_URL:
        errors.append("REDIS_REST_URL is empty — check SHARED_UPSTASH_REDIS_REST_URL")
    if not REDIS_REST_TOKEN:
        errors.append("REDIS_REST_TOKEN is empty — check SHARED_UPSTASH_REDIS_REST_TOKEN")
    return errors

def is_redis_configured():
    """Проверяет, достаточно ли переменных для подключения."""
    return bool(REDIS_REST_URL and REDIS_REST_TOKEN)

def get_redis_info():
    """Возвращает краткую информацию о конфигурации Redis (без токена)."""
    url_display = REDIS_REST_URL[:30] + "..." if len(REDIS_REST_URL) > 30 else REDIS_REST_URL
    return {
        "url": url_display,
        "token_set": bool(REDIS_REST_TOKEN),
        "timeout": REDIS_TIMEOUT,
        "hash_name": REDIS_HASH_NAME,
        "cb_threshold": CB_FAILURE_THRESHOLD,
        "cb_recovery": CB_RECOVERY_TIMEOUT,
        "configured": is_redis_configured(),
    }


if __name__ == "__main__":
    info = get_redis_info()
    errors = get_redis_config_errors()
    print("Redis Configuration:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    if errors:
        print("\nERRORS:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\n  Status: OK")
