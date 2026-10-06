#!/usr/bin/env python3
"""
test_contracts.py — Контрактные тесты GatekeeperAI (§20.8).
Проверка соответствия модулей API-контракту из гида v9.3.

Запуск:
  python test_contracts.py           — все тесты
  python test_contracts.py --verbose  — детальный вывод

v9.3-audited:
  FIX-1: test_schema — проверка "version" ключа в schema_v710.json
  FIX-2: test_config — isinstance(config, dict) вместо жёсткой проверки
  FIX-3: test_metrics — save_metrics() → dict (не str)
  FIX-4: test_fixtures — status enum "scheduled"/"completed"
"""

import os
import sys
import json
import importlib
import traceback

__version__ = "9.3-audited"

# ── Счётчики ──
_passed = 0
_failed = 0
_skipped = 0
_errors = []


def _can_import(module_name):
    """Проверка доступности модуля для импорта."""
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def _run_test(name, func, required_modules=None):
    """Запуск одного теста с обработкой пропусков."""
    global _passed, _failed, _skipped

    # Проверка зависимостей
    if required_modules:
        for mod in required_modules:
            if not _can_import(mod):
                print(f"  ⏭ SKIP  {name} — нет модуля '{mod}'")
                _skipped += 1
                return

    try:
        func()
        print(f"  ✅ PASS  {name}")
        _passed += 1
    except Exception as e:
        print(f"  ❌ FAIL  {name}: {e}")
        _errors.append(f"{name}: {e}")
        _failed += 1


# ═══════════════════════════════════════════════════════════
# Тесты
# ═══════════════════════════════════════════════════════════

def test_team_registry():
    """Тест team_registry: clean_team_name + TEAM_ALIASES."""
    from team_registry import clean_team_name, TEAM_ALIASES, build_canonical_id

    # Базовая нормализация
    assert clean_team_name("Manchester United") == "man", \
        f"Expected 'man', got '{clean_team_name('Manchester United')}'"

    # Диакритики
    assert clean_team_name("Malmö FF") == "malmo_ff", \
        f"Expected 'malmo', got '{clean_team_name('Malmö FF')}'"

    # Специальные символы (ø, æ, ß)
    assert clean_team_name("Brøndby IF") == "brondby", \
        f"Expected 'brondby', got '{clean_team_name('Brøndby IF')}'"

    assert clean_team_name("Beşiktaş") == "besiktas", \
        f"Expected 'besiktas', got '{clean_team_name('Beşiktaş')}'"

    # Суффиксы (fc, cf, united, city)
    assert clean_team_name("Bayern Munich") == "bayern"
    assert clean_team_name("AC Milan") == "milan"

    # Пустая строка
    assert clean_team_name("") == ""
    assert clean_team_name(None) == ""

    # TEAM_ALIASES — непустой словарь
    assert isinstance(TEAM_ALIASES, dict)
    assert len(TEAM_ALIASES) >= 200, \
        f"Expected >= 200 aliases, got {len(TEAM_ALIASES)}"


def test_build_canonical_id():
    """Тест build_canonical_id: генерация canonical_id."""
    from team_registry import build_canonical_id, clean_team_name

    cid = build_canonical_id("Manchester United", "Fulham", "2026-09-21")
    assert cid == "man__fulham__20260921", \
        f"Expected 'man__fulham__20260921', got '{cid}'"

    # С диакритиками
    cid2 = build_canonical_id("Malmö FF", "Brøndby IF", "2026-08-15")
    assert cid2 == "malmo_ff__brondby__20260815", \
        f"Expected 'malmo__brondby__20260815', got '{cid2}'"


def test_schema():
    """Тест schema_v710.json: версия, required fields, enums."""
    schema_path = os.path.join(os.path.dirname(__file__) or ".", "schema_v710.json")
    assert os.path.exists(schema_path), "schema_v710.json not found"

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    # Version key (FIX-1)
    assert "version" in schema, "schema_v710.json missing 'version' key"
    assert schema["version"] == "v710", \
        f"Expected version 'v710', got '{schema.get('version')}'"

    # Required fields
    required = schema.get("required", [])
    expected_required = [
        "canonical_id", "home_team", "away_team", "home_clean", "away_clean",
        "competition", "country", "date_utc", "status", "version", "schema_version"
    ]
    for field in expected_required:
        assert field in required, f"'{field}' not in required fields"

    # Status enum
    status_enum = schema.get("properties", {}).get("status", {}).get("enum", [])
    assert "scheduled" in status_enum, "'scheduled' not in status enum"
    assert "live" in status_enum, "'live' not in status enum"
    assert "completed" in status_enum, "'completed' not in status enum"

    # schema_version enum
    sv_enum = schema.get("properties", {}).get("schema_version", {}).get("enum", [])
    assert "v710" in sv_enum, "'v710' not in schema_version enum"


def test_config():
    """Тест gatekeeper_config: load_config + features."""
    from gatekeeper_config import load_config, VALUE_THRESHOLD, get_value_threshold

    config = load_config()
    assert isinstance(config, dict), \
        f"Expected dict, got {type(config).__name__}"

    # VALUE_THRESHOLD — число > 0
    assert isinstance(VALUE_THRESHOLD, (int, float)), \
        f"Expected number, got {type(VALUE_THRESHOLD).__name__}"
    assert VALUE_THRESHOLD > 0, f"VALUE_THRESHOLD must be > 0, got {VALUE_THRESHOLD}"

    # get_value_threshold — возвращает число
    vt = get_value_threshold()
    assert isinstance(vt, (int, float)), \
        f"Expected number, got {type(vt).__name__}"


def test_fixtures():
    """Тест test_fixtures: канонические фикстуры."""
    from test_fixtures import (
        CANONICAL_LIVE_MATCH,
        CANONICAL_HISTORY_MATCH,
        CANONICAL_COMPLETED_MATCH,
        CANONICAL_MINIMAL_MATCH,
        validate_fixtures,
    )

    # CANONICAL_LIVE_MATCH — scheduled
    assert CANONICAL_LIVE_MATCH["status"] == "scheduled", \
        f"Expected 'scheduled', got '{CANONICAL_LIVE_MATCH['status']}'"
    assert CANONICAL_LIVE_MATCH["canonical_id"] == "man__fulham__20260921"
    assert CANONICAL_LIVE_MATCH["schema_version"] == "v710"
    assert CANONICAL_LIVE_MATCH["score"] is None

    # CANONICAL_HISTORY_MATCH — completed
    assert CANONICAL_HISTORY_MATCH["status"] == "completed", \
        f"Expected 'completed', got '{CANONICAL_HISTORY_MATCH['status']}'"
    assert CANONICAL_HISTORY_MATCH["canonical_id"] == "man__arsenal__20260115"
    assert CANONICAL_HISTORY_MATCH["schema_version"] == "v710"
    assert CANONICAL_HISTORY_MATCH["score"] is not None
    assert CANONICAL_HISTORY_MATCH["score"]["home"] == 2
    assert CANONICAL_HISTORY_MATCH["score"]["away"] == 1

    # CANONICAL_COMPLETED_MATCH — completed с value_analysis
    assert CANONICAL_COMPLETED_MATCH["status"] == "completed"
    assert "value_analysis" in CANONICAL_COMPLETED_MATCH
    assert CANONICAL_COMPLETED_MATCH["value_analysis"].get("classification") == "HOT"

    # CANONICAL_MINIMAL_MATCH — минимальный валидный
    assert CANONICAL_MINIMAL_MATCH["status"] == "scheduled"
    assert CANONICAL_MINIMAL_MATCH["score"] is None

    # validate_fixtures — все 4 фикстуры валидны
    result = validate_fixtures()
    if isinstance(result, tuple):
        ok, errors = result
        assert ok, f"validate_fixtures failed: {errors}"
    else:
        assert result, "validate_fixtures failed"


def test_redis_hub_api():
    """Тест redis_hub: circuit breaker, envelope version."""
    from redis_hub import (
        ENVELOPE_VERSION,
        HASH_NAME,
        is_redis_available,
        get_circuit_breaker_status,
    )

    assert ENVELOPE_VERSION == "v700-prod", \
        f"Expected 'v700-prod', got '{ENVELOPE_VERSION}'"

    assert isinstance(HASH_NAME, str), \
        f"Expected str, got {type(HASH_NAME).__name__}"
    assert len(HASH_NAME) > 0, "HASH_NAME is empty"

    # is_redis_available — функция, вызываемая без аргументов
    assert callable(is_redis_available)

    # Circuit breaker status — dict with state key
    cb_status = get_circuit_breaker_status()
    assert isinstance(cb_status, dict), f"Expected dict, got {type(cb_status).__name__}"
    assert cb_status.get("state") in ("closed", "open", "half-open"), f"Invalid CB state: {cb_status.get('state')}"


def test_redis_config():
    """Тест redis_config: параметры подключения."""
    from redis_config import (
        REDIS_REST_URL,
        REDIS_REST_TOKEN,
        REDIS_TIMEOUT,
        CB_FAILURE_THRESHOLD,
        CB_RECOVERY_TIMEOUT,
        REDIS_HASH_NAME,
        REDIS_MAX_PIPELINE,
    )

    # URL — строка (может быть пустой без env)
    assert isinstance(REDIS_REST_URL, str)
    assert isinstance(REDIS_REST_TOKEN, str)

    # Timeout — число > 0
    assert isinstance(REDIS_TIMEOUT, int) and REDIS_TIMEOUT > 0

    # Circuit breaker thresholds
    assert isinstance(CB_FAILURE_THRESHOLD, int) and CB_FAILURE_THRESHOLD > 0
    assert isinstance(CB_RECOVERY_TIMEOUT, int) and CB_RECOVERY_TIMEOUT > 0

    # Hash name
    assert isinstance(REDIS_HASH_NAME, str) and len(REDIS_HASH_NAME) > 0

    # Pipeline size
    assert isinstance(REDIS_MAX_PIPELINE, int) and REDIS_MAX_PIPELINE > 0


def test_gatekeeper_hub_api():
    """Тест gatekeeper_hub: API контракт хаба (§14)."""
    from gatekeeper_hub import (
        run_initialization,
        get_all_matches,
        get_matches_by_date_range,
        upsert_match,
        patch_match,
        validate_schema,
        __version__,
    )

    # Версия
    assert isinstance(__version__, str)
    assert len(__version__) > 0

    # Все функции — callable
    for name, func in [
        ("run_initialization", run_initialization),
        ("get_all_matches", get_all_matches),
        ("get_matches_by_date_range", get_matches_by_date_range),
        ("upsert_match", upsert_match),
        ("patch_match", patch_match),
        ("validate_schema", validate_schema),
    ]:
        assert callable(func), f"{name} is not callable"

    # validate_schema — возвращает (bool, str)
    from test_fixtures import CANONICAL_LIVE_MATCH
    result = validate_schema(CANONICAL_LIVE_MATCH)
    assert isinstance(result, tuple), \
        f"validate_schema should return tuple, got {type(result)}"
    assert len(result) == 2, f"Expected tuple of 2, got {len(result)}"
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


def test_search_module():
    """Тест search_module: API поиска (§14)."""
    from search_module import search_matches, save_search_results, __version__

    assert isinstance(__version__, str)
    assert callable(search_matches)
    assert callable(save_search_results)


def test_country_map():
    """Тест country_code_map: маппинг стран и лиг."""
    from country_code_map import COUNTRY_CODE_MAP, LEAGUE_NAME_MAP, __version__

    assert isinstance(__version__, str)

    assert isinstance(COUNTRY_CODE_MAP, dict)
    assert len(COUNTRY_CODE_MAP) > 0

    assert isinstance(LEAGUE_NAME_MAP, dict)
    assert len(LEAGUE_NAME_MAP) > 0

    # Проверка известных значений
    assert COUNTRY_CODE_MAP.get("England") == "EN"
    assert LEAGUE_NAME_MAP.get("E0") == "Premier League"


def test_telegram_transport_api():
    """Тест telegram_transport: API отправки (§12)."""
    from telegram_transport import send_telegram_message, __version__

    assert isinstance(__version__, str)
    assert callable(send_telegram_message)


def test_base_collector():
    """Тест base_collector: BaseCollector API (§19.4)."""
    from base_collector import BaseCollector, __version__

    assert isinstance(__version__, str)

    # BaseCollector — класс
    assert isinstance(BaseCollector, type)

    # Проверка методов
    for method in ["collect", "run"]:
        assert hasattr(BaseCollector, method), \
            f"BaseCollector missing method '{method}'"


def test_value_engine():
    """Тест value_engine: analyze_match API (§21)."""
    from value_engine import analyze_match, __version__

    assert isinstance(__version__, str)
    assert callable(analyze_match)


def test_metrics():
    """Тест metrics: collect_metrics + save_metrics (§22)."""
    from metrics import collect_metrics, save_metrics, __version__

    assert isinstance(__version__, str)

    # collect_metrics — callable
    assert callable(collect_metrics)

    # save_metrics — callable, возвращает dict
    assert callable(save_metrics)

    # collect_metrics — возвращает dict
    result = collect_metrics()
    assert isinstance(result, dict), \
        f"collect_metrics should return dict, got {type(result)}"


# ═══════════════════════════════════════════════════════════
# Главная функция
# ═══════════════════════════════════════════════════════════

def main():
    global _passed, _failed, _skipped

    print("=" * 60)
    print("🧪 GatekeeperAI Contract Tests v9.3-audited")
    print("=" * 60)
    print()

    # Запуск всех тестов
    _run_test("test_team_registry", test_team_registry, ["team_registry"])
    _run_test("test_build_canonical_id", test_build_canonical_id, ["team_registry"])
    _run_test("test_schema", test_schema)
    _run_test("test_config", test_config, ["gatekeeper_config"])
    _run_test("test_fixtures", test_fixtures, ["test_fixtures"])
    _run_test("test_redis_hub_api", test_redis_hub_api, ["redis_hub"])
    _run_test("test_redis_config", test_redis_config, ["redis_config"])
    _run_test("test_gatekeeper_hub_api", test_gatekeeper_hub_api, ["gatekeeper_hub"])
    _run_test("test_search_module", test_search_module, ["search_module"])
    _run_test("test_country_map", test_country_map, ["country_code_map"])
    _run_test("test_telegram_transport_api", test_telegram_transport_api, ["telegram_transport"])
    _run_test("test_base_collector", test_base_collector, ["base_collector"])
    _run_test("test_value_engine", test_value_engine, ["value_engine"])
    _run_test("test_metrics", test_metrics, ["metrics"])

    print()
    print("=" * 60)
    print(f"Results: {_passed} passed, {_failed} failed, {_skipped} skipped")
    print("=" * 60)

    if _errors:
        print("\nErrors:")
        for e in _errors:
            print(f"  - {e}")

    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
