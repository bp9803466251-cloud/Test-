#!/usr/bin/env python3
"""
collector_bzzoiro.py — PRIMARY Ingestion Collector для Bzzoiro (v600-prod-fix11 адаптированный).
Сбор сетки матчей, коэффициентов, статистики и H2H через точечные эндпоинты (/odds, /h2h, /stats).
"""

import os
import sys
import time
import json
import datetime as dt
from typing import Any
import logging

# Использование curl_cffi с impersonate="chrome" для обхода WAF/Cloudflare внешних API (§1.3, §1.23)
try:
    from curl_cffi import requests
except ImportError:
    import requests  # fallback, но рекомендуется curl_cffi

# ─── Logging setup ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [BZZOIRO] %(message)s'
)
logger = logging.getLogger("collector_bzzoiro")

try:
    from gatekeeper_hub import (
        run_initialization,
        upsert_match,
        patch_match,
        get_all_fields,
        normalize_date,
        is_future_match,
        now_msk,
        save_meta,
    )
except ImportError:
    logger.error("gatekeeper_hub не найден")
    sys.exit(1)

# ─── Константы и маркеры ───
_NOT_FOUND = object()
DEBUG_EVENT_COUNT = int(os.environ.get("DEBUG_EVENT_COUNT", "3"))

BZZOIRO_BASE = os.environ.get("BZZOIRO_BASE_URL", "https://sports.bzzoiro.com/api/v2")
BZZOIRO_API_KEY = os.environ.get("BZZOIRO_API_KEY", "")
RATE_DELAY = float(os.environ.get("BZZOIRO_RATE_DELAY", "0.3"))
ENRICH_DELAY = float(os.environ.get("BZZOIRO_ENRICH_DELAY", "0.2"))
MAX_RETRIES = int(os.environ.get("BZZOIRO_MAX_RETRIES", "1"))
DAYS_AHEAD = 1
PAGE_LIMIT = 200

FINISHED_STATUSES = {"finished", "completed", "ended", "cancelled", "awarded", "forfeited"}
PREMATCH_STATUSES = ("notstarted", "", "scheduled", "postponed")

# ─── Кэширование ───
_cache = {}

def _cache_response(url: str, data: Any):
    _cache[url] = data

def _check_cache(url: str) -> Any:
    return _cache.get(url)


def _fetch_bzzoiro(url: str, headers: dict, max_retries: int = 1) -> Any:
    cached_data = _check_cache(url)
    if cached_data is not None:
        return cached_data

    for attempt in range(max_retries + 1):
        try:
            try:
                resp = requests.get(url, headers=headers, timeout=30, impersonate="chrome")
            except TypeError:
                resp = requests.get(url, headers=headers, timeout=30)

            if resp.status_code == 404:
                _cache_response(url, _NOT_FOUND)
                return _NOT_FOUND
            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                logger.warning(f"Rate limit. Waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                if attempt < max_retries:
                    time.sleep(RATE_DELAY * 2)
                    continue
                logger.error(f"HTTP {resp.status_code}: {url}")
                return None

            data = resp.json()
            _cache_response(url, data)
            time.sleep(RATE_DELAY)
            return data

        except Exception as e:
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 2)
                continue
            logger.error(f"Request error on {url}: {e}")
            return None


def _parse_event_date(ev: dict) -> dt.datetime:
    date_str = _extract_date(ev)
    if not date_str:
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        try:
            return dt.datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            return dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def _extract_date(ev: dict) -> str:
    for key in ("event_date", "date", "date_utc", "match_date", "time", "start_time", "datetime"):
        val = ev.get(key)
        if val:
            return str(val)
    timestamp = ev.get("timestamp")
    if timestamp:
        try:
            return dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc).isoformat()
        except Exception:
            pass
    return ""


def _extract_score(ev: dict) -> Any:
    score = ev.get("score") or ev.get("scores")
    if isinstance(score, dict):
        return {
            "home": score.get("home", score.get("home_score")),
            "away": score.get("away", score.get("away_score"))
        }
    home_score = ev.get("home_score")
    away_score = ev.get("away_score")
    if home_score is not None or away_score is not None:
        return {"home": home_score, "away": away_score}
    return None


def _extract_best_odds(odds_data: Any) -> dict:
    """Универсальный парсер котировок под строгие требования _validate_section хаба."""
    flat_1x2 = {}
    items = []
    if isinstance(odds_data, dict):
        items = odds_data.get("results", odds_data.get("data", [odds_data]))
    elif isinstance(odds_data, list):
        items = odds_data

    for item in items:
        if not isinstance(item, dict):
            continue
        market = item.get("market", "1x2")
        outcome = item.get("outcome", "")
        price = item.get("decimal_odds", item.get("price", item.get("odd")))

        if price is None:
            for k, v in item.items():
                if isinstance(v, (int, float, str)) and v not in ("-", ""):
                    try:
                        price = float(v)
                        outcome = k
                        break
                    except ValueError:
                        pass

        if price is None:
            continue

        try:
            val = float(price)
        except (ValueError, TypeError):
            continue

        oc_upper = str(outcome).upper()
        if market in ("1x2", "match_result", "full_time"):
            if oc_upper in ("HOME", "1", "HOME_WIN"):
                flat_1x2["home"] = val
            elif oc_upper in ("DRAW", "X", "DRAW_X"):
                flat_1x2["draw"] = val
            elif oc_upper in ("AWAY", "2", "AWAY_WIN"):
                flat_1x2["away"] = val

    if not flat_1x2 and isinstance(odds_data, dict):
        odds = odds_data.get("odds", odds_data)
        if isinstance(odds, dict):
            for k, v in odds.items():
                if k in ("1", "home", "home_win") and v:
                    flat_1x2["home"] = float(v)
                elif k in ("X", "draw") and v:
                    flat_1x2["draw"] = float(v)
                elif k in ("2", "away", "away_win") and v:
                    flat_1x2["away"] = float(v)

    return flat_1x2


def _flatten_stats(stats_data: dict, parent_key: str = "", sep: str = "_") -> dict:
    items = []
    if not isinstance(stats_data, dict):
        return {}
    for k, v in stats_data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_stats(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


# ─── League pre-fetch ───
_league_cache: dict[int, dict] = {}

def _fetch_league_info(league_id: int, headers: dict) -> dict:
    if not league_id:
        return {"name": "", "country": ""}

    if league_id in _league_cache:
        return _league_cache[league_id]

    url = f"{BZZOIRO_BASE}/leagues/{league_id}"
    data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)

    if data is _NOT_FOUND or not data or not isinstance(data, dict):
        result = {"name": "", "country": ""}
    else:
        name = data.get("name", "") or ""
        country = data.get("country", "") or ""
        result = {"name": name, "country": country}

    _league_cache[league_id] = result
    return result


def collect_bzzoiro(events_only: bool = False) -> dict:
    headers = {
        "Authorization": f"Token {BZZOIRO_API_KEY}",
        "Accept": "application/json",
    }

    print("=" * 60)
    print("[BZZOIRO] Collector Bzzoiro v600-prod-fix11 (точечные эндпоинты) started.")
    print("=" * 60)

    # ─── Шаг 0: Инициализация Redis ───
    print("[BZZOIRO] Шаг 0: Инициализация Redis...")
    init_metrics = run_initialization()
    if not init_metrics or not init_metrics.get("redis_available"):
        print("[BZZOIRO] ERROR: Redis init failed")
        save_meta("bzzoiro", stored_matches=0, error_count=1)
        return {"error": "redis_init_failed"}

    print(f"[BZZOIRO] Cleanup: {init_metrics.get('cleanup_count', 0)} ключей удалено")
    existing_keys = set(get_all_fields().keys())

    # ─── Шаг 1: Загрузка событий ───
    print("[BZZOIRO] Шаг 1: Загрузка событий...")
    now = dt.datetime.now(dt.timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + dt.timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    pages_fetched = 0
    url = f"{BZZOIRO_BASE}/events?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"

    while url:
        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        pages_fetched += 1

        if data is None:
            print(f"[BZZOIRO] Ошибка загрузки страницы {pages_fetched}")
            break
        if data is _NOT_FOUND:
            break

        if isinstance(data, dict):
            items = data.get("results", data.get("data", data.get("events", [])))
        elif isinstance(data, list):
            items = data
        else:
            break

        if not isinstance(items, list) or not items:
            break

        all_events.extend(items)
        total_count = data.get("count", 0) if isinstance(data, dict) else 0
        print(f"[BZZOIRO] Загружено: {len(all_events)}/{total_count}")

        url = data.get("next") if isinstance(data, dict) else None
        if not url or (total_count and len(all_events) >= total_count):
            break

    all_events.sort(key=_parse_event_date)
    print(f"[BZZOIRO] Всего событий: {len(all_events)} (страниц: {pages_fetched})")

    # ─── Шаг 1.5: Pre-fetch лиг ───
    league_ids = {ev.get("league_id") for ev in all_events if ev.get("league_id")}
    if league_ids:
        print(f"[BZZOIRO] Шаг 1.5: Pre-fetch {len(league_ids)} лиг...")
        for lid in league_ids:
            _fetch_league_info(lid, headers)
            time.sleep(RATE_DELAY)

    # ─── Шаг 2: Запись матчей ───
    print("[BZZOIRO] Шаг 2: Запись матчей в Redis...")
    stored_matches: list[tuple[str, int, dict]] = []
    created, updated, skipped_past, skipped_finished, deduped, no_date_count = 0, 0, 0, 0, 0, 0
    seen = set()

    for idx, ev in enumerate(all_events):
        bzzoiro_id = ev.get("id")
        if not bzzoiro_id:
            continue

        home_team = ev.get("home_team", "")
        away_team = ev.get("away_team", "")
        if not home_team or not away_team:
            continue

        status = ev.get("status", "scheduled") or "scheduled"
        if status in FINISHED_STATUSES:
            skipped_finished += 1
            continue

        date_str = _extract_date(ev)
        dedup_key = f"{home_team}|{away_team}|{date_str}"
        if dedup_key in seen:
            deduped += 1
            continue
        seen.add(dedup_key)

        if not date_str:
            no_date_count += 1

        if date_str and not is_future_match(date_str):
            skipped_past += 1
            continue

        league_id = ev.get("league_id")
        league_info = _fetch_league_info(league_id, headers) if league_id else {"name": "", "country": ""}
        competition = league_info.get("name", "") or ev.get("stage_name", "") or ev.get("stage", "")
        country = league_info.get("country", "")

        extra = {
            "source_ids": {"bzzoiro": str(bzzoiro_id)},
        }

        result_id = upsert_match(
            home_team=home_team,
            away_team=away_team,
            date_utc=date_str,
            competition=competition,
            country=country,
            status=status,
            source="bzzoiro",
            **extra,
        )
        if result_id:
            if f"match:{result_id}" in existing_keys:
                updated += 1
            else:
                created += 1
            stored_matches.append((result_id, bzzoiro_id, ev))
        else:
            skipped_past += 1

    print(f"[BZZOIRO] Записано {len(stored_matches)} матчей (создано {created}, обновлено {updated})")

    # ─── Шаг 3: Enrichment (через точечные эндпоинты) ───
    odds_enriched = 0
    stats_enriched = 0
    h2h_enriched = 0
    score_enriched = 0
    enrichment_errors = 0
    not_found = 0

    if events_only:
        print("[BZZOIRO] Режим events-only — enrichment пропущен")
    elif stored_matches:
        print(f"[BZZOIRO] Шаг 3: Обогащение {len(stored_matches)} матчей через точечные эндпоинты...")

        for idx, (cid, bzzoiro_id, ev) in enumerate(stored_matches):
            status = ev.get("status", "")
            is_prematch = status in PREMATCH_STATUSES

            # ─── Score ───
            if not is_prematch:
                score = _extract_score(ev)
                if score:
                    if patch_match(cid, "score", score, source="bzzoiro"):
                        score_enriched += 1
                    else:
                        enrichment_errors += 1

            # ─── Odds (/events/{id}/odds) ───
            odds_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/odds"
            odds_data = _fetch_bzzoiro(odds_url, headers, max_retries=MAX_RETRIES)

            if odds_data is _NOT_FOUND:
                not_found += 1
            elif odds_data and odds_data is not _NOT_FOUND:
                best_odds = _extract_best_odds(odds_data)
                if best_odds:
                    if idx < 3:
                        logger.info(f"DEBUG ODDS payload for match {cid}: {best_odds}")
                    if patch_match(cid, "odds", best_odds, source="bzzoiro"):
                        odds_enriched += 1
                    else:
                        logger.warning(f"ODDS validation FAILED for match {cid} with data: {best_odds}")
                        enrichment_errors += 1
                else:
                    not_found += 1
            else:
                enrichment_errors += 1

            time.sleep(ENRICH_DELAY)

            # ─── H2H (/events/{id}/h2h с fallback на head_to_head) ───
            h2h_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/h2h"
            h2h_data = _fetch_bzzoiro(h2h_url, headers, max_retries=MAX_RETRIES)

            if h2h_data is _NOT_FOUND:
                h2h_url_legacy = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/head_to_head"
                h2h_data = _fetch_bzzoiro(h2h_url_legacy, headers, max_retries=MAX_RETRIES)

            if h2h_data is _NOT_FOUND:
                not_found += 1
            elif h2h_data and isinstance(h2h_data, dict):
                inner_h2h = h2h_data.get("head_to_head", h2h_data.get("h2h", h2h_data))
                if isinstance(inner_h2h, dict) and inner_h2h:
                    if idx < 3:
                        logger.info(f"DEBUG H2H payload for match {cid}: {list(inner_h2h.keys())}")
                    if patch_match(cid, "h2h", inner_h2h, source="bzzoiro"):
                        h2h_enriched += 1
                    else:
                        logger.warning(f"H2H validation FAILED for match {cid}")
                        enrichment_errors += 1
                else:
                    not_found += 1
            else:
                enrichment_errors += 1

            time.sleep(ENRICH_DELAY)

            # ─── Stats (/events/{id}/stats) ───
            if not is_prematch:
                stats_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/stats"
                stats_data = _fetch_bzzoiro(stats_url, headers, max_retries=MAX_RETRIES)

                if stats_data is _NOT_FOUND:
                    not_found += 1
                elif stats_data and isinstance(stats_data, dict):
                    inner_stats = stats_data.get("stats", stats_data)
                    if isinstance(inner_stats, dict) and inner_stats:
                        flat_stats = _flatten_stats(inner_stats)
                        if any(v is not None for v in flat_stats.values()):
                            if patch_match(cid, "stats", flat_stats, source="bzzoiro"):
                                stats_enriched += 1
                            else:
                                logger.warning(f"STATS validation FAILED for match {cid}")
                                enrichment_errors += 1
                        else:
                            not_found += 1
                    else:
                        not_found += 1
                else:
                    enrichment_errors += 1

                time.sleep(ENRICH_DELAY)

            if (idx + 1) % 50 == 0:
                print(f"[BZZOIRO] Обогащение: {idx + 1}/{len(stored_matches)} (odds={odds_enriched}, stats={stats_enriched}, h2h={h2h_enriched})")

    # ─── Шаг 4 & 5: Итоги и сохранение мета ───
    result = {
        "last_run": now_msk(),
        "total_events": len(all_events),
        "stored_matches": len(stored_matches),
        "created": created,
        "updated": updated,
        "error_count": enrichment_errors,
        "pages_fetched": pages_fetched,
        "enrichment": {
            "odds": odds_enriched,
            "stats": stats_enriched,
            "h2h": h2h_enriched,
            "score": score_enriched,
            "errors": enrichment_errors,
            "not_found": not_found,
        },
    }
    
    save_meta("bzzoiro",
              total_events=len(all_events),
              stored_matches=len(stored_matches),
              created=created,
              updated=updated,
              skipped_past=skipped_past,
              deduped=deduped,
              pages_fetched=pages_fetched,
              error_count=enrichment_errors,
              h2h_count=h2h_enriched)

    print(f"[BZZOIRO] Пайплайн успешно завершен: {result}")
    return result


if __name__ == "__main__":
    events_only = "--events-only" in sys.argv
    collect_bzzoiro(events_only=events_only)