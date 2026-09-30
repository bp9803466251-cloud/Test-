# source_diagnostics.py
"""
Единый модуль диагностики Gatekeeper-AI v700-prod.
Заменяет debug_inspect.py и debug_odds.py.
Объединяет диагностику всех источников, Redis и матчей в одном файле.

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
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

from gatekeeper_hub import (
    run_initialization,
    get_matches_by_date_range,
    get_all_matches,
)
from redis_hub import get_from_cache, get_all_fields, is_redis_available

MSK_TIMEZONE = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# Helpers для v700 формата
# ---------------------------------------------------------------------------

def _get_current_odds(match: dict) -> dict | None:
    """
    Извлекает текущие коэффициенты из матча в v700 формате.
    Проверяет:
      1) match["odds"]["current"]["home"/"draw"/"away"]
      2) match["odds"]["home"/"draw"/"away"]  (старый плоский формат)
      3) match["sources"] — список с odds от разных upstream'ов
    Возвращает dict {home, draw, away} или None.
    """
    # 1. v700 nested: odds.current.{home,draw,away}
    odds = match.get("odds", {})
    if isinstance(odds, dict):
        current = odds.get("current")
        if isinstance(current, dict):
            home = current.get("home")
            draw = current.get("draw")
            away = current.get("away")
            if home and home != "-" and home != "":
                return {"home": home, "draw": draw, "away": away}
        # 2. Старый плоский формат: odds.{home,draw,away}
        home = odds.get("home")
        if home and home != "-" and home != "":
            return {"home": home, "draw": odds.get("draw"), "away": odds.get("away")}

    # 3. sources[] — odds от разных upstream'ов
    sources = match.get("sources", [])
    if isinstance(sources, list):
        for s in sources:
            if not isinstance(s, dict):
                continue
            s_odds = s.get("odds", {})
            if not isinstance(s_odds, dict):
                continue
            current = s_odds.get("current")
            if isinstance(current, dict):
                home = current.get("home")
                if home and home != "-" and home != "":
                    return {"home": home, "draw": current.get("draw"), "away": current.get("away")}
            # плоский fallback
            home = s_odds.get("home")
            if home and home != "-" and home != "":
                return {"home": home, "draw": s_odds.get("draw"), "away": s_odds.get("away")}

    return None


# Ключи, которые означают, что H2H реально заполнен (а не пустая заглушка)
_H2H_KEYS = {"total", "home_wins", "draws", "away_wins", "recent", "matches", "source"}

def _has_meaningful_h2h(match: dict) -> bool:
    """Проверяет, есть ли в матче реальный H2H (а не пустой словарь)."""
    h2h = match.get("h2h", {})
    if not isinstance(h2h, dict) or not h2h:
        return False
    # Есть хотя бы один meaningful key с непустым значением
    for k in _H2H_KEYS:
        v = h2h.get(k)
        if v is not None and v != "" and v != 0:
            return True
    # Проверяем extra — Bzzoiro может класть h2h туда
    extra = match.get("extra", {})
    if isinstance(extra, dict):
        bzz_h2h = extra.get("bzzoiro_h2h")
        if isinstance(bzz_h2h, dict) and bzz_h2h:
            for k in _H2H_KEYS:
                v = bzz_h2h.get(k)
                if v is not None and v != "" and v != 0:
                    return True
    return False


# Ключи, которые означают, что prediction реально заполнен
_PRED_KEYS = {
    "predicted_home", "predicted_away", "predicted_score_home", "predicted_score_away",
    "home_win_prob", "draw_prob", "away_win_prob",
    "winner", "predicted_winner", "source",
    "prediction", "predictions", "model",
}

def _has_meaningful_pred(match: dict) -> bool:
    """Проверяет, есть ли в матче реальный prediction (а не пустой словарь)."""
    predictions = match.get("predictions", {})
    if isinstance(predictions, dict) and predictions:
        for k in _PRED_KEYS:
            if k in predictions:
                return True
    # Проверяем extra
    extra = match.get("extra", {})
    if isinstance(extra, dict):
        bzz_pred = extra.get("bzzoiro_prediction")
        if isinstance(bzz_pred, dict) and bzz_pred:
            for k in _PRED_KEYS:
                if k in bzz_pred:
                    return True
    return False


# ---------------------------------------------------------------------------
# 1. Диагностика источников
# ---------------------------------------------------------------------------
def diagnose_sources() -> dict:
    print("\n" + "=" * 60)
    print("📡 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print("=" * 60)

    sources = {}

    # SharpAPI
    sharpapi_meta = get_from_cache("sharpapi:meta")
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
    bzzoiro_meta = get_from_cache("bzzoiro:meta")
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
    oddsapi_meta = get_from_cache("odds_api:meta")
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

    # Propline (Pinnacle)
    propline_meta = get_from_cache("propline:meta")
    propline_key = "✅" if os.getenv("PROPLINE_API_KEY") else "❌"
    if propline_meta and isinstance(propline_meta, dict):
        events = propline_meta.get("total_events", 0)
        stored = propline_meta.get("stored_matches", 0)
        created = propline_meta.get("created", 0)
        updated = propline_meta.get("updated", 0)
        error_count = propline_meta.get("error_count", 0)
        quota_remaining = propline_meta.get("quota_remaining", "?")
        quota_used = propline_meta.get("quota_used", "?")
        last_run = propline_meta.get("last_run", "неизвестно")
        deduped = propline_meta.get("deduped", 0)
        sources["propline"] = {
            "key": propline_key,
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
        print(f"   🏆 Propline: ключ {propline_key}, событий {events}, "
              f"записано {stored} (создано {created}, обновлено {updated}, "
              f"дедупликатов {deduped}), ошибок {error_count}")
        print(f"           Quota: remaining={quota_remaining}, "
              f"used={quota_used}, last_run: {last_run}")
    else:
        sources["propline"] = {"key": propline_key, "last_run": "нет данных"}
        print(f"   🏆 Propline: ключ {propline_key}, мета отсутствует")

    # API-Football
    football_key = "✅" if os.getenv("API_FOOTBALL_KEY") else "❌"
    sources["api_football"] = {"key": football_key}
    print(f"   ⚽ API-Football: ключ {football_key}")

    # Football-Data
    fd_key = "✅" if os.getenv("FOOTBALL_DATA_API_KEY") else "❌"
    sources["football_data"] = {"key": fd_key}
    print(f"   🌐 Football-Data: ключ {fd_key}")

    return sources


# ---------------------------------------------------------------------------
# 2. Диагностика Redis
# ---------------------------------------------------------------------------
def diagnose_redis() -> dict:
    print("\n" + "=" * 60)
    print("🗄 ДИАГНОСТИКА REDIS")
    print("=" * 60)

    if not is_redis_available():
        print("   ❌ Redis недоступен")
        return {"available": False, "total_keys": 0}

    all_fields = get_all_fields()
    if not all_fields:
        print("   ❌ Redis пуст")
        return {"available": True, "total_keys": 0}

    match_keys = [k for k in all_fields if k.startswith("match:") and not k.startswith("match:index:")]
    index_keys = [k for k in all_fields if k.startswith("match:index:")]
    search_keys = [k for k in all_fields if k.startswith("search:results:")]
    system_keys = [k for k in all_fields if k.startswith("system:") or k.endswith(":meta")]
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

    # Пытаемся через шарды, fallback на плоский индекс
    matches = get_matches_by_date_range()
    if not matches:
        matches_list = get_all_matches()
        matches = {}
        for m in matches_list:
            cid = m.get("canonical_id", "")
            if cid:
                matches[cid] = m

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

        # --- Odds: v700 nested формат ---
        current_odds = _get_current_odds(match)
        if current_odds and current_odds.get("home") and current_odds["home"] != "-":
            with_odds += 1

        # --- Value bet ---
        value = match.get("value")
        if value is not None:
            try:
                if float(value) > 0:
                    with_value += 1
            except (ValueError, TypeError):
                pass

        # --- Stats ---
        stats = match.get("stats", {})
        if isinstance(stats, dict) and stats:
            with_stats += 1

        # --- Predictions: meaningful keys ---
        if _has_meaningful_pred(match):
            with_predictions += 1

        # --- H2H: meaningful keys ---
        if _has_meaningful_h2h(match):
            with_h2h += 1

        # --- Sources ---
        sources_list = match.get("sources", [])
        if isinstance(sources_list, list):
            for s in sources_list:
                source_counts[s] = source_counts.get(s, 0) + 1

        # --- Age ---
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

    bzzoiro_meta = get_from_cache("bzzoiro:meta")
    if bzzoiro_meta and isinstance(bzzoiro_meta, dict):
        err_count = bzzoiro_meta.get("error_count", 0)
        enrichment = bzzoiro_meta.get("enrichment", {})
        if isinstance(enrichment, dict):
            err_count = max(err_count, enrichment.get("errors", 0))
        err_count = max(err_count, bzzoiro_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "bzzoiro", "count": err_count})
            print(f"   🐝 Bzzoiro: {err_count} ошибок")

    sharpapi_meta = get_from_cache("sharpapi:meta")
    if sharpapi_meta and isinstance(sharpapi_meta, dict):
        err_count = sharpapi_meta.get("error_count", sharpapi_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "sharpapi", "count": err_count})
            print(f"   📊 SharpAPI: {err_count} ошибок")

    oddsapi_meta = get_from_cache("odds_api:meta")
    if oddsapi_meta and isinstance(oddsapi_meta, dict):
        err_count = oddsapi_meta.get("error_count", oddsapi_meta.get("errors", 0))
        if err_count > 0:
            errors.append({"source": "odds_api", "count": err_count})
            print(f"   🎲 OddsAPI: {err_count} ошибок")

    # Propline
    propline_meta = get_from_cache("propline:meta")
    if propline_meta and isinstance(propline_meta, dict):
        err_count = propline_meta.get("error_count", 0)
        if err_count > 0:
            errors.append({"source": "propline", "count": err_count})
            print(f"   🏆 Propline: {err_count} ошибок")

    health = get_from_cache("system:health")
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
    print("🔧 ДИАГНОСТИКА GATEKEEPER-AI v700-prod")
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
    if mode in ("full", "redis"):
        print("[DIAG] Инициализация Redis...")
        init = run_initialization()
        if not init.get("redis_available"):
            print("[DIAG] ❌ Redis недоступен — диагностика ограничена")

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
