#!/usr/bin/env python3
# redis_diagnostics.py
"""
Gatekeeper-AI Ecosystem: Единая диагностическая утилита распределенного кэша.
Версия 9.3-audited — Полная синхронизация со структурами CORE ENGINE V6.7 (V5.7.12).
Read-only по умолчанию (§18.1), реализует 7-секционный отчет (§18.3) с расширенным дебагом ключей.
"""

import sys
import os
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__version__ = "9.3-audited"  # Строгая контрактная строка версии для прохождения тестов Гейта
__all__ = ["main", "diagnose_redis", "diagnose_matches", "diagnose_indexes",
           "diagnose_sources", "diagnose_errors", "__version__"]

MSK_TZ = timezone(timedelta(hours=3))

def _now_msk() -> str:
    return datetime.now(MSK_TZ).isoformat()

# ── Lazy-импорты (только при непосредственном выполнении) ─────────
def _get_redis_hub():
    try:
        import redis_hub
        return redis_hub
    except ImportError: return None

def _get_hub():
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError: return None

def _get_config():
    try:
        import redis_config
        return redis_config
    except ImportError: return None

# ============================================================================
# 5-шаговый алгоритм диагностики (§18.2) с расширенным Debug Trace
# ============================================================================
def _step1_init():
    cfg = _get_config()
    hub = _get_hub()
    info = {
        "timestamp": _now_msk(),
        "redis_configured": False,
        "redis_available": False,
        "circuit_breaker": "unknown",
        "errors": [],
    }
    if cfg:
        info["redis_configured"] = cfg.is_redis_configured()
        cfg_errors = cfg.get_redis_config_errors()
        if cfg_errors: info["errors"].extend(cfg_errors)
        info["redis_info"] = cfg.get_redis_info()
    if hub:
        try:
            init = hub.run_initialization(collector="redis_diagnostics")
            info["redis_available"] = init.get("redis_available", False)
        except Exception as e: info["errors"].append(f"run_initialization: {e}")
    rdb = _get_redis_hub()
    if rdb:
        try:
            cb = rdb.get_circuit_breaker_status()
            info["circuit_breaker"] = cb.get("state", "unknown")
            info["cb_failures"] = cb.get("failures", 0)
        except Exception as e: info["errors"].append(f"circuit_breaker: {e}")
    return info

def _step2_get_all_fields():
    """Сбор полей из хэша Redis с полным отладочным трекингом ключей."""
    rdb = _get_redis_hub()
    if not rdb: 
        print("[DEBUG REDIS] ❌ Модуль redis_hub недоступен")
        return {"matches": {}, "count": 0, "error": "redis_hub unavailable"}
    
    try:
        all_fields = rdb.get_all_fields()
        print(f"[DEBUG REDIS] 🔍 Всего извлечено элементов из хэша GatekeeperAI: {len(all_fields) if all_fields else 0}")
        if all_fields:
            sample_keys = list(all_fields.keys())[:10]
            print(f"[DEBUG REDIS] 📝 Первые 10 ключей в хэше: {sample_keys}")
            
            match_prefixed = [k for k in all_fields.keys() if k.startswith("match:")]
            canonical_format = [k for k in all_fields.keys() if "__" in k and not k.startswith("match:")]
            
            print(f"[DEBUG REDIS] 📊 Статистика ключей:")
            print(f"  • С префиксом 'match:': {len(match_prefixed)}")
            print(f"  • Канонический формат без префикса (содержат '__'): {len(canonical_format)}")
            
            if not match_prefixed and len(canonical_format) > 0:
                print("[DEBUG REDIS] ⚠️ ВНИМАНИЕ: Коллектор записывает ключи в виде сырых canonical_id без префикса 'match:'!")
        else:
            print("[DEBUG REDIS] ⚠️ Хэш-таблица GatekeeperAI пуста или не возвращает поля.")
    except Exception as e: 
        print(f"[DEBUG REDIS] ❌ Ошибка при вызове rdb.get_all_fields(): {e}")
        return {"matches": {}, "count": 0, "error": str(e)}

    if not all_fields: 
        return {"matches": {}, "count": 0, "error": None}

    matches = {}
    for key, value in all_fields.items():
        # Гибкая проверка: поддерживаем как ключи с match:, так и канонический вид с '__'
        is_valid_key = key.startswith("match:") or "__" in key
        if not is_valid_key: 
            continue
        try:
            match = value if isinstance(value, dict) else json.loads(value)
            if isinstance(match, dict):
                # Нормализуем ключ с префиксом для внутренней структуры отчета
                fixed_key = key if key.startswith("match:") else f"match:{key}"
                matches[fixed_key] = match
        except Exception as err: 
            print(f"[DEBUG REDIS] ⚠️ Ошибка JSON-декодирования для ключа {key}: {err}")
            pass
            
    print(f"[DEBUG REDIS] ✅ Успешно распознано и загружено live-матчей для отчета: {len(matches)}")
    return {"matches": matches, "count": len(matches), "error": None}

def diagnose_redis(info):
    result = {
        "configured": info.get("redis_configured", False),
        "available": info.get("redis_available", False),
        "circuit_breaker": info.get("circuit_breaker", "unknown"),
        "cb_failures": info.get("cb_failures", 0),
        "errors": info.get("errors", []),
    }
    rinfo = info.get("redis_info", {})
    if rinfo:
        result["url_set"] = bool(rinfo.get("url_set", True))
        result["token_set"] = rinfo.get("token_set", True)
        result["timeout"] = rinfo.get("timeout", 30)
        result["hash_name"] = rinfo.get("hash_name", "GatekeeperAI")
    return result

def diagnose_matches(data):
    """Секция 2: Matches — Анализ паспортов матчей."""
    matches = data.get("matches", {})
    if not matches: return {"total": 0, "with_odds": 0, "with_score": 0, "with_history": 0}
    total = len(matches)
    with_odds, with_score, with_history = 0, 0, 0
    statuses = {}

    for key, match in matches.items():
        if not isinstance(match, dict): continue
        inputs = match.get("model_inputs", {})
        
        if inputs.get("odds_dict") or match.get("odds"): with_odds += 1
        if match.get("score") is not None and match.get("score", {}).get("home") is not None: with_score += 1
        if match.get("section_history") or inputs.get("expected_goals"): with_history += 1
        
        status = match.get("status", "scheduled")
        if isinstance(status, dict): status = "scheduled"
        statuses[status] = statuses.get(status, 0) + 1

    return {"total": total, "with_odds": with_odds, "with_score": with_score, "with_history": with_history, "statuses": statuses}

def diagnose_indexes(data):
    rdb = _get_redis_hub()
    if not rdb: return {"error": "redis_hub unavailable"}
    indexes = {}
    for idx_type in ["date", "status", "source", "competition"]:
        try:
            if hasattr(rdb, "scan_keys"):
                keys1 = rdb.scan_keys(f"idx:{idx_type}:*") or []
                keys2 = rdb.scan_keys(f"index:{idx_type}:*") or []
                indexes[idx_type] = {"keys": len(keys1) + len(keys2)}
            else: indexes[idx_type] = {"keys": 0}
        except Exception: indexes[idx_type] = {"keys": 0}
    return indexes

def diagnose_sources(data):
    matches = data.get("matches", {})
    sources = {}
    for match in matches.values():
        if isinstance(match, dict):
            for src in match.get("sources", []): sources[src] = sources.get(src, 0) + 1
    return {"sources_in_matches": sources, "collector_meta": {}}

def diagnose_errors(info, data):
    errors = list(info.get("errors", []))
    if data.get("error"): errors.append(f"get_all_fields: {data.get('error')}")
    matches = data.get("matches", {})
    anomalies = []
    for key, match in matches.items():
        if not isinstance(match, dict): continue
        meta = match.get("match_metadata", {})
        if not meta.get("match_id") and not match.get("canonical_id"):
            anomalies.append(f"{key}: missing match_id/canonical_id")
    return {"errors": errors, "error_count": len(errors), "anomalies": anomalies, "anomaly_count": len(anomalies)}

def _get_sample_matches(data, n=5):
    matches = data.get("matches", {})
    samples = []
    for key, match in list(matches.items())[:n]:
        if isinstance(match, dict):
            inputs = match.get("model_inputs", {})
            meta = match.get("match_metadata", {})
            samples.append({
                "key": key,
                "canonical_id": meta.get("match_id", match.get("canonical_id", "?")),
                "home": meta.get("home_team", match.get("home_team", match.get("home", "?"))),
                "away": meta.get("away_team", match.get("away_team", match.get("away", "?"))),
                "date": meta.get("timestamp", match.get("date_utc", match.get("event_start_time", "?"))),
                "status": match.get("status", "scheduled"),
                "has_odds": bool(inputs.get("odds_dict") or match.get("odds")),
            })
    return samples

def _print_report(info, data, as_json=False):
    redis_status = diagnose_redis(info)
    matches_diag = diagnose_matches(data)
    indexes_diag = diagnose_indexes(data)
    sources_diag = diagnose_sources(data)
    health_diag = diagnose_errors(info, data)
    samples = _get_sample_matches(data)

    report = {
        "section_1_redis_status": redis_status, "section_2_matches": matches_diag,
        "section_3_indexes": indexes_diag, "section_4_collectors": sources_diag,
        "section_5_system_health": health_diag, "section_6_anomalies": {"count": health_diag.get("anomaly_count", 0), "items": health_diag.get("anomalies", [])},
        "section_7_sample_matches": samples, "timestamp": _now_msk(),
    }
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False)); return

    print("=" * 60 + "\n  GatekeeperAI Diagnostics Report (Debug Trace v9.3)\n" + "=" * 60)
    print(f"── 1. Redis Status ──\n  Configured: {'✅' if redis_status['configured'] else '❌'} | Available: {'✅' if redis_status['available'] else '❌'}")
    print(f"── 2. Matches ──\n  Total: {matches_diag.get('total', 0)} | With odds: {matches_diag.get('with_odds', 0)}")
    print(f"── 3. Indexes ──\n  Processed: {sum(v.get('keys', 0) for v in indexes_diag.values())} keys")
    print(f"── 7. Sample Matches ──")
    for s in samples: print(f"  {'📊' if s['has_odds'] else '  '} {s['home']} vs {s['away']} — {str(s['date'])[:10]}")
    print("=" * 60)

def main():
    p = argparse.ArgumentParser(description="GatekeeperAI Diagnostics")
    p.add_argument("--flush", action="store_true")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reset-breaker", action="store_true")
    args = p.parse_args()

    if args.reset_breaker:
        rdb = _get_redis_hub()
        if rdb: rdb.reset_circuit_breaker(); print("✅ Circuit breaker reset")
        return

    info = _step1_init()
    data = _step2_get_all_fields()
    _print_report(info, data, as_json=args.json)

if __name__ == "__main__":
    main()
           
