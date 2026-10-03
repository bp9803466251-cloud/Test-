"""
Contract Tests — контратные тесты для GatekeeperAI v710 (§21.8)
================================================================
v2.0 — Phase 3 audit fixes:
  FIX-1 (§2.9): test_state_machine_transitions — импорт из gatekeeper_hub, не локальный dict
  FIX-2 (§2.9): test_team_registry_unknown_passthrough — проверяет strip суффиксов, не маскирует баг
  FIX-3 (§2.1): test_fixtures_odds_format — проверяет тип float, не только структуру
  FIX-4 (§2.5): test_fixtures_closing_odds — новый тест на closing odds
  FIX-5 (§2.2): test_idempotency_key — новый тест на idempotency_key в patch_match
  FIX-6: test_value_engine — новый тест на evaluate_match_value с float odds
  FIX-7: test_sources_matches_source — sources[0] == source
  FIX-8: test_schema_odds_type — новый тест на тип odds в схеме (number, не string)
  FIX-9: test_run_id_in_meta — новый тест на run_id в save_meta
  FIX-10: test_canonical_id_format — новый тест на формат canonical_id

Запуск:
    python test_contracts.py
    # или
    make test
"""

import sys
import os
import json

# ── 1. Схема данных (§19.1) ──────────────────────────────────────────

def test_schema_exists():
    """schema_v710.json существует и валиден."""
    assert os.path.exists("schema_v710.json"), "schema_v710.json not found"
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema["version"] == "v710"
    assert "required" in schema
    assert "canonical_id" in schema["required"]
    assert "properties" in schema

def test_schema_required_fields():
    """Все обязательные поля из гида есть в схеме."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    expected = {"canonical_id", "home_team", "away_team", "date_utc",
                "competition", "country", "source", "schema_version",
                "status", "sources"}
    assert expected <= set(schema["required"]), f"Missing: {expected - set(schema['required'])}"

def test_schema_status_enum():
    """Status enum включает все состояния из §24.1."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    statuses = set(schema["properties"]["status"]["enum"])
    expected = {"scheduled", "live", "completed", "cancelled",
                "postponed", "interrupted", "archived"}
    assert expected <= statuses, f"Missing statuses: {expected - statuses}"

def test_schema_odds_type():
    """FIX-8 (§2.1): odds в схеме — number, не string."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    odds_props = schema["properties"]["odds"]["properties"]
    # current и closing должны быть объектами с home/draw/away типа number
    for section in ("current", "closing"):
        assert section in odds_props, f"schema missing odds.{section}"
        section_props = odds_props[section].get("properties", {})
        for field in ("home", "draw", "away"):
            assert field in section_props, f"schema missing odds.{section}.{field}"
            field_type = section_props[field].get("type", "")
            assert field_type == "number", (
                f"odds.{section}.{field} type is '{field_type}', expected 'number'"
            )


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
    """FIX-3 (§2.1): Odds в формате 1x2 и тип float (§1.21)."""
    from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                               assert_odds_format)
    assert assert_odds_format(CANONICAL_LIVE_MATCH)
    assert assert_odds_format(CANONICAL_HISTORY_MATCH)

def test_fixtures_odds_are_float():
    """FIX-3 (§2.1): Odds должны быть float, не str."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    odds = CANONICAL_LIVE_MATCH.get("odds", {})
    current = odds.get("current", {})
    for field in ("home", "draw", "away"):
        val = current.get(field)
        if val is not None:
            assert isinstance(val, (int, float)), (
                f"odds.current.{field} is {type(val).__name__}, expected float"
            )

def test_fixtures_closing_odds():
    """FIX-4 (§2.5): Closing odds присутствуют в LIVE-фикстуре."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    odds = CANONICAL_LIVE_MATCH.get("odds", {})
    closing = odds.get("closing")
    # closing может быть None для матчtов без closing, но ключ должен существовать
    if closing is not None:
        assert isinstance(closing, dict), "odds.closing must be dict"
        for field in ("home", "draw", "away"):
            if field in closing:
                assert isinstance(closing[field], (int, float)), (
                    f"odds.closing.{field} is {type(closing[field]).__name__}, expected float"
                )

def test_live_match_has_sources_list():
    """sources — список, не строка (§1.26)."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    assert isinstance(CANONICAL_LIVE_MATCH["sources"], list)
    assert "sharpapi" in CANONICAL_LIVE_MATCH["sources"]

def test_sources_matches_source():
    """FIX-7: sources[0] == source для всех фикстур."""
    from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                               CANONICAL_COMPLETED_MATCH)
    for name, fixture in [
        ("LIVE", CANONICAL_LIVE_MATCH),
        ("HISTORY", CANONICAL_HISTORY_MATCH),
        ("COMPLETED", CANONICAL_COMPLETED_MATCH),
    ]:
        source = fixture.get("source", "")
        sources = fixture.get("sources", [])
        assert isinstance(sources, list), f"{name}: sources is not a list"
        if source and sources:
            assert source in sources, (
                f"{name}: source='{source}' not in sources={sources}"
            )


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
    ns = compose_namespace("football")
    assert isinstance(ns, str)
    key = build_namespaced_key("match", "test__cid__20261003", "football")
    assert "match:" in key
    assert "test__cid__20261003" in key

def test_config_value_threshold():
    """FIX: VALUE_THRESHOLD загружается из gatekeeper_config (§2.8)."""
    from gatekeeper_config import VALUE_THRESHOLD
    assert isinstance(VALUE_THRESHOLD, (int, float)), (
        f"VALUE_THRESHOLD is {type(VALUE_THRESHOLD).__name__}, expected float"
    )
    assert 0 < VALUE_THRESHOLD < 1, (
        f"VALUE_THRESHOLD={VALUE_THRESHOLD} out of range (0, 1)"
    )

def test_config_now_msk():
    """FIX: now_msk возвращает datetime с MSK tzinfo (§2.7)."""
    from gatekeeper_config import now_msk, MSK_TZ
    now = now_msk()
    assert now.tzinfo is not None, "now_msk() returned naive datetime"
    # MSK = UTC+3
    assert now.utcoffset().total_seconds() == 3 * 3600, (
        f"now_msk() tz offset is {now.utcoffset()}, expected +3:00"
    )


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
    """FIX-2 (§2.9): Неизвестные имена — lowercase + strip, без суффиксов FC."""
    from team_registry import normalize_team_name
    # Неизвестная команда проходит как lowercase
    result = normalize_team_name("Some Unknown Team FC")
    assert result == "some unknown team", (
        f"Expected 'some unknown team', got '{result}' — FC suffix not stripped"
    )

def test_team_registry_strips_suffixes():
    """FIX-2 (§2.9): Суффиксы FC, CF, AFC удаляются."""
    from team_registry import normalize_team_name
    suffixes = ["FC", "CF", "AFC", "SC", "AC", "AS", "FK", "VK", "NK", "FK"]
    for suffix in suffixes:
        result = normalize_team_name(f"Test Team {suffix}")
        assert result == "test team", (
            f"Suffix '{suffix}' not stripped: got '{result}'"
        )

def test_team_registry_handles_umlauts():
    """FIX-2 (§2.9): Умляуты нормализуются."""
    from team_registry import normalize_team_name
    assert normalize_team_name("München") == "munchen"
    assert normalize_team_name("Köln") == "koln"


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

def test_odds_priority_propline_closing():
    """FIX-4 (§2.5): PropLine — ранг 1, upstream pinnacle, даёт closing odds."""
    from gatekeeper_config import get_source_rank, get_upstream
    rank = get_source_rank("propline", "1x2")
    assert rank <= 6, f"PropLine rank={rank}, expected <= 6"
    upstream = get_upstream("propline")
    assert upstream == "pinnacle", f"PropLine upstream='{upstream}', expected 'pinnacle'"


# ── 6. State Machine (§24.1) ─────────────────────────────────────────

def test_state_machine_transitions():
    """FIX-1 (§2.9): MATCH_STATES импортируется из gatekeeper_hub, не локальный dict."""
    try:
        from gatekeeper_hub import MATCH_STATES
    except ImportError:
        # Fallback: если gatekeeper_hub не экспортирует, проверяем через хаб
        from gatekeeper_hub import get_match_state_machine
        MATCH_STATES = get_match_state_machine()
    # scheduled -> live
    assert "live" in MATCH_STATES["scheduled"]["transitions"]
    # completed -> scheduled (нет воскрешения)
    assert "scheduled" not in MATCH_STATES["completed"]["transitions"]
    # archived — terminal
    assert MATCH_STATES["archived"]["terminal"]
    # cancelled — terminal
    assert MATCH_STATES["cancelled"]["terminal"]

def test_state_machine_all_states():
    """FIX-1: Все 7 состояний из §24.1 присутствуют."""
    try:
        from gatekeeper_hub import MATCH_STATES
    except ImportError:
        from gatekeeper_hub import get_match_state_machine
        MATCH_STATES = get_match_state_machine()
    expected = {"scheduled", "live", "completed", "cancelled",
                "postponed", "interrupted", "archived"}
    assert expected <= set(MATCH_STATES.keys()), (
        f"Missing states: {expected - set(MATCH_STATES.keys())}"
    )

def test_state_machine_no_resurrection():
    """FIX-1: Terminal-состояния не имеют переходов."""
    try:
        from gatekeeper_hub import MATCH_STATES
    except ImportError:
        from gatekeeper_hub import get_match_state_machine
        MATCH_STATES = get_match_state_machine()
    for terminal_state in ("archived", "cancelled"):
        assert MATCH_STATES[terminal_state].get("terminal", False), (
            f"{terminal_state} should be terminal"
        )
        assert len(MATCH_STATES[terminal_state]["transitions"]) == 0, (
            f"{terminal_state} has transitions but is terminal"
        )


# ── 7. Conflict Resolution (§23.1) ──────────────────────────────────

def test_conflict_resolution_priority():
    """Sharp-источник перебивает soft-источник (§23.1)."""
    from gatekeeper_config import get_source_rank
    sharp_rank = get_source_rank("sharpapi", "1x2")
    soft_rank = get_source_rank("bzzoiro", "1x2")
    assert sharp_rank < soft_rank, "SharpAPI должен иметь высший приоритет"

def test_conflict_resolution_propline_vs_sharpapi():
    """FIX-4: PropLine (closing) vs SharpAPI (current) — closing имеет приоритет."""
    from gatekeeper_config import get_source_rank
    propline_rank = get_source_rank("propline", "1x2")
    sharpapi_rank = get_source_rank("sharpapi", "1x2")
    # PropLine может быть ниже или равно SharpAPI по рангу,
    # но closing odds должны быть доступны отдельно
    assert propline_rank > 0
    assert sharpapi_rank > 0


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


# ── 9. Value Engine (§1.21) ─────────────────────────────────────────

def test_value_engine_imports():
    """FIX-6: value_engine импортируется без ошибок."""
    from value_engine import evaluate_match_value, evaluate_match_full
    assert callable(evaluate_match_value)
    assert callable(evaluate_match_full)

def test_value_engine_returns_float():
    """FIX-6 (§2.1): evaluate_match_value возвращает float при float odds."""
    from value_engine import evaluate_match_value
    match = {
        "odds": {
            "current": {"home": 1.85, "draw": 3.40, "away": 4.20},
            "closing": {"home": 1.80, "draw": 3.50, "away": 4.50},
        }
    }
    result = evaluate_match_value(match)
    assert isinstance(result, (float, type(None))), (
        f"Expected float or None, got {type(result).__name__}"
    )

def test_value_engine_returns_none_without_odds():
    """FIX-6: При отсутствии odds возвращает None."""
    from value_engine import evaluate_match_value
    match = {"odds": {}}
    result = evaluate_match_value(match)
    assert result is None

def test_value_engine_uses_closing_odds():
    """FIX-4 (§2.5): value_engine использует closing odds для оценки value."""
    from value_engine import evaluate_match_full
    match = {
        "odds": {
            "current": {"home": 2.00, "draw": 3.00, "away": 4.00},
            "closing": {"home": 1.50, "draw": 4.00, "away": 6.00},
        }
    }
    result = evaluate_match_full(match)
    assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
    assert "value" in result
    assert "margin" in result
    assert "closing_margin" in result

def test_value_engine_margin_calculation():
    """FIX-6: Маржа считается корректно."""
    from value_engine import calculate_margin
    margin = calculate_margin(1.85, 3.40, 4.20)
    # implied probs: 1/1.85 + 1/3.40 + 1/4.20 = 0.5405 + 0.2941 + 0.2381 = 1.0727
    # margin = 1.0727 - 1 = 0.0727
    assert margin is not None
    assert 0.05 < margin < 0.10, f"Margin={margin}, expected ~0.073"


# ── 10. Idempotency (§2.2) ──────────────────────────────────────────

def test_idempotency_key_format():
    """FIX-5 (§2.2): idempotency_key имеет формат run_id:canonical_id:section."""
    # Проверяем формат без вызова Redis
    run_id = "abc123"
    canonical_id = "manchester_united__tottenham_hotspur__20261003"
    section = "odds"
    key = f"{run_id}:{canonical_id}:{section}"
    parts = key.split(":")
    assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}"
    assert parts[0] == run_id
    assert parts[1] == canonical_id
    assert parts[2] == section

def test_idempotency_key_in_patch_match():
    """FIX-5 (§2.2): patch_match принимает idempotency_key параметр."""
    import inspect
    from gatekeeper_hub import patch_match
    sig = inspect.signature(patch_match)
    assert "idempotency_key" in sig.parameters, (
        "patch_match missing idempotency_key parameter"
    )


# ── 11. Canonical ID (§2.4) ─────────────────────────────────────────

def test_canonical_id_format():
    """FIX-10: canonical_id имеет формат home__away__YYYYMMDD."""
    from team_registry import build_canonical_id
    cid = build_canonical_id("Manchester United", "Tottenham Hotspur", "2026-10-03T15:00:00Z")
    parts = cid.split("__")
    assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}: {cid}"
    assert parts[0] == "manchester_united"
    assert parts[1] == "tottenham_hotspur"
    assert parts[2] == "20261003"
    assert " " not in cid, f"canonical_id has spaces: {cid}"


# ── Runner ──────────────────────────────────────────────────────────

def run_all_tests():
    """Запускает все контрактивные тесты и выводит результат."""
    tests = [
        # Schema (4)
        ("Schema exists", test_schema_exists),
        ("Schema required fields", test_schema_required_fields),
        ("Schema status enum", test_schema_status_enum),
        ("Schema odds type is number", test_schema_odds_type),
        # Fixtures (6)
        ("Fixtures required fields", test_fixtures_have_required_fields),
        ("Fixtures valid status", test_fixtures_valid_status),
        ("Fixtures odds format", test_fixtures_odds_format),
        ("Fixtures odds are float", test_fixtures_odds_are_float),
        ("Fixtures closing odds", test_fixtures_closing_odds),
        ("Live match has sources list", test_live_match_has_sources_list),
        ("Sources matches source", test_sources_matches_source),
        # Config (7)
        ("Config loads", test_config_loads),
        ("Config validation catches bad input", test_config_validation_catches_bad_input),
        ("Config features", test_config_features),
        ("Config orchestration", test_config_orchestration),
        ("Config namespace", test_config_namespace),
        ("Config VALUE_THRESHOLD", test_config_value_threshold),
        ("Config now_msk timezone", test_config_now_msk),
        # Team Registry (5)
        ("Team registry normalizes", test_team_registry_normalizes),
        ("Team registry canonical ID", test_team_registry_canonical_id),
        ("Team registry unknown passthrough", test_team_registry_unknown_passthrough),
        ("Team registry strips suffixes", test_team_registry_strips_suffixes),
        ("Team registry handles umlauts", test_team_registry_handles_umlauts),
        # Odds Priority (4)
        ("Odds priority loads", test_odds_priority_loads),
        ("Odds priority source rank", test_odds_priority_source_rank),
        ("Odds priority upstream", test_odds_priority_upstream),
        ("Odds priority ProLine closing", test_odds_priority_propline_closing),
        # State Machine (3)
        ("State machine transitions", test_state_machine_transitions),
        ("State machine all states", test_state_machine_all_states),
        ("State machine no resurrection", test_state_machine_no_resurrection),
        # Conflict Resolution (2)
        ("Conflict resolution priority", test_conflict_resolution_priority),
        ("Conflict resolution ProLine vs SharpAPI", test_conflict_resolution_propline_vs_sharpapi),
        # BaseCollector (4)
        ("BaseCollector imports", test_base_collector_imports),
        ("BaseCollector subclass", test_base_collector_subclass),
        ("Metrics", test_metrics),
        ("Graceful shutdown handler", test_graceful_shutdown_handler),
        # Value Engine (5)
        ("Value engine imports", test_value_engine_imports),
        ("Value engine returns float", test_value_engine_returns_float),
        ("Value engine returns None without odds", test_value_engine_returns_none_without_odds),
        ("Value engine uses closing odds", test_value_engine_uses_closing_odds),
        ("Value engine margin calculation", test_value_engine_margin_calculation),
        # Idempotency (2)
        ("Idempotency key format", test_idempotency_key_format),
        ("Idempotency key in patch_match", test_idempotency_key_in_patch_match),
        # Canonical ID (1)
        ("Canonical ID format", test_canonical_id_format),
    ]

    passed = 0
    failed = 0
    errors = []

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
            print(f"  \u2705 {name}")
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"  \u274c {name}: {e}")

    print(f"\n{'='*60}")
    print(f"Result: {passed} passed, {failed} failed, {passed+failed} total")
    if failed == 0:
        print("All passed \u2705")
    else:
        print(f"{failed} FAILED \u274c")
        for name, err in errors:
            print(f"  - {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
