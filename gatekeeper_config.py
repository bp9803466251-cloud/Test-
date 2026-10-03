"""
GatekeeperAI Configuration Loader (§24.3, §24.4, §24.7)
======================================================
Загружает и валидирует gatekeeper_config.yaml при старте каждого модуля.
При ошибках конфигурации — log + fallback на дефолты (не падение).

Использование:
    from gatekeeper_config import get_config, get_env, is_feature_enabled
    cfg = get_config()
    if is_feature_enabled("schema_validation"):
        ...
"""

import os
import json
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

# ── Константы ─────────────────────────────────────────────────────────
CONFIG_FILE = "gatekeeper_config.yaml"
ODDS_PRIORITY_FILE = "odds_priority.yaml"

# ── Схема валидации конфигурации (§24.3) ─────────────────────────────
_CONFIG_SCHEMA = {
    "required": ["schema_version"],
    "optional": [
        "odds_priority", "features", "namespace", "canary",
        "data_lineage", "shared_code", "pipeline_batch_size",
        "orchestration", "environments",
    ],
    "types": {
        "schema_version": str,
        "pipeline_batch_size": int,
        "features": dict,
        "namespace": dict,
        "canary": dict,
        "data_lineage": dict,
        "environments": dict,
        "orchestration": dict,
        "shared_code": dict,
    },
}

# ── Дефолтная конфигурация (fallback) ─────────────────────────────────
_DEFAULT_CONFIG = {
    "schema_version": "v710",
    "pipeline_batch_size": 10,
    "odds_priority": {
        "sharpapi": 1, "odds_api": 3, "propline": 6,
        "bzzoiro": 8, "football_data": 10,
    },
    "features": {
        "schema_validation": {"enabled": True, "level": "strict"},
        "audit_log": {"enabled": True},
        "provenance": {"enabled": True},
        "auto_migrate": {"enabled": False},
        "conflict_resolution": {"enabled": True},
        "idempotency_keys": {"enabled": True},
        "graceful_shutdown": {"enabled": True},
        "state_machine": {"enabled": True},
        "metrics": {"enabled": True},
    },
    "namespace": {"prefix": "", "domains": {}},
    "orchestration": {"cleanup_owner": "sharpapi", "sequence": [], "parallel_groups": []},
    "environments": {},
    "shared_code": {"distribution": "vendored", "pinned_version": "v8.9"},
    "canary": {"enabled": False},
    "data_lineage": {},
}

# ── Кеш конфигурации ──────────────────────────────────────────────────
_cached_config: dict | None = None
_cached_odds_priority: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивное слияние словарей: override перебивает base."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def validate_config(config: dict) -> list[str]:
    """
    Валидирует конфигурацию (§24.3).
    Возвращает список ошибок (пустой = OK).
    """
    errors = []
    for key in _CONFIG_SCHEMA["required"]:
        if key not in config:
            errors.append(f"Missing required key: '{key}'")
    for key, expected_type in _CONFIG_SCHEMA["types"].items():
        if key in config and not isinstance(config[key], expected_type):
            got = type(config[key]).__name__
            want = expected_type.__name__
            errors.append(f"'{key}' must be {want}, got {got}")
    known = set(_CONFIG_SCHEMA["required"] + _CONFIG_SCHEMA["optional"])
    unknown = set(config.keys()) - known
    if unknown:
        errors.append(f"Unknown keys: {', '.join(sorted(unknown))}")
    # Семантические проверки
    if "features" in config:
        for feat, cfg in config["features"].items():
            if isinstance(cfg, dict) and "enabled" not in cfg:
                errors.append(f"features.{feat}: missing 'enabled' key")
    if "orchestration" in config:
        orch = config["orchestration"]
        if "cleanup_owner" in orch:
            seq_names = [s.get("name") for s in orch.get("sequence", [])]
            if orch["cleanup_owner"] not in seq_names:
                errors.append(f"orchestration.cleanup_owner '{orch["cleanup_owner"]}' not in sequence")
    return errors


def load_config() -> dict:
    """
    Загружает и валидирует конфигурацию (§24.3).
    При ошибках — log + fallback на дефолты.
    """
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    if yaml is None:
        print("[gatekeeper_config] PyYAML not installed, using defaults")
        _cached_config = _DEFAULT_CONFIG.copy()
        return _cached_config

    filepath = os.environ.get("GK_CONFIG_FILE", CONFIG_FILE)
    if not os.path.exists(filepath):
        print(f"[gatekeeper_config] {filepath} not found, using defaults")
        _cached_config = _DEFAULT_CONFIG.copy()
        return _cached_config

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"[gatekeeper_config] YAML parse error: {e}")
        _cached_config = _DEFAULT_CONFIG.copy()
        return _cached_config

    errors = validate_config(raw)
    if errors:
        for e in errors:
            print(f"[gatekeeper_config] ERROR: {e}")
        _cached_config = _deep_merge(_DEFAULT_CONFIG, raw)
    else:
        _cached_config = raw

    return _cached_config


def get_config() -> dict:
    """Возвращает кешированную конфигурацию (или загружает при первом вызове)."""
    return load_config()


def reload_config() -> dict:
    """Принудительная перезагрузка (для тестов)."""
    global _cached_config, _cached_odds_priority
    _cached_config = None
    _cached_odds_priority = None
    return load_config()


# ── Feature Flags (§22.3) ────────────────────────────────────────────
def is_feature_enabled(feature_name: str) -> bool:
    """Проверяет, включён ли feature flag."""
    cfg = get_config()
    feat = cfg.get("features", {}).get(feature_name, {})
    return feat.get("enabled", False) if isinstance(feat, dict) else False


def get_feature_config(feature_name: str) -> dict:
    """Возвращает конфигурацию feature flag."""
    cfg = get_config()
    return cfg.get("features", {}).get(feature_name, {})


# ── Environment (§24.4) ───────────────────────────────────────────────
def get_env() -> str:
    """Текущее окружение из ENV var (§24.4)."""
    return os.environ.get("GK_ENV", "prod")


def get_env_config() -> dict:
    """Конфигурация для текущего окружения."""
    env = get_env()
    cfg = get_config()
    envs = cfg.get("environments", {})
    return envs.get(env, envs.get("prod", {}))


def get_redis_url_env() -> str:
    """Имя ENV-переменной с Redis URL для текущего окружения."""
    env_cfg = get_env_config()
    return env_cfg.get("redis_url_env", "SHARED_UPSTASH_REDIS_REST_URL")


def get_redis_token_env() -> str:
    """Имя ENV-переменной с Redis Token для текущего окружения."""
    env_cfg = get_env_config()
    return env_cfg.get("redis_token_env", "SHARED_UPSTASH_REDIS_REST_TOKEN")


def get_namespace_prefix() -> str:
    """
    Возвращает префикс namespace для текущего окружения (§24.4).
    Композиция с §23.7: env_prefix (внешний) + domain_prefix (внутренний).
    Ключ: match:{env_prefix}:{domain_prefix}:{cid}
    """
    env_cfg = get_env_config()
    return env_cfg.get("namespace_prefix", "")


# ── Orchestration (§24.7) ─────────────────────────────────────────────
def should_run_cleanup(collector: str) -> bool:
    """
    Только один коллектор (cleanup_owner) вызывает cleanup_expired() (§24.7).
    Заменяет правило §1.7a при реализации оркестрации.
    До реализации §24.7 действует §1.7a.
    """
    cfg = get_config()
    orch = cfg.get("orchestration", {})
    owner = orch.get("cleanup_owner", "sharpapi")
    return collector == owner


def get_orchestration_sequence() -> list[dict]:
    """Возвращает последовательность запуска коллекторов."""
    cfg = get_config()
    return cfg.get("orchestration", {}).get("sequence", [])


def get_parallel_groups() -> list[list[str]]:
    """Возвращает группы параллельного запуска."""
    cfg = get_config()
    return cfg.get("orchestration", {}).get("parallel_groups", [])


# ── Odds Priority (§19.2) ─────────────────────────────────────────────
def load_odds_priority() -> dict:
    """Загружает odds_priority.yaml (§19.2)."""
    global _cached_odds_priority
    if _cached_odds_priority is not None:
        return _cached_odds_priority
    if yaml is None:
        return get_config().get("odds_priority", {})
    filepath = ODDS_PRIORITY_FILE
    if not os.path.exists(filepath):
        _cached_odds_priority = get_config().get("odds_priority", {})
        return _cached_odds_priority
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            _cached_odds_priority = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        _cached_odds_priority = get_config().get("odds_priority", {})
    return _cached_odds_priority


def get_source_rank(source: str, market: str = "1x2") -> int:
    """
    Возвращает ранг источника для рынка (§19.2, §23.1).
    Меньший ранг = высший приоритет.
    """
    pri = load_odds_priority()
    market_priorities = pri.get(market, pri.get("1x2", {}))
    return market_priorities.get(source, 999)


def get_upstream(source: str) -> str:
    """Возвращает upstream для источника (fallback из §1.20)."""
    pri = load_odds_priority()
    upstream_map = pri.get("upstream_map", {})
    return upstream_map.get(source, "unknown")


# ── Namespace Composition (§23.7 + §24.4) ─────────────────────────────
def compose_namespace(domain: str = "") -> str:
    """
    Композиция двух namespace-префиксов (§23.7 + §24.4).
    Environment-префикс применяется первым (внешний).
    Domain-префикс — вторым (внутренний).
    Ключ: match:{env_prefix}:{domain_prefix}:{cid}
    """
    env_prefix = get_namespace_prefix()
    cfg = get_config()
    domains = cfg.get("namespace", {}).get("domains", {})
    domain_prefix = domains.get(domain, "") if domain else ""
    parts = [p for p in [env_prefix, domain_prefix] if p]
    return ":".join(parts) if parts else ""


def build_namespaced_key(base: str, cid: str, domain: str = "") -> str:
    """
    Строит namespaced-ключ для Redis.
    base: "match" | "history:match" | "search"
    """
    ns = compose_namespace(domain)
    if ns:
        return f"{base}:{ns}:{cid}"
    return f"{base}:{cid}"


# ── Data Lineage (§23.5) ──────────────────────────────────────────────
def get_lineage_for_source(source: str) -> dict:
    """Возвращает lineage-инфо для источника (§23.5)."""
    cfg = get_config()
    return cfg.get("data_lineage", {}).get(source, {})


def get_impact_report(source: str) -> list[str]:
    """Возвращает список модулей, зависящих от источника (§23.5)."""
    lineage = get_lineage_for_source(source)
    return lineage.get("consumed_by", [])


# ── Self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    cfg = get_config()
    print(f"Schema version: {cfg.get('schema_version')}")
    print(f"Pipeline batch size: {cfg.get('pipeline_batch_size')}")
    print(f"Env: {get_env()}")
    print(f"Cleanup owner: {cfg.get('orchestration', {}).get('cleanup_owner')}")
    print(f"Should sharpapi cleanup: {should_run_cleanup('sharpapi')}")
    print(f"Should odds_api cleanup: {should_run_cleanup('odds_api')}")
    print(f"Feature schema_validation: {is_feature_enabled('schema_validation')}")
    print(f"Feature auto_migrate: {is_feature_enabled('auto_migrate')}")
    pri = load_odds_priority()
    print(f"Odds priority 1x2: {pri.get('1x2', {})}")
    print(f"Source rank sharpapi (1x2): {get_source_rank('sharpapi', '1x2')}")
    print(f"Source rank bzzoiro (1x2): {get_source_rank('bzzoiro', '1x2')}")
    print(f"Upstream sharpapi: {get_upstream('sharpapi')}")
    print(f"Lineage sharpapi: {get_lineage_for_source('sharpapi')}")
    print(f"Impact report: {get_impact_report('sharpapi')}")
    ns = compose_namespace("football")
    print(f"Namespace (football): {ns!r}")
    key = build_namespaced_key("match", "manutd__spurs__20261003", "football")
    print(f"Namespaced key: {key!r}")

    # Валидация
    bad_cfg = {"schema_version": 123, "unknown_key": True}
    errors = validate_config(bad_cfg)
    print(f"\nValidation errors for bad config: {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    if not errors:
        print("  ❌ Expected errors but got none")
        sys.exit(1)
    print("\nAll checks passed ✅")
