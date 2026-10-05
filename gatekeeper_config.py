#!/usr/bin/env python3
"""
gatekeeper_config.py — Единая конфигурация GatekeeperAI v8.11-patched.
Загружает параметры из gatekeeper_config.yaml (§20.3).
Env-переменные остаются только для секретов (API-ключи, Redis URL).

Экспортирует:
  load_config, reload_config, register_reload_callback
  is_feature_enabled, should_run_cleanup, get_env, get_config_errors
  now_msk, now_msk_short, ns_key, MSK_TZ
  get_value_threshold, VALUE_THRESHOLD
  get_upstream_map, get_collector_config, get_cleanup_config
"""

import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("gatekeeper_config")
logger.addHandler(logging.NullHandler())

__version__ = "8.11-patched"
__all__ = [
    "load_config", "reload_config", "register_reload_callback",
    "is_feature_enabled", "should_run_cleanup", "get_env", "get_config_errors",
    "now_msk", "now_msk_short", "ns_key", "MSK_TZ",
    "get_value_threshold", "VALUE_THRESHOLD",
    "get_upstream_map", "get_collector_config", "get_cleanup_config",
    "__version__",
]

# ═══════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════

MSK_TZ = timezone(timedelta(hours=3))

# §20.3: путь к YAML — env override или auto-detect
_CONFIG_FILE = os.environ.get("GATEKEEPER_CONFIG_FILE", "")
if not _CONFIG_FILE:
    import os as _os
    for _candidate in ("gatekeeper_config.yaml", "config/gatekeeper_config.yaml", "/etc/gatekeeper/gatekeeper_config.yaml"):
        if _os.path.exists(_candidate):
            _CONFIG_FILE = _candidate
            break
    if not _CONFIG_FILE:
        _CONFIG_FILE = "gatekeeper_config.yaml"  # fallback

# Default feature flags (overridden by YAML if present)
_DEFAULT_FEATURES = {
    "graceful_shutdown": True,
    "auto_migrate": True,
    "telegram_dashboard": True,
    "value_bets": True,
    "safe_mode": True,
}

# Default upstream map (overridden by YAML upstream_map section)
_DEFAULT_UPSTREAM = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "bzzoiro": "opta",
    "propline": "pinnacle",
    "football_data": "bet365",
}

# Default value engine
_DEFAULT_VALUE_THRESHOLD = 0.03

# ═══════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════

_config_cache = None
_config_errors = []
_reload_callbacks = []
VALUE_THRESHOLD = _DEFAULT_VALUE_THRESHOLD


# ═══════════════════════════════════════════════════════════
# YAML loading (§20.3)
# ═══════════════════════════════════════════════════════════

def _read_yaml(path):
    """Читает YAML файл через yaml.safe_load. Возвращает dict или {}."""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML не установлен — используются defaults")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning(f"{path}: ожидается dict, получен {type(data).__name__}")
            return {}
        return data
    except FileNotFoundError:
        logger.debug(f"{path} не найден — используются defaults")
        return {}
    except Exception as e:
        logger.error(f"Ошибка чтения {path}: {e}")
        return {}


def load_config():
    """Ленивая загрузка конфигурации. Возвращает dict.
    При первом вызове читает YAML, при последующих — отдаёт кеш.
    Для принудительного перечитывания вызовите reload_config()."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    _config_cache = _read_yaml(_CONFIG_FILE)
    _apply_yaml_values()
    return _config_cache


def reload_config():
    """Hot-reload: перечитывает YAML, сбрасывает кеш, вызывает callbacks.
    Используется для обновления параметров без перезапуска."""
    global _config_cache, _config_errors, VALUE_THRESHOLD
    _config_cache = _read_yaml(_CONFIG_FILE)
    _config_errors = []
    _apply_yaml_values()
    # Fire callbacks (hub сбрасывает кеш odds priority и т.д.)
    for cb in list(_reload_callbacks):
        try:
            cb()
        except Exception as e:
            logger.error(f"reload callback error: {e}")
    logger.info(f"Config reloaded from {_CONFIG_FILE}")


def register_reload_callback(callback):
    """Регистрирует callback, вызываемый при reload_config().
    Hub использует это для сброса кеша odds_priority."""
    if callable(callback) and callback not in _reload_callbacks:
        _reload_callbacks.append(callback)


def _apply_yaml_values():
    """Применяет значения из YAML к глобальным переменным."""
    global VALUE_THRESHOLD, _config_errors
    if not _config_cache:
        return
    # VALUE_THRESHOLD (§20.3)
    ve = _config_cache.get("value_engine", {})
    if isinstance(ve, dict):
        threshold = ve.get("threshold")
        if threshold is not None:
            try:
                VALUE_THRESHOLD = float(threshold)
            except (ValueError, TypeError):
                _config_errors.append(
                    f"value_engine.threshold невалидный: {threshold}"
                )


# ═══════════════════════════════════════════════════════════
# Feature flags
# ═══════════════════════════════════════════════════════════

def is_feature_enabled(name):
    """Проверяет, включён ли feature flag.
    Приоритет: YAML features → env GATEKEEPER_{NAME} → defaults.
    FIX-7: принимает 1 аргумент (не 2)."""
    # 1. YAML
    cfg = load_config()
    features = cfg.get("features", {})
    if isinstance(features, dict) and name in features:
        return bool(features[name])
    # 2. Env
    env_val = os.environ.get(f"GATEKEEPER_{name.upper()}")
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes", "on")
    # 3. Defaults
    return _DEFAULT_FEATURES.get(name, False)


def should_run_cleanup(source):
    """Определяет, должен ли данный источник запустить cleanup.
    FIX-7: принимает 1 аргумент (не 2).
    Только 'main' запускает cleanup после pipeline (§24.7)."""
    cfg = load_config()
    cleanup_cfg = cfg.get("cleanup", {})
    if isinstance(cleanup_cfg, dict):
        owner = cleanup_cfg.get("owner", "main")
        return source == owner
    return source == "main"


# ═══════════════════════════════════════════════════════════
# Environment
# ═══════════════════════════════════════════════════════════

def get_env():
    """Возвращает окружение для логов."""
    return os.environ.get("GATEKEEPER_ENV", os.environ.get("ENVIRONMENT", "production"))


def get_config_errors():
    """Возвращает список ошибок конфигурации."""
    if not _config_errors:
        load_config()
    return list(_config_errors)


# ═══════════════════════════════════════════════════════════
# Time utilities
# ═══════════════════════════════════════════════════════════

def now_msk():
    """Текущее время в МСК в ISO формате."""
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def now_msk_short():
    """Текущее время в МСК в коротком формате."""
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════
# Namespaced keys
# ═══════════════════════════════════════════════════════════

def ns_key(base, cid, domain=""):
    """Создаёт namespaced Redis-ключ.
    Если задан domain, добавляет его как префикс: domain:base:cid
    Если cid пустой — возвращает только base."""
    if not cid:
        return base
    if domain:
        return f"{domain}:{base}:{cid}"
    return f"{base}:{cid}"


# ═══════════════════════════════════════════════════════════
# Value engine (§20.3)
# ═══════════════════════════════════════════════════════════

def get_value_threshold():
    """Возвращает текущий VALUE_THRESHOLD из YAML.
    Используется value engine для динамического обновления."""
    # Убеждаемся, что YAML загружен
    load_config()
    return VALUE_THRESHOLD


# ═══════════════════════════════════════════════════════════
# Upstream map (§1.20, §20.3)
# ═══════════════════════════════════════════════════════════

def get_upstream_map():
    """Возвращает upstream map из YAML или defaults."""
    cfg = load_config()
    upstream = cfg.get("collector_upstream", cfg.get("upstream_map", {}))
    if isinstance(upstream, dict) and upstream:
        return upstream
    return dict(_DEFAULT_UPSTREAM)


# ═══════════════════════════════════════════════════════════
# Per-collector config (§20.3)
# ═══════════════════════════════════════════════════════════

def get_collector_config(name):
    """Возвращает конфигурацию коллектора из YAML.
    Параметры: rate_delay, max_retries, timeout, days_ahead и т.д."""
    cfg = load_config()
    collectors = cfg.get("collectors", {})
    if isinstance(collectors, dict):
        return collectors.get(name, {})
    return {}


# ═══════════════════════════════════════════════════════════
# Cleanup config (§20.3)
# ═══════════════════════════════════════════════════════════

def get_cleanup_config():
    """Возвращает конфигурацию cleanup из YAML.
    Параметры: match_ttl_hours, index_lookback_days,
    index_lookahead_days, auto_migrate, owner."""
    cfg = load_config()
    cleanup = cfg.get("cleanup", {})
    if isinstance(cleanup, dict):
        return cleanup
    return {}


# ═══════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════

# При импорте модуля загружаем конфиг
load_config()
