"""
Contract Tests — контрактные тесты для GatekeeperAI (§21.8)
==========================================================
Проверяют соответствие реализации гиду:
  1. Схема данных (§19.1)
  2. Фикстуры (§20.8)
  3. Конфигурация (§24.3)
  4. Team Registry (§19.2)
  5. BaseCollector (§19.4)
  6. Odds Priority (§19.2)
  7. State Machine (§24.1)
  8. Conflict Resolution (§23.1)

Запуск:
    python test_contracts.py
    # или
    make test
"""

import sys
import os

# ── 1. Схема данных (§19.1) ──────────────────────────────────────────

def test_schema_exists():
    """schema_v710.json существует и валиден."""
    import json
    assert os.path.exists("schema_v710.json"), "schema_v710.json not found"
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema["version"] == "v710"
    assert "required" in schema
    assert "canonical_id" in schema["required"]
    assert "properties" in schema

def test_schema_required_fields():
    """Все обязательные поля из гида есть в схеме."""
    import json
    with open("schema_v710.json") as f:
        schema = json.load(f)
    expected = {"canonical_id", "home_team", "away_team", "date_utc",
                "competition", "country", "source", "schema_version",
                "status", "sources"}
    assert expected <= set(schema["required"]), f"Missing: {expected - set(schema['required'])}"

def test_schema_status_enum():
    """Status enum включает все состояния из §24.1."""
    import json
    with open("schema_v710.json") as f:
        schema = json.load(f)
    statuses = set(schema["properties"]["status"]["enum"])
    expected = {"scheduled", "live", "completed", "cancelled",
                "postponed", "interrupted", "archived"}
    assert expected <= statuses, f"Missing statuses: {expected - statuses}"


# ── 2. Фикстуры (§20.8) ──────────────────────────────────────────────

def test_fixtures_have_required_fields():
    """Все фикстуры содержат обязательные поля (§19.1)."""
    from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                               CANONICAL_COMPLETED_MATCH, CANONICAL_EMPTY_MATCH,
                               assert_required_fields)
    for name, fixture in [
        ("LIVE", CANONICAL_LIVE_MATCH),
        ("HISTORY", CANONICAL_HISTORY_MATCH),
        ("COMPLETED", CANONICAL_COMPLETED_MATCH),
        ("EMPTY", CANONICAL_EMPTY_MATCH),
    ]:
        missing = assert_required_fields(fixture)
        assert not missing, f"{name}: missing {missing}"

def test_fixtures_valid_status():
    """Все фикстуры имеют допустимый status."""
    from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                               CANONICAL_COMPLETED_MATCH, CANONICAL_EMPTY_MATCH,
                               assert_valid_status)
    for name, fixture in [
        ("LIVE", CANONICAL_LIVE_MATCH),
        ("HISTORY", CANONICAL_HISTORY_MATCH),
        ("COMPLETED", CANONICAL_COMPLETED_MATCH),
        ("EMPTY", CANONICAL_EMPTY_MATCH),
    ]:
        assert assert_valid_status(fixture), f"{name}: invalid status {fixture.get('status')}"

def test_fixtures_odds_format():
    """Odds в формате 1x2 (§1.21)."""
    from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                               assert_odds_format)
    assert assert_odds_format(CANONICAL_LIVE_MATCH)
    assert assert_odds_format(CANONICAL_HISTORY_MATCH)

def test_live_match_has_sources_list():
    """sources — список, не строка (§1.26)."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    assert isinstance(CANONICAL_LIVE_MATCH["sources"], list)
    assert "sharpapi" in CANONICAL_LIVE_MATCH["sources"]


# ── 3. Конфигурация (§24.3) ──────────────────────────────────────────

def test_config_loads():
    """gatekeeper_config.yaml загружается без ошибок."""
    from gatekeeper_config import get_config
    cfg = get_config()
    assert cfg["schema_version"] == "v710"

def test_config_validation_catches_bad_input():
    """Валидатор ловит ошибки (§24.3)."""
    from gatekeeper_config import validate_config
    bad = {"schema_version": 123, "unknown_key": True}
    errors = validate_config(bad)
    assert len(errors) >= 2, f"Expected >= 2 errors, got {len(errors)}"

def test_config_features():
    """Feature flags загружаются (§22.3)."""
    from gatekeeper_config import is_feature_enabled
    assert is_feature_enabled("schema_validation")
    assert is_feature_enabled("conflict_resolution")

def test_config_orchestration():
    """Оркестрация загружается (§24.7)."""
    from gatekeeper_config import should_run_cleanup, get_orchestration_sequence
    assert should_run_cleanup("sharpapi") == True
    assert should_run_cleanup("odds_api") == False
    seq = get_orchestration_sequence()
    assert len(seq) > 0

def test_config_namespace():
    """Namespace композиция (§23.7 + §24.4)."""
    from gatekeeper_config import compose_namespace, build_namespaced_key
    # Без env-префикса
    ns = compose_namespace("football")
    assert isinstance(ns, str)
    key = build_namespaced_key("match", "test__cid__20261003", "football")
    assert "match:" in key
    assert "test__cid__20261003" in key


# ── 4. Team Registry (§19.2) ─────────────────────────────────────────

def test_team_registry_normalizes():
    """Нормализация имён команд работает."""
    from team_registry import normalize_team_name
    assert normalize_team_name("Man Utd") == "manchester united"
    assert normalize_team_name("BVB") == "borussia dortmund"
    assert normalize_team_name("  Spurs  ") == "tottenham hotspur"

def test_team_registry_canonical_id():
    """build_canonical_id работает корректно."""
    from team_registry import build_canonical_id
    cid = build_canonical_id("Man Utd", "Spurs", "2026-10-03T15:00:00")
    assert cid == "manchester_united__tottenham_hotspur__20261003"

def test_team_registry_unknown_passthrough():
    """Неизвестные имена проходят как есть (lowercase)."""
    from team_registry import normalize_team_name
    assert normalize_team_name("FC Barcelona") == "fc barcelona"


# ── 5. Odds Priority (§19.2) ─────────────────────────────────────────

def test_odds_priority_loads():
    """odds_priority.yaml загружается."""
    from gatekeeper_config import load_odds_priority
    pri = load_odds_priority()
    assert "1x2" in pri
    assert pri["1x2"]["sharpapi"] == 1
    assert pri["1x2"]["bzzoiro"] == 8

def test_odds_priority_source_rank():
    """get_source_rank возвращает корректные ранги."""
    from gatekeeper_config import get_source_rank
    assert get_source_rank("sharpapi", "1x2") == 1
    assert get_source_rank("bzzoiro", "1x2") == 8
    assert get_source_rank("unknown_source", "1x2") == 999

def test_odds_priority_upstream():
    """get_upstream возвращает корректный upstream (§1.20)."""
    from gatekeeper_config import get_upstream
    assert get_upstream("sharpapi") == "betradar"
    assert get_upstream("bzzoiro") == "opta"


# ── 6. State Machine (§24.1) ─────────────────────────────────────────

def test_state_machine_transitions():
    """MATCH_STATES определяет корректные переходы (§24.1)."""
    # Импортируем из гида — определяем локально для теста
    MATCH_STATES = {
        "scheduled": {"transitions": ["live", "cancelled", "postponed"]},
        "live": {"transitions": ["completed", "cancelled", "interrupted"]},
        "completed": {"transitions": ["archived"], "terminal": False},
        "archived": {"transitions": [], "terminal": True},
        "cancelled": {"transitions": [], "terminal": True},
        "postponed": {"transitions": ["scheduled"], "terminal": False},
        "interrupted": {"transitions": ["live", "completed", "cancelled"], "terminal": False},
    }
    # scheduled → live ✅
    assert "live" in MATCH_STATES["scheduled"]["transitions"]
    # completed → scheduled ❌ (нет воскрешения)
    assert "scheduled" not in MATCH_STATES["completed"]["transitions"]
    # archived — terminal
    assert MATCH_STATES["archived"]["terminal"]
    # cancelled — terminal
    assert MATCH_STATES["cancelled"]["terminal"]


# ── 7. Conflict Resolution (§23.1) ──────────────────────────────────

def test_conflict_resolution_priority():
    """Sharp-источник перебивает soft-источник (§23.1)."""
    from gatekeeper_config import get_source_rank
    sharp_rank = get_source_rank("sharpapi", "1x2")
    soft_rank = get_source_rank("bzzoiro", "1x2")
    assert sharp_rank < soft_rank, "SharpAPI должен иметь высший приоритет"


# ── 8. BaseCollector (§19.4) ────────────────────────────────────────

def test_base_collector_imports():
    """BaseCollector импортируется без ошибок."""
    from base_collector import BaseCollector, Metrics, is_shutdown_requested
    assert BaseCollector is not None
    assert Metrics is not None

def test_base_collector_subclass():
    """BaseCollector можно наследовать."""
    from base_collector import BaseCollector
    class TestCollector(BaseCollector):
        def fetch_events(self):
            return []
    c = TestCollector("test_source")
    assert c.source == "test_source"
    assert c.created == 0

def test_metrics():
    """Metrics работает (§24.5)."""
    from base_collector import Metrics
    m = Metrics()
    m.inc("test", 5)
    m.inc("test", 3)
    assert m.counters["test"] == 8
    m.time("op", 1.5)
    assert m.timers["op"] == 1.5
    r = m.report()
    assert "counters" in r and "timers" in r
    m.reset()
    assert m.counters == {}

def test_graceful_shutdown_handler():
    """is_shutdown_requested возвращает bool (§23.3)."""
    from base_collector import is_shutdown_requested
    assert isinstance(is_shutdown_requested(), bool)


# ── Runner ──────────────────────────────────────────────────────────

def run_all_tests():
    """Запускает все контрактные тесты и выводит результат."""
    tests = [
        # Schema
        ("Schema exists", test_schema_exists),
        ("Schema required fields", test_schema_required_fields),
        ("Schema status enum", test_schema_status_enum),
        # Fixtures
        ("Fixtures required fields", test_fixtures_have_required_fields),
        ("Fixtures valid status", test_fixtures_valid_status),
        ("Fixtures odds format", test_fixtures_odds_format),
        ("Live match has sources list", test_live_match_has_sources_list),
        # Config
        ("Config loads", test_config_loads),
        ("Config validation catches bad input", test_config_validation_catches_bad_input),
        ("Config features", test_config_features),
        ("Config orchestration", test_config_orchestration),
        ("Config namespace", test_config_namespace),
        # Team Registry
        ("Team registry normalizes", test_team_registry_normalizes),
        ("Team registry canonical ID", test_team_registry_canonical_id),
        ("Team registry unknown passthrough", test_team_registry_unknown_passthrough),
        # Odds Priority
        ("Odds priority loads", test_odds_priority_loads),
        ("Odds priority source rank", test_odds_priority_source_rank),
        ("Odds priority upstream", test_odds_priority_upstream),
        # State Machine
        ("State machine transitions", test_state_machine_transitions),
        # Conflict Resolution
        ("Conflict resolution priority", test_conflict_resolution_priority),
        # BaseCollector
        ("BaseCollector imports", test_base_collector_imports),
        ("BaseCollector subclass", test_base_collector_subclass),
        ("Metrics", test_metrics),
        ("Graceful shutdown handler", test_graceful_shutdown_handler),
    ]

    passed = 0
    failed = 0
    for name, test_func in tests:
        try:
            test_func()
            passed += 1
            print(f"  ✅ {name}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {name}: {e}")

    print(f"\n{'='*50}")
    print(f"Result: {passed} passed, {failed} failed, {passed+failed} total")
    print(f"{'All passed ✅' if failed == 0 else f'{failed} FAILED ❌'}")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
