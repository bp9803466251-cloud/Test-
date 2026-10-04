"""
test_contracts.py — Тесты контрактов API GatekeeperAI.
Проверяет соответствие модулей архитектурному гиду.
"""

import json
import sys

def test_team_registry():
    """Проверка team_registry."""
    from team_registry import clean_team_name, TEAM_ALIASES
    
    assert clean_team_name("Manchester United") == "man", "Manchester United → man"
    assert clean_team_name("Man Utd") == "man", "Man Utd → man"
    assert clean_team_name("Man United") == "man", "Man United → man"
    assert clean_team_name("Nott'm Forest") == "nottingham_forest", "Nott'm Forest"
    assert clean_team_name("Spurs") == "tottenham", "Spurs → tottenham"
    assert clean_team_name("CF Pachuca") == "pachuca", "CF Pachuca"
    assert len(TEAM_ALIASES) > 100, f"TEAM_ALIASES has {len(TEAM_ALIASES)} entries"
    print("[TEST] team_registry: PASS")


def test_schema():
    """Проверка загрузки схемы."""
    with open("schema_v710.json", "r", encoding="utf-8") as f:
        schema = json.load(f)
    assert schema["version"] == "v710", "Schema version must be v710"
    assert "canonical_id" in schema["required"], "canonical_id required"
    assert "home_clean" in schema["required"], "home_clean required"
    assert "away_clean" in schema["required"], "away_clean required"
    print("[TEST] schema_v710: PASS")


def test_config():
    """Проверка загрузки конфигурации."""
    from gatekeeper_config import load_config, is_feature_enabled, get_env
    config = load_config()
    assert "redis" in config, "redis config exists"
    assert "collectors" in config, "collectors config exists"
    assert "cleanup" in config, "cleanup config exists"
    assert isinstance(is_feature_enabled("graceful_shutdown"), bool)
    print("[TEST] gatekeeper_config: PASS")


def test_fixtures():
    """Проверка эталонных объектов."""
    from test_fixtures import CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH
    assert CANONICAL_LIVE_MATCH["schema_version"] == "v710"
    assert CANONICAL_LIVE_MATCH["canonical_id"] == "man__fulham__20260921"
    assert CANONICAL_HISTORY_MATCH["status"] == "completed"
    assert CANONICAL_HISTORY_MATCH["score"] == {"home": 2, "away": 1}
    print("[TEST] test_fixtures: PASS")


def test_redis_hub_api():
    """Проверка API redis_hub (без подключения к Redis)."""
    from redis_hub import (
        ENVELOPE_VERSION, HASH_NAME,
        get_circuit_breaker_status, reset_circuit_breaker,
    )
    assert ENVELOPE_VERSION == "v700-prod"
    assert HASH_NAME == "GatekeeperAI"
    status = get_circuit_breaker_status()
    assert "state" in status
    assert "failures" in status
    print("[TEST] redis_hub API: PASS")


def test_base_collector():
    """Проверка базового класса коллектора."""
    from base_collector import BaseCollector
    assert hasattr(BaseCollector, "run"), "BaseCollector.run exists"
    assert hasattr(BaseCollector, "fetch_events"), "BaseCollector.fetch_events exists"
    assert hasattr(BaseCollector, "process_event"), "BaseCollector.process_event exists"
    assert hasattr(BaseCollector, "enrich_events"), "BaseCollector.enrich_events exists"
    print("[TEST] base_collector: PASS")


def test_search_module():
    """Проверка search_module."""
    from search_module import clean_team_name
    assert clean_team_name("Barcelona") == "barcelona"
    assert clean_team_name("Real Madrid") == "real_madrid"
    print("[TEST] search_module: PASS")


def test_country_map():
    """Проверка country_code_map."""
    from country_code_map import get_country_name, get_league_name
    assert get_country_name("EN") == "England"
    assert get_country_name("BR") == "Brazil"
    assert get_league_name("premier_league") == "Premier League"
    assert get_league_name("la_liga") == "La Liga"
    print("[TEST] country_code_map: PASS")


def run_all_tests():
    tests = [
        test_team_registry,
        test_schema,
        test_config,
        test_fixtures,
        test_redis_hub_api,
        test_base_collector,
        test_search_module,
        test_country_map,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[TEST] {test.__name__}: FAIL — {e}")
            failed += 1
    print(f"\n{'='*40}")
    print(f"Tests: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
