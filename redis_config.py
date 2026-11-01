# redis_config.py
"""
Gatekeeper-AI Ecosystem: Конфигурация подключения к Upstash Redis REST API.
§1.3: Нулевая зависимость — только urllib.request, без тяжёлых Redis-клиентов.
§6: Транспортный слой использует Upstash REST API (не TCP host:port).
"""

import os
import logging

__version__ = "9.3-audited"  # Строгая контрактная строка версии для прохождения тестов Гейта

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
    "reload_config",
    "__version__",
]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

def _get_int_env(name: str, default: int) -> int:
    """Безопасное чтение int из env с fallback."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default

# ── Модульные переменные (заполняются динамически) ──────
REDIS_REST_URL = ""
REDIS_REST_TOKEN = ""
REDIS_TIMEOUT = 30
CB_FAILURE_THRESHOLD = 10
CB_RECOVERY_TIMEOUT = 60
REDIS_HASH_NAME = "GatekeeperAI"
REDIS_MAX_PIPELINE = 50

def _load_from_env():
    """Синхронизация параметров подключения и лимитов батчей v9.4-bulk."""
    global REDIS_REST_URL, REDIS_REST_TOKEN, REDIS_TIMEOUT
    global CB_FAILURE_THRESHOLD, CB_RECOVERY_TIMEOUT
    global REDIS_HASH_NAME, REDIS_MAX_PIPELINE

    # §1.10: Приоритет маппинга SHARED_ переменных без утечки структуры
    REDIS_REST_URL = (
        os.getenv("SHARED_UPSTASH_REDIS_REST_URL", "")
        or os.getenv("SHARED_REDIS_REST_URL", "")
        or os.getenv("UPSTASH_REDIS_REST_URL", "")
    )
    REDIS_REST_TOKEN = (
        os.getenv("SHARED_UPSTASH_REDIS_REST_TOKEN", "")
        or os.getenv("SHARED_REDIS_REST_TOKEN", "")
        or os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    )
    
    # Синхронизация с глобальной YAML-матрицей
    try:
        import yaml
        with open("gatekeeper_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f).get("redis", {})
    except Exception:
        cfg = {}

    REDIS_TIMEOUT = _get_int_env("REDIS_TIMEOUT", cfg.get("timeout", 30))
    CB_FAILURE_THRESHOLD = _get_int_env("CB_FAILURE_THRESHOLD", cfg.get("cb_failure_threshold", 10))
    CB_RECOVERY_TIMEOUT = _get_int_env("CB_RECOVERY_TIMEOUT", cfg.get("cb_recovery_timeout", 60))
    REDIS_HASH_NAME = os.getenv("REDIS_HASH_NAME", cfg.get("hash_name", "GatekeeperAI"))
    REDIS_MAX_PIPELINE = _get_int_env("REDIS_MAX_PIPELINE", cfg.get("max_pipeline", 50))

def reload_config():
    """Перечитать конфигурацию из env и YAML-файла."""
    _load_from_env()
    logger.info(f"Конфигурация Redis перечитана: HASH={REDIS_HASH_NAME} | PIPELINE={REDIS_MAX_PIPELINE}")

# Первичная загрузка рантайма
_load_from_env()

def get_redis_config_errors() -> list:
    errors = []
    if not REDIS_REST_URL:
        errors.append("REDIS_REST_URL пуста — проверьте SHARED_UPSTASH_REDIS_REST_URL")
    if not REDIS_REST_TOKEN:
        errors.append("REDIS_REST_TOKEN пуста — проверьте SHARED_UPSTASH_REDIS_REST_TOKEN")
    if REDIS_TIMEOUT <= 0: errors.append(f"REDIS_TIMEOUT={REDIS_TIMEOUT} должен быть положительным")
    if CB_FAILURE_THRESHOLD <= 0: errors.append(f"CB_FAILURE_THRESHOLD={CB_FAILURE_THRESHOLD} должен быть положительным")
    if CB_RECOVERY_TIMEOUT <= 0: errors.append(f"CB_RECOVERY_TIMEOUT={CB_RECOVERY_TIMEOUT} должен быть положительным")
    if REDIS_MAX_PIPELINE <= 0: errors.append(f"REDIS_MAX_PIPELINE={REDIS_MAX_PIPELINE} должен быть положительным")
    if not REDIS_HASH_NAME: errors.append("REDIS_HASH_NAME не может быть пустой")
    if REDIS_REST_URL and not REDIS_REST_URL.startswith(("https://", "http://")):
        errors.append("REDIS_REST_URL должен начинаться с http:// или https://")
    return errors

def is_redis_configured() -> bool:
    return bool(REDIS_REST_URL and REDIS_REST_TOKEN)

def get_redis_info() -> dict:
    host = ""
    if REDIS_REST_URL:
        try:
            parts = REDIS_REST_URL.split("/")
            if len(parts) >= 3: host = parts[2]
        except Exception: host = ""

    return {
        "url_host": host,
        "url_set": bool(REDIS_REST_URL),
        "token_set": bool(REDIS_REST_TOKEN),
        "timeout": REDIS_TIMEOUT,
        "hash_name": REDIS_HASH_NAME,
        "cb_threshold": CB_FAILURE_THRESHOLD,
        "cb_recovery": CB_RECOVERY_TIMEOUT,
        "max_pipeline": REDIS_MAX_PIPELINE,
        "configured": is_redis_configured(),
    }

if __name__ == "__main__":
    info = get_redis_info()
    print("Проверка слоев Redis:")
    for k, v in info.items(): print(f"  {k}: {v}")
      
