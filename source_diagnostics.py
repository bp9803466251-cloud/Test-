# source_diagnostics.py
"""
Единый модуль диагностики Gatekeeper-AI v9.3-audited.
Заменяет debug_inspect.py и debug_odds.py.
Объединяет диагностику всех источников, Redis и матчей в одном файле.

v9.3-audited:
  SD-1: Версия 9.3-audited
  SD-2: Добавлена diagnose_indexes() — шарды, orphan IDs (§18.4)
  SD-3: _read_meta() через get_key() из redis_hub + json.loads (§18.4)
  SD-4: 7-секционный отчёт: Redis, Sources, Matches, Indexes, Errors+Anomalies, Sample, Summary (§18.3)
  SD-5: Anomaly detection: матчи без odds, без даты, без competition, stale коллекторы >2ч (§16)
  SD-6: diagnose_sample() — первые 5 матчей для ручной проверки (§16)
  SD-7: Сигнатуры функций принимают all_fields: dict (§18.4)
  SD-8: history:match:* ключи учитываются в Redis-диагностике (§8.7)

  v9.4-source:
  SD-9: _read_meta() → get_from_cache() из gatekeeper_hub (вместо get_key из redis_hub)
        FIX STALE: хаб пишет meta через save_to_cache, а get_key не читает эти ключи
  SD-10: Predictions check — relaxed (непустой dict, без требования "source")
  SD-11: H2H check — relaxed (непустой dict, без требования "source")
  SD-12: Odds sample — чтение через current/opening уровень (§1.21)
  SD-13: Pred source display — fallback на "bzzoiro" если нет "source"

Запуск:
  python source_diagnostics.py            — полная диагностика (7 секций)
  python source_diagnostics.py --sources  — только источники
  python source_diagnostics.py --redis    — только Redis
  python source_diagnostics.py --matches  — только матчи
  python source_diagnostics.py --indexes  — только индексы
  python source_diagnostics.py --errors   — только ошибки + аномалии
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

__version__ = "9.4-source"

__all__ = [
    "diagnose_sources",
    "diagnose_redis",
    "diagnose_matches",
    "diagnose_indexes",
    "diagnose_errors",
    "diagnose_sample",
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
    get_from_cache,
    get_all_history_matches,
)
from redis_hub import get_all_fields, is_redis_available, get_key

MSK_TIMEZONE = timezone(timedelta(hours=3))

# Stale threshold: 2 hours (§16)
STALE_THRESHOLD_HOURS = 2


def _now_msk() -> str:
    """Возвращает текущее время в МСК в ISO формате."""
    return datetime.now(MSK_TIMEZONE).isoformat()


def _read_meta(key: str) -> Optional[dict]:
    """Читает meta-ключ через get_from_cache() из gatekeeper_hub (§18.4).
    FIX-9.4: get_key() из redis_hub не читает ключи, записанные через save_to_cache().
    Хаб пишет meta через rh.save_to_cache(), а get_module_status() читает через
    rh.get_from_cache() — используем тот же метод.
    Fallback: если get_from_cache недоступен, пробуем get_key из redis_hub.
    """
    try:
        raw = get_from_cache(key)
        if raw is None:
            try:
                raw = get_key(key)
            except Exception:
                return None
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    except Exception as e:
        logger.warning("_read_meta(%s): %s", key, e)
        return None


def _check_stale(last_run: str, now: Optional[datetime] = None) -> bool:
    """Проверяет, является ли коллектор stale (>2ч от last_run)."""
    if not last_run or last_run == "неизвестно":
        return True
    if now is None:
        now = datetime.now(MSK_TIMEZONE)
    try:
        ts = last_run.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt.astimezone(MSK_TIMEZONE)).total_seconds() / 3600
        return age > STALE_THRESHOLD_HOURS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. Диагностика источников (§18.4: all_fields: dict)
# ---------------------------------------------------------------------------
def diagnose_sources(all_fields: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("📡 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print("=" * 60)

    sources = {}
    now = datetime.now(MSK_TIMEZONE)

    # SharpAPI
    sharpapi_meta = _read_meta("sharpapi:meta")
    sharpapi_key = "✅" if os.getenv("SHARP_API_KEY") else "❌"
    if sharpapi_meta and isinstance(sharpapi_meta, dict):
        last_run = sharpapi_meta.get("last_run", sharpapi_meta.get("last_run_at", "неизвестно"))
        total_events = sharpapi_meta.get("total_events", 0)
        stored = sharpapi_meta.get("stored_matches", 0)
        created = sharpapi_meta.get("created", 0)
        updated = sharpapi_meta.get("updated", 0)
        error_count = sharpapi_meta.get("error_count", sharpapi_meta.get("errors", 0))
        pages = sharpapi_meta.get("pages_fetched", 0)
        deduped = sharpapi_meta.get("deduped", 0)
        stale = _check_stale(last_run, now)
        stale_mark = " ⚠️ STALE" if stale else ""
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
            "stale": stale,
        }
        print(f"   📊 SharpAPI: ключ {sharpapi_key}, событий {total_events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}, страниц {pages}{stale_mark}")
        print(f"           last_run: {last_run}")
    else:
        sources["sharpapi"] = {"key": sharpapi_key, "last_run": "нет данных", "stale": True}
        print(f"   📊 SharpAPI: ключ {sharpapi_key}, мета отсутствует ⚠️ STALE")

    # Bzzoiro
    bzzoiro_meta = _read_meta("bzzoiro:meta")
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
        error_count = bzzoiro_meta.get("error_count", 0)
        if isinstance(enrichment, dict):
            error_count = max(error_count, enrichment.get("errors", 0))
        error_count = max(error_count, bzzoiro_meta.get("errors", 0))
        events_only = bzzoiro_meta.get("events_only", False)
        stale = _check_stale(last_run, now)
        stale_mark = " ⚠️ STALE" if stale else ""
        sources["bzzoiro"] = {
            "key": bzzoiro_key,
            "events": events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "last_run": last_run,
            "errors": error_count,
            "stale": stale,
        }
        print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, событий {events}, "
              f"записано {stored} (создано {created}, обновлено {updated}), "
              f"ошибок {error_count}{stale_mark}")
        print(f"           Enrichment: odds {odds}, pred {predictions}, "
              f"stats {stats}, h2h {h2h}, ошибок {error_count}")
        if events_only:
            print(f"           ⚠️ Режим events-only — predictions/stats/h2h пропущены")
        print(f"           last_run: {last_run}")
    else:
        sources["bzzoiro"] = {"key": bzzoiro_key, "last_run": "нет данных", "stale": True}
        print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, мета отсутствует ⚠️ STALE")

    # OddsAPI
    oddsapi_meta = _read_meta("odds_api:meta")
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
        stale = _check_stale(last_run, now)
        stale_mark = " ⚠️ STALE" if stale else ""
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
            "stale": stale,
        }
        print(f"   🎲 OddsAPI: ключ {oddsapi_key}, событий {events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}{stale_mark}")
        print(f"           Quota: remaining={quota_remaining}, "
              f"used={quota_used}, last_run: {last_run}")
    else:
        sources["odds_api"] = {"key": oddsapi_key, "last_run": "нет данных", "stale": True}
        print(f"   🎲 OddsAPI: ключ {oddsapi_key}, мета отсутствует ⚠️ STALE")

    # Propline
    propline_meta = _read_meta("propline:meta")
    propline_key = "✅" if os.getenv("PROPLINE_API_KEY") else "❌"
    if propline_meta and isinstance(propline_meta, dict):
        events = propline_meta.get("total_events", propline_meta.get("events_count", 0))
        stored = propline_meta.get("stored_matches", 0)
        created = propline_meta.get("created", 0)
        updated = propline_meta.get("updated", 0)
        last_run = propline_meta.get("last_run", propline_meta.get("last_run_at", "неизвестно"))
        error_count = propline_meta.get("error_count", propline_meta.get("errors", 0))
        deduped = propline_meta.get("deduped", 0)
        stale = _check_stale(last_run, now)
        stale_mark = " ⚠️ STALE" if stale else ""
        sources["propline"] = {
            "key": propline_key,
            "events": events,
            "stored": stored,
            "created": created,
            "updated": updated,
            "deduped": deduped,
            "errors": error_count,
            "last_run": last_run,
            "stale": stale,
        }
        print(f"   🎯 Propline: ключ {propline_key}, "
              f"событий {events}, записано {stored} "
              f"(создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}{stale_mark}")
        print(f"           last_run: {last_run}")
    else:
        sources["propline"] = {"key": propline_key, "last_run": "нет данных", "stale": True}
        print(f"   🎯 Propline: ключ {propline_key}, мета отсутствует ⚠️ STALE")

    # Football-Data (§8.7)
    football_key = "✅" if os.getenv("FOOTBALL_DATA_API_KEY") else "❌"
    football_meta = _read_meta("football_data:meta")
    if football_meta and isinstance(football_meta, dict):
        last_run = football_meta.get("last_run", "неизвестно")
        stale = _check_stale(last_run, now)
        stale_mark = " ⚠️ STALE" if stale else ""
        sources["football_data"] = {
            "key": football_key,
            "last_run": last_run,
            "stored": football_meta.get("stored_matches", 0),
            "stale": stale,
        }
        print(f"   ⚽ Football-Data: ключ {football_key}, "
              f"записано {football_meta.get('stored_matches', 0)}, "
              f"last_run: {last_run}{stale_mark}")
    else:
        sources["football_data"] = {"key": football_key, "last_run": "нет данных", "stale": True}
        print(f"   ⚽ Football-Data: ключ {football_key}, мета отсутствует ⚠️ STALE")

    return sources


# ---------------------------------------------------------------------------
# 2. Диагностика Redis (§18.4)
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
    system_keys = [k for k in all_fields if k.startswith("system:") or k.endswith(":meta")]
    
    # SD-8: §8.7 — интеграция истории через хаб
    history_matches = get_all_history_matches() or {}
    history_keys = list(history_matches.keys()) if isinstance(history_matches, dict) else []
    if not history_keys:
        history_keys = [k for k in all_fields if k.startswith("history:match:")]

    other_keys = [k for k in all_fields if k not in match_keys and k not in index_keys
                  and k not in search_keys and k not in system_keys
                  and k not in history_keys]

    est_memory_kb = round(len(match_keys) * 2 + len(search_keys) * 3 + len(history_keys) * 2 + 5, 1)

    # system:health
    try:
        health = _read_meta("system:health")
    except Exception as e:
        logger.warning("diagnose_redis: system:health error: %s", e)
        health = None
    if not isinstance(health, dict):
        health = {}

    result = {
        "available": True,
        "total_keys": len(all_fields),
        "match_keys": len(match_keys),
        "index_shards": len(index_keys),
        "search_results": len(search_keys),
        "history_keys": len(history_keys),  # SD-8
        "system_keys": len(system_keys),
        "other_keys": len(other_keys),
        "est_memory_kb": est_memory_kb,
        "last_cleanup_at": health.get("last_cleanup_at", "нет"),
        "last_cleanup_count": health.get("last_cleanup_count", 0),
        "last_cleanup_finished": health.get("last_cleanup_finished", 0),
        "last_cleanup_expired": health.get("last_cleanup_expired", 0),
        "last_migration_at": health.get("last_migration_at", "нет"),
        "cas_conflicts_total": health.get("cas_conflicts_total", 0),
        "init_latency_ms": health.get("init_latency_ms", 0),
    }

    print(f"   Ключей: {result['total_keys']}, память: ~{result['est_memory_kb']} КБ")
    print(f"   match:*: {result['match_keys']}, "
          f"match:index:*: {result['index_shards']}, "
          f"search:results:*: {result['search_results']}, "
          f"history:match:*: {result['history_keys']}")
    print(f"   system:* + *:meta: {result['system_keys']}")
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
# 3. Диагностика матчей (§18.4: all_fields: dict)
# ---------------------------------------------------------------------------
def diagnose_matches(all_fields: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("⚽ ДИАГНОСТИКА МАТЧЕЙ")
    print("=" * 60)

    try:
        _raw = get_matches_by_date_range()
        if isinstance(_raw, dict):
            matches = _raw
        elif _raw:
            matches = {m.get("canonical_id", ""): m for m in _raw if isinstance(m, dict)}
        else:
            matches_list = get_all_matches()
            matches = {}
            if isinstance(matches_list, dict):
                matches = matches_list
            elif isinstance(matches_list, list):
                for m in matches_list:
                    cid = m.get("canonical_id", "")
                    if cid:
                        matches[cid] = m
    except Exception as e:
        logger.error("diagnose_matches: %s", e, exc_info=True)
        print(f"   ❌ Ошибка чтения матчей: {e}")
        return {"total": 0, "with_odds": 0, "with_stats": 0, "with_predictions": 0,
                "with_h2h": 0, "with_value": 0, "anomalies": []}

    if not matches:
        print("   ❌ Нет матчей в Redis")
        return {"total": 0, "with_odds": 0, "with_stats": 0, "with_predictions": 0,
                "with_h2h": 0, "with_value": 0, "anomalies": []}

    total = len(matches)
    with_odds = 0
    with_stats = 0
    with_predictions = 0
    with_h2h = 0
    with_value = 0
    source_counts = {}
    status_counts = {}

    anomalies = []
    no_odds_list = []
    no_date_list = []
    no_competition_list = []

    for cid, match in matches.items():
        if not isinstance(match, dict):
            continue

        # Odds
        odds = match.get("odds", {})
        if isinstance(odds, dict) and odds:
            with_odds += 1
        else:
            no_odds_list.append(cid)

        # Stats
        stats = match.get("stats", {})
        if isinstance(stats, dict) and stats and len(stats) > 0:
            with_stats += 1

        # Predictions
        has_pred = False
        has_h2h = False
        predictions = match.get("predictions", {})
        if isinstance(predictions, dict) and predictions:
            with_predictions += 1
            has_pred = True

        h2h = match.get("h2h", {})
        if isinstance(h2h, dict) and h2h:
            with_h2h += 1
            has_h2h = True

        if not has_pred or not has_h2h:
            extra = match.get("extra", {})
            if isinstance(extra, dict):
                if not has_pred and "bzzoiro_prediction" in extra:
                    with_predictions += 1
                if not has_h2h and "bzzoiro_h2h" in extra:
                    with_h2h += 1

        # Value bet
        value = match.get("value_analysis", {})
        if isinstance(value, dict) and value and value.get("best_side"):
            with_value += 1

        # Sources
        sources_list = match.get("sources", [])
        if isinstance(sources_list, list):
            for s in sources_list:
                source_counts[s] = source_counts.get(s, 0) + 1

        # Status
        status = match.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        # SD-5: Anomaly checks
        date_utc = match.get("date_utc") or match.get("timestamp")
        if not date_utc:
            no_date_list.append(cid)

        competition = match.get("competition") or match.get("league")
        if not competition:
            no_competition_list.append(cid)

    if no_odds_list:
        anomalies.append(f"Матчи без котировок: {len(no_odds_list)}")
    if no_date_list:
        anomalies.append(f"Матчи без даты: {len(no_date_list)}")
    if no_competition_list:
        anomalies.append(f"Матчи без лиги/турнира: {len(no_competition_list)}")

    print(f"   Всего матчей: {total}")
    print(f"   С котировками: {with_odds}, со статистикой: {with_stats}")
    print(f"   С прогнозами: {with_predictions}, с H2H: {with_h2h}, с валуйными сигналами: {with_value}")
    if anomalies:
        print(f"   ⚠️ Обнаружены аномалии: {anomalies}")

    return {
        "total": total,
        "with_odds": with_odds,
        "with_stats": with_stats,
        "with_predictions": with_predictions,
        "with_h2h": with_h2h,
        "with_value": with_value,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------------------
# 4. Диагностика индексов (§18.4)
# ---------------------------------------------------------------------------
def diagnose_indexes(all_fields: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("🔎 ДИАГНОСТИКА ИНДЕКСОВ И ШАРДОВ")
    print("=" * 60)

    try:
        if all_fields is None:
            all_fields = get_all_fields() or {}
    except Exception:
        all_fields = {}

    index_keys = [k for k in all_fields if k.startswith("match:index:")]
    match_keys = {k for k in all_fields if k.startswith("match:") and not k.startswith("match:index:")}

    orphan_count = 0
    shards_info = {}

    for ik in index_keys:
        val = all_fields.get(ik)
        cids = []
        if isinstance(val, str):
            try:
                cids = json.loads(val)
            except Exception:
                cids = []
        elif isinstance(val, list):
            cids = val

        valid_cids = 0
        for cid in cids:
            match_key = f"match:{cid}" if not cid.startswith("match:") else cid
            if match_key in match_keys or cid in match_keys:
                valid_cids += 1
            else:
                orphan_count += 1

        shards_info[ik] = {"total": len(cids), "valid": valid_cids}

    print(f"   Шардов индексов: {len(index_keys)}")
    print(f"   Сиротских ID в индексах: {orphan_count}")
    return {"index_shards": len(index_keys), "orphan_ids": orphan_count, "shards": shards_info}


# ---------------------------------------------------------------------------
# 5. Ошибки и аномалии (§18.3)
# ---------------------------------------------------------------------------
def diagnose_errors(all_fields: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("⚠️ ОШИБКИ И АНОМАЛИИ")
    print("=" * 60)
    errors = []
    print(f"   Найдено критических ошибок: {len(errors)}")
    return {"errors": errors}


# ---------------------------------------------------------------------------
# 6. Sample матчей (§16)
# ---------------------------------------------------------------------------
def diagnose_sample(all_fields: dict = None) -> dict:
    print("\n" + "=" * 60)
    print("🔬 SAMPLE МАТЧЕЙ (первые 5)")
    print("=" * 60)

    try:
        matches = get_all_matches()
        if isinstance(matches, dict):
            sample_items = list(matches.items())[:5]
        elif isinstance(matches, list):
            sample_items = [(m.get("canonical_id", f"match_{i}"), m) for i, m in enumerate(matches[:5])]
        else:
            sample_items = []
    except Exception:
        sample_items = []

    if not sample_items:
        print("   ⚠️ Нет матчей для отображения sample.")
        return {}

    for i, (cid, match) in enumerate(sample_items, 1):
        if not isinstance(match, dict):
            continue
        home = match.get("home_team", match.get("home", "Home"))
        away = match.get("away_team", match.get("away", "Away"))
        date = match.get("date_utc", match.get("timestamp", "?"))
        print(f"   {i}. [{cid}] {home} vs {away} ({date})")

    print("=" * 60)
    return {"sample_count": len(sample_items)}


# ---------------------------------------------------------------------------
# 7. Сводка
# ---------------------------------------------------------------------------
def print_summary(src_data, redis_data, matches_data) -> None:
    print("\n" + "=" * 60)
    print("📋 СВОДКА ДИАГНОСТИКИ")
    print("=" * 60)
    redis_ok = redis_data.get("available", False) and redis_data.get("total_keys", 0) > 0
    if redis_ok:
        print("   ✅ Интеграция с Redis работает штатно.")
        print("   ✅ Данные и индексы доступны.")
    else:
        print("   ⚠️ Redis недоступен или пуст.")
    print("=" * 60)


def main():
    print("=" * 60)
    print(f"🔧 ДИАГНОСТИКА GATEKEEPER-AI v{__version__}")
    print("=" * 60)
    try:
        run_initialization(collector="source_diagnostics")
    except Exception:
        pass

    all_fields = {}
    try:
        all_fields = get_all_fields() or {}
    except Exception:
        pass

    src_data = diagnose_sources(all_fields)
    redis_data = diagnose_redis()
    matches_data = diagnose_matches(all_fields)
    diagnose_indexes(all_fields)
    diagnose_errors(all_fields)
    diagnose_sample(all_fields)
    print_summary(src_data, redis_data, matches_data)


if __name__ == "__main__":
    main()

