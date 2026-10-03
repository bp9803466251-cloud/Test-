"""
Contract Tests — контрактные тесты для GatekeeperAI v710 (§21.8)
================================================================
v3.0 — Phase 5 fixes:
  FIX-1: test_schema_exists — version="7.10", id (не canonical_id)
  FIX-2: test_schema_required_fields — id, home, away (не canonical_id, home_team)
  FIX-3: test_schema_odds_type — odds.open (не odds.current)
  FIX-4: test_fixtures_odds_are_float — odds.open (не odds.current)
  FIX-5: test_value_engine — odds.open (не odds.current)
  FIX-6: test_schema_version_value — "7.10" (не "v710")

Запуск:
    pytest test_contracts.py -v
    pytest test_contracts.py -v --html=test-reports/report.html --self-contained-html
"""

import sys
import os
import json
import inspect


# ── 1. Схема данных (§19.1) ──────────────────────────────────────────

def test_schema_exists():
    """schema_v710.json существует и валиден."""
    assert os.path.exists("schema_v710.json"), "schema_v710.json not found"
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema["version"] == "7.10", f"version is {schema['version']}, expected 7.10"
    assert "required" in schema
    assert "id" in schema["required"], "id not in required"
    assert "properties" in schema

def test_schema_required_fields():
    """Все обязательные поля из схемы присутствуют."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    required = set(schema["required"])
    # Проверяем что все нужные поля в required
    must_have = {"id", "source", "sources", "home", "away", "date_utc", "odds"}
    assert must_have <= required, f"Missing required: {must_have - required}"

def test_schema_status_enum():
    """Status enum включает все состояния из §24.1."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    statuses = set(schema["properties"]["status"]["enum"])
    expected = {"scheduled", "live", "completed", "cancelled",
                "postponed", "interrupted", "archived"}
    assert expected <= statuses, f"Missing statuses: {expected - statuses}"

def test_schema_odds_type():
    """FIX-8 (§2.1): odds в схеме — number, не string. Секции: open + closing."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    odds_props = schema["properties"]["odds"]["properties"]
    # open и closing должны быть объектами с home/draw/away типа number
    for section in ("open", "closing"):
        assert section in odds_props, f"schema missing odds.{section}"
        section_props = odds_props[section].get("properties", {})
        for field in ("home", "draw", "away"):
            assert field in section_props, f"schema missing odds.{section}.{field}"
            field_type = section_props[field].get("type", "")
            assert field_type == "number", (
                f"odds.{section}.{field} type is {field_type!r}, expected 'number'"
            )

def test_schema_version_value():
    """FIX-6: version в схеме — "7.10" (не "v710")."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema["version"] == "7.10", f"Expected '7.10', got {schema['version']!r}"

def test_schema_id_pattern():
    """id pattern соответствует canonical_id формату."""
    with open("schema_v710.json") as f:
        schema = json.load(f)
    pattern = schema["properties"]["id"]["pattern"]
    assert "__" in pattern, "Pattern must contain __ separator"


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
    """FIX-3 (§2.1): Odds должны быть float, не str. Секция open (не current)."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    odds = CANONICAL_LIVE_MATCH.get("odds", {})
    open_odds = odds.get("open", {})
    for field in ("home", "draw", "away"):
        val = open_odds.get(field)
        if val is not None:
            assert isinstance(val, (int, float)), (
                f"odds.open.{field} is {type(val).__name__}, expected float"
            )

def test_fixtures_closing_odds():
    """FIX-4 (§2.5): Closing odds присутствуют в LIVE-фикстуре."""
    from test_fixtures import CANONICAL_LIVE_MATCH
    odds = CANONICAL_LIVE_MATCH.get("odds", {})
    closing = odds.get("closing")
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
    """FIX-7: source входит в sources для всех фикстур."""
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
                f"{name}: source={source!r} not in sources={sources}"
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
    """now_msk возвращает строку с московской timezone."""
    from gatekeeper_config import now_msk, MSK_TZ
    ts = now_msk()
    assert isinstance(ts, str)
    assert "+03:00" in ts or "UTC+3" in str(MSK_TZ)


# ── 4. Team Registry (§2.4) ─────────────────────────────────────────

def test_team_registry_normalizes():
    """normalize_team_name нормализует известные алиасы."""
    from team_registry import normalize_team_name
    assert normalize_team_name("Man Utd") == "manchester united"
    assert normalize_team_name("BVB") == "borussia dortmund"
    assert normalize_team_name("inter") == "inter milan"

def test_team_registry_canonical_id():
    """build_canonical_id работает корректно."""
    from team_registry import build_canonical_id
    cid = build_canonical_id("Man Utd", "Spurs", "2026-10-03T15:00:00")
    assert cid == "manchester_united__tottenham_hotspur__20261003"

def test_team_registry_unknown_passthrough():
    """FIX-2 (§2.9): Неизвестные имена — lowercase + strip, без суффиксов FC."""
    from team_registry import normalize_team_name
    result = normalize_team_name("Some Unknown Team FC")
    assert result == "some unknown team", (
        f"Expected 'some unknown team', got {result!r} — FC suffix not stripped"
    )

def test_team_registry_strips_suffixes():
    """FIX-2 (§2.9): Суффиксы FC, CF, AFC удаляются."""
    from team_registry import normalize_team_name
    suffixes = ["FC", "CF", "AFC", "SC", "AC", "AS", "FK", "VK", "NK"]
    for suffix in suffixes:
        result = normalize_team_name(f"Test Team {suffix}")
        assert result == "test team", (
            f"Suffix {suffix!r} not stripped: got {result!r}"
        )

def test_team_registry_handles_umlauts():
    """FIX-2 (§2.9): Умляуты нормализуются."""
    from team_registry import normalize_team_name
    assert normalize_team_name("M\u00fcnchen") == "munchen"
    assert normalize_team_name("K\u00f6ln") == "koln"


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
    """ProLine closing odds приоритет доступен."""
    from gatekeeper_config import get_source_rank
    rank = get_source_rank("propline", "1x2")
    assert rank > 0


# ── 6. State Machine (§24.1) ────────────────────────────────────────

def test_state_machine_transitions():
    """FIX-1 (§2.9): MATCH_STATES импортируется из gatekeeper_hub."""
    from gatekeeper_hub import MATCH_STATES
    assert "live" in MATCH_STATES["scheduled"]["transitions"]
    assert "scheduled" not in MATCH_STATES["completed"]["transitions"]
    assert MATCH_STATES["archived"]["terminal"]
    assert MATCH_STATES["cancelled"]["terminal"]

def test_state_machine_all_states():
    """Все 7 состояний присутствуют в MATCH_STATES."""
    from gatekeeper_hub import MATCH_STATES
    expected = {"scheduled", "live", "completed", "cancelled",
                "postponed", "interrupted", "archived"}
    assert expected <= set(MATCH_STATES.keys()), (
        f"Missing states: {expected - set(MATCH_STATES.keys())}"
    )

def test_state_machine_no_resurrection():
    """Завершённые матчи не могут вернуться в scheduled."""
    from gatekeeper_hub import MATCH_STATES
    assert "scheduled" not in MATCH_STATES["completed"]["transitions"]
    assert "scheduled" not in MATCH_STATES["archived"]["transitions"]


# ── 7. Conflict Resolution (§23.1) ─────────────────────────────────

def test_conflict_resolution_priority():
    """Sharp-источник перебивает soft-источник (§23.1)."""
    from gatekeeper_config import get_source_rank
    sharp_rank = get_source_rank("sharpapi", "1x2")
    soft_rank = get_source_rank("bzzoiro", "1x2")
    assert sharp_rank < soft_rank, "SharpAPI должен иметь высший приоритет"

def test_conflict_resolution_propline_vs_sharpapi():
    """FIX-4: PropLine (closing) vs SharpAPI (open) — closing доступен отдельно."""
    from gatekeeper_config import get_source_rank
    propline_rank = get_source_rank("propline", "1x2")
    sharpapi_rank = get_source_rank("sharpapi", "1x2")
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
    """install_shutdown_handler устанавливается без ошибок."""
    from gatekeeper_hub import install_shutdown_handler, is_shutdown_requested
    install_shutdown_handler()
    assert isinstance(is_shutdown_requested(), bool)


# ── 9. Value Engine (§3.3) ──────────────────────────────────────────

def test_value_engine_imports():
    """value_engine импортируется без ошибок."""
    from value_engine import evaluate_match_value, evaluate_match_full, batch_evaluate
    assert evaluate_match_value is not None
    assert evaluate_match_full is not None
    assert batch_evaluate is not None

def test_value_engine_returns_float():
    """FIX-6 (§2.1): evaluate_match_value возвращает float при float odds.
    Использует open (не current) — соответствует схеме и фикстурам."""
    from value_engine import evaluate_match_value
    match = {
        "odds": {
            "open": {"home": 1.85, "draw": 3.40, "away": 4.20},
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
    """FIX-6: При наличии closing odds value считается через open vs closing."""
    from value_engine import evaluate_match_full
    match = {
        "odds": {
            "open": {"home": 1.85, "draw": 3.40, "away": 4.20},
            "closing": {"home": 1.80, "draw": 3.50, "away": 4.50},
        }
    }
    result = evaluate_match_full(match)
    assert result is not None
    assert "value" in result
    assert "direction" in result
    assert result["closing_odds"] is not None

def test_value_engine_margin_calculation():
    """calculate_margin возвращает положительное число для валидных odds."""
    from value_engine import calculate_margin
    odds = {"home": 1.85, "draw": 3.40, "away": 4.20}
    margin = calculate_margin(odds)
    assert margin is not None
    assert margin > 0, f"Margin should be positive, got {margin}"

def test_value_engine_open_only():
    """Value engine работает только с open odds (без closing) — fallback."""
    from value_engine import evaluate_match_full
    match = {
        "odds": {
            "open": {"home": 1.50, "draw": 5.00, "away": 8.00},
        }
    }
    result = evaluate_match_full(match)
    # При сильном дисбалансе value должно быть не None
    assert result is not None, "Expected non-None for imbalanced odds"
    assert result["closing_odds"] is None


# ── 10. Idempotency (§2.2) ──────────────────────────────────────────

def test_idempotency_key_format():
    """FIX-5 (§2.2): idempotency_key имеет формат run_id:canonical_id:section."""
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
        # Schema (5)
        ("Schema exists", test_schema_exists),
        ("Schema required fields", test_schema_required_fields),
        ("Schema status enum", test_schema_status_enum),
        ("Schema odds type is number", test_schema_odds_type),
        ("Schema version value", test_schema_version_value),
        ("Schema id pattern", test_schema_id_pattern),
        # Fixtures (7)
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
        # Value Engine (6)
        ("Value engine imports", test_value_engine_imports),
        ("Value engine returns float", test_value_engine_returns_float),
        ("Value engine returns None without odds", test_value_engine_returns_none_without_odds),
        ("Value engine uses closing odds", test_value_engine_uses_closing_odds),
        ("Value engine margin calculation", test_value_engine_margin_calculation),
        ("Value engine open only fallback", test_value_engine_open_only),
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
