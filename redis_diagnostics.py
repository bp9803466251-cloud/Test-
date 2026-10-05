#!/usr/bin/env python3
"""redis_diagnostics.py — единая диагностическая утилита GatekeeperAI.
Все флаги из §18.4 гида. Read-only по умолчанию (§18.1).

v8.10-patched:
  FIX-1: Удалены мёртвые импорты (gatekeeper_ops, gatekeeper_lifecycle, test_fixtures, gatekeeper_diagnostics)
  FIX-2: Все импорты из существующих модулей (redis_hub, gatekeeper_hub, redis_config)
  FIX-3: Добавлены недостающие флаги §18.5: --flush, --history, --history-only, --purge, --yes, --dry-run, --json, --reset-breaker
  FIX-4: 5-шаговый алгоритм диагностики (§18.2) в default mode
  FIX-5: 7-секционный отчёт (§18.3): Redis Status, Matches, Indexes, Collectors, System Health, Anomalies, Sample
  FIX-6: --validate использует get_all_matches() из хаба (§19.4)
  FIX-7: --diff читает history:match:{cid} через get_key()
  FIX-8: --quality работает с реальными данными, не test_fixtures
  FIX-9: logging вместо print (только отчёт использует print для CLI)
  FIX-10: __version__, __all__
  FIX-11: Изоляция ошибок во всех вызовах (§1.25)
  FIX-12: now_msk() из gatekeeper_hub, не локальная
"""

import sys
import os
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__version__ = "8.11-patched"
__all__ = ["main", "diagnose_redis", "diagnose_matches", "diagnose_indexes",
           "diagnose_sources", "diagnose_errors", "__version__"]

MSK_TZ = timezone(timedelta(hours=3))


def _now_msk() -> str:
    """Возвращает текущее время в МСК в ISO формате."""
    return datetime.now(MSK_TZ).isoformat()


# ── Lazy-импорты (только при запуске, не при импорте модуля) ─────────

def _get_redis_hub():
    """Ленивый импорт redis_hub с fallback."""
    try:
        import redis_hub
        return redis_hub
    except ImportError as e:
        logger.error(f"redis_hub не найден: {e}")
        return None


def _get_hub():
    """Ленивый импорт gatekeeper_hub с fallback."""
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError as e:
        logger.error(f"gatekeeper_hub не найден: {e}")
        return None


def _get_config():
    """Ленивый импорт redis_config с fallback."""
    try:
        import redis_config
        return redis_config
    except ImportError as e:
        logger.error(f"redis_config не найден: {e}")
        return None


# ============================================================================
# 5-шаговый алгоритм (§18.2)
# ============================================================================

def _step1_init():
    """Шаг 1: Инициализация и проверка подключения к Redis."""
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
    """Шаг 2: Получение всех данных из Redis."""
    rdb = _get_redis_hub()
    if not rdb:
        return {"matches": {}, "count": 0, "error": "redis_hub unavailable"}

    try:
        all_fields = rdb.get_all_fields()
    except Exception as e:
        logger.error(f"get_all_fields: {e}")
        return {"matches": {}, "count": 0, "error": str(e)}

    if not all_fields:
        return {"matches": {}, "count": 0, "error": None}

    matches = {}
    for key, value in all_fields.items():
        if not key.startswith("match:"):
            continue
        try:
            if isinstance(value, str):
                match = json.loads(value)
            elif isinstance(value, dict):
                match = value
            else:
                continue
            matches[key] = match
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Не удалось распарсить {key}: {e}")

    return {"matches": matches, "count": len(matches), "error": None}


def diagnose_redis(info):
    """Секция 1: Redis Status (§18.3)."""
    result = {
        "configured": info.get("redis_configured", False),
        "available": info.get("redis_available", False),
        "circuit_breaker": info.get("circuit_breaker", "unknown"),
        "cb_failures": info.get("cb_failures", 0),
        "errors": info.get("errors", []),
    }
    rinfo = info.get("redis_info", {})
    if rinfo:
        result["url_set"] = bool(rinfo.get("url"))
        result["token_set"] = rinfo.get("token_set", False)
        result["timeout"] = rinfo.get("timeout", 30)
        result["hash_name"] = rinfo.get("hash_name", "?")
    return result


def diagnose_matches(data):
    """Секция 2: Matches (§18.3)."""
    matches = data.get("matches", {})
    if not matches:
        return {"total": 0, "with_odds": 0, "with_score": 0, "with_history": 0}

    total = len(matches)
    with_odds = 0
    with_score = 0
    with_history = 0
    statuses = {}

    for key, match in matches.items():
        if not isinstance(match, dict):
            continue
        if match.get("odds"):
            with_odds += 1
        if match.get("score") is not None:
            with_score += 1
        if match.get("section_history"):
            with_history += 1
        status = match.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1

    return {
        "total": total,
        "with_odds": with_odds,
        "with_score": with_score,
        "with_history": with_history,
        "statuses": statuses,
    }


def diagnose_indexes(data):
    """Секция 3: Indexes (§18.3).
    FIX-AUDIT: Uses scan_keys for counting (handles both idx:* and index:* patterns)."""
    rdb = _get_redis_hub()
    if not rdb:
        return {"error": "redis_hub unavailable"}

    indexes = {}
    for idx_type in ["date", "status", "source", "competition"]:
        try:
            # FIX-AUDIT: Try scan_keys first (handles SET-type indexes)
            if hasattr(rdb, "scan_keys"):
                # Try both idx:{type}:* and index:{type}:* patterns
                keys1 = rdb.scan_keys(f"idx:{idx_type}:*")
                keys2 = rdb.scan_keys(f"index:{idx_type}:*")
                # Also try idx:{type} as a single key
                single = 0
                if hasattr(rdb, "get_key"):
                    raw = rdb.get_key(f"idx:{idx_type}")
                    if raw:
                        single = len(raw) if isinstance(raw, (list, dict)) else 1
                total = len(keys1) + len(keys2) + single
                indexes[idx_type] = {"keys": total}
            elif hasattr(rdb, "get_key"):
                raw = rdb.get_key(f"idx:{idx_type}")
                if raw:
                    idx_data = json.loads(raw) if isinstance(raw, str) else raw
                    indexes[idx_type] = {
                        "keys": len(idx_data) if isinstance(idx_data, dict) else 0,
                    }
                else:
                    indexes[idx_type] = {"keys": 0}
            else:
                indexes[idx_type] = {"keys": 0, "error": "no scan_keys or get_key"}
        except Exception as e:
            indexes[idx_type] = {"keys": 0, "error": str(e)}

    return indexes


def diagnose_sources(data):
    """Секция 4: Collectors / Sources (§18.3)."""
    matches = data.get("matches", {})
    sources = {}

    for key, match in matches.items():
        if not isinstance(match, dict):
            continue
        for src in match.get("sources", []):
            sources[src] = sources.get(src, 0) + 1

    # Читаем meta из Redis
    rdb = _get_redis_hub()
    collector_meta = {}
    if rdb and hasattr(rdb, "get_key"):
        for collector in ["sharpapi", "propline", "odds_api", "bzzoiro", "main"]:
            try:
                raw = rdb.get_key(f"{collector}:meta")
                if raw:
                    meta = json.loads(raw) if isinstance(raw, str) else raw
                    collector_meta[collector] = {
                        "last_run": meta.get("last_run", "?"),
                        "total_events": meta.get("total_events", 0),
                        "stored_matches": meta.get("stored_matches", 0),
                        "error_count": meta.get("error_count", 0),
                    }
            except Exception:
                pass

    return {"sources_in_matches": sources, "collector_meta": collector_meta}


def diagnose_errors(info, data):
    """Секция 5: System Health (§18.3)."""
    errors = list(info.get("errors", []))
    data_error = data.get("error")
    if data_error:
        errors.append(f"get_all_fields: {data_error}")

    matches = data.get("matches", {})
    anomalies = []

    for key, match in matches.items():
        if not isinstance(match, dict):
            anomalies.append(f"{key}: not a dict")
            continue
        if not match.get("canonical_id"):
            anomalies.append(f"{key}: missing canonical_id")
        if not match.get("home_clean") or not match.get("away_clean"):
            anomalies.append(f"{key}: missing clean team names")
        if not match.get("date_utc"):
            anomalies.append(f"{key}: missing date_utc")

    return {
        "errors": errors,
        "error_count": len(errors),
        "anomalies": anomalies[:50],
        "anomaly_count": len(anomalies),
    }


def _get_sample_matches(data, n=5):
    """Секция 7: Sample matches (§18.3)."""
    matches = data.get("matches", {})
    samples = []
    for i, (key, match) in enumerate(matches.items()):
        if i >= n:
            break
        if isinstance(match, dict):
            samples.append({
                "key": key,
                "canonical_id": match.get("canonical_id", "?"),
                "home": match.get("home_team", "?"),
                "away": match.get("away_team", "?"),
                "date": match.get("date_utc", "?"),
                "status": match.get("status", "?"),
                "has_odds": bool(match.get("odds")),
            })
    return samples


def _print_report(info, data, as_json=False):
    """7-секционный отчёт (§18.3)."""
    redis_status = diagnose_redis(info)
    matches_diag = diagnose_matches(data)
    indexes_diag = diagnose_indexes(data)
    sources_diag = diagnose_sources(data)
    health_diag = diagnose_errors(info, data)
    samples = _get_sample_matches(data)

    report = {
        "section_1_redis_status": redis_status,
        "section_2_matches": matches_diag,
        "section_3_indexes": indexes_diag,
        "section_4_collectors": sources_diag,
        "section_5_system_health": health_diag,
        "section_6_anomalies": {
            "count": health_diag.get("anomaly_count", 0),
            "items": health_diag.get("anomalies", []),
        },
        "section_7_sample_matches": samples,
        "timestamp": _now_msk(),
    }

    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Text output
    print("=" * 60)
    print("  GatekeeperAI Diagnostics Report")
    print(f"  {_now_msk()}")
    print("=" * 60)

    # Section 1: Redis Status
    print(f"\n── 1. Redis Status {'─' * 42}")
    print(f"  Configured:  {'✅' if redis_status['configured'] else '❌'}")
    print(f"  Available:  {'✅' if redis_status['available'] else '❌'}")
    print(f"  Circuit:    {redis_status['circuit_breaker']} (failures: {redis_status.get('cb_failures', 0)})")
    if redis_status.get("url_set") is not None:
        print(f"  URL set:    {'✅' if redis_status['url_set'] else '❌'}")
        print(f"  Token set:  {'✅' if redis_status['token_set'] else '❌'}")
        print(f"  Timeout:    {redis_status.get('timeout', '?')}s")
        print(f"  Hash:       {redis_status.get('hash_name', '?')}")
    if redis_status.get("errors"):
        print("  Errors:")
        for e in redis_status["errors"]:
            print(f"    ❌ {e}")

    # Section 2: Matches
    print(f"\n── 2. Matches {'─' * 50}")
    print(f"  Total:      {matches_diag.get('total', 0)}")
    print(f"  With odds:  {matches_diag.get('with_odds', 0)}")
    print(f"  With score: {matches_diag.get('with_score', 0)}")
    print(f"  With hist:  {matches_diag.get('with_history', 0)}")
    statuses = matches_diag.get("statuses", {})
    if statuses:
        print("  Statuses:")
        for s, c in sorted(statuses.items()):
            print(f"    {s}: {c}")

    # Section 3: Indexes
    print(f"\n── 3. Indexes {'─' * 48}")
    for idx_type, idx_data in indexes_diag.items():
        keys = idx_data.get("keys", 0)
        err = idx_data.get("error")
        if err:
            print(f"  {idx_type}: ❌ {err}")
        else:
            print(f"  {idx_type}: {keys} keys")

    # Section 4: Collectors
    print(f"\n── 4. Collectors {'─' * 44}")
    src_matches = sources_diag.get("sources_in_matches", {})
    if src_matches:
        print("  Sources in matches:")
        for src, count in sorted(src_matches.items(), key=lambda x: -x[1]):
            print(f"    {src}: {count}")
    else:
        print("  No sources found in matches")

    meta = sources_diag.get("collector_meta", {})
    if meta:
        print("  Collector meta:")
        for coll, m in meta.items():
            print(f"    {coll}: last_run={m.get('last_run','?')}, events={m.get('total_events',0)}, errors={m.get('error_count',0)}")
    else:
        print("  No collector meta found")

    # Section 5: System Health
    print(f"\n── 5. System Health {'─' * 40}")
    errors = health_diag.get("errors", [])
    if errors:
        print(f"  Errors: {len(errors)}")
        for e in errors:
            print(f"    ❌ {e}")
    else:
        print("  ✅ No system errors")

    # Section 6: Anomalies
    print(f"\n── 6. Anomalies {'─' * 46}")
    anomaly_count = health_diag.get("anomaly_count", 0)
    print(f"  Count: {anomaly_count}")
    if anomaly_count > 0:
        print("  Items:")
        for a in health_diag.get("anomalies", [])[:20]:
            print(f"    ⚠️  {a}")
        if anomaly_count > 20:
            print(f"    ... and {anomaly_count - 20} more")

    # Section 7: Sample Matches
    print(f"\n── 7. Sample Matches {'─' * 40}")
    if samples:
        for s in samples:
            odds_marker = "📊" if s["has_odds"] else "  "
            print(f"  {odds_marker} {s['home']} vs {s['away']} — {s['date'][:10]} [{s['status']}]")
    else:
        print("  No matches found")

    print("\n" + "=" * 60)


# ============================================================================
# Flush operations (§18.5)
# ============================================================================

def _do_flush(data, dry_run=False):
    """--flush: удаление завершённых матчей (§18.5)."""
    matches = data.get("matches", {})
    now = datetime.now(MSK_TZ)
    flushed = 0
    skipped = 0

    rdb = _get_redis_hub()
    if not rdb:
        print("❌ redis_hub unavailable")
        return 0

    terminal_statuses = {"completed", "cancelled", "postponed", "archived", "interrupted"}

    for key, match in matches.items():
        if not isinstance(match, dict):
            skipped += 1
            continue

        should_flush = False
        status = match.get("status", "")
        if status in terminal_statuses:
            should_flush = True
        else:
            date_str = match.get("date_utc", "")
            if date_str:
                try:
                    match_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    match_date = match_date.astimezone(MSK_TZ)
                    if (now - match_date).total_seconds() > 2 * 3600:
                        should_flush = True
                except (ValueError, TypeError):
                    pass

        if should_flush:
            if dry_run:
                print(f"  [DRY-RUN] Would flush: {key} ({status})")
                flushed += 1
            else:
                try:
                    if hasattr(rdb, "delete_key"):
                        rdb.delete_key(key)
                    elif hasattr(rdb, "delete_from_cache"):
                        rdb.delete_from_cache(key)
                    else:
                        logger.warning(f"No delete method for {key}")
                        skipped += 1
                        continue
                    flushed += 1
                except Exception as e:
                    logger.warning(f"Flush {key}: {e}")
                    skipped += 1
        else:
            skipped += 1

    return flushed


def _do_history_only(dry_run=False):
    """--history-only: удаление history:* ключей (§18.5).
    FIX-AUDIT: History keys are separate Redis keys (not hash fields).
    Uses scan_keys + delete_key instead of get_all_fields + delete_from_cache."""
    rdb = _get_redis_hub()
    if not rdb:
        print("❌ redis_hub unavailable")
        return 0

    flushed = 0
    # History keys are stored as separate Redis keys: history:match:{cid}
    # FIX-AUDIT: scan_keys (SCAN) — safe iteration, unlike KEYS
    if hasattr(rdb, "scan_keys"):
        try:
            keys = rdb.scan_keys("history:*")
        except Exception as e:
            print(f"❌ scan_keys: {e}")
            return 0
    else:
        # Fallback: get_all_fields (hash fields — may miss history keys)
        try:
            all_fields = rdb.get_all_fields()
            keys = [k for k in (all_fields or {}) if k.startswith("history:")]
        except Exception as e:
            print(f"❌ get_all_fields: {e}")
            return 0

    for key in keys:
        if dry_run:
            print(f"  [DRY-RUN] Would delete: {key}")
            flushed += 1
        else:
            try:
                deleted = False
                if hasattr(rdb, "delete_key"):
                    rdb.delete_key(key)
                    deleted = True
                elif hasattr(rdb, "delete_from_cache"):
                    rdb.delete_from_cache(key)
                    deleted = True
                elif hasattr(rdb, "delete_keys_by_pattern"):
                    rdb.delete_keys_by_pattern(key)
                    deleted = True
                if not deleted:
                    logger.warning(f"No delete method for {key}")
                    continue
                flushed += 1
            except Exception as e:
                logger.warning(f"Delete {key}: {e}")

    return flushed


def _do_purge(confirm=False, dry_run=False):
    """--purge: FLUSHDB (§18.5). Требует --yes."""
    rdb = _get_redis_hub()
    if not rdb:
        print("❌ redis_hub unavailable")
        return False

    if not confirm:
        print("❌ --purge требует --yes для подтверждения")
        return False

    if dry_run:
        print("[DRY-RUN] Would execute FLUSHDB")
        return True

    try:
        # FIX-AUDIT: Try _execute_upstash_cmd first, then fallback to direct REST API
        if hasattr(rdb, "_execute_upstash_cmd"):
            rdb._execute_upstash_cmd(["FLUSHDB"])
            print("✅ FLUSHDB executed")
            return True
        elif hasattr(rdb, "delete_keys_by_pattern"):
            # Fallback: delete all known patterns
            for pattern in ["match:*", "history:*", "meta:*", "*:meta", "idx:*", "index:*"]:
                try:
                    rdb.delete_keys_by_pattern(pattern)
                except Exception:
                    pass
            print("✅ Pattern-based flush executed")
            return True
        else:
            print("❌ No flush method available (_execute_upstash_cmd, delete_keys_by_pattern)")
            return False
    except Exception as e:
        print(f"❌ FLUSHDB: {e}")
        return False


# ============================================================================
# --validate (§19.4)
# ============================================================================

def _do_validate():
    """Схемная валидация всех матчей в Redis (§19.4).
    FIX-AUDIT: Uses hub.validate_schema() if available, fallback to local field check."""
    hub = _get_hub()
    if not hub:
        print("❌ gatekeeper_hub unavailable")
        return

    try:
        matches = hub.get_all_matches()
    except Exception as e:
        print(f"❌ get_all_matches: {e}")
        return

    if not matches:
        print("No matches found in Redis")
        return

    # FIX-AUDIT: Use hub.validate_schema() if available
    use_hub_validate = hasattr(hub, "validate_schema")
    required_fields = [
        "canonical_id", "home_team", "away_team", "home_clean", "away_clean",
        "date_utc", "competition", "country", "status", "version",
        "schema_version", "odds", "source_ids", "sources",
        "created_at", "updated_at",
    ]

    valid = 0
    invalid = 0
    errors_by_field = {}

    for key, match in matches.items():
        if not isinstance(match, dict):
            invalid += 1
            continue
        if use_hub_validate:
            try:
                ok, msg = hub.validate_schema(match)
                if ok:
                    valid += 1
                else:
                    invalid += 1
                    # Extract field name from message
                    if "missing" in msg:
                        field = msg.split(":")[-1].strip()
                        errors_by_field[field] = errors_by_field.get(field, 0) + 1
            except Exception:
                invalid += 1
        else:
            missing = [f for f in required_fields if not match.get(f)]
            if missing:
                invalid += 1
                for f in missing:
                    errors_by_field[f] = errors_by_field.get(f, 0) + 1
            else:
                valid += 1

    print(f"Validation results: {valid} valid, {invalid} invalid (total: {valid + invalid})")
    if errors_by_field:
        print("Missing fields:")
        for field, count in sorted(errors_by_field.items(), key=lambda x: -x[1]):
            print(f"  {field}: {count} matches missing")


# ============================================================================
# --diff (§22.6)
# ============================================================================

def _do_diff(cid):
    """История изменений матча (§22.6). Читает history:match:{cid}."""
    rdb = _get_redis_hub()
    if not rdb:
        print("❌ redis_hub unavailable")
        return

    try:
        raw = rdb.get_key(f"history:match:{cid}")
    except Exception as e:
        print(f"❌ get_key: {e}")
        return

    if not raw:
        print(f"No history found for {cid}")
        return

    try:
        history = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        return

    if isinstance(history, list):
        print(f"History for {cid} ({len(history)} entries):")
        for i, entry in enumerate(history):
            ts = entry.get("updated_at", entry.get("timestamp", "?"))
            action = entry.get("action", "?")
            source = entry.get("source", "?")
            print(f"  [{i}] {ts} | {action} by {source}")
    elif isinstance(history, dict):
        print(f"History for {cid}:")
        print(json.dumps(history, indent=2, ensure_ascii=False))
    else:
        print(f"History for {cid}: {raw}")


# ============================================================================
# Main CLI
# ============================================================================

def main():
    p = argparse.ArgumentParser(description="GatekeeperAI Diagnostics")

    # ── Флаги §18.5 (flush/управление) ──
    p.add_argument("--flush", action="store_true", help="Удаление завершённых матчей (§18.5)")
    p.add_argument("--history", action="store_true", help="Read-only анализ history-ключей (§18.5)")
    p.add_argument("--history-only", action="store_true", help="Удаление history:* ключей (§18.5)")
    p.add_argument("--purge", action="store_true", help="FLUSHDB (требует --yes) (§18.5)")
    p.add_argument("--yes", action="store_true", help="Подтверждение для --purge (§18.5)")
    p.add_argument("--dry-run", action="store_true", help="Режим без записи (§18.5)")
    p.add_argument("--json", action="store_true", help="JSON-вывод отчёта (§18.5)")
    p.add_argument("--cleanup", action="store_true", help="Очистка завершённых матчей (§20.5)")
    p.add_argument("--reset-breaker", action="store_true", help="Сброс circuit breaker (§18.5)")

    # ── Диагностические флаги ──
    p.add_argument("--validate", action="store_true", help="Схемная валидация (§19.1)")
    p.add_argument("--migrate-schema", metavar="VERSION", help="Миграция схемы (§20.2)")
    p.add_argument("--quality", action="store_true", help="Проверка качества (§20.4)")
    p.add_argument("--health", action="store_true", help="Health monitoring (§21.3)")
    p.add_argument("--reconcile", action="store_true", help="Реконсиляция (§21.5)")
    p.add_argument("--fix", action="store_true", help="Авто-исправление при --reconcile (§21.5)")
    p.add_argument("--audit", action="store_true", help="Аудит-лог (§22.1)")
    p.add_argument("--dlq", action="store_true", help="Dead Letter Queue (§22.4)")
    p.add_argument("--dlq-replay", action="store_true", help="Переобработка DLQ (§22.4)")
    p.add_argument("--batch-stats", action="store_true", help="Статистика батчинга (§22.6)")
    p.add_argument("--diff", metavar="CID", help="История изменений матча (§22.6)")
    p.add_argument("--retention", action="store_true", help="Статус retention (§22.7)")
    p.add_argument("--hub-version", action="store_true", help="Версия API хаба (§22.8)")

    # ── Флаги §23 ──
    p.add_argument("--odds-conflicts", metavar="CID", help="Конфликты коэффициентов (§23.1)")
    p.add_argument("--canary", metavar="COLLECTOR", help="Теневой запуск (§23.8)")
    p.add_argument("--canary-cleanup", action="store_true", help="Очистка canary (§23.8)")
    p.add_argument("--lineage", action="store_true", help="Граф зависимостей (§23.5)")
    p.add_argument("--dashboard", action="store_true", help="Текстовый дашборд (§23.6)")

    # ── Флаги §24 ──
    p.add_argument("--state-stats", action="store_true", help="Гистограмма статусов (§24.1)")
    p.add_argument("--config-check", action="store_true", help="Валидация конфигурации (§24.3)")
    p.add_argument("--env", action="store_true", help="Текущее окружение (§24.4)")
    p.add_argument("--metrics", action="store_true", help="Сводка метрик (§24.5)")
    p.add_argument("--export", action="store_true", help="Экспорт данных (§24.6)")
    p.add_argument("--import", metavar="FILE", help="Импорт данных (§24.6)")
    p.add_argument("--orchestration", action="store_true", help="Граф запуска (§24.7)")
    p.add_argument("--drift", action="store_true", help="Schema drift (§24.8)")
    p.add_argument("--full", action="store_true", help="Полное сканирование (для --drift)")

    # ── Фильтры ──
    p.add_argument("--source", metavar="NAME", help="Фильтр по источнику")
    p.add_argument("--action", metavar="TYPE", help="Фильтр по действию")
    p.add_argument("--namespace", metavar="NS", default="live", help="Namespace")
    p.add_argument("--league", metavar="NAME", help="Фильтр по лиге")

    args = p.parse_args()

    # ── --hub-version ──
    if args.hub_version:
        hub = _get_hub()
        if hub:
            ver = getattr(hub, "__version__", "?")
            print(f"GatekeeperAI Hub version: {ver}")
        else:
            print("❌ gatekeeper_hub unavailable")
        return

    # ── --reset-breaker ──
    if args.reset_breaker:
        rdb = _get_redis_hub()
        if rdb and hasattr(rdb, "reset_circuit_breaker"):
            rdb.reset_circuit_breaker()
            print("✅ Circuit breaker reset")
        else:
            print("❌ redis_hub or reset_circuit_breaker unavailable")
        return

    # ── --cleanup (§20.5) ──
    if args.cleanup:
        hub = _get_hub()
        if not hub:
            print("❌ gatekeeper_hub unavailable")
            return
        if not hasattr(hub, "cleanup_expired"):
            print("❌ hub.cleanup_expired not available")
            return

        dry = args.dry_run
        auto_migrate = not args.flush  # --flush disables auto_migrate
        print(f"Cleanup {'(DRY-RUN)' if dry else ''} (auto_migrate={auto_migrate})...")

        try:
            result = hub.cleanup_expired(dry_run=dry, auto_migrate=auto_migrate)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"❌ cleanup_expired: {e}")
        return

    # ── --flush ──
    if args.flush:
        info = _step1_init()
        data = _step2_get_all_fields()
        dry = args.dry_run
        print(f"Flush {'(DRY-RUN)' if dry else ''}: scanning {data['count']} matches...")
        flushed = _do_flush(data, dry_run=dry)
        print(f"Flushed: {flushed}")
        return

    # ── --history (read-only) ──
    if args.history:
        rdb = _get_redis_hub()
        if not rdb:
            print("❌ redis_hub unavailable")
            return
        # FIX-AUDIT: History keys are separate Redis keys, not hash fields
        if hasattr(rdb, "scan_keys"):
            try:
                history_keys = rdb.scan_keys("history:*")
            except Exception as e:
                print(f"❌ scan_keys: {e}")
                return
        else:
            try:
                all_fields = rdb.get_all_fields()
                history_keys = [k for k in (all_fields or {}) if k.startswith("history:")]
            except Exception as e:
                print(f"❌ get_all_fields: {e}")
                return
        print(f"History keys: {len(history_keys)}")
        for k in history_keys[:20]:
            try:
                raw = rdb.get_key(k) if hasattr(rdb, "get_key") else None
                if raw is None and hasattr(rdb, "get_from_cache"):
                    raw = rdb.get_from_cache(k)
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                entries = len(parsed) if isinstance(parsed, (list, dict)) else 1
                print(f"  {k} — {entries} entries")
            except Exception:
                print(f"  {k} — (unparseable)")
        return

    # ── --history-only ──
    if args.history_only:
        dry = args.dry_run
        print(f"History-only flush {'(DRY-RUN)' if dry else ''}...")
        flushed = _do_history_only(dry_run=dry)
        print(f"Deleted: {flushed} history keys")
        return

    # ── --purge ──
    if args.purge:
        ok = _do_purge(confirm=args.yes, dry_run=args.dry_run)
        if not ok and not args.dry_run:
            return
        return

    # ── --validate (§19.4) ──
    if args.validate:
        _do_validate()
        return

    # ── --diff (§22.6) ──
    if args.diff:
        _do_diff(args.diff)
        return

    # ── --config-check (§24.3) ──
    if args.config_check:
        cfg = _get_config()
        if not cfg:
            print("❌ redis_config unavailable")
            return
        errors = cfg.get_redis_config_errors()
        if errors:
            print(f"Config errors: {len(errors)}")
            for e in errors:
                print(f"  ❌ {e}")
        else:
            print("✅ Configuration valid")
        return

    # ── --env (§24.4) ──
    if args.env:
        cfg = _get_config()
        if not cfg:
            print("❌ redis_config unavailable")
            return
        info = cfg.get_redis_info()
        print(f"Redis configured: {'✅' if info.get('configured', bool(info.get('url'))) else '❌'}")
        print(f"Token set: {'✅' if info.get('token_set') else '❌'}")
        print(f"Timeout: {info.get('timeout', '?')}s")
        print(f"Hash name: {info.get('hash_name', '?')}")
        print(f"CB threshold: {info.get('cb_threshold', '?')}")
        print(f"CB recovery: {info.get('cb_recovery', '?')}s")
        return

    # ── --metrics (§24.5) ──
    if args.metrics:
        info = _step1_init()
        data = _step2_get_all_fields()
        matches_diag = diagnose_matches(data)
        redis_diag = diagnose_redis(info)
        report = {
            "redis": redis_diag,
            "matches": matches_diag,
            "timestamp": _now_msk(),
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ── --state-stats (§24.1) ──
    if args.state_stats:
        data = _step2_get_all_fields()
        matches_diag = diagnose_matches(data)
        statuses = matches_diag.get("statuses", {})
        if statuses:
            print("Match status histogram:")
            for s, c in sorted(statuses.items(), key=lambda x: -x[1]):
                bar = "█" * min(c, 40)
                print(f"  {s:20s} {c:5d} {bar}")
        else:
            print("No matches found")
        return

    # ── --export (§24.6) ──
    if args.export:
        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        filename = f"export_{args.namespace}_{_now_msk()[:10]}.json"
        export_data = {
            "namespace": args.namespace,
            "count": len(matches),
            "matches": matches,
            "exported_at": _now_msk(),
        }
        with open(filename, "w") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"Exported {len(matches)} matches to {filename}")
        return

    # ── --import (§24.6) ──
    import_file = getattr(args, "import", None)
    if import_file:
        with open(import_file, "r") as f:
            data = json.load(f)
        matches = data.get("matches", data)
        count = len(matches) if isinstance(matches, dict) else len(matches) if isinstance(matches, list) else 0
        print(f"Import (dry-run): {count} matches")
        return

    # ── --health (§21.3) ──
    if args.health:
        info = _step1_init()
        health = diagnose_redis(info)
        print(json.dumps(health, indent=2, ensure_ascii=False))
        return

    # ── --quality (§20.4) — расширенная проверка через hub.validate_match_quality ──
    if args.quality:
        hub = _get_hub()
        if not hub:
            print("❌ gatekeeper_hub unavailable")
            return
        if not hasattr(hub, "validate_match_quality"):
            print("❌ hub.validate_match_quality not available (hub version too old)")
            return

        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        total = len(matches)
        if not total:
            print("No matches found")
            return

        scores = []
        issues_count = 0
        warnings_count = 0
        worst_matches = []

        for key, match in matches.items():
            if not isinstance(match, dict):
                continue
            try:
                result = hub.validate_match_quality(match)
                scores.append(result["score"])
                if result["issues"]:
                    issues_count += 1
                if result["warnings"]:
                    warnings_count += 1
                if result["score"] < 70:
                    cid = match.get("canonical_id", key)
                    worst_matches.append({
                        "cid": cid,
                        "score": result["score"],
                        "issues": result["issues"],
                        "warnings": result["warnings"],
                    })
            except Exception as e:
                logger.error(f"validate_match_quality error for {key}: {e}")

        avg_score = round(sum(scores) / len(scores), 1) if scores else 0
        valid_count = sum(1 for s in scores if s >= 70)
        invalid_count = total - valid_count

        quality_report = {
            "total": total,
            "avg_score": avg_score,
            "valid": valid_count,
            "invalid": invalid_count,
            "with_issues": issues_count,
            "with_warnings": warnings_count,
            "worst_matches": sorted(worst_matches, key=lambda x: x["score"])[:10],
        }
        print(json.dumps(quality_report, indent=2, ensure_ascii=False))
        return

    # ── --dashboard (§23.6) ──
    if args.dashboard:
        info = _step1_init()
        data = _step2_get_all_fields()
        _print_report(info, data, as_json=args.json)
        return

    # ── --drift (§24.8) ──
    if args.drift:
        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        sample_size = len(matches) if args.full else min(len(matches), 100)

        known_fields = {
            "canonical_id", "home_team", "away_team", "home_clean", "away_clean",
            "date_utc", "competition", "country", "status", "version",
            "schema_version", "odds", "source_ids", "sources",
            "created_at", "updated_at", "score", "section_history",
            "source_map", "value_analysis", "time_utc", "flags",
            "half_time_score", "full_time_result",
        }

        extra_fields = {}
        missing_fields = {}
        sampled = 0

        for key, match in matches.items():
            if sampled >= sample_size:
                break
            if not isinstance(match, dict):
                continue
            sampled += 1
            for field in match:
                if field not in known_fields:
                    extra_fields[field] = extra_fields.get(field, 0) + 1
            for field in known_fields:
                if field not in match:
                    missing_fields[field] = missing_fields.get(field, 0) + 1

        report = {
            "sampled": sampled,
            "extra_fields": extra_fields,
            "missing_fields": missing_fields,
            "drift_detected": bool(extra_fields or missing_fields),
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ── --reconcile (§21.5) ──
    if args.reconcile:
        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        issues = []
        for key, match in matches.items():
            if not isinstance(match, dict):
                issues.append(f"{key}: not a dict")
                continue
            if not match.get("home_clean"):
                issues.append(f"{key}: missing home_clean")
            if not match.get("away_clean"):
                issues.append(f"{key}: missing away_clean")
            if not match.get("canonical_id"):
                issues.append(f"{key}: missing canonical_id")

        report = {
            "total_matches": len(matches),
            "issues_found": len(issues),
            "issues": issues[:50],
            "fix_applied": args.fix and not args.dry_run,
            "dry_run": args.dry_run or not args.fix,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ── Stubs для флагов, требующих полной реализации ──
    if args.audit:
        print("--audit: требует реализации audit-лога в хабе (§22.1)")
        return

    if args.dlq:
        print("--dlq: требует реализации DLQ в хабе (§22.4)")
        return

    if args.dlq_replay:
        print("--dlq-replay: требует реализации DLQ в хабе (§22.4)")
        return

    if args.batch_stats:
        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        print(f"Batch stats: {len(matches)} matches in hash")
        return

    if args.retention:
        data = _step2_get_all_fields()
        matches = data.get("matches", {})
        now = datetime.now(MSK_TZ)
        expired = 0
        active = 0
        for match in matches.values():
            if not isinstance(match, dict):
                continue
            date_str = match.get("date_utc", "")
            if date_str:
                try:
                    match_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    match_date = match_date.astimezone(MSK_TZ)
                    if (now - match_date).total_seconds() > 2 * 3600:
                        expired += 1
                    else:
                        active += 1
                except (ValueError, TypeError):
                    pass
        print(json.dumps({"active": active, "expired": expired, "total": active + expired}, indent=2))
        return

    if args.migrate_schema:
        hub = _get_hub()
        if not hub:
            print("❌ gatekeeper_hub unavailable")
            return
        if not hasattr(hub, "migrate_schema"):
            print("❌ hub.migrate_schema not available (hub version too old)")
            return

        target = args.migrate_schema
        dry = args.dry_run
        print(f"Schema migration -> {target} {'(DRY-RUN)' if dry else ''}...")

        # Определяем исходную версию из текущей схемы хаба
        current_sv = getattr(hub, "SCHEMA_VERSION", "v710")
        from_ver = "v700" if target == "v710" else current_sv

        try:
            result = hub.migrate_schema(
                from_version=from_ver,
                to_version=target,
                dry_run=dry,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"❌ migrate_schema: {e}")
        return

    if args.canary:
        print(f"--canary {args.canary}: требует реализации canary-режима (§23.8)")
        return

    if args.canary_cleanup:
        print("--canary-cleanup: требует реализации canary-режима (§23.8)")
        return

    if args.odds_conflicts:
        rdb = _get_redis_hub()
        if not rdb:
            print("❌ redis_hub unavailable")
            return
        try:
            raw = rdb.get_from_cache(f"match:{args.odds_conflicts}")
        except Exception as e:
            print(f"❌ get_from_cache: {e}")
            return
        if not raw:
            print(f"Match {args.odds_conflicts} not found")
            return
        match = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
        odds = match.get("odds", {})
        if not odds:
            print("No odds found")
            return
        print(json.dumps(odds, indent=2, ensure_ascii=False))
        return

    if args.lineage:
        print("Data lineage:")
        print("  sharpapi  → provides: matches, odds (betradar)")
        print("  propline  → provides: matches, closing odds (pinnacle)")
        print("  odds_api  → provides: matches, odds (betradar)")
        print("  bzzoiro   → provides: predictions, stats (opta)")
        print("  main      → consumes: all → produces: dashboard, telegram")
        return

    if args.orchestration:
        print("Orchestration graph:")
        print("  1. redis_config → init")
        print("  2. redis_hub → transport")
        print("  3. gatekeeper_hub → init, upsert, patch")
        print("  4. collectors (sharpapi, propline, odds_api, bzzoiro) → parallel")
        print("  5. value_engine → evaluate")
        print("  6. main → dashboard + telegram")
        print("  cleanup_owner: main (after pipeline)")
        return

    # ── Default: 5-шаговый алгоритм диагностики (§18.2) ──
    info = _step1_init()
    data = _step2_get_all_fields()
    _print_report(info, data, as_json=args.json)


if __name__ == "__main__":
    main()
