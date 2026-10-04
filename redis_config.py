"""
redis_config.py — Конфигурация подключения к Upstash Redis REST API.
§1.3: Нулевая зависимость — только urllib.request, без тяжёлых Redis-клиентов.
§6: Транспортный слой использует Upstash REST API (не TCP host:port).
§1.9: Circuit breaker — 10 ошибок → 60с (env-configurable).
§1.10: SHARED_ переменные с fallback || ''.

Переменные окружения (GitHub Actions secrets):
  SHARED_UPSTASH_REDIS_REST_URL    — Base URL Upstash REST API
  SHARED_UPSTASH_REDIS_REST_TOKEN  — Bearer token для аутентификации

Дополнительно (опционально):
  SHARED_REDIS_REST_URL    — Fallback URL (для не-shared окружений)
  SHARED_REDIS_REST_TOKEN  — Fallback token
  REDIS_TIMEOUT            — Таймаут запроса (сек), по умолчанию 30
  CB_FAILURE_THRESHOLD     — Порог circuit breaker, по умолчанию 10
  CB_RECOVERY_TIMEOUT      — Таймаут восстановления CB (сек), по умолчанию 60
  REDIS_HASH_NAME          — Имя хэша Redis, по умолчанию GatekeeperAI
  REDIS_MAX_PIPELINE       — Размер батча для HKEYS/HMGET, по умолчанию 10
"""

import os
import logging

__version__ = "8.10-patched"

__all__ = [
    "REDIS_REST_URL",
    "REDIS_REST_TOKEN",
    "REDIS_TIMEOUT",
    "REDIS_HASH_NAME",
    "CB_FAILURE_THRESHOLD",
    "CB_RECOVERY_TIMEOUT",
    "REDIS_MAX_PIPELINE",
    "get_redis_config_errors",
    "is_redis_configured",
    "get_redis_info",
    "__version__",
]

logger = logging.getLogger(__name__)


def _get_int_env(name: str, default: int) -> int:
    """Безопасное чтение int из env с fallback."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("redis_config: env %s=%r not a number, using default=%d", name, raw, default)
        return default


# ── Upstash REST API URL ────────────────────────────────────
# §1.10: Приоритет SHARED_ → fallback → пустая строка
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

# ── Таймауты (§6: 30с для Redis) ─────────────────────────────
REDIS_TIMEOUT = _get_int_env("REDIS_TIMEOUT", 30)

# ── Circuit Breaker параметры (§1.9) ─────────────────────────
CB_FAILURE_THRESHOLD = _get_int_env("CB_FAILURE_THRESHOLD", 10)
CB_RECOVERY_TIMEOUT = _get_int_env("CB_RECOVERY_TIMEOUT", 60)

# ── Дополнительные параметры ─────────────────────────────────
REDIS_HASH_NAME = os.getenv("REDIS_HASH_NAME", "GatekeeperAI")
REDIS_MAX_PIPELINE = _get_int_env("REDIS_MAX_PIPELINE", 10)


# ── Проверка конфигурации ────────────────────────────────────
def get_redis_config_errors() -> list:
    """Возвращает список ошибок конфигурации Redis."""
    errors = []
    if not REDIS_REST_URL:
        errors.append("REDIS_REST_URL is empty — check SHARED_UPSTASH_REDIS_REST_URL")
    if not REDIS_REST_TOKEN:
        errors.append("REDIS_REST_TOKEN is empty — check SHARED_UPSTASH_REDIS_REST_TOKEN")
    if REDIS_TIMEOUT <= 0:
        errors.append(f"REDIS_TIMEOUT={REDIS_TIMEOUT} must be positive")
    if CB_FAILURE_THRESHOLD <= 0:
        errors.append(f"CB_FAILURE_THRESHOLD={CB_FAILURE_THRESHOLD} must be positive")
    if CB_RECOVERY_TIMEOUT <= 0:
        errors.append(f"CB_RECOVERY_TIMEOUT={CB_RECOVERY_TIMEOUT} must be positive")
    if REDIS_MAX_PIPELINE <= 0:
        errors.append(f"REDIS_MAX_PIPELINE={REDIS_MAX_PIPELINE} must be positive")
    return errors


def is_redis_configured() -> bool:
    """Проверяет, достаточно ли переменных для подключения."""
    return bool(REDIS_REST_URL and REDIS_REST_TOKEN)


def get_redis_info() -> dict:
    """Возвращает краткую информацию о конфигурации Redis (без токена)."""
    return {
        "url": bool(REDIS_REST_URL),  # не показываем URL — безопасность
        "token_set": bool(REDIS_REST_TOKEN),
        "timeout": REDIS_TIMEOUT,
        "hash_name": REDIS_HASH_NAME,
        "cb_threshold": CB_FAILURE_THRESHOLD,
        "cb_recovery": CB_RECOVERY_TIMEOUT,
        "max_pipeline": REDIS_MAX_PIPELINE,
        "configured": is_redis_configured(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    info = get_redis_info()
    errors = get_redis_config_errors()
    print("Redis Configuration:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    if errors:
        print()
        print("ERRORS:")
        for e in errors:
            print(f"  - {e}")
    else:
        print()
        print("  Status: OK")
