#!/usr/bin/env python3
# gatekeeper_config.py
"""
Gatekeeper-AI Ecosystem: Модуль конфигурации GatekeeperConfig.
Version: 9.6.8-patched — Адаптивные макро-пороги (§28.2), фикс очистки (§1.7a).
[PATCH v9.6.8] walrus-артефакт в _read_yaml устранен; нормализация VALUE_THRESHOLD
(доля эджа <-> множитель EV); разделение ошибок чтения YAML; мертвый дефолт исправлен.
"""

import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("gatekeeper_config")
logger.addHandler(logging.NullHandler())

__version__ = "9.6.8-patched"
__all__ = [
    "load_config", "reload_config", "register_reload_callback",
    "is_feature_enabled", "should_run_cleanup", "get_env", "get_config_errors",
    "now_msk", "now_msk_short", "ns_key", "MSK_TZ",
    "get_value_threshold", "VALUE_THRESHOLD", "get_adaptive_thresholds",
    "get_upstream_map", "get_collector_config", "get_cleanup_config",
    "__version__",
]

# ── Константы и Таймзоны ──────────────────────────────────
MSK_TZ = timezone(timedelta(hours=3))
_CONFIG_FILE = os.environ.get("GATEKEEPER_CONFIG_FILE", "gatekeeper_config.yaml")

_DEFAULT_FEATURES = {
    "graceful_shutdown": True,
    "auto_migrate": True,
    "telegram_dashboard": True,
    "value_bets": True,
    "safe_mode": True,
}

_DEFAULT_UPSTREAM = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "bzzoiro": "opta",
    "propline": "pinnacle",
    "football_data": "bet365",
}

# Дефолт в долях эджа (edge fraction). Боевой порог для value_engine всегда множитель EV.
_DEFAULT_VALUE_THRESHOLD = 0.03
_DEFAULT_VALUE_THRESHOLD_MULT = 1.03

# ── Глобальное состояние ──────────────────────────────────
_config_cache = None
_config_errors = []
_reload_callbacks = []
VALUE_THRESHOLD = _DEFAULT_VALUE_THRESHOLD_MULT


def _normalize_ev_multiplier(raw_value: float) -> float:
    """
    [PATCH v9.6.8] Нормализация семантики порога.
    value_engine трактует VALUE_THRESHOLD как множитель EV (например 1.08).
    Конфигурационные конвенции оператора допускают и долю эджа (0.08).
    Значения <= 1.0 трактуются как доля эджа и конвертируются в множитель.
    Значения > 1.0 проходят как есть (множитель EV).
    """
    if raw_value <= 1.0:
        return 1.0 + raw_value
    return raw_value


def _read_yaml(path):
    """
    [PATCH v9.6.8] Честное открытие без walrus-артефакта ('Harvey_f_str').
    Разделение FileNotFoundError (ожидаемая ситуация — работа на дефолтах)
    и реальных ошибок парсинга (битый YAML фиксируется в _config_errors).
    """
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.warning(f"[CONFIG] Файл {path} не найден — работа на жестких дефолтах v9.6.8.")
        return {}
    except Exception as e:
        logger.error(f"[CONFIG] Ошибка парсинга YAML {path}: {e}")
        _config_errors.append(f"Ошибка парсинга YAML {path}: {e}")
        return {}


def load_config():
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    _config_cache = _read_yaml(_CONFIG_FILE)
    _apply_yaml_values()
    return _config_cache


def reload_config():
    global _config_cache, _config_errors
    _config_cache = _read_yaml(_CONFIG_FILE)
    _config_errors = []
    _apply_yaml_values()
    for cb in list(_reload_callbacks):
        try:
            cb()
        except Exception:
            pass
    logger.info(f"Конфигурация горячо перезагружена из {_CONFIG_FILE}")


def register_reload_callback(callback):
    if callable(callback) and callback not in _reload_callbacks:
        _reload_callbacks.append(callback)


def _apply_yaml_values():
    """
    [PATCH v9.6.8] Нормализация применяется ВСЕГДА (мертвый дефолт устранен).
    Ранее выход при пустом кэше оставлял дефолт 0.03 не нормализованным,
    и value_engine получал бы порог EV 0.03 -> каждый матч HOT.
    При невалидном значении — фикс в _config_errors и откат на дефолт.
    """
    global VALUE_THRESHOLD, _config_errors
    ve = (_config_cache or {}).get("value_engine", {})
    threshold = None
    if isinstance(ve, dict):
        threshold = ve.get("threshold", _config_cache.get("value_threshold"))
    if threshold is None:
        VALUE_THRESHOLD = _normalize_ev_multiplier(_DEFAULT_VALUE_THRESHOLD)
        return
    try:
        VALUE_THRESHOLD = _normalize_ev_multiplier(float(threshold))
    except (TypeError, ValueError):
        _config_errors.append(f"value_engine.threshold невалидный: {threshold}")
        VALUE_THRESHOLD = _DEFAULT_VALUE_THRESHOLD_MULT


def is_feature_enabled(name):
    cfg = load_config()
    features = cfg.get("features", {})
    if isinstance(features, dict) and name in features:
        return bool(features[name])
    env_val = os.environ.get(f"GATEKEEPER_{name.upper()}")
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes", "on")
    return _DEFAULT_FEATURES.get(name, False)


def should_run_cleanup(source):
    """
    ФИКС УЯЗВИМОСТИ §1.7a: Все живые модули и коллекторы обязаны
    запускать очистку памяти Upstash во избежание переполнения квот кэша.
    """
    if source in ("main", "sharpapi", "odds_api", "bzzoiro", "propline", "unknown"):
        return True
    cfg = load_config()
    return bool(cfg.get("cleanup", {}).get("enabled", True))


def get_env():
    return os.environ.get("GATEKEEPER_ENV", os.environ.get("ENVIRONMENT", "production"))


def get_config_errors():
    if not _config_errors:
        load_config()
    return list(_config_errors)


def now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def now_msk_short():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def ns_key(base, cid, domain=""):
    if not cid:
        return base
    return f"{domain}:{base}:{cid}" if domain else f"{base}:{cid}"


def get_value_threshold():
    load_config()
    return VALUE_THRESHOLD


def get_adaptive_thresholds(data_completeness: float) -> dict:
    """
    Блок 19, §28.2: Адаптивные пороги валуйности в долях эджа.
    ВНИМАНИЕ: эти доли — конвенция метрик полноты, не множитель EV.
    Потребитель обязан нормализовать через _normalize_ev_multiplier.
    """
    return {
        "HOT": 0.05 if data_completeness > 70 else 0.07,
        "WARM": 0.03 if data_completeness > 60 else 0.05,
    }


def get_upstream_map():
    cfg = load_config()
    upstream = cfg.get("upstream_map", cfg.get("collector_upstream", {}))
    return upstream if isinstance(upstream, dict) and upstream else dict(_DEFAULT_UPSTREAM)


def get_collector_config(name):
    cfg = load_config()
    collectors = cfg.get("collectors", {})
    return collectors.get(name, {}) if isinstance(collectors, dict) else {}


def get_cleanup_config():
    cfg = load_config()
    cleanup = cfg.get("cleanup", {})
    return cleanup if isinstance(cleanup, dict) else {}


# Инициализация при импорте
load_config()
