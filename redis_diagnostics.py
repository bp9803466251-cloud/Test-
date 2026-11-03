#!/usr/bin/env python3
# redis_diagnostics.py
"""
Gatekeeper-AI Ecosystem: Единая диагностическая утилита распределенного кэша.
Версия 9.6.8-patched — Синхронизация со структурами CORE ENGINE V6.7 (V5.7.12).
Read-only по умолчанию (§18.1), реализует 7-секционный отчет (§18.3) с расширенным дебагом ключей.

[9.6.8 FIX] diagnose_indexes: вызов несуществующего в v710 scan_keys заменен на прямой
            SCAN через _execute_upstash_cmd (как в redis_flush 9.6.6). Прежняя версия
            молча отчетила 0 ключей индексов при полной базе.
[9.6.8 FIX] diagnose_matches/sources/errors/sample: изоляция сбоев per-match и
            null-безопасность (model_inputs: null, sources: null, match_metadata: null
            раньше роняли TypeError и глотали секцию целиком).
[9.6.8 FIX] Контракт v710: детекция котировок учитывает плоский odds_1x2.
"""
import sys
import os
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__version__ = "9.6.8-patched"
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
    except ImportError:
        return None


def _get_hub():
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError:
        return None


def _get_config():
    try:
        import redis_config
        return redis_config
    except ImportError:
        return None


# ── [9.6.8] Нативный SCAN по паттерну (замена удаленного scan_keys) ──
def _scan_by_pattern(pattern):
    """Прямой SCAN через _execute_upstash_cmd с итерацией по курсору.
    Прежний вызов несуществующего rdb.scan_keys молча возвращал пустой список."""
    rdb = _get_redis_hub()
    keys = []
    if not rdb:
        return keys
    cmd_fn = getattr(rdb, "_execute_upstash_cmd", None)
    if cmd_fn is None:
        logger.warning("SCAN(%s): _execute_upstash_cmd недоступен в redis_hub", pattern)
        return keys
    cursor = "0"
    try:
        while True:
            raw = cmd_fn(["SCAN", cursor, "MATCH", pattern, "COUNT", "250"])
            if not isinstance(raw, list) or len(raw) < 2:
                break
            next_cursor, found = raw[0], raw[1]
            if isinstance(next_cursor, bytes):
                next_cursor = next_cursor.decode("utf-8")
            cursor = str(next_cursor)
            if found and isinstance(found, list):
                keys.extend(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in found)
            if cursor == "0":
                break
    except Exception as e:
        logger.warning("SCAN(%s) завершился с ошибкой: %s", pattern, e)
    return keys


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
        if cfg_errors:
            info["errors"].extend(cfg_errors)
        info["redis_info"] = cfg.get_redis_info()
    if hub:
        try:
            init = hub.run_initialization(collector="redis_diagnostics")
            info["redis_available"] = init.get("redis_available", False)
        except Exception as e:
            info["errors"].append(f"run_initialization: {e}")
    rdb = _get_redis_hub()
    if rdb:
        try:
            cb = rdb.get_circuit_breaker_status()
            info["circuit_breaker"] = cb.get("state", "unknown")
            info["cb_failures"] = cb.get("failures", 0)
        except Exception as e:
            info["errors"].append(f"circuit_breaker: {e}")
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

            print("[DEBUG REDIS] 📊 Статистика ключей:")
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
        if not (key.startswith("match:") or "__" in key):
            continue
        try:
            match = value if isinstance(value, dict) else json.loads(value)
            if isinstance(match, dict):
                fixed_key = key if key.startswith("match:") else f"match:{key}"
                matches[fixed_key] = match
        except Exception as err:
            print(f"[DEBUG REDIS] ⚠️ Ошибка JSON-декодирования для ключа {key}: {err}")

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
    """Секция 2: Matches — Анализ паспортов матчей.
    [9.6.8 FIX] Изоляция сбоев per-match: одно null-поле больше не глотает секцию."""
    matches = data.get("matches", {})
    if not matches:
        return {"total": 0, "with_odds": 0, "with_score": 0, "with_history": 0}
    total = len(matches)
    with_odds, with_score, with_history = 0, 0, 0
    statuses = {}

    for key, match in matches.items():
        if not isinstance(match, dict):
            continue
        try:
            # [9.6.8 FIX] model_inputs: null раньше ронял inputs.get() на TypeError
            inputs = match.get("model_inputs")
            if not isinstance(inputs, dict):
                inputs = {}

            # [9.6.8 FIX] Контракт v710: детекция котировок учитывает плоский odds_1x2
            flat_odds = match.get("odds_1x2")
            has_odds = bool(
                inputs.get("odds_dict")
                or isinstance(match.get("odds"), dict) and match.get("odds")
                or isinstance(flat_odds, dict) and flat_odds
            )
            if has_odds:
                with_odds += 1

            score = match.get("score")
            if isinstance(score, dict) and score.get("home") is not None:
                with_score += 1

            if match.get("section_history") or inputs.get("expected_goals"):
                with_history += 1

            status = match.get("status", "scheduled")
            if not isinstance(status, str):
                status = "scheduled"
            statuses[status] = statuses.get(status, 0) + 1
        except Exception as e:
            logger.warning("diagnose_matches(%s): %s", key, e)

    return {"total": total, "with_odds": with_odds, "with_score": with_score,
            "with_history": with_history, "statuses": statuses}


def diagnose_indexes(data):
    """Секция 3: Индексы — прямой SCAN вместо несуществующего scan_keys (9.6.8 FIX)."""
    indexes = {}
    for idx_type in ["date", "status", "source", "competition"]:
        keys1 = _scan_by_pattern(f"idx:{idx_type}:*")
        keys2 = _scan_by_pattern(f"index:{idx_type}:*")
        indexes[idx_type] = {"keys": len(keys1) + len(keys2)}
    return indexes


def diagnose_sources(data):
    """Секция 4: Источники. [9.6.8 FIX] sources: null больше не роняет итерацию."""
    matches = data.get("matches", {})
    sources = {}
    for match in matches.values():
        if not isinstance(match, dict):
            continue
        srcs = match.get("sources")
        if not isinstance(srcs, list):
            continue
        for src in srcs:
            if isinstance(src, str):
                sources[src] = sources.get(src, 0) + 1
    return {"sources_in_matches": sources, "collector_meta": {}}


def diagnose_errors(info, data):
    """Секция 5: Системное здоровье. [9.6.8 FIX] match_metadata: null безопасен,
    сбои per-match изолированы."""
    errors = list(info.get("errors", []))
    if data.get("error"):
        errors.append(f"get_all_fields: {data.get('error')}")
    matches = data.get("matches", {})
    anomalies = []
    for key, match in matches.items():
        if not isinstance(match, dict):
            continue
        try:
            meta = match.get("match_metadata")
            if not isinstance(meta, dict):
                meta = {}
            if not meta.get("match_id") and not match.get("canonical_id"):
                anomalies.append(f"{key}: missing match_id/canonical_id")
        except Exception as e:
            logger.warning("diagnose_errors(%s): %s", key, e)
    return {"errors": errors, "error_count": len(errors),
            "anomalies": anomalies, "anomaly_count": len(anomalies)}


def _get_sample_matches(data, n=5):
    """Секция 7: сэмплы. [9.6.8 FIX] null-безопасность всех полей."""
    matches = data.get("matches", {})
    samples = []
    for key, match in list(matches.items())[:n]:
        if not isinstance(match, dict):
            continue
        try:
            inputs = match.get("model_inputs")
            if not isinstance(inputs, dict):
                inputs = {}
            meta = match.get("match_metadata")
            if not isinstance(meta, dict):
                meta = {}
            flat_odds = match.get("odds_1x2")
            samples.append({
                "key": key,
                "canonical_id": meta.get("match_id", match.get("canonical_id", "?")),
                "home": meta.get("home_team", match.get("home_team", match.get("home", "?"))),
                "away": meta.get("away_team", match.get("away_team", match.get("away", "?"))),
                "date": meta.get("timestamp", match.get("date_utc", match.get("event_start_time", "?"))),
                "status": match.get("status", "scheduled"),
                "has_odds": bool(inputs.get("odds_dict") or match.get("odds") or flat_odds),
            })
        except Exception as e:
            logger.warning("sample(%s): %s", key, e)
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
        "section_5_system_health": health_diag,
        "section_6_anomalies": {"count": health_diag.get("anomaly_count", 0),
                                "items": health_diag.get("anomalies", [])},
        "section_7_sample_matches": samples, "timestamp": _now_msk(),
    }
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    print("=" * 60)
    print(f"📊 РЕДИС ДИАГНОСТИКА — GatekeeperAI v{__version__}")
    print(f"   Отчет сгенерирован: {_now_msk()}")
    print("=" * 60)

    print("\n[СЕКЦИЯ 1] Статус Redis:")
    for k, v in redis_status.items():
        print(f"  • {k}: {v}")

    print("\n[СЕКЦИЯ 2] Матчи:")
    for k, v in matches_diag.items():
        print(f"  • {k}: {v}")

    print("\n[СЕКЦИЯ 3] Индексы:")
    for idx_type, stat in indexes_diag.items():
        print(f"  • {idx_type}: {stat.get('keys', 0)} ключей")

    print("\n[СЕКЦИЯ 4] Источники в матчах:")
    if sources_diag.get("sources_in_matches"):
        for src, cnt in sources_diag["sources_in_matches"].items():
            print(f"  • {src}: {cnt}")
    else:
        print("  • (нет данных)")

    print("\n[СЕКЦИЯ 5] Системное здоровье:")
    print(f"  • Ошибок: {health_diag.get('error_count', 0)}")
    for e in health_diag.get("errors", []):
        print(f"    - {e}")

    print("\n[СЕКЦИЯ 6] Аномалии:")
    print(f"  • Всего: {health_diag.get('anomaly_count', 0)}")
    for a in health_diag.get("anomalies", [])[:10]:
        print(f"    - {a}")

    print("\n[СЕКЦИЯ 7] Сэмплы матчей:")
    if samples:
        for s in samples:
            print(f"  • {s.get('canonical_id')}: {s.get('home')} vs {s.get('away')} "
                  f"| {s.get('date')} | {s.get('status')} | odds={s.get('has_odds')}")
    else:
        print("  • (нет данных)")

    print("\n" + "=" * 60)
    print("✅ Диагностика завершена (read-only, §18.1)")


def main():
    parser = argparse.ArgumentParser(
        description="Gatekeeper-AI: единая диагностическая утилита распределенного кэша (§18)"
    )
    parser.add_argument("--json", action="store_true", help="Вывести отчет в JSON формате")
    args = parser.parse_args()

    print("[DIAG] Шаг 1/5: инициализация и статус Redis...")
    info = _step1_init()

    print("[DIAG] Шаг 2/5: сбор полей хэша...")
    data = _step2_get_all_fields()

    _print_report(info, data, as_json=args.json)


if __name__ == "__main__":
    main()
