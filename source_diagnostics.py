#!/usr/bin/env python3
"""
source_diagnostics.py — Диагностика источников GatekeeperAI v710.
Объединяет диагностику всех коллекторов, Redis и матчей.

Запуск:
  python source_diagnostics.py            — полная диагностика
  python source_diagnostics.py --sources  — только источники
  python source_diagnostics.py --redis    — только Redis
  python source_diagnostics.py --matches  — только матчи
  python source_diagnostics.py --errors   — только ошибки
  python source_diagnostics.py --json     — JSON-вывод
  python source_diagnostics.py --summary  — только сводка
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from gatekeeper_hub import (
    run_initialization,
    get_matches_by_date_range,
    get_all_matches,
)
from redis_hub import get_all_fields, is_redis_available, get_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [DIAG] %(message)s",
)
logger = logging.getLogger(__name__)

__version__ = "8.10-patched"
__all__ = [
    "diagnose_sources",
    "diagnose_redis",
    "diagnose_matches",
    "diagnose_errors",
    "print_summary",
    "main",
    "__version__",
]

MSK_TIMEZONE = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# Хелперы для определения формата данных (v710 — 3 формата odds)
# ---------------------------------------------------------------------------

def _get_current_odds(match: dict) -> Optional[dict]:
    """
    Извлекает текущие коэффициенты из матча.
    Поддерживает 3 формата (§1.21):
      1. Плоский:       odds = {"home": 1.85, "draw": 3.60, "away": 2.10}
      2. Структурированный: odds = {"current": {"home": ..., "draw": ..., "away": ...}}
      3. v710:          odds = {"1x2": {"current": {"home": ...}, "sources": [...]}}
    Возвращает {"home": ..., "draw": ..., "away": ...} или None.
    """
    try:
        odds = match.get("odds")
        if not odds or not isinstance(odds, dict):
            return None

        # Формат 3: v710 — odds.1x2.current.{home,draw,away}
        x12 = odds.get("1x2")
        if isinstance(x12, dict):
            current = x12.get("current")
            if isinstance(current, dict) and current.get("home") and str(current.get("home")) != "-":
                return current
            # opening fallback
            opening = x12.get("opening")
            if isinstance(opening, dict) and opening.get("home") and str(opening.get("home")) != "-":
                return opening
            # sources[0].price
            sources = x12.get("sources")
            if isinstance(sources, list) and sources:
                for s in sources:
                    price = s.get("price") if isinstance(s, dict) else None
                    if isinstance(price, dict) and price.get("home") and str(price.get("home")) != "-":
                        return price

        # Формат 2: структурированный — odds.current.{home,draw,away}
        current = odds.get("current")
        if isinstance(current, dict) and current.get("home") and str(current.get("home")) != "-":
            return current

        # Формат 2b: odds.sources[].price.{home,draw,away}
        sources = odds.get("sources")
        if isinstance(sources, list) and sources:
            for s in sources:
                price = s.get("price") if isinstance(s, dict) else None
                if isinstance(price, dict) and price.get("home") and str(price.get("home")) != "-":
                    return price

        # Формат 1: плоский — odds.{home,draw,away}
        if odds.get("home") and str(odds.get("home")) != "-":
            return odds

        return None
    except Exception:
        return None


def _has_meaningful_h2h(match: dict) -> bool:
    """Проверяет, есть ли реальная H2H-секция."""
    try:
        h2h = match.get("h2h")
        if not h2h or not isinstance(h2h, dict):
            return False
        meaningful_keys = {
            "total_meetings", "home_wins", "draws", "away_wins",
            "home_win", "away_win", "meetings",
        }
        return any(k in h2h for k in meaningful_keys)
    except Exception:
        return False


def _has_meaningful_pred(match: dict) -> bool:
    """Проверяет, есть ли реальная prediction-секция."""
    try:
        pred = match.get("prediction")
        if not pred or not isinstance(pred, dict):
            return False
        meaningful_keys = {
            "home_win", "draw", "away_win", "predicted_home",
            "predicted_away", "home_win_prob", "away_win_prob",
            "winner", "prediction",
        }
        if any(k in pred for k in meaningful_keys):
            return True
        if "source" in pred and len(pred) > 1:
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. Диагностика источников
# ---------------------------------------------------------------------------

def diagnose_sources() -> dict:
    """Диагностика всех коллекторов — мета, ключи, статистика."""
    print("\n" + "=" * 60)
    print("📡 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print("=" * 60)

    sources = {}

    # --- SharpAPI ---
    try:
        sharpapi_meta = get_key("sharpapi:meta")
        sharpapi_key = "✅" if os.getenv("SHARPAPI_API_KEY") or os.getenv("SHARP_API_KEY") else "❌"
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
    except Exception as e:
        logger.warning(f"SharpAPI diagnostics failed: {e}")
        sources["sharpapi"] = {"error": str(e)}

    # --- Bzzoiro ---
    try:
        bzzoiro_meta = get_key("bzzoiro:meta")
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
            events_only = bzzoiro_meta.get("events_only", False)
            sources["bzzoiro"] = {
                "key": bzzoiro_key,
                "events": events,
                "stored": stored,
                "created": created,
                "updated": updated,
                "odds_enriched": odds,
                "predictions_enriched": predictions,
                "stats_enriched": stats,
                "h2h_enriched": h2h,
                "errors": error_count,
                "last_run": last_run,
            }
            print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, событий {events}, "
                  f"записано {stored} (создано {created}, обновлено {updated})")
            print(f"           Enrichment: odds={odds}, pred={predictions}, "
                  f"stats={stats}, h2h={h2h}, errors={error_count}")
            if events_only:
                print(f"           ⚠️ Режим events-only — predictions/stats/h2h пропущены")
            print(f"           last_run: {last_run}")
        else:
            sources["bzzoiro"] = {"key": bzzoiro_key, "last_run": "нет данных"}
            print(f"   🐝 Bzzoiro:  ключ {bzzoiro_key}, мета отсутствует")
    except Exception as e:
        logger.warning(f"Bzzoiro diagnostics failed: {e}")
        sources["bzzoiro"] = {"error": str(e)}

    # --- OddsAPI ---
    try:
        oddsapi_meta = get_key("odds_api:meta")
        oddsapi_key = "✅" if os.getenv("ODDS_API_KEY") else "❌"
        if oddsapi_meta and isinstance(oddsapi_meta, dict):
            events = oddsapi_meta.get("total_events", 0)
            stored = oddsapi_meta.get("stored_matches", 0)
            created = oddsapi_meta.get("created", 0)
            updated = oddsapi_meta.get("updated", 0)
            error_count = oddsapi_meta.get("error_count", oddsapi_meta.get("errors", 0))
            last_run = oddsapi_meta.get("last_run", "неизвестно")
            quota_remaining = oddsapi_meta.get("quota_remaining", "?")
            quota_used = oddsapi_meta.get("quota_used", "?")
            sources["odds_api"] = {
                "key": oddsapi_key,
                "events": events,
                "stored": stored,
                "created": created,
                "updated": updated,
                "errors": error_count,
                "quota_remaining": quota_remaining,
                "quota_used": quota_used,
                "last_run": last_run,
            }
            print(f"   🎲 OddsAPI:  ключ {oddsapi_key}, событий {events}, "
                  f"записано {stored} (создано {created}, обновлено {updated}), "
                  f"ошибок {error_count}")
            print(f"           Quota: осталось {quota_remaining}, использовано {quota_used}")
            print(f"           last_run: {last_run}")
        else:
            sources["odds_api"] = {"key": oddsapi_key, "last_run": "нет данных"}
            print(f"   🎲 OddsAPI:  ключ {oddsapi_key}, мета отсутствует")
    except Exception as e:
        logger.warning(f"OddsAPI diagnostics failed: {e}")
        sources["odds_api"] = {"error": str(e)}

    # --- Propline ---
    try:
        propline_meta = get_key("propline:meta")
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
            print(f"           Quota: осталось {quota_remaining}, использовано {quota_used}")
            print(f"           last_run: {last_run}")
        else:
            sources["propline"] = {"key": propline_key, "last_run": "нет данных"}
            print(f"   🏆 Propline: ключ {propline_key}, мета отсутствует")
    except Exception as e:
        logger.warning(f"Propline diagnostics failed: {e}")
        sources["propline"] = {"error": str(e)}

    # --- Football-Data ---
    fd_key = "✅" if os.getenv("FOOTBALL_DATA_API_KEY") else "❌"
    sources["football_data"] = {"key": fd_key}
    print(f"   🌐 Football-Data: ключ {fd_key}")

    return sources


# ---------------------------------------------------------------------------
# 2. Диагностика Redis
# ---------------------------------------------------------------------------

def diagnose_redis() -> dict:
    """Диагностика Redis — ключи, память, health."""
    print("\n" + "=" * 60)
    print("🗄 ДИАГНОСТИКА REDIS")
    print("=" * 60)

    try:
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

        health = get_key("system:health") or {}
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
    except Exception as e:
        logger.error(f"Redis diagnostics failed: {e}")
        print(f"   ❌ Ошибка диагностики Redis: {e}")
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 3. Диагностика матчей
# ---------------------------------------------------------------------------

def diagnose_matches() -> dict:
    """Диагностика матчей — odds, stats, predictions, value, sources."""
    print("\n" + "=" * 60)
    print("⚽ ДИАГНОСТИКА МАТЧЕЙ")
    print("=" * 60)

    try:
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
            return {"total": 0, "with_odds": 0, "with_stats": 0,
                    "with_predictions": 0, "with_value": 0}

        total = len(matches)
        with_odds = 0
        with_stats = 0
        with_predictions = 0
        with_h2h = 0
        with_value = 0
        source_counts = {}
        now = datetime.now(MSK_TIMEZONE)
        ages = []
        _odds_debug_samples = []
        _odds_debug_no_odds = []

        for match_id, match in matches.items():
            if not isinstance(match, dict):
                continue

            # --- Odds (3 формата §1.21) ---
            odds_found = _get_current_odds(match)
            if odds_found:
                with_odds += 1
                if len(_odds_debug_samples) < 3:
                    _odds_debug_samples.append({
                        "id": match_id,
                        "odds_keys": list(match.get("odds", {}).keys()) if isinstance(match.get("odds"), dict) else [],
                        "odds_sample": json.dumps(match.get("odds", {}), ensure_ascii=False)[:300],
                    })
            else:
                if len(_odds_debug_no_odds) < 3:
                    odds_raw = match.get("odds")
                    _odds_debug_no_odds.append({
                        "id": match_id,
                        "odds_type": type(odds_raw).__name__,
                        "odds_value": json.dumps(odds_raw, ensure_ascii=False)[:200] if odds_raw else "None/empty",
                        "sources": match.get("sources", []),
                    })

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

            # --- Predictions ---
            if _has_meaningful_pred(match):
                with_predictions += 1

            # --- H2H ---
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

        if _odds_debug_samples:
            print(f"\n   🔍 Образцы odds (найдены):")
            for s in _odds_debug_samples:
                print(f"     • {s['id']}: keys={s['odds_keys']}")
                print(f"       {s['odds_sample']}")

        if _odds_debug_no_odds:
            print(f"\n   🔍 Образцы без odds:")
            for s in _odds_debug_no_odds:
                print(f"     • {s['id']}: type={s['odds_type']}, sources={s['sources']}")
                print(f"       {s['odds_value']}")

        return result
    except Exception as e:
        logger.error(f"Match diagnostics failed: {e}")
        print(f"   ❌ Ошибка диагностики матчей: {e}")
        return {"total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# 4. Диагностика ошибок
# ---------------------------------------------------------------------------

def diagnose_errors() -> dict:
    """Диагностика ошибок — error_count из мета всех коллекторов + CAS."""
    print("\n" + "=" * 60)
    print("🚨 ДИАГНОСТИКА ОШИБОК")
    print("=" * 60)

    errors = []

    # Bzzoiro
    try:
        bzzoiro_meta = get_key("bzzoiro:meta")
        if bzzoiro_meta and isinstance(bzzoiro_meta, dict):
            err_count = bzzoiro_meta.get("error_count", 0)
            enrichment = bzzoiro_meta.get("enrichment", {})
            if isinstance(enrichment, dict):
                err_count = max(err_count, enrichment.get("errors", 0))
            err_count = max(err_count, bzzoiro_meta.get("errors", 0))
            if err_count > 0:
                errors.append({"source": "bzzoiro", "count": err_count})
                print(f"   🐝 Bzzoiro: {err_count} ошибок")
    except Exception as e:
        logger.warning(f"Bzzoiro error check failed: {e}")

    # SharpAPI
    try:
        sharpapi_meta = get_key("sharpapi:meta")
        if sharpapi_meta and isinstance(sharpapi_meta, dict):
            err_count = sharpapi_meta.get("error_count", sharpapi_meta.get("errors", 0))
            if err_count > 0:
                errors.append({"source": "sharpapi", "count": err_count})
                print(f"   📊 SharpAPI: {err_count} ошибок")
    except Exception as e:
        logger.warning(f"SharpAPI error check failed: {e}")

    # OddsAPI
    try:
        oddsapi_meta = get_key("odds_api:meta")
        if oddsapi_meta and isinstance(oddsapi_meta, dict):
            err_count = oddsapi_meta.get("error_count", oddsapi_meta.get("errors", 0))
            if err_count > 0:
                errors.append({"source": "odds_api", "count": err_count})
                print(f"   🎲 OddsAPI: {err_count} ошибок")
    except Exception as e:
        logger.warning(f"OddsAPI error check failed: {e}")

    # Propline
    try:
        propline_meta = get_key("propline:meta")
        if propline_meta and isinstance(propline_meta, dict):
            err_count = propline_meta.get("error_count", 0)
            if err_count > 0:
                errors.append({"source": "propline", "count": err_count})
                print(f"   🏆 Propline: {err_count} ошибок")
    except Exception as e:
        logger.warning(f"Propline error check failed: {e}")

    # CAS conflicts
    try:
        health = get_key("system:health")
        if health and isinstance(health, dict):
            cas = health.get("cas_conflicts_total", 0)
            if cas > 0:
                errors.append({"source": "CAS", "count": cas})
                print(f"   🔒 CAS-конфликтов: {cas}")
    except Exception as e:
        logger.warning(f"CAS error check failed: {e}")

    if not errors:
        print("   ✅ Ошибок не обнаружено")

    return {"errors": errors}


# ---------------------------------------------------------------------------
# 5. Сводка
# ---------------------------------------------------------------------------

def print_summary(sources: dict, redis_data: dict, matches_data: dict, errors_data: dict) -> None:
    """Печать сводной информации."""
    print("\n" + "=" * 60)
    print("📋 СВОДКА")
    print("=" * 60)

    src_count = len(sources)
    enriching = sum(1 for s in sources.values()
                    if isinstance(s, dict) and (s.get("odds", 0) or s.get("predictions", 0)
                                                or s.get("odds_enriched", 0) or s.get("predictions_enriched", 0)))
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
    """Точка входа — полная или частичная диагностика."""
    print("=" * 60)
    print("🔧 ДИАГНОСТИКА GATEKEEPER-AI v710")
    print(f"   Время: {datetime.now(MSK_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    print("=" * 60)

    # --- CLI flags ---
    json_output = "--json" in sys.argv
    summary_only = "--summary" in sys.argv

    mode = "full"
    if "--sources" in sys.argv:
        mode = "sources"
    elif "--redis" in sys.argv:
        mode = "redis"
    elif "--matches" in sys.argv:
        mode = "matches"
    elif "--errors" in sys.argv:
        mode = "errors"

    if summary_only:
        mode = "full"

    # --- Инициализация Redis (§1.7a) ---
    if mode in ("full", "redis"):
        print("[DIAG] Инициализация Redis...")
        try:
            init = run_initialization(collector="source_diagnostics")
            if not init.get("redis_available"):
                print("[DIAG] ❌ Redis недоступен — диагностика ограничена")
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
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

    # --- JSON output ---
    if json_output:
        output = {
            "version": __version__,
            "timestamp": datetime.now(MSK_TIMEZONE).isoformat(),
            "sources": src_data,
            "redis": redis_data,
            "matches": matches_data,
            "errors": errors_data,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
