"""
gatekeeper_config.py — Единая конфигурация GatekeeperAI (§20.3).
Загрузка из gatekeeper_config.yaml + env-переменные.
Кэшируется на уровне модуля.
"""

import os
import logging
try:
    import yaml
except ImportError:
    yaml = None
from datetime import datetime, timezone, timedelta

__version__ = "8.10-patched"

logger = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────
MSK_TZ = timezone(timedelta(hours=3))

_CONFIG_CACHE = None
_RELOAD_CALLBACKS = []


# ── Время ──────────────────────────────────────────────────
def now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def now_msk_short():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ── Namespace keys ─────────────────────────────────────────
def ns_key(base, cid, domain=""):
    """Построение ключа Redis с namespace."""
    if domain:
        return f"{base}:{domain}:{cid}" if cid else f"{base}:{domain}"
    return f"{base}:{cid}" if cid else base


# ── Загрузка конфигурации ──────────────────────────────────
def load_config(reload=False):
    """
    Единая загрузка конфигурации. Кэшируется на уровне модуля.
    (§20.3)
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE

    config = {}

    # Загрузка YAML
    if yaml is None:
        logger.info("PyYAML not installed, using defaults only")
        config = {}
    else:
        try:
            with open("gatekeeper_config.yaml", "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("gatekeeper_config.yaml not found, using defaults")
            config = {}
        except yaml.YAMLError as e:
            logger.error("gatekeeper_config.yaml parse error: %s", e)
            config = {}
        except Exception as e:
            logger.error("gatekeeper_config.yaml error: %s", e)
            config = {}

    # Fallback значения
    if "collectors" not in config:
        config["collectors"] = {}
    if "redis" not in config:
        config["redis"] = {
            "read_timeout": 30,
            "write_timeout": 30,
            "pipeline_batch_size": 10,
            "circuit_breaker_threshold": 10,
            "circuit_breaker_reset": 60,
        }
    if "cleanup" not in config:
        config["cleanup"] = {
            "match_ttl_hours": 2,
            "index_lookback_days": -2,
            "index_lookahead_days": 7,
        }
    if "features" not in config:
        config["features"] = {
            "graceful_shutdown": True,
            "auto_migrate": True,
        }

    _CONFIG_CACHE = config
    return config


def reload_config():
    """Принудительная перезагрузка конфигурации + callbacks."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    config = load_config(reload=True)
    for cb in _RELOAD_CALLBACKS:
        try:
            cb()
        except Exception as e:
            logger.warning("Reload callback failed: %s", e, exc_info=True)
    return config


def register_reload_callback(callback):
    """Регистрация callback для сброса кеша при reload."""
    _RELOAD_CALLBACKS.append(callback)


# ── Env ─────────────────────────────────────────────────────
def get_env():
    """Возвращает текущее окружение."""
    return os.environ.get("GATEKEEPER_ENV", "prod")


# ── Feature flags ──────────────────────────────────────────
# Фичи, которые включены по умолчанию
_FEATURE_DEFAULTS = {
    "graceful_shutdown": True,
    "auto_migrate": True,
}


def is_feature_enabled(feature_name):
    """
    Проверка включённости фича-флага (§24.3).
    Принимает 1 аргумент (FIX-7).
    Если флаг отсутствует в конфиге — используется значение по умолчанию.
    """
    config = load_config()
    features = config.get("features", {})
    if feature_name in features:
        return features[feature_name]
    return _FEATURE_DEFAULTS.get(feature_name, False)


# ── Cleanup ─────────────────────────────────────────────────
_CLEANUP_OWNERS = {
    "sharpapi", "odds_api", "bzzoiro", "propline",
    "football_data", "main", "self_test",
}


def should_run_cleanup(collector):
    """
    Проверка, должен ли данный коллектор запустить cleanup.
    Принимает 1 аргумент (FIX-7).
    Все live-коллекторы являются cleanup owners.
    """
    return collector in _CLEANUP_OWNERS


# ── Value threshold ────────────────────────────────────────
# FIX: value_engine.py импортирует VALUE_THRESHOLD отсюда.
# Приоритет: config → ENV → default 0.03
def _get_value_threshold():
    config = load_config()
    cfg_val = config.get("value", {}).get("threshold")
    if cfg_val is not None:
        try:
            return float(cfg_val)
        except (TypeError, ValueError):
            pass
    env_val = os.environ.get("VALUE_THRESHOLD", "") or os.environ.get("VALUE_BET_THRESHOLD", "")
    if env_val:
        try:
            return float(env_val)
        except (TypeError, ValueError):
            pass
    return 0.03


VALUE_THRESHOLD = _get_value_threshold()


# ── Ошибки конфигурации ─────────────────────────────────────
def get_config_errors():
    """Возвращает список ошибок конфигурации."""
    errors = []
    config = load_config()

    if not os.environ.get("SHARED_UPSTASH_REDIS_REST_URL") and \
       not os.environ.get("UPSTASH_REDIS_REST_URL"):
        errors.append("SHARED_UPSTASH_REDIS_REST_URL not set")

    if not os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN") and \
       not os.environ.get("UPSTASH_REDIS_REST_TOKEN"):
        errors.append("SHARED_UPSTASH_REDIS_REST_TOKEN not set")

    # Проверка value threshold
    try:
        vt = float(config.get("value", {}).get("threshold", 0.03))
        if vt <= 0 or vt > 1:
            errors.append(f"value.threshold out of range: {vt}")
    except (TypeError, ValueError):
        errors.append("value.threshold is not a valid number")

    return errors


__all__ = [
    "MSK_TZ", "now_msk", "now_msk_short", "ns_key",
    "load_config", "reload_config", "register_reload_callback",
    "get_env", "is_feature_enabled",
    "should_run_cleanup", "get_config_errors",
    "VALUE_THRESHOLD", "__version__",
]
