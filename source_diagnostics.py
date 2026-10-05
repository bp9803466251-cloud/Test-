# source_diagnostics.py
"""
Единый модуль диагностики Gatekeeper-AI v8.11-patched.
Заменяет debug_inspect.py и debug_odds.py.
Объединяет диагностику всех источников, Redis и матчей в одном файле.

v8.11-patched:
  FIX-1: SHARP_API_KEY → SHARPAPI_API_KEY (§1.8 — SHARPAPI_ префикс)
  FIX-2: API_FOOTBALL_KEY → FOOTBALL_DATA_API_KEY (синхрон с workflow env)
  FIX-3: FOOTBALL_DATA_TOKEN → FOOTBALL_DATA_API_KEY (синхрон с workflow env)
  FIX-4: Добавлен logging.getLogger + NullHandler (§20.5)
  FIX-5: __version__ + __all__
  FIX-6: run_initialization(collector="source_diagnostics") (§1.7a)
  FIX-7: get_from_cache импортирован из hub, не redis_hub (§1.4)
  FIX-8: Error isolation — try/except на все Redis-вызовы (§1.25)
  FIX-9: system_keys классификация — endswith(":meta") для {collector}:meta

Запуск:
  python source_diagnostics.py            — полная диагностика
  python source_diagnostics.py --sources  — только источники
  python source_diagnostics.py --redis    — только Redis
  python source_diagnostics.py --matches  — только матчи
  python source_diagnostics.py --errors   — только ошибки
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

__version__ = "8.11-patched"

__all__ = [
    "diagnose_sources",
    "diagnose_redis",
    "diagnose_matches",
    "diagnose_errors",
    "print_summary",
    "main",
    "__version__",
]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

from gatekeeper_hub import (
    run_initialization,
    get_matches_by_date_range,
    get_all_matches,
    get_from_cache,  # FIX-7: из хаба, не redis_hub (§1.4)
)
# get_all_fields и is_redis_available — инфраструктурные функции,
# недоступные через хаб. Доступ только для чтения (read-only diagnostics).
from redis_hub import get_all_fields, is_redis_available

MSK_TIMEZONE = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# 1. Диагностика источников
# ---------------------------------------------------------------------------
def diagnose_sources() -> dict:
    print("\n" + "=" * 60)
    print("📡 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print("=" * 60)

    sources = {}

    # SharpAPI
    try:
        sharpapi_meta = get_from_cache("sharpapi:meta")
    except Exception as e:
        logger.warning("diagnose_sources: sharpapi:meta read error: %s", e)
        sharpapi_meta = None
    sharpapi_key = "✅" if os.getenv("SHARPAPI_API_KEY") else "❌"  # FIX-1: §1.8
    if sharpapi_meta and isinstance(sharpapi_meta, dict):
        last_run = sharpapi_meta.get("last_run", sharpapi_meta.get("last_run_at", "неизвестно"))
        total_events = sharpapi_meta.get("total_events", 0)
        stored = sharpapi_meta.get("stored_matches", 0)
        created = sharpapi_meta.get("created", 0)
        updated = sharpapi_meta.get("updated", 0)
        error_count = sharpapi_meta.get("error_count", sharpapi_meta.get("errors", 0))
        pages = sharpapi_meta.get("pages_fetched", 0)
        deduped = sharpapi_meta.get("deduped", 0)
        sources["sharpapi"] = {
            "key": sharpapi_key,
            "last_run": last_run,
            "total_events": total_events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "deduped": deduped,
            "errors": error_count,
            "pages": pages,
        }
        print(f"   📊 SharpAPI: ключ {sharpapi_key}, событий {total_events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}, страниц {pages}")
        print(f"           last_run: {last_run}")
    else:
        sources["sharpapi"] = {"key": sharpapi_key, "last_run": "нет данных"}
        print(f"   📊 SharpAPI: ключ {sharpapi_key}, мета отсутствует")

    # Bzzoiro
    try:
        bzzoiro_meta = get_from_cache("bzzoiro:meta")
    except Exception as e:
        logger.warning("diagnose_sources: bzzoiro:meta read error: %s", e)
        bzzoiro_meta = None
    bzzoiro_key = "✅" if os.getenv("BZZOIRO_API_KEY") else "❌"
    if bzzoiro_meta and isinstance(bzzoiro_meta, dict):
        events = bzzoiro_meta.get("total_events", bzzoiro_meta.get("events_count", 0))
        stored = bzzoiro_meta.get("stored_matches", 0)
        created = bzzoiro_meta.get("created", 0)
        updated = bzzoiro_meta.get("updated", 0)
        last_run = bzzoiro_meta.get("last_run", bzzoiro_meta.get("last_run_at", "неизвестно"))
        enrichment = bzzoiro_meta.get("enrichment", {})
        if not isinstance(enrichment, dict):
            enrichment = {}
        odds = enrichment.get("odds", bzzoiro_meta.get("odds_enriched", 0))
        predictions = enrichment.get("predictions", bzzoiro_meta.get("predictions_enriched", 0))
        stats = enrichment.get("stats", bzzoiro_meta.get("stats_enriched", 0))
        h2h = enrichment.get("h2h", bzzoiro_meta.get("h2h_enriched", 0))
        error_count = bzzoiro_meta.get("error_count", bzzoiro_meta.get("errors", 0))
        deduped = bzzoiro_meta.get("deduped", 0)
        events_only = bzzoiro_meta.get("events_only", False)
        sources["bzzoiro"] = {
            "key": bzzoiro_key,
            "events": events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "deduped": deduped,
            "odds": odds,
            "predictions": predictions,
            "stats": stats,
            "h2h": h2h,
            "errors": error_count,
            "last_run": last_run,
            "events_only": events_only,
        }
        print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, событий {events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped})")
        print(f"           Enrichment: odds {odds}, pred {predictions}, "
              f"stats {stats}, h2h {h2h}, ошибок {error_count}")
        if events_only:
            print(f"           ⚠️ Режим events-only — predictions/stats/h2h пропущены")
        print(f"           last_run: {last_run}")
    else:
        sources["bzzoiro"] = {"key": bzzoiro_key, "last_run": "нет данных"}
        print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, мета отсутствует")

    # OddsAPI
    try:
        oddsapi_meta = get_from_cache("odds_api:meta")
    except Exception as e:
        logger.warning("diagnose_sources: odds_api:meta read error: %s", e)
        oddsapi_meta = None
    oddsapi_key = "✅" if os.getenv("ODDS_API_KEY") else "❌"
    if oddsapi_meta and isinstance(oddsapi_meta, dict):
        events = oddsapi_meta.get("total_events", oddsapi_meta.get("events_count", 0))
        stored = oddsapi_meta.get("stored_matches", 0)
        created = oddsapi_meta.get("created", 0)
        updated = oddsapi_meta.get("updated", 0)
        error_count = oddsapi_meta.get("error_count", oddsapi_meta.get("errors", 0))
        quota_remaining = oddsapi_meta.get("quota_remaining", "?")
        quota_used = oddsapi_meta.get("quota_used", "?")
        last_run = oddsapi_meta.get("last_run", oddsapi_meta.get("last_run_at", "неизвестно"))
        deduped = oddsapi_meta.get("deduped", 0)
        sources["odds_api"] = {
            "key": oddsapi_key,
            "events": events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "deduped": deduped,
            "errors": error_count,
            "quota_remaining": quota_remaining,
            "quota_used": quota_used,
            "last_run": last_run,
        }
        print(f"   🎲 OddsAPI: ключ {oddsapi_key}, событий {events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}")
        print(f"           Quota: remaining={quota_remaining}, "
              f"used={quota_used}, last_run: {last_run}")
    else:
        sources["odds_api"] = {"key": oddsapi_key, "last_run": "нет данных"}
        print(f"   🎲 OddsAPI: ключ {oddsapi_key}, мета отсутствует")

    # Propline
    try:
        propline_meta = get_from_cache("propline:meta")
    except Exception as e:
        logger.warning("diagnose_sources: propline:meta read error: %s", e)
        propline_meta = None
    propline_key = "✅" if os.getenv("PROPLINE_API_KEY") else "❌"
    if propline_meta and isinstance(propline_meta, dict):
        events = propline_meta.get("total_events", propline_meta.get("events_count", 0))
        stored = propline_meta.get("stored_matches", 0)
        created = propline_meta.get("created", 0)
        updated = propline_meta.get("updated", 0)
        last_run = propline_meta.get("last_run", propline_meta.get("last_run_at", "неизвестно"))
        error_count = propline_meta.get("error_count", propline_meta.get("errors", 0))
        deduped = propline_meta.get("deduped", 0)
        sources["propline"] = {
            "key": propline_key,
            "events": events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "deduped": deduped,
            "errors": error_count,
            "last_run": last_run,
        }
        print(f"   🎯 Propline: ключ {propline_key}, "
              f"событий {events}, записано {stored} "
              f"(создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}")
        print(f"           last_run: {last_run}")
    else:
        sources["propline"] = {"key": propline_key, "last_run": "нет данных"}
        print(f"   🎯 Propline: ключ {propline_key}, мета отсутствует")

    # API-Football
    football_key = "✅" if os.getenv("FOOTBALL_DATA_API_KEY") else "❌"  # FIX-2
    sources["football_data"] = {"key": football_key}
    print(f"   ⚽ Football-Data: ключ {football_key}")  # FIX-3: объединено с Football-Data

    return sources


# ---------------------------------------------------------------------------
# 2. Диагностика Redis
# ---------------------------------------------------------------------------
def diagnose_redis() -> dict:
    print("\n" + "=" * 60)
    print("🗄 ДИАГНОСТИКА REDIS")
    print("=" * 60)

    try:
        if not is_redis_available():
            print("   ❌ Redis недоступен")
            return {"available": False, "total_keys": 0}
    except Exception as e:
        logger.warning("diagnose_redis: is_redis_available error: %s", e)
        print("   ❌ Redis недоступен (ошибка проверки)")
        return {"available": False, "total_keys": 0}

    try:
        all_fields = get_all_fields()
    except Exception as e:
        logger.error("diagnose_redis: get_all_fields error: %s", e)
        print(f"   ❌ Ошибка чтения Redis: {e}")
        return {"available": True, "total_keys": 0, "error": str(e)}

    if not all_fields:
        print("   ❌ Redis пуст")
        return {"available": True, "total_keys": 0}

    match_keys = [k for k in all_fields if k.startswith("match:") and not k.startswith("match:index:")]
    index_keys = [k for k in all_fields if k.startswith("match:index:")]
    search_keys = [k for k in all_fields if k.startswith("search:results:")]
    system_keys = [k for k in all_fields if k.startswith("system:") or k.endswith(":meta")]  # FIX-9: {collector}:meta
    other_keys = [k for k in all_fields if k not in match_keys and k not in index_keys
                  and k not in search_keys and k not in system_keys]

    est_memory_kb = round(len(match_keys) * 2 + len(search_keys) * 3 + 5, 1)

    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = {}

    result = {
        "available": True,
        "total_keys": len(all_fields),
        "match_keys": len(match_keys),
        "index_shards": len(index_keys),
        "search_results": len(search_keys),
        "system_keys": len(system_keys),
        "est_memory_kb": est_memory_kb,
        "last_cleanup_at": health.get("last_cleanup_at", "нет данных"),
        "last_cleanup_count": health.get("last_cleanup_count", "нет данных"),
        "last_cleanup_finished": health.get("last_cleanup_finished", "нет данных"),
        "last_cleanup_expired": health.get("last_cleanup_expired", "нет данных"),
        "last_migration_at": health.get("last_migration_at", "нет данных"),
        "cas_conflicts_total": health.get("cas_conflicts_total", 0),
        "init_latency_ms": health.get("init_latency_ms", 0),
    }

    print(f"   Ключей: {result['total_keys']}, память: ~{result['est_memory_kb']} КБ")
    print(f"   match:*: {result['match_keys']}, "
          f"match:index:*: {result['index_shards']}, "
          f"search:results:*: {result['search_results']}")
    print(f"   Последняя очистка: {result['last_cleanup_at']} "
          f"(удалено {result['last_cleanup_count']}, "
          f"завершённых {result['last_cleanup_finished']}, "
          f"просроченных {result['last_cleanup_expired']})")
    print(f"   Последняя миграция: {result['last_migration_at']}")
    print(f"   CAS-конфликтов: {result['cas_conflicts_total']}")
    print(f"   Init latency: {result['init_latency_ms']} мс")

    if other_keys:
        print(f"   Прочие ключи ({len(other_keys)}):")
        for k in other_keys[:10]:
            print(f"     • {k}")

    return result


# ---------------------------------------------------------------------------
# 3. Диагностика матчей
# ---------------------------------------------------------------------------
def diagnose_matches() -> dict:
    print("\n" + "=" * 60)
    print("⚽ ДИАГНОСТИКА МАТЧЕЙ")
    print("=" * 60)

    try:  # FIX-8: error isolation
        matches = get_matches_by_date_range()
        if not matches:
            matches_list = get_all_matches()
            matches = {}
            for m in matches_list:
                cid = m.get("canonical_id", "")
                if cid:
                    matches[cid] = m
    except Exception as e:
        logger.error("diagnose_matches: %s", e, exc_info=True)
        print(f"   ❌ Ошибка чтения матчей: {e}")
        return {"total": 0, "with_odds": 0, "with_stats": 0, "with_predictions": 0, "with_value": 0}

    if not matches:
        print("   ❌ Нет матчей в Redis")
        return {"total": 0, "with_odds": 0, "with_stats": 0, "with_predictions": 0, "with_value": 0}

    total = len(matches)
    with_odds = 0
    with_stats = 0
    with_predictions = 0
    with_h2h = 0
    with_value = 0
    source_counts = {}
    now = datetime.now(MSK_TIMEZONE)
    ages = []

    for match_id, match in matches.items():
        if not isinstance(match, dict):
            continue

        odds = match.get("odds", {})
        if isinstance(odds, dict) and odds.get("home") and odds.get("home") != "-":
            with_odds += 1

        value = match.get("value")
        if value is not None:
            try:
                if float(value) > 0:
                    with_value += 1
            except (ValueError, TypeError):
                pass

        stats = match.get("stats", {})
        if isinstance(stats, dict) and stats:
            with_stats += 1

        has_pred = False
        has_h2h = False
        predictions = match.get("predictions", {})
        if isinstance(predictions, dict) and predictions and "source" in predictions:
            with_predictions += 1
            has_pred = True

        h2h = match.get("h2h", {})
        if isinstance(h2h, dict) and h2h and "source" in h2h:
            with_h2h += 1
            has_h2h = True

        if not has_pred or not has_h2h:
            extra = match.get("extra", {})
            if isinstance(extra, dict):
                if not has_pred and "bzzoiro_prediction" in extra:
                    with_predictions += 1
                if not has_h2h and "bzzoiro_h2h" in extra:
                    with_h2h += 1

        sources_list = match.get("sources", [])
        if isinstance(sources_list, list):
            for s in sources_list:
                source_counts[s] = source_counts.get(s, 0) + 1

        created = match.get("created_at", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created)
                age_hours = (now - created_dt).total_seconds() / 3600
                ages.append(round(age_hours, 1))
            except Exception:
                pass

    age_min = min(ages) if ages else 0
    age_max = max(ages) if ages else 0

    result = {
        "total": total, "with_odds": with_odds, "with_stats": with_stats,
        "with_predictions": with_predictions, "with_h2h": with_h2h,
        "with_value": with_value,
        "source_counts": source_counts,
        "age_min_hours": age_min, "age_max_hours": age_max,
    }

    print(f"   Всего матчей: {total}")
    pct = round(with_odds / total * 100) if total else 0
    print(f"   С коэффициентами: {with_odds} ({pct}%)")
    print(f"   Со статистикой: {with_stats}")
    print(f"   С предсказаниями: {with_predictions}")
    print(f"   С H2H: {with_h2h}")
    print(f"   С value bet: {with_value}")
    print(f"   Возраст данных: {age_min}ч — {age_max}ч")

    if source_counts:
        print(f"   Источники:")
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
            print(f"     • {src}: {cnt} матчей")

    missing_odds = total - with_odds
    if missing_odds > 0:
        print(f"   ⚠️ Без коэффициентов: {missing_odds} матчей")

    return result


# ---------------------------------------------------------------------------
# 4. Диагностика ошибок
# ---------------------------------------------------------------------------
def diagnose_errors() -> dict:
    print("\n" + "=" * 60)
    print("🚨 ДИАГНОСТИКА ОШИБОК")
    print("=" * 60)

    errors = []

    try:
        bzzoiro_meta = get_from_cache("bzzoiro:meta")
    except Exception as e:
        logger.warning("diagnose_errors: bzzoiro:meta error: %s", e)
        bzzoiro_meta = None
    if bzzoiro_meta and isinstance(bzzoiro_meta, dict):
        err_count = bzzoiro_meta.get("error_count", 0)
        enrichment = bzzoiro_meta.get("enrichment", {})
        if isinstance(enrichment, dict):
            err_count = max(err_count, enrichment.get("errors", 0))
        err_count = max(err_count, bzzoiro_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "bzzoiro", "count": err_count})
            print(f"   🐝 Bzzoiro: {err_count} ошибок")

    try:
        sharpapi_meta = get_from_cache("sharpapi:meta")
    except Exception as e:
        logger.warning("diagnose_errors: sharpapi:meta error: %s", e)
        sharpapi_meta = None
    if sharpapi_meta and isinstance(sharpapi_meta, dict):
        err_count = sharpapi_meta.get("error_count", sharpapi_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "sharpapi", "count": err_count})
            print(f"   📊 SharpAPI: {err_count} ошибок")

    try:
        oddsapi_meta = get_from_cache("odds_api:meta")
    except Exception as e:
        logger.warning("diagnose_errors: odds_api:meta error: %s", e)
        oddsapi_meta = None
    if oddsapi_meta and isinstance(oddsapi_meta, dict):
        err_count = oddsapi_meta.get("error_count", oddsapi_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "odds_api", "count": err_count})
            print(f"   🎲 OddsAPI: {err_count} ошибок")

    try:
        propline_meta = get_from_cache("propline:meta")
    except Exception as e:
        logger.warning("diagnose_errors: propline:meta error: %s", e)
        propline_meta = None
    if propline_meta and isinstance(propline_meta, dict):
        err_count = propline_meta.get("error_count", propline_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "propline", "count": err_count})
            print(f"   🎯 Propline: {err_count} ошибок")

    try:
        health = get_from_cache("system:health")
    except Exception as e:
        logger.warning("diagnose_errors: system:health error: %s", e)
        health = None
    if health and isinstance(health, dict):
        cas = health.get("cas_conflicts_total", 0)
        if cas > 0:
            errors.append({"source": "CAS", "count": cas})
            print(f"   🔒 CAS-конфликтов: {cas}")

    if not errors:
        print("   ✅ Ошибок не обнаружено")

    return {"errors": errors}


# ---------------------------------------------------------------------------
# 5. Сводка
# ---------------------------------------------------------------------------
def print_summary(sources, redis_data, matches_data, errors_data) -> None:
    print("\n" + "=" * 60)
    print("📋 СВОДКА")
    print("=" * 60)

    src_count = len(sources)
    enriching = sum(1 for s in sources.values()
                    if isinstance(s, dict) and (s.get("odds", 0) or s.get("predictions", 0)))
    total_deduped = sum(s.get("deduped", 0) for s in sources.values()
                        if isinstance(s, dict))
    err_count = sum(e.get("count", 0) for e in errors_data.get("errors", []))

    print(f"   Источников: {src_count}, обогащают: {enriching}, дедупликатов: {total_deduped}")
    print(f"   Матчей в Redis: {redis_data.get('total_keys', 0)}")
    print(f"   Ошибок: {err_count}")

    if err_count == 0:
        print("   ✅ Система в норме")
    else:
        print("   ⚠️ Есть ошибки — проверьте детали выше")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("🔧 ДИАГНОСТИКА GATEKEEPER-AI v8.11-patched")
    print(f"   Время: {datetime.now(MSK_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    print("=" * 60)

    mode = "full"
    if "--sources" in sys.argv:
        mode = "sources"
    elif "--redis" in sys.argv:
        mode = "redis"
    elif "--matches" in sys.argv:
        mode = "matches"
    elif "--errors" in sys.argv:
        mode = "errors"

    # Инициализация Redis (health-check + миграция + очистка)
    # FIX-AUDIT: run_initialization для ВСЕХ режимов (§1.7a), не только full/redis
    print("[DIAG] Инициализация Redis...")
    try:
        init = run_initialization(collector="source_diagnostics")
        if not init.get("redis_available"):
            print("[DIAG] ❌ Redis недоступен — диагностика ограничена")
    except Exception as e:
        logger.error("main: run_initialization failed: %s", e, exc_info=True)
        print(f"[DIAG] ❌ Ошибка инициализации: {e}")

    src_data, redis_data, matches_data, errors_data = {}, {}, {}, {}

    if mode in ("full", "sources"):
        src_data = diagnose_sources()
    if mode in ("full", "redis"):
        redis_data = diagnose_redis()
    if mode in ("full", "matches"):
        matches_data = diagnose_matches()
    if mode in ("full", "errors"):
        errors_data = diagnose_errors()
    if mode == "full":
        print_summary(src_data, redis_data, matches_data, errors_data)


if __name__ == "__main__":
    main()
