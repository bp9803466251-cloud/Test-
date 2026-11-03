# redis_config.py
"""
Gatekeeper-AI Ecosystem: Конфигурация подключения к Upstash Redis REST API.
§1.3: Нулевая зависимость — только urllib.request, без тяжёлых Redis-клиентов.
§6: Транспортный слой использует Upstash REST API (не TCP host:port).
[PATCHED 9.6.8] Порог CB выровнен на 15; https-only; strip() env-значений;
коэрсия YAML-чисел; защита от пустого/недикт YAML.
"""

import os
import logging

__version__ = "9.6.8-patched"

__all__ = [
    "REDIS_REST_URL", "REDIS_REST_TOKEN", "REDIS_TIMEOUT", "REDIS_HASH_NAME",
    "CB_FAILURE_THRESHOLD", "CB_RECOVERY_TIMEOUT", "REDIS_MAX_PIPELINE",
    "get_redis_config_errors", "is_redis_configured", "get_redis_info",
    "reload_config", "__version__",
]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_CB_THRESHOLD = 15  # Контракт пакета: cb_failure_threshold: 15 (YAML/chaos/ledger)

def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        val = int(raw.strip())
        return val if val > 0 else default
    except (ValueError, TypeError):
        return default

def _yaml_int(value, default: int) -> int:
    try:
        val = int(str(value).strip())
        return val if val > 0 else default
    except (ValueError, TypeError):
        return default

REDIS_REST_URL = ""
REDIS_REST_TOKEN = ""
REDIS_TIMEOUT = 30
CB_FAILURE_THRESHOLD = DEFAULT_CB_THRESHOLD
CB_RECOVERY_TIMEOUT = 60
REDIS_HASH_NAME = "GatekeeperAI"
REDIS_MAX_PIPELINE = 50

def _load_yaml_redis_section():
    """Чтение секции redis из YAML с защитой от пустого/недикт файла."""
    try:
        import yaml
        with open("gatekeeper_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            logger.warning("gatekeeper_config.yaml пуст или не является словарем — дефолты redis")
            return {}
        section = cfg.get("redis", {})
        if not isinstance(section, dict):
            logger.warning("Секция redis не является словарем — дефолты")
            return {}
        return section
    except FileNotFoundError:
        logger.debug("gatekeeper_config.yaml отсутствует — работаем на дефолтах redis")
        return {}
    except Exception as e:
        logger.warning(f"Ошибка парсинга gatekeeper_config.yaml: {e} — дефолты redis")
        return {}

def _load_from_env():
    global REDIS_REST_URL, REDIS_REST_TOKEN, REDIS_TIMEOUT
    global CB_FAILURE_THRESHOLD, CB_RECOVERY_TIMEOUT
    global REDIS_HASH_NAME, REDIS_MAX_PIPELINE

    REDIS_REST_URL = (
        os.getenv("SHARED_UPSTASH_REDIS_REST_URL", "").strip()
        or os.getenv("SHARED_REDIS_REST_URL", "").strip()
        or os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
    )
    REDIS_REST_TOKEN = (
        os.getenv("SHARED_UPSTASH_REDIS_REST_TOKEN", "").strip()
        or os.getenv("SHARED_REDIS_REST_TOKEN", "").strip()
        or os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    )

    cfg = _load_yaml_redis_section()
    REDIS_TIMEOUT = _get_int_env("REDIS_TIMEOUT", _yaml_int(cfg.get("timeout"), 30))
    CB_FAILURE_THRESHOLD = _get_int_env("CB_FAILURE_THRESHOLD", _yaml_int(cfg.get("cb_failure_threshold"), DEFAULT_CB_THRESHOLD))
    CB_RECOVERY_TIMEOUT = _get_int_env("CB_RECOVERY_TIMEOUT", _yaml_int(cfg.get("cb_recovery_timeout"), 60))
    REDIS_HASH_NAME = os.getenv("REDIS_HASH_NAME", "").strip() or str(cfg.get("hash_name") or "GatekeeperAI")
    REDIS_MAX_PIPELINE = _get_int_env("REDIS_MAX_PIPELINE", _yaml_int(cfg.get("max_pipeline"), 50))

def reload_config():
    _load_from_env()
    logger.info(f"Конфигурация Redis перечитана: HASH={REDIS_HASH_NAME} | PIPELINE={REDIS_MAX_PIPELINE}")

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
    if REDIS_REST_URL and not REDIS_REST_URL.startswith("https://"):
        errors.append("REDIS_REST_URL должен начинаться с https:// (REST-токен в заголовках)")
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
        "url_host": host, "url_set": bool(REDIS_REST_URL), "token_set": bool(REDIS_REST_TOKEN),
        "timeout": REDIS_TIMEOUT, "hash_name": REDIS_HASH_NAME,
        "cb_threshold": CB_FAILURE_THRESHOLD, "cb_recovery": CB_RECOVERY_TIMEOUT,
        "max_pipeline": REDIS_MAX_PIPELINE, "configured": is_redis_configured(),
    }

if __name__ == "__main__":
    info = get_redis_info()
    print("Проверка слоев Redis:")
    for k, v in info.items(): print(f"  {k}: {v}")
