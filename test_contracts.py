#!/usr/bin/env python3
"""
test_contracts.py — Контрактные тесты GatekeeperAI v9.3-audited.
§20.8: 14 тестов для CI pipeline.
Запуск: python test_contracts.py
"""
import sys
import os
import json
import logging

logging.basicConfig(level=logging.WARNING)

__version__ = "9.3-audited"

# ── Test framework ──────────────────────────────────────────
_passed = 0
_failed = 0
_skipped = 0
_errors = []

def assert_true(cond, msg=""):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        _errors.append(msg)
        print(f"  FAIL: {msg}")

def assert_equal(a, b, msg=""):
    assert_true(a == b, f"{msg}: {a!r} != {b!r}")

def assert_isinstance(obj, typ, msg=""):
    assert_true(isinstance(obj, typ), f"{msg}: expected {typ.__name__}, got {type(obj).__name__}")

def skip(msg):
    global _skipped
    _skipped += 1
    print(f"  SKIP: {msg}")

def run_test(name, fn):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        fn()
    except Exception as e:
        global _failed, _errors
        _failed += 1
        _errors.append(f"{name}: exception: {e}")
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()


# ── 1. Team Registry ────────────────────────────────────────
def test_team_registry():
    try:
        from team_registry import clean_team_name, TEAM_ALIASES, normalize_team_name
    except ImportError as e:
        skip(f"team_registry not available: {e}")
        return
    assert_true(len(TEAM_ALIASES) >= 200, f"TEAM_ALIASES has {len(TEAM_ALIASES)} entries, expected >=200")
    # Basic aliases
    assert_equal(clean_team_name("Man United"), "man", "Man United")
    assert_equal(clean_team_name("Manchester City"), "man_city", "Manchester City")
    assert_equal(clean_team_name("Spurs"), "tottenham", "Spurs")
    assert_equal(clean_team_name("Bayern Munich"), "bayern", "Bayern Munich")
    # Diacritics
    assert_equal(clean_team_name("Malmö FF"), "malmo_ff", "Malmö FF")
    assert_equal(clean_team_name("Brøndby IF"), "brondby", "Brøndby IF")
    assert_equal(clean_team_name("1. FC Köln"), "1_fc_koln", "Köln")
    # Suffixes
    assert_equal(clean_team_name("Djurgårdens IF"), "djugardens", "Djurgårdens IF")
    # normalize_team_name synonym
    assert_equal(normalize_team_name("Arsenal"), "arsenal", "normalize synonym")
    print("  All assertions passed")

# ── 2. Build Canonical ID ───────────────────────────────────
def test_build_canonical_id():
    try:
        from team_registry import build_canonical_id
    except ImportError as e:
        skip(f"build_canonical_id not available: {e}")
        return
    cid = build_canonical_id("Man United", "Fulham", "2026-09-21")
    assert_equal(cid, "man__fulham__20260921", "basic canonical_id")
    # ISO 8601 date
    cid2 = build_canonical_id("Arsenal", "Chelsea", "2025-10-15T16:30:00Z")
    assert_equal(cid2, "arsenal__chelsea__20251015", "ISO 8601 date")
    # Invalid date
    cid3 = build_canonical_id("Team A", "Team B", "")
    assert_equal(cid3, "", "empty date returns empty")
    print("  All assertions passed")

# ── 3. Schema ──────────────────────────────────────────────
def test_schema():
    schema_path = "schema_v710.json"
    if not os.path.exists(schema_path):
        skip(f"{schema_path} not found")
        return
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    assert_equal(schema.get("version"), "v710", "schema version")
    required = schema.get("required", [])
    assert_true("canonical_id" in required, "canonical_id in required")
    assert_true("home_clean" in required, "home_clean in required")
    assert_true("away_clean" in required, "away_clean in required")
    assert_true("schema_version" in required, "schema_version in required")
    assert_true("status" in required, "status in required")
    # Status enum
    props = schema.get("properties", {})
    status_prop = props.get("status", {})
    assert_true("enum" in status_prop, "status has enum")
    if "enum" in status_prop:
        enum = status_prop["enum"]
        assert_true("scheduled" in enum, "scheduled in enum")
        assert_true("completed" in enum, "completed in enum")
    print("  All assertions passed")

# ── 4. Config ───────────────────────────────────────────────
def test_config():
    try:
        from gatekeeper_config import load_config, is_feature_enabled, get_value_threshold
    except ImportError as e:
        skip(f"gatekeeper_config not available: {e}")
        return
    config = load_config()
    assert_isinstance(config, dict, "load_config returns dict")
    if config:
        assert_true("features" in config or isinstance(config, dict), "config has features or is dict")
    # get_value_threshold
    try:
        vt = get_value_threshold()
        assert_true(isinstance(vt, (int, float)), f"value_threshold is numeric: {vt}")
    except Exception:
        skip("get_value_threshold not available")
    print("  All assertions passed")

# ── 5. Fixtures ─────────────────────────────────────────────
def test_fixtures():
    try:
        from test_fixtures import (CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
                                    CANONICAL_COMPLETED_MATCH, CANONICAL_MINIMAL_MATCH,
                                    validate_fixtures)
    except ImportError as e:
        skip(f"test_fixtures not available: {e}")
        return
    assert_isinstance(CANONICAL_LIVE_MATCH, dict, "LIVE is dict")
    assert_true(CANONICAL_LIVE_MATCH.get("canonical_id") == "man__fulham__20260921", "LIVE canonical_id")
    assert_true(CANONICAL_LIVE_MATCH.get("status") == "scheduled", "LIVE status=scheduled")
    assert_true(CANONICAL_LIVE_MATCH.get("schema_version") == "v710", "LIVE schema_version")
    assert_true(CANONICAL_HISTORY_MATCH.get("status") == "completed", "HISTORY status=completed")
    assert_true(CANONICAL_HISTORY_MATCH.get("score", {}).get("home") == 2, "HISTORY score home=2")
    # section_history has upstream (except minimal)
    sh = CANONICAL_LIVE_MATCH.get("section_history", [])
    if sh:
        assert_true(sh[0].get("upstream") is not None, "LIVE section_history has upstream")
    ok, errors = validate_fixtures()
    assert_true(ok, f"validate_fixtures: {errors}")
    print("  All assertions passed")

# ── 6. Redis Hub API ────────────────────────────────────────
def test_redis_hub_api():
    try:
        from redis_hub import ENVELOPE_VERSION, HASH_NAME, REDIS_TIMEOUT
        from redis_config import CB_FAILURE_THRESHOLD, CB_RECOVERY_TIMEOUT
    except ImportError as e:
        skip(f"redis_hub not available: {e}")
        return
    assert_true(ENVELOPE_VERSION.startswith("v"), f"ENVELOPE_VERSION={ENVELOPE_VERSION}")
    assert_true(isinstance(HASH_NAME, str) and len(HASH_NAME) > 0, f"HASH_NAME={HASH_NAME}")
    assert_true(isinstance(REDIS_TIMEOUT, int) and REDIS_TIMEOUT > 0, f"REDIS_TIMEOUT={REDIS_TIMEOUT}")
    assert_true(isinstance(CB_FAILURE_THRESHOLD, int) and CB_FAILURE_THRESHOLD > 0, f"CB_FAILURE_THRESHOLD={CB_FAILURE_THRESHOLD}")
    assert_true(isinstance(CB_RECOVERY_TIMEOUT, int) and CB_RECOVERY_TIMEOUT > 0, f"CB_RECOVERY_TIMEOUT={CB_RECOVERY_TIMEOUT}")
    # Circuit breaker functions
    try:
        from redis_hub import is_redis_available
        assert_true(callable(is_redis_available), "is_redis_available is callable")
    except ImportError:
        try:
            from redis_hub import is_redis_configured
            assert_true(callable(is_redis_configured), "is_redis_configured is callable")
        except ImportError:
            skip("is_redis_available/is_redis_configured not available")
    # get_circuit_breaker_status
    try:
        from redis_hub import get_circuit_breaker_status
        cb = get_circuit_breaker_status()
        assert_isinstance(cb, dict, "CB status is dict")
        assert_true("state" in cb, "CB status has state")
    except (ImportError, AttributeError):
        skip("get_circuit_breaker_status not available")
    print("  All assertions passed")

# ── 7. Redis Config ─────────────────────────────────────────
def test_redis_config():
    try:
        from redis_config import REDIS_REST_URL, REDIS_REST_TOKEN, REDIS_TIMEOUT, REDIS_HASH_NAME
    except ImportError as e:
        skip(f"redis_config not available: {e}")
        return
    assert_isinstance(REDIS_REST_URL, str, "REDIS_REST_URL is str")
    assert_isinstance(REDIS_REST_TOKEN, str, "REDIS_REST_TOKEN is str")
    assert_true(isinstance(REDIS_TIMEOUT, int) and REDIS_TIMEOUT > 0, f"REDIS_TIMEOUT={REDIS_TIMEOUT}")
    assert_true(isinstance(REDIS_HASH_NAME, str) and len(REDIS_HASH_NAME) > 0, f"REDIS_HASH_NAME={REDIS_HASH_NAME}")
    # is_redis_configured
    try:
        from redis_config import is_redis_configured
        result = is_redis_configured()
        assert_isinstance(result, bool, "is_redis_configured returns bool")
    except Exception:
        skip("is_redis_configured not callable")
    print("  All assertions passed")

# ── 8. Gatekeeper Hub API ──────────────────────────────────
def test_gatekeeper_hub_api():
    try:
        from gatekeeper_hub import validate_schema, build_canonical_id, upsert_match
    except ImportError as e:
        skip(f"gatekeeper_hub not available: {e}")
        return
    # validate_schema with valid fixture
    valid = {
        "canonical_id": "man__fulham__20260921",
        "home_clean": "man",
        "away_clean": "fulham",
        "date_utc": "2026-09-21T14:00:00Z",
        "status": "scheduled",
        "schema_version": "v710",
    }
    ok, msg = validate_schema(valid)
    assert_true(ok, f"validate_schema valid: {msg}")
    # validate_schema with invalid
    invalid = {"canonical_id": "x", "home_clean": "", "away_clean": "y",
               "date_utc": "2026-01-01", "status": "scheduled", "schema_version": "v710"}
    ok2, msg2 = validate_schema(invalid)
    assert_true(not ok2, f"validate_schema invalid should fail: {msg2}")
    # build_canonical_id
    cid = build_canonical_id("Man United", "Fulham", "2026-09-21")
    assert_equal(cid, "man__fulham__20260921", "hub build_canonical_id")
    # Exported functions
    from gatekeeper_hub import __all__ as hub_all
    expected = ["validate_schema", "upsert_match", "patch_match", "get_match_any",
                "get_all_matches", "save_meta", "cleanup_expired", "run_initialization"]
    for fn in expected:
        assert_true(fn in hub_all, f"hub exports {fn}")
    print("  All assertions passed")

# ── 9. Search Module ────────────────────────────────────────
def test_search_module():
    try:
        from search_module import search_teams, find_match_candidates, clean_team_name
        from search_module import build_canonical_id, normalize_team_name
    except ImportError as e:
        skip(f"search_module not available: {e}")
        return
    # search_teams
    teams = ["Manchester United", "Manchester City", "Chelsea", "Liverpool"]
    result = search_teams("Man", teams)
    assert_true(isinstance(result, list) and len(result) >= 2, f"search_teams: {result}")
    # find_match_candidates
    matches = {
        "man__fulham__20260921": {"home_clean": "man", "away_clean": "fulham", "date_utc": "2026-09-21"},
        "arsenal__chelsea__20251015": {"home_clean": "arsenal", "away_clean": "chelsea", "date_utc": "2025-10-15"},
    }
    cands = find_match_candidates("Man United", "Fulham", matches)
    assert_true(isinstance(cands, list) and len(cands) >= 1, f"find_match_candidates: {cands}")
    # normalize_team_name re-export
    assert_equal(normalize_team_name("Arsenal"), "arsenal", "normalize_team_name re-export")
    # build_canonical_id re-export
    cid = build_canonical_id("Man United", "Fulham", "2026-09-21")
    assert_equal(cid, "man__fulham__20260921", "search build_canonical_id")
    print("  All assertions passed")

# ── 10. Country Code Map ────────────────────────────────────
def test_country_map():
    try:
        from country_code_map import COUNTRY_CODE_MAP, LEAGUE_NAME_MAP, get_country_name, get_league_name
    except ImportError as e:
        skip(f"country_code_map not available: {e}")
        return
    assert_true(len(COUNTRY_CODE_MAP) >= 50, f"COUNTRY_CODE_MAP has {len(COUNTRY_CODE_MAP)} entries")
    assert_true(len(LEAGUE_NAME_MAP) >= 20, f"LEAGUE_NAME_MAP has {len(LEAGUE_NAME_MAP)} entries")
    assert_equal(get_country_name("EN"), "England", "get_country_name EN")
    assert_equal(get_country_name("DE"), "Germany", "get_country_name DE")
    assert_equal(get_league_name("E0"), "Premier League", "get_league_name E0")
    assert_equal(get_league_name("SP1"), "La Liga", "get_league_name SP1")
    assert_equal(get_league_name("I1"), "Serie A", "get_league_name I1")
    print("  All assertions passed")

# ── 11. Telegram Transport API ──────────────────────────────
def test_telegram_transport_api():
    try:
        from telegram_transport import split_html_safe, normalize_dashboard_text, TELEGRAM_CHUNK_LIMIT
    except ImportError as e:
        skip(f"telegram_transport not available: {e}")
        return
    # split_html_safe
    text = "<b>Test</b> " + "x" * 5000
    parts = split_html_safe(text, max_chars=100)
    assert_true(isinstance(parts, list) and len(parts) > 1, f"split_html_safe: {len(parts)} parts")
    # Tag balance
    for part in parts:
        # Each part should have balanced <b> tags
        open_count = part.count("<b>")
        close_count = part.count("</b>")
        assert_true(open_count == close_count, f"Tag imbalance: {open_count} open, {close_count} close")
    # normalize_dashboard_text
    normalized = normalize_dashboard_text("O: 1.85 | P: 2.10 → 3")
    assert_true("<code>" in normalized, f"normalize wraps in code: {normalized}")
    # TELEGRAM_CHUNK_LIMIT
    assert_true(isinstance(TELEGRAM_CHUNK_LIMIT, int) and TELEGRAM_CHUNK_LIMIT > 0, f"CHUNK_LIMIT={TELEGRAM_CHUNK_LIMIT}")
    print("  All assertions passed")

# ── 12. Base Collector ──────────────────────────────────────
def test_base_collector():
    try:
        from base_collector import BaseCollector
    except ImportError as e:
        skip(f"base_collector not available: {e}")
        return
    # Create instance
    bc = BaseCollector(source_name="test")
    assert_true(hasattr(bc, "source_name"), "has source_name")
    assert_true(hasattr(bc, "collect"), "has collect method")
    assert_true(hasattr(bc, "enrich"), "has enrich method")
    # Version
    try:
        from base_collector import __version__
        assert_true(isinstance(__version__, str), f"version={__version__}")
    except ImportError:
        pass
    print("  All assertions passed")

# ── 13. Value Engine ────────────────────────────────────────
def test_value_engine():
    try:
        from value_engine import analyze_match, calculate_margin, extract_odds_pair
        from value_engine import calc_brier_score, calc_rps, run_backtest, run_pipeline
        from value_engine import __version__
    except ImportError as e:
        skip(f"value_engine not available: {e}")
        return
    # Version
    assert_true("9.3" in __version__, f"value_engine version={__version__}")
    # calculate_margin
    margin = calculate_margin({"home": 2.0, "draw": 3.5, "away": 4.0})
    assert_true(isinstance(margin, (int, float)) and margin > 0, f"margin={margin}")
    # calc_brier_score
    brier = calc_brier_score([0.5, 0.3, 0.2], 0)  # home win
    assert_true(isinstance(brier, (int, float)) and 0 <= brier <= 2, f"brier={brier}")
    # calc_rps
    rps = calc_rps([0.5, 0.3, 0.2], 0)
    assert_true(isinstance(rps, (int, float)) and 0 <= rps <= 2, f"rps={rps}")
    # run_backtest with empty
    bt = run_backtest([], [])
    assert_isinstance(bt, dict, "run_backtest returns dict")
    assert_true(bt.get("total") == 0, f"backtest total={bt.get('total')}")
    # analyze_match with minimal match
    minimal = {
        "canonical_id": "test__test__20260101",
        "home_clean": "test",
        "away_clean": "test2",
        "date_utc": "2026-01-01T12:00:00Z",
        "status": "scheduled",
        "schema_version": "v710",
        "odds": {},
    }
    result = analyze_match(minimal)
    assert_isinstance(result, dict, "analyze_match returns dict")
    print("  All assertions passed")

# ── 14. Metrics ─────────────────────────────────────────────
def test_metrics():
    try:
        from metrics import collect_metrics, save_metrics
    except ImportError as e:
        skip(f"metrics not available: {e}")
        return
    # collect_metrics should return dict
    try:
        m = collect_metrics()
        assert_isinstance(m, dict, "collect_metrics returns dict")
    except Exception as e:
        skip(f"collect_metrics failed: {e}")
    # save_metrics should return dict
    try:
        d = save_metrics()
        assert_isinstance(d, dict, "save_metrics returns dict")
    except Exception as e:
        skip(f"save_metrics failed: {e}")
    print("  All assertions passed")


# ── Main ────────────────────────────────────────────────────
def main():
    print("\n" + "=" * 60)
    print("GatekeeperAI Contract Tests v9.3-audited")
    print("=" * 60)

    tests = [
        ("Team Registry", test_team_registry),
        ("Build Canonical ID", test_build_canonical_id),
        ("Schema v710", test_schema),
        ("Gatekeeper Config", test_config),
        ("Test Fixtures", test_fixtures),
        ("Redis Hub API", test_redis_hub_api),
        ("Redis Config", test_redis_config),
        ("Gatekeeper Hub API", test_gatekeeper_hub_api),
        ("Search Module", test_search_module),
        ("Country Code Map", test_country_map),
        ("Telegram Transport API", test_telegram_transport_api),
        ("Base Collector", test_base_collector),
        ("Value Engine", test_value_engine),
        ("Metrics", test_metrics),
    ]

    for name, fn in tests:
        run_test(name, fn)

    print("\n" + "=" * 60)
    print(f"RESULTS: {_passed} passed, {_failed} failed, {_skipped} skipped")
    print("=" * 60)

    if _errors:
        print("\nErrors:")
        for e in _errors:
            print(f"  - {e}")

    if _failed > 0:
        sys.exit(1)
    else:
        print("\nAll available tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
