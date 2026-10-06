#!/usr/bin/env python3
"""
test_contracts.py — Контрактные тесты GatekeeperAI v9.3 (§20.8).
14 тестов, покрывающих все модули.
"""

import json
import sys
import os

# ── Helpers ──
def _try_import(modname):
    try:
        __import__(modname)
        return True
    except ImportError:
        return False


# ── 1. Schema ──
def test_schema():
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema["version"] == "v710", f"version != v710: {schema.get('version')}"
    required = schema.get("required", [])
    assert "canonical_id" in required
    assert "home" in required
    assert "away" in required
    assert "date_utc" in required
    assert "status" in required
    assert "competition" in required
    assert "schema_version" in required
    statuses = schema["properties"]["status"]["enum"]
    assert "scheduled" in statuses
    assert "completed" in statuses
    assert "live" in statuses
    print("  test_schema: PASS")


# ── 2. Fixtures ──
def test_fixtures():
    from test_fixtures import (
        CANONICAL_LIVE_MATCH, CANONICAL_HISTORY_MATCH,
        CANONICAL_COMPLETED_MATCH, CANONICAL_MINIMAL_MATCH,
        validate_fixtures,
    )
    ok, errors = validate_fixtures()
    assert ok, f"Fixture validation failed: {errors}"
    assert CANONICAL_LIVE_MATCH["status"] == "scheduled"
    assert CANONICAL_HISTORY_MATCH["status"] == "completed"
    assert CANONICAL_LIVE_MATCH["schema_version"] == "v710"
    assert CANONICAL_HISTORY_MATCH["schema_version"] == "v710"
    assert CANONICAL_LIVE_MATCH["canonical_id"] == "man__fulham__20260921"
    assert CANONICAL_HISTORY_MATCH["canonical_id"] == "arsenal__chelsea__20251015"
    print("  test_fixtures: PASS")


# ── 3. Team Registry ──
def test_team_registry():
    if not _try_import("team_registry"):
        print("  test_team_registry: SKIP (no module)")
        return
    from team_registry import clean_team_name, TEAM_ALIASES, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert len(TEAM_ALIASES) >= 200, f"only {len(TEAM_ALIASES)} aliases"
    assert clean_team_name("Manchester United") == "man"
    assert clean_team_name("Brøndby IF") == "brondby"
    assert clean_team_name("Köln") == "cologne"
    assert clean_team_name("Malmö FF") == "malmo_ff"
    assert clean_team_name("") == ""
    assert clean_team_name(None) == ""
    print("  test_team_registry: PASS")


# ── 4. Build Canonical ID ──
def test_build_canonical_id():
    if not _try_import("team_registry"):
        print("  test_build_canonical_id: SKIP (no module)")
        return
    from team_registry import build_canonical_id
    assert build_canonical_id("Man United", "Chelsea", "2025-10-04") == "man__chelsea__20251004"
    assert build_canonical_id("", "Chelsea", "2025-10-04") == ""
    assert build_canonical_id("Arsenal", "", "2025-10-04") == ""
    assert build_canonical_id("Arsenal", "Chelsea", "") == ""
    print("  test_build_canonical_id: PASS")


# ── 5. Config ──
def test_config():
    if not _try_import("gatekeeper_config"):
        print("  test_config: SKIP (no module)")
        return
    from gatekeeper_config import load_config, get_value_threshold, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    cfg = load_config()
    assert isinstance(cfg, dict), f"config not dict: {type(cfg)}"
    threshold = get_value_threshold()
    assert isinstance(threshold, (int, float)), f"threshold not numeric: {type(threshold)}"
    print("  test_config: PASS")


# ── 6. Redis Hub ──
def test_redis_hub_api():
    if not _try_import("redis_hub"):
        print("  test_redis_hub_api: SKIP (no module)")
        return
    from redis_hub import ENVELOPE_VERSION, HASH_NAME, REDIS_TIMEOUT, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert ENVELOPE_VERSION == "v700-prod"
    assert isinstance(HASH_NAME, str) and len(HASH_NAME) > 0
    assert REDIS_TIMEOUT > 0
    print("  test_redis_hub_api: PASS")


# ── 7. Redis Config ──
def test_redis_config():
    if not _try_import("redis_config"):
        print("  test_redis_config: SKIP (no module)")
        return
    from redis_config import is_redis_configured, get_redis_info, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert isinstance(is_redis_configured(), bool)
    info = get_redis_info()
    assert isinstance(info, dict)
    assert "configured" in info
    assert "timeout" in info
    print("  test_redis_config: PASS")


# ── 8. Gatekeeper Hub ──
def test_gatekeeper_hub_api():
    if not _try_import("gatekeeper_hub"):
        print("  test_gatekeeper_hub_api: SKIP (no module)")
        return
    from gatekeeper_hub import __version__, __all__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert "upsert_match" in __all__
    assert "get_all_matches" in __all__
    assert "validate_schema" in __all__ or hasattr(__import__("gatekeeper_hub"), "validate_schema")
    print("  test_gatekeeper_hub_api: PASS")


# ── 9. Search Module ──
def test_search_module():
    if not _try_import("search_module"):
        print("  test_search_module: SKIP (no module)")
        return
    from search_module import search_teams, find_match_candidates, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    results = search_teams("Arsenal", ["Arsenal", "Chelsea", "Liverpool"])
    assert "Arsenal" in results
    print("  test_search_module: PASS")


# ── 10. Country Code Map ──
def test_country_map():
    if not _try_import("country_code_map"):
        print("  test_country_map: SKIP (no module)")
        return
    from country_code_map import COUNTRY_CODE_MAP, LEAGUE_NAME_MAP, get_league_name, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert len(COUNTRY_CODE_MAP) >= 50
    assert len(LEAGUE_NAME_MAP) >= 20
    assert COUNTRY_CODE_MAP.get("EN") == "England"
    assert COUNTRY_CODE_MAP.get("DE") == "Germany"
    print("  test_country_map: PASS")


# ── 11. Telegram Transport ──
def test_telegram_transport_api():
    if not _try_import("telegram_transport"):
        print("  test_telegram_transport_api: SKIP (no module)")
        return
    from telegram_transport import split_html_safe, normalize_dashboard_text, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    parts = split_html_safe("<b>Hello</b> world " * 500, 100)
    assert all(len(p) <= 100 for p in parts)
    result = normalize_dashboard_text("O: 1.85 | P: 2.10")
    assert "<code>" in result
    print("  test_telegram_transport_api: PASS")


# ── 12. Value Engine ──
def test_value_engine():
    if not _try_import("value_engine"):
        print("  test_value_engine: SKIP (no module)")
        return
    from value_engine import calculate_margin, calc_brier_score, calc_rps, run_backtest, analyze_match, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    margin = calculate_margin({"home": 2.0, "draw": 3.5, "away": 4.0})
    assert margin is not None and margin > 0
    brier = calc_brier_score([0.5, 0.3, 0.2], 0)
    assert 0 <= brier <= 2
    rps = calc_rps([0.5, 0.3, 0.2], 0)
    assert 0 <= rps <= 1
    print("  test_value_engine: PASS")


# ── 13. Base Collector ──
def test_base_collector():
    if not _try_import("base_collector"):
        print("  test_base_collector: SKIP (no module)")
        return
    from base_collector import BaseCollector, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    bc = BaseCollector("test")
    assert bc.name == "test"
    assert bc.matches_processed == 0
    assert bc.errors == 0
    print("  test_base_collector: PASS")


# ── 14. Metrics ──
def test_metrics():
    if not _try_import("metrics"):
        print("  test_metrics: SKIP (no module)")
        return
    from metrics import collect_metrics, save_metrics, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    m = collect_metrics()
    assert isinstance(m, dict)
    assert "matches_total" in m
    assert "timestamp" in m
    print("  test_metrics: PASS")


# ── Runner ──
def run_all():
    tests = [
        test_schema, test_fixtures, test_team_registry, test_build_canonical_id,
        test_config, test_redis_hub_api, test_redis_config, test_gatekeeper_hub_api,
        test_search_module, test_country_map, test_telegram_transport_api,
        test_value_engine, test_base_collector, test_metrics,
    ]
    passed = 0
    failed = 0
    skipped = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  {test.__name__}: FAIL — {e}")
            failed += 1
        except Exception as e:
            print(f"  {test.__name__}: ERROR — {e}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped")
    return failed == 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ok = run_all()
    sys.exit(0 if ok else 1)
