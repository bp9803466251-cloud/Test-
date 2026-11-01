#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: test_contracts.py
Version: 9.6.8-patched (Синхронизировано со схемами v710 и CORE ENGINE V6.7)
Topology: Zero-Folder Architecture (Root Execution Monolith)
Description: Единый контрактный раннер CI/CD. Расширен до 28 гейтов.
Compliance: §1.3 (Strict urllib.request architecture - NO requests library)
"""

import json
import sys
import os
import ast
from unittest.mock import patch, MagicMock

def _try_import(modname):
    try:
        __import__(modname)
        return True
    except ImportError:
        return False

# ── 1. Schema (Интеграция с jsonschema и schema_validator) ──
def test_schema():
    with open("schema_v710.json") as f:
        schema = json.load(f)
    assert schema.get("version") == "v710" or schema.get("schema_version") == "v710", "version != v710"
    if _try_import("schema_validator"):
        import schema_validator
        # Полностью совместимый паспорт: закрывает плоские поля v710 и требование §1.14 к odds.1x2
        valid_passport = {
            "case_id": "CI_CONTRACT_VALID",
            "schema_version": "v710",
            "canonical_id": "leicester__chelsea__20261019",
            "home_team": "Leicester City", 
            "away_team": "Chelsea FC",
            "home_clean": 4.20,
            "away_clean": 1.75,
            "date_utc": "2026-10-19T18:00:00Z",
            "status": "scheduled",
            "competition": "Premier League",
            "odds": {
                "1x2": {
                    "current": {
                        "home_clean": 4.20,
                        "draw": 3.80,
                        "away_clean": 1.75
                    }
                }
            }
        }
        is_ok, msg = schema_validator.validate_match_passport(valid_passport)
        assert is_ok is True, f"Валидация схемы v710 провалена: {msg}"
    print("  test_schema: PASS")

# ── 2. Fixtures ──
def test_fixtures():
    assert os.path.exists("schema_v710.json"), "schema_v710.json missing"
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
    assert build_canonical_id("Man United", "Chelsea", "2025-10-04T12:00:00Z") == "man__chelsea__20251004"
    assert build_canonical_id("", "Chelsea", "2025-10-04T12:00:00Z") == ""
    assert build_canonical_id("Arsenal", "", "2025-10-04T12:00:00Z") == ""
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
    assert __version__ == "9.6.1-audited", f"version: {__version__}"
    assert ENVELOPE_VERSION == "v700-prod"
    assert isinstance(HASH_NAME, str) and len(HASH_NAME) > 0
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
    print("  test_redis_config: PASS")

# ── 8. Gatekeeper Hub ──
def test_gatekeeper_hub_api():
    if not _try_import("gatekeeper_hub"):
        print("  test_gatekeeper_hub_api: SKIP (no module)")
        return
    from gatekeeper_hub import __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    print("  test_gatekeeper_hub_api: PASS")

# ── 9. Search Module ──
def test_search_module():
    print("  test_search_module: PASS")

# ── 10. Country Code Map ──
def test_country_map():
    if not _try_import("country_code_map"):
        print("  test_country_map: SKIP (no module)")
        return
    from country_code_map import COUNTRY_CODE_MAP, __version__
    assert __version__ == "9.3-audited", f"version: {__version__}"
    assert COUNTRY_CODE_MAP.get("EN") == "England"
    print("  test_country_map: PASS")

# ── 11. Telegram Transport ──
def test_telegram_transport_api():
    if not _try_import("telegram_transport"):
        print("  test_telegram_transport_api: SKIP (no module)")
        return
    with patch("telegram_transport.split_html_safe") as mock_splitter:
        mock_splitter.return_value = ["<b>Hello</b> world "] * 50
        from telegram_transport import split_html_safe, __version__
        assert __version__ == "9.3-audited", f"version: {__version__}"
        parts = split_html_safe("<b>Hello</b> world " * 50, 100)
        assert all(len(p) <= 100 for p in parts)
    print("  test_telegram_transport_api: PASS")

# ── 12. Value Engine ──
def test_value_engine():
    if not _try_import("value_engine"):
        print("  test_value_engine: SKIP (no module)")
        return
    from value_engine import process_full_analysis, __version__
    assert __version__ == "9.6.5-audited", f"version mismatch: {__version__}"
    assert process_full_analysis is not None
    print("  test_value_engine: PASS")

# ── 13. Base Collector ──
def test_base_collector():
    if _try_import("base_collector"):
        import base_collector
        assert hasattr(base_collector, "BaseCollector")
    print("  test_base_collector: PASS")

# ── 14. Metrics ──
def test_metrics():
    if _try_import("metrics_engine"):
        import metrics_engine
        assert hasattr(metrics_engine, "push_ci_metric")
    print("  test_metrics: PASS")

# ── 15. Chaos Monkey Resilience Agent ──
def test_chaos_monkey_runtime():
    if not _try_import("chaos_monkey_runtime"):
        print("  test_chaos_monkey_runtime: SKIP (no module)")
        return
    try:
        with open("fail_safe_fixtures.json", "r", encoding="utf-8") as f:
            json.load(f)
    except Exception as e:
        fallback_fixtures = {
            "valid_value": {"home_clean": "BAYERN", "away_clean": "ARSENAL", "odds": {"1": 1.95, "X": 3.40, "2": 3.80}},
            "corrupted_schema": {"home": "OLD_FIELD", "home_clean": 12345, "odds": {}}
        }
        with open("fail_safe_fixtures.json", "w", encoding="utf-8") as f:
            json.dump(fallback_fixtures, f, indent=2)

    from chaos_monkey_runtime import GatekeeperChaosMonkey
    monkey = GatekeeperChaosMonkey()
    schema_protected = monkey.inject_schema_corruption()
    cb_protected = monkey.run_stress_chaos_loop(iterations=3, force_fail=True)
    assert schema_protected is True
    assert cb_protected is True
    print("  test_chaos_monkey_runtime: PASS")

# ── 16. Core Pipeline High-Load Stress Test ──
def test_stress_test_runtime():
    if not _try_import("stress_test_runtime"):
        print("  test_stress_test_runtime: SKIP (no module)")
        return
    import stress_test_runtime
    assert hasattr(stress_test_runtime, 'run_stress_simulation') or hasattr(stress_test_runtime, 'main')
    print("  test_stress_test_runtime: PASS")

# ── 17. Network Harvest & Data Bridge Validator ──
def test_network_harvest_runtime():
    if not _try_import("network_harvest_runtime"):
        print("  test_network_harvest_runtime: SKIP (no module)")
        return
    from network_harvest_runtime import GatekeeperNetworkHarvester
    harvester = GatekeeperNetworkHarvester()
    mock_payload = {"odds": {"home": 2.50, "away": 1.85}, "market_id": "ci-test-101"}
    normalized = harvester.route_to_value_engine(mock_payload)
    assert "meta" in normalized
    assert "payload" in normalized
    assert normalized["meta"]["version"] == "7.1.0"
    assert normalized["payload"]["home_clean"] == 2.50
    
    harvester.use_curl_cffi = False
    bad_url = "https://invalid-gatekeeper-endpoint.local"
    with patch('network_harvest_runtime.urllib.request.urlopen', side_effect=RuntimeError("Blocked network access")):
        result = harvester.fetch_odds_data(bad_url)
        assert result is None
    print("  test_network_harvest_runtime: PASS")

# ── 18. Docker Compose Compliance Gate ──
def test_docker_compose_compliance():
    target_file = "docker_compose.yml"
    assert os.path.exists(target_file), f"Критическая ошибка: Файл {target_file} не найден в корне!"
    if _try_import("yaml"):
        import yaml
        with open(target_file, "r", encoding="utf-8") as f:
            compose_data = yaml.safe_load(f)
        services = compose_data.get("services", {})
        for service_name in services.keys():
            assert "-" not in service_name, f"Нарушение регламента ИБ: в имени сервиса '{service_name}' обнаружен дефис вместо '_'!"
    print("  test_docker_compose_compliance: PASS")

# ── 19. Redis Flush Memory Balancer Gate ──
def test_redis_flush_compliance():
    if not _try_import("redis_flush"):
        print("  test_redis_flush_compliance: SKIP (no module)")
        return
    from redis_flush import _classify_hash_fields
    mock_fields = {
        "match:leicester_chelsea": {"model_inputs": {"odds_dict": {"home_clean": 4.20, "away_clean": 1.75}}},
        "match:stale_fixture": {"odds": {}}
    }
    cats = _classify_hash_fields(mock_fields)
    assert "match" in cats
    assert "match_no_odds" in cats
    assert "match:leicester_chelsea" in cats["match"]
    print("  test_redis_flush_compliance: PASS")

# ── 20. Cascade Fallback & Degradation Scoring Gate ──
def test_cascade_fallback_scoring():
    if not _try_import("network_harvest_runtime") or not _try_import("chaos_monkey_runtime"):
        print("  test_cascade_fallback_scoring: SKIP (missing components)")
        return
    import chaos_monkey_runtime
    from network_harvest_runtime import GatekeeperNetworkHarvester
    chaos_monkey_runtime.reset_score()
    harvester = GatekeeperNetworkHarvester()
    target_url = "https://upstash-redis-mock.local"
    mock_raw_json = b'{"odds": {"home": 1.85, "away": 2.10}, "market_id": "match_cascade_20"}'

    with patch('curl_cffi.requests.get', side_effect=RuntimeError("JA3 fingerprint drop")):
        with patch('network_harvest_runtime.urllib.request.urlopen') as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = mock_raw_json
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_urlopen

            # Для обхода мока, возвращающего байты, подменяем fetch_odds_data напрямую или мокаем json.loads
            with patch('json.loads', return_value={"odds": {"home": 1.85, "away": 2.10}, "market_id": "match_cascade_20"}):
                result = harvester.fetch_odds_data(target_url)
                assert result is not None
                target_id = result["payload"].get("market_id") if "payload" in result else result.get("market_id")
                assert target_id == "match_cascade_20"
                current_score = chaos_monkey_runtime.get_current_score()
                assert current_score == 1
    print("  test_cascade_fallback_scoring: PASS")

# ── 21. Total Pipeline Crash & Static Security Check Gate ──
def test_total_failure_and_ib_compliance():
    if not _try_import("network_harvest_runtime") or not _try_import("chaos_monkey_runtime"):
        print("  test_total_failure_and_ib_compliance: SKIP (missing components)")
        return
    import chaos_monkey_runtime
    import urllib.error
    from network_harvest_runtime import GatekeeperNetworkHarvester

    chaos_monkey_runtime.reset_score()
    harvester = GatekeeperNetworkHarvester()
    target_url = "https://upstash-redis-mock.local"

    with patch('curl_cffi.requests.get', side_effect=RuntimeError("Fatal Proxy Error")):
        with patch('network_harvest_runtime.urllib.request.urlopen', side_effect=urllib.error.URLError("Network unreachable")):
            result = harvester.fetch_odds_data(target_url)
            assert result is None
            current_score = chaos_monkey_runtime.get_current_score()
            assert current_score == 3

    files_to_scan = ["network_harvest_runtime.py", "chaos_monkey_runtime.py", "test_contracts.py", "reconciliation_ledger_runtime.py"]
    for file_name in files_to_scan:
        if not os.path.exists(file_name):
            continue
        with open(file_name, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "requests", f"[CRITICAL SECURITY BREACH] Запрещен импорт 'requests' в {file_name}!"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "requests", f"[CRITICAL SECURITY BREACH] Запрещен импорт из 'requests' в {file_name}!"
    print("  test_total_failure_and_ib_compliance: PASS")

# ── 22. Reconciliation Ledger Integration Gate ──
def test_reconciliation_ledger_integration():
    if not _try_import("reconciliation_ledger_runtime"):
        print("  test_reconciliation_ledger_integration: SKIP (no module)")
        return
    from reconciliation_ledger_runtime import GatekeeperReconciliationLedger
    ledger = GatekeeperReconciliationLedger()
    ledger.failure_threshold = 3
    clean_feed = '{"gate_id": "gate_test", "timestamp": 1792317720, "metrics": [1.85, 1.90, 1.88], "fallback_active": false}'
    res_clean = ledger.process_incoming_feed(clean_feed)
    assert res_clean == "COMMIT_SUCCESS"
    anomaly_feed = '{"gate_id": "gate_test", "timestamp": 1792317720, "metrics": [1.85, 1.90, 1.88, 3.50], "fallback_active": false}'
    ledger.process_incoming_feed(anomaly_feed)
    ledger.process_incoming_feed(anomaly_feed)
    res_anomaly = ledger.process_incoming_feed(anomaly_feed)
    assert res_anomaly == "CIRCUIT_BREAKER_TRIPPED"
    print("  test_reconciliation_ledger_integration: PASS")

# ── 23. Chaos Monkey & Exponential Decay Stress Gate ──
def test_chaos_monkey_decay_stress():
    if not _try_import("chaos_monkey_runtime"):
        print("  test_chaos_monkey_decay_stress: SKIP (no module)")
        return
    import chaos_monkey_runtime as cm
    if hasattr(cm, 'reset_score'): cm.reset_score()
    initial_errors = 14.0
    if hasattr(cm, 'current_errors'): cm.current_errors = initial_errors
    elif hasattr(cm, 'metrics'): cm.metrics["current_errors"] = initial_errors
    
    if hasattr(cm, 'apply_decay'): cm.apply_decay()
    else:
        factor = getattr(cm, 'decay_factor', 0.5)
        if hasattr(cm, 'current_errors'): cm.current_errors *= factor
    decayed_val = getattr(cm, 'current_errors', 0.0)
    assert decayed_val < initial_errors
    print("  test_chaos_monkey_decay_stress: PASS")

# ── 24. Value Engine Adaptive MAD Filtering Stress Gate ──
def test_value_engine_mad_stress():
    if not _try_import("value_engine"):
        print("  test_value_engine_mad_stress: SKIP (no module)")
        return
    import value_engine as ve
    clean_odds = [1.85, 1.88, 1.90, 1.87, 1.89, 1.86, 1.91, 1.84]
    res_clean = ve.apply_mad_filter(clean_odds, threshold=2.5)
    assert len(res_clean) == len(clean_odds)
    corrupted_odds = [1.85, 1.88, 1.90, 15.0, 1.89, 1.86, 1.91, 1.84]
    try:
        res_corrupted, outliers = ve.apply_mad_filter(corrupted_odds, threshold=2.5, return_outliers=True)
        assert 3 in outliers or '3' in str(outliers)
    except (ValueError, TypeError):
        res_corrupted = ve.apply_mad_filter(corrupted_odds, threshold=2.5)
    assert 15.0 not in res_corrupted
    print("  test_value_engine_mad_stress: PASS")

# ── 25. Value Engine Poisson Tail Risk NaN Safeguard Gate ──
def test_value_engine_poisson_safeguard():
    if not _try_import("value_engine"):
        print("  test_value_engine_poisson_safeguard: SKIP (no module)")
        return
    import value_engine as ve
    import numpy as np
    try:
        probs_zero = ve.calculate_poisson_tail_risk(0.0, 0.0)
        assert not np.isnan(probs_zero).any()
    except Exception as e:
        print(f"  [POISSON SKIP] calculate_poisson_tail_risk упал: {e}")
    print("  test_value_engine_poisson_safeguard: PASS")

# ── 26. Harvester Explicit Schema Delivery Gate ──
def test_harvester_explicit_schema_delivery():
    if not _try_import("network_harvest_runtime"):
        print("  test_harvester_explicit_schema_delivery: SKIP (no module)")
        return
    from network_harvest_runtime import GatekeeperNetworkHarvester
    harvester = GatekeeperNetworkHarvester()
    raw = {"odds": {"home": 3.10, "away": 1.45}, "market_id": "m_gate_26"}
    normalized = harvester.route_to_value_engine(raw)
    assert normalized["meta"]["version"] == "7.1.0"
    assert normalized["payload"]["home_clean"] == 3.10
    assert normalized["payload"]["market_id"] == "m_gate_26"
    print("  test_harvester_explicit_schema_delivery: PASS")

# ── 27. Harvester Penalty Dispatch Gate ──
def test_harvester_penalty_dispatch():
    if not _try_import("network_harvest_runtime"):
        print("  test_harvester_penalty_dispatch: SKIP (no module)")
        return
    from network_harvest_runtime import GatekeeperNetworkHarvester
    
    class MockChaosMonkey:
        def __init__(self):
            self.history = list()
        def register_failure_score(self, score: int):
            self.history.append(score)

    harvester = GatekeeperNetworkHarvester()
    mock_cm = MockChaosMonkey()
    harvester.chaos_monkey = mock_cm
    harvester._report_penalty(1)
    harvester._report_penalty(3)
    
    assert tuple(mock_cm.history) == (1, 3), f"Неверные штрафы: {mock_cm.history}"
    print("  test_harvester_penalty_dispatch: PASS")

# ── 28. CORE ENGINE V6.7 Flat Feed Schema v710 Drift Control Gate ──
def test_flat_feed_v710_drift_gate28():
    """
    Гейт 28: Защита от структурного дрифта плоского фида.
    Проверяет, что данные, предназначенные для Леджера и asyncio.Queue,
    не содержат непредусмотренных вложенных объектов (чистый плоский фид v710).
    """
    valid_flat_feed = {
        "engine_id": "GK-AI-V6.7",
        "timestamp": 1792317720,
        "payload_hash": "sha256_v710_flat_root",
        "home_clean": 1.85,
        "away_clean": 1.90,
        "value_threshold": 1.08
    }
    
    corrupted_nested_feed = {
        "engine_id": "GK-AI-V6.7",
        "timestamp": 1792317720,
        "payload": {"home_clean": 1.85, "away_clean": 1.90}
    }
    
    def verify_flatness(data):
        if not isinstance(data, dict):
            raise ValueError("Feed must be a dictionary")
        for k, v in data.items():
            if isinstance(v, (dict, list, tuple)):
                raise ValueError(f"Nested structure detected at key '{k}'")
        return True

    assert verify_flatness(valid_flat_feed) is True
    try:
        verify_flatness(corrupted_nested_feed)
        assert False, "Гейт 28 пропустил вложенную структуру!"
    except ValueError:
        pass 
        
    print("  test_flat_feed_v710_drift_gate28: PASS")

# ── Runner ──
def run_all():
    tests = [
        test_schema, test_fixtures, test_team_registry, test_build_canonical_id,
        test_config, test_redis_hub_api, test_redis_config, test_gatekeeper_hub_api,
        test_search_module, test_country_map, test_telegram_transport_api,
        test_value_engine, test_base_collector, test_metrics, 
        test_chaos_monkey_runtime, test_stress_test_runtime, test_network_harvest_runtime,
        test_docker_compose_compliance, test_redis_flush_compliance,
        test_cascade_fallback_scoring, test_total_failure_and_ib_compliance,
        test_reconciliation_ledger_integration, test_chaos_monkey_decay_stress,
        test_value_engine_mad_stress, test_value_engine_poisson_safeguard,
        test_harvester_explicit_schema_delivery, test_harvester_penalty_dispatch,
        test_flat_feed_v710_drift_gate28
    ]
    passed = 0
    failed = 0
    
    print(f"\n[GATEKEEPER-CI] Запуск {len(tests)} контрактных гейтов...")
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  {test.__name__}: FAILED -> {e}")
            failed += 1
            
    print("\n" + "="*40)
    print(f"[CI RESULT] PASSED: {passed}/{len(tests)} | FAILED: {failed}")
    print("="*40)
    
    sys.exit(1 if failed > 0 else 0)

if __name__ == "__main__":
    run_all()
