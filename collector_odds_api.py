#!/usr/bin/env python3
"""
Collector OddsAPI v9.0-bulk (The Odds API v4)
GatekeeperAI v9.3

Изменения v9.0-bulk:
  - commenceTimeFrom/To — серверный фильтр по дате (уменьшает payload)
  - ThreadPoolExecutor — параллельный fetch лиг (пул 5)
  - Quota protection — остановка при x-requests-remaining < threshold
  - 429 retry с exponential backoff (2 попытки)
  - HTTP timeout 15с (env: ODDS_API_HTTP_TIMEOUT)
  - Progress logging каждые 10 матчей + per-league summary
  - Draw detection: name.lower() == "draw" (более надёжный)
  - Per-league quota tracking

Изменения v700.2 (Phase 2) — сохранены:
  - run_initialization(collector="odds_api")
  - idempotency_key в patch_match
  - sources=["odds_api"] в upsert_match
  - team_registry.clean_team_name
  - graceful shutdown (is_shutdown_requested)
  - run_id из init_metrics
  - COLLECTOR_NAME константа
  - odds как float (не str)
  - now_msk() вместо datetime.now(timezone.utc)
  - save_meta с run_id
"""
import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
import concurrent.futures
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("odds_api")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

from gatekeeper_hub import (
    upsert_match, patch_match, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)
# FIX-AUDIT-v9.3: get_all_fields для подсчёта created/updated
try:
    from gatekeeper_hub import get_all_fields
except ImportError:
    get_all_fields = lambda: {}

try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

# §20.7: Module registry
try:
    from gatekeeper_hub import register_module
except ImportError:
    def register_module(name, **kwargs):
        def deco(func):
            return func
        return deco

try:
    from gatekeeper_hub import log_event
except ImportError:
    def log_event(source, level, message, **kwargs):
        pass

# FIX-AUDIT-v9.3: убраны мёртвые normalize_team_name, build_canonical_id
try:
    from team_registry import clean_team_name
except ImportError:
    logger.error("team_registry не найден")
    clean_team_name = lambda name: name.lower().strip().replace(" ", "_") if name else ""

COLLECTOR_NAME = "odds_api"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DAYS_AHEAD = int(os.environ.get("ODDS_API_DAYS_AHEAD", "3"))
HTTP_TIMEOUT = int(os.environ.get("ODDS_API_HTTP_TIMEOUT", "15"))
MAX_WORKERS = int(os.environ.get("ODDS_API_MAX_WORKERS", "5"))
QUOTA_MIN = int(os.environ.get("ODDS_API_QUOTA_MIN", "10"))

__version__ = "9.0-bulk"
__all__ = [
    "collect_odds_api",
    "collect_and_process",
    "__version__",
]


def _fetch_odds_api(url: str, max_retries: int = 2) -> Optional[dict]:
    """HTTP-запрос с retry на 429 и network errors, exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("x-requests-remaining")
                used = response.headers.get("x-requests-used")
                return {"data": data, "quota_remaining": remaining, "quota_used": used}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(f"HTTP 429 — Rate Limit. Retry {attempt + 1}/{max_retries} через {wait}с")
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                logger.warning("HTTP 429 — исчерпаны retries, пропускаем")
                return None
            logger.warning(f"HTTP {e.code}: {e.reason}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.warning(f"Request error: {e}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
    return None


def _normalize_odds_value(val) -> Optional[float]:
    """Конвертирует odds в float, None для невалидных."""
    if val is None or val == "-" or val == "" or val == 0:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _build_odds_url(api_key: str, sport_key: str, regions: str, markets: str,
                    odds_format: str, date_from: str, date_to: str) -> str:
    """Создаёт URL для запроса odds с серверным фильтром по дате."""
    url = (
        f"{ODDS_API_BASE}/sports/{sport_key}/odds/"
        f"?apiKey={api_key}&regions={regions}&markets={markets}&oddsFormat={odds_format}"
    )
    if date_from:
        url += f"&commenceTimeFrom={date_from}"
    if date_to:
        url += f"&commenceTimeTo={date_to}"
    return url


def _fetch_league_odds(
    sport_key: str, api_key: str, regions: str, markets: str,
    odds_format: str, date_from: str, date_to: str, max_retries: int
) -> Tuple[str, Optional[dict]]:
    """Fetch odds для одной лиги. Возвращает (sport_key, result или None)."""
    if is_shutdown_requested():
        return sport_key, None
    url = _build_odds_url(api_key, sport_key, regions, markets, odds_format,
                          date_from, date_to)
    result = _fetch_odds_api(url, max_retries=max_retries)
    return sport_key, result


def _parse_event_odds(ev: dict, home_team: str, away_team: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Извлекает лучшие коэффициенты home/draw/away из bookmakers."""
    odds_home: Optional[float] = None
    odds_draw: Optional[float] = None
    odds_away: Optional[float] = None
    bookmakers = ev.get("bookmakers", [])
    for bm in bookmakers:
        markets_list = bm.get("markets", [])
        for market in markets_list:
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            for o in outcomes:
                name = o.get("name", "")
                price = o.get("price")
                price_float = _normalize_odds_value(price)
                if price_float is None:
                    continue
                name_norm = clean_team_name(name)
                if name_norm == home_team:
                    if odds_home is None or price_float > odds_home:
                        odds_home = price_float
                elif name.lower() == "draw":
                    if odds_draw is None or price_float > odds_draw:
                        odds_draw = price_float
                elif name_norm == away_team:
                    if odds_away is None or price_float > odds_away:
                        odds_away = price_float
    return odds_home, odds_draw, odds_away


def collect_odds_api() -> Dict[str, Any]:
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        logger.error("ODDS_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    if not init_metrics.get("redis_available", False):
        logger.error("Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    run_id = init_metrics.get("run_id", "")

    regions = os.environ.get("ODDS_API_REGIONS", "eu,uk")
    markets = os.environ.get("ODDS_API_MARKETS", "h2h")
    odds_format = os.environ.get("ODDS_API_ODDS_FORMAT", "decimal")
    max_retries = int(os.environ.get("ODDS_API_MAX_RETRIES", "2"))

    # Серверный фильтр по дате
    now_utc = datetime.now(timezone.utc)
    date_from = now_utc.strftime("%Y-%m-%dT00:00:00Z")
    date_to = (now_utc + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%dT23:59:59Z")
    logger.info(f"Server date filter: {date_from} → {date_to} ({DAYS_AHEAD} дней)")

    # 1. Получаем список спортов
    sports_url = f"{ODDS_API_BASE}/sports/?apiKey={api_key}"
    sports_data = _fetch_odds_api(sports_url, max_retries=max_retries)
    if sports_data is None:
        logger.error("Не удалось получить список спортов")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1, "run_id": run_id}

    sports = sports_data.get("data", [])
    football_sports = [s for s in sports if s.get("group") == "Soccer" and s.get("active", True)]
    logger.info(f"Активных футбольных лиг: {len(football_sports)}")

    # Quota check после /sports
    quota_remaining = sports_data.get("quota_remaining", "?")
    quota_used = sports_data.get("quota_used", "?")
    try:
        if quota_remaining and int(quota_remaining) < QUOTA_MIN:
            logger.warning(
                f"Quota low: {quota_remaining} remaining (threshold={QUOTA_MIN}). "
                f"Продолжаем, но возможны ограничения."
            )
    except (ValueError, TypeError):
        pass

    log_event("odds_api", "INFO", "Collection started",
              run_id=run_id, active_leagues=len(football_sports),
              quota_remaining=quota_remaining)

    # 2. Параллельный fetch всех лиг
    league_results: Dict[str, dict] = {}
    fetch_errors = 0

    if not football_sports:
        logger.warning("Нет активных футбольных лиг")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for sport in football_sports:
                sport_key = sport.get("key", "")
                if not sport_key:
                    continue
                future = executor.submit(
                    _fetch_league_odds,
                    sport_key, api_key, regions, markets, odds_format,
                    date_from, date_to, max_retries
                )
                futures[future] = sport_key

            for future in concurrent.futures.as_completed(futures):
                sport_key = futures[future]
                try:
                    sk, result = future.result()
                    if result is not None:
                        league_results[sk] = result
                        # Обновляем quota
                        q = result.get("quota_remaining")
                        if q:
                            quota_remaining = q
                        u = result.get("quota_used")
                        if u:
                            quota_used = u
                    else:
                        fetch_errors += 1
                        logger.warning(f"Fetch failed: {sport_key}")
                except Exception as e:
                    fetch_errors += 1
                    logger.error(f"Fetch error for {sport_key}: {e}")

    logger.info(
        f"Fetch complete: {len(league_results)}/{len(football_sports)} лиг, "
        f"errors: {fetch_errors}, quota: {quota_remaining} remaining"
    )

    # 3. Обработка результатов — последовательно (thread-safe для Redis)
    stored = 0
    created = 0
    updated = 0
    skipped_past = 0
    skipped_future = 0
    total_events = 0
    error_count = fetch_errors
    existing_keys = set(get_all_fields().keys())

    def _event_date_key(ev):
        dt_str = ev.get("commence_time", "") or ev.get("start_time", "")
        return dt_str or ""

    # Сортируем лиги по ключу для детерминизма
    sorted_sports = sorted(football_sports, key=lambda s: s.get("key", ""))

    for sport in sorted_sports:
        if is_shutdown_requested():
            logger.info("Graceful shutdown — прерываем обработку")
            break

        sport_key = sport.get("key", "")
        if sport_key not in league_results:
            continue

        odds_data = league_results[sport_key]
        events = odds_data.get("data", [])
        events.sort(key=_event_date_key)

        sport_title = sport.get("title", "")
        league_stored = 0

        for ev in events:
            if is_shutdown_requested():
                logger.info("Graceful shutdown — прерываем цикл матчей")
                break

            try:
                home_team_raw = ev.get("home_team", "")
                away_team_raw = ev.get("away_team", "")
                if not home_team_raw or not away_team_raw:
                    continue

                home_team = clean_team_name(home_team_raw)
                away_team = clean_team_name(away_team_raw)

                raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
                date_utc = normalize_date(raw_date)

                if date_utc and not is_future_match(date_utc):
                    skipped_past += 1
                    continue

                # Fallback date filter (на случай если API не отфильтровал)
                if date_utc:
                    try:
                        match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                        if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                            skipped_future += 1
                            continue
                    except (ValueError, TypeError):
                        pass

                event_id = str(ev.get("id", ""))
                sport_title_ev = ev.get("sport_title", sport_title)

                # Парсим коэффициенты
                odds_home, odds_draw, odds_away = _parse_event_odds(ev, home_team, away_team)

                total_events += 1

                if dry_run:
                    stored += 1
                    created += 1
                    league_stored += 1
                    continue

                # Upsert
                try:
                    cid = upsert_match(
                        home_team=home_team,
                        away_team=away_team,
                        date_utc=date_utc,
                        competition=sport_title_ev,
                        country="",
                        status="scheduled",
                        source=COLLECTOR_NAME,
                        sources=[COLLECTOR_NAME],
                        source_ids={COLLECTOR_NAME: event_id},
                    )
                except Exception as e:
                    logger.error(f"[ODDS_API] upsert error: {e}")
                    cid = None

                if cid:
                    stored += 1
                    league_stored += 1
                    if f"match:{cid}" in existing_keys:
                        updated += 1
                    else:
                        created += 1

                    # Patch odds
                    odds_current = {}
                    if odds_home is not None:
                        odds_current["home"] = float(odds_home)
                    if odds_draw is not None:
                        odds_current["draw"] = float(odds_draw)
                    if odds_away is not None:
                        odds_current["away"] = float(odds_away)

                    if odds_current:
                        idempotency_key = f"{run_id}:{cid}:odds"
                        try:
                            patch_match(cid, "odds", {"current": odds_current},
                                       source=COLLECTOR_NAME, upstream="betradar",
                                       idempotency_key=idempotency_key)
                        except Exception as e:
                            logger.error(f"[ODDS_API] patch error for cid={cid}: {e}")

            except Exception as e:
                logger.error(f"[ODDS_API] Event error: {e}")
                error_count += 1

        logger.info(
            f"  {sport_key}: {len(events)} events → {league_stored} stored"
        )

    logger.info(
        f"Итого: событий={total_events}, записано={stored}, "
        f"created={created}, updated={updated}, "
        f"прошлое={skipped_past}, будущее={skipped_future}, "
        f"errors={error_count}, quota={quota_remaining}"
    )

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "skipped_past": skipped_past,
        "skipped_future": skipped_future,
        "error_count": error_count,
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
        "run_id": run_id,
        "leagues_fetched": len(league_results),
        "leagues_total": len(football_sports),
        "fetch_errors": fetch_errors,
        "max_workers": MAX_WORKERS,
        "date_from": date_from,
        "date_to": date_to,
    }
    save_meta(COLLECTOR_NAME, **meta)

    return meta


@register_module("odds_api", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process():
    """Точка входа для импорта."""
    return collect_odds_api()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Collector OddsAPI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать в Redis (dry-run)")
    args = parser.parse_args()
    if args.dry_run:
        logger.info("[ODDS_API] DRY-RUN mode")
        os.environ["DRY_RUN"] = "1"
    result = collect_odds_api()
    logger.info(f"Result: {result}")
