#!/usr/bin/env python3
"""
Collector Propline v9.0-bulk для Gatekeeper-AI v9.3-audited.
Получает матчи и коэффициенты через PropLine API, сохраняет в Redis через gatekeeper_hub.

Изменения v9.0-bulk:
  - Динамические лиги: GET /sports → все soccer_* (31+ вместо 6 хардкод)
  - Параллельный fetch: ThreadPoolExecutor, пул 8 (вместо последовательного)
  - Markets: только h2h (вместо h2h,spreads,totals) — меньше payload
  - Bookmakers: только pinnacle — острая линия
  - Quota protection: X-Daily-Remaining < quota_min → остановка
  - Правильный парсинг The Odds API: bookmakers[].markets[].outcomes[]
  - American → decimal конвертация
  - Без per-event delay (bulk = 1 запрос на лигу)
  - Progress logging каждые 10 матчей
  - Fallback: 32 soccer-лиги, если /sports недоступен

FIX v810:
  - Использует gatekeeper_hub (upsert_match, patch_match, save_meta) — §1.4
  - canonical_id через build_canonical_id — §1.6
  - clean_team_name из team_registry — §2.4
  - run_initialization(collector="propline") — §2.6
  - graceful shutdown (is_shutdown_requested) — §2.3
"""
import os
import sys
import time
import json
import argparse
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("propline")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

# ── gatekeeper_hub ─────────────────────────────────────────
try:
    from gatekeeper_hub import (
        run_initialization,
        upsert_match,
        patch_match,
        normalize_date,
        is_future_match,
        now_msk,
        save_meta,
    )
except ImportError:
    logger.error("gatekeeper_hub не найден")
    sys.exit(1)

try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

try:
    from gatekeeper_hub import get_all_fields
except ImportError:
    get_all_fields = lambda: {}

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

try:
    from team_registry import clean_team_name
except ImportError:
    logger.error("team_registry не найден — clean_team_name fallback")
    def clean_team_name(name: str) -> str:
        return name.lower().strip().replace(" ", "_") if name else ""

COLLECTOR_NAME = "propline"
BASE_URL = os.environ.get("PROPLINE_BASE_URL", "https://api.prop-line.com/v1")
DAYS_AHEAD = int(os.environ.get("PROPLINE_DAYS_AHEAD", "3"))
MAX_RETRIES = int(os.environ.get("PROPLINE_MAX_RETRIES", "2"))
HTTP_TIMEOUT = int(os.environ.get("PROPLINE_API_TIMEOUT", "10"))
MAX_WORKERS = int(os.environ.get("PROPLINE_MAX_WORKERS", "8"))
QUOTA_MIN = int(os.environ.get("PROPLINE_QUOTA_MIN", "10"))

# Fallback: 32 soccer-лиги (если /sports недоступен)
FALLBACK_LEAGUES = [
    "soccer_epl", "soccer_la_liga", "soccer_serie_a",
    "soccer_bundesliga", "soccer_ligue_1", "soccer_mls",
    "soccer_portugal_primeira_liga", "soccer_netherlands_eredivisie",
    "soccer_belgium_first_div", "soccer_turkey_super_league",
    "soccer_spain_segunda_division", "soccer_italy_serie_b",
    "soccer_germany_bundesliga2", "soccer_france_ligue2",
    "soccer_england Championship", "soccer_england_league1",
    "soccer_england_league2", "soccer_england_efl_cup",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_brazil_serie_a", "soccer_argentina_primera_division",
    "soccer_mexico_ligamx", "soccer_japan_j_league",
    "soccer_korea_kleague1", "soccer_china_superleague",
    "soccer_saudi_pro_league", "soccer_austria_bundesliga",
    "soccer_switzerland_superleague", "soccer_denmark_superliga",
    "soccer_sweden_allsvenskan",
]

__version__ = "9.0-bulk"
__all__ = ["collect_propline", "collect_and_process", "__version__"]

# Quota tracking
_quota_remaining: Optional[int] = None


def _american_to_decimal(odds_val) -> Optional[float]:
    """Конвертирует american odds в decimal. None для невалидных."""
    if odds_val is None or odds_val == 0:
        return None
    try:
        ov = float(odds_val)
    except (ValueError, TypeError):
        return None
    if ov > 0:
        return (ov / 100) + 1
    elif ov < 0:
        return (100 / abs(ov)) + 1
    return None


def _fetch_sports() -> List[str]:
    """GET /sports — динамическое получение списка soccer-лиг."""
    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        return FALLBACK_LEAGUES

    url = f"{BASE_URL}/sports?apiKey={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GatekeeperAI"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
            remaining = response.headers.get("X-Daily-Remaining")
            if remaining:
                global _quota_remaining
                _quota_remaining = int(remaining)
                logger.info(f"Quota remaining: {remaining}")

            sports = data if isinstance(data, list) else data.get("sports", [])
            soccer_keys = []
            for sport in sports:
                key = sport.get("key", "")
                if key.startswith("soccer_"):
                    soccer_keys.append(key)

            if soccer_keys:
                logger.info(f"Dynamic leagues: {len(soccer_keys)} soccer leagues discovered")
                return soccer_keys
            else:
                logger.info("No soccer leagues in /sports — using fallback")
                return FALLBACK_LEAGUES
    except Exception as e:
        logger.warning(f"GET /sports failed: {e} — using fallback ({len(FALLBACK_LEAGUES)} leagues)")
        return FALLBACK_LEAGUES


def _fetch_odds(sport_key: str, max_retries: int = 2, timeout: int = 10) -> Optional[dict]:
    """Получает odds от PropLine API для одной лиги (bulk endpoint)."""
    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        return None

    # Только h2h, только pinnacle — острая линия, минимум payload
    url = (f"{BASE_URL}/sports/{sport_key}/odds"
           f"?apiKey={api_key}&markets=h2h&bookmakers=pinnacle")

    global _quota_remaining
    if _quota_remaining is not None and _quota_remaining < QUOTA_MIN:
        logger.warning(f"Quota low ({_quota_remaining} < {QUOTA_MIN}) — остановка")
        return None

    for attempt in range(max_retries):
        if is_shutdown_requested():
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GatekeeperAI"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

                # Update quota
                remaining = response.headers.get("X-Daily-Remaining")
                if remaining:
                    _quota_remaining = int(remaining)

                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                logger.warning(f"Rate limit on {sport_key}. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            if e.code == 404:
                # Нет событий — нормально, возвращаем пустой
                return {"events": []}
            logger.error(f"HTTP {e.code} for {sport_key}: {e.reason}")
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"Network error for {sport_key}: {e}")
    return None


def _parse_h2h_odds(ev: dict, home_team: str, away_team: str) -> tuple:
    """Парсит h2h odds из The Odds API формата.
    Возвращает (odds_home, odds_draw, odds_away) — лучшие десятичные коэффициенты.

    Формат The Odds API:
    "bookmakers": [{
        "key": "pinnacle",
        "markets": [{
            "key": "h2h",
            "outcomes": [
                {"name": "Arsenal", "price": -150},
                {"name": "Draw", "price": 320},
                {"name": "Leeds", "price": 480}
            ]
        }]
    }]
    """
    odds_home = None
    odds_draw = None
    odds_away = None

    bookmakers = ev.get("bookmakers", [])
    if not bookmakers:
        # Legacy формат: ev.odds.h2h
        odds_data = ev.get("odds", ev)
        h2h = odds_data.get("h2h")
        if h2h is not None:
            if isinstance(h2h, list):
                for item in h2h:
                    name = item.get("name", "")
                    price = item.get("price")
                    decimal = _american_to_decimal(price)
                    if decimal is None:
                        continue
                    name_norm = clean_team_name(name)
                    if name_norm == home_team:
                        if odds_home is None or decimal > odds_home:
                            odds_home = decimal
                    elif name in ("Draw", "draw"):
                        if odds_draw is None or decimal > odds_draw:
                            odds_draw = decimal
                    elif name_norm == away_team:
                        if odds_away is None or decimal > odds_away:
                            odds_away = decimal
            elif isinstance(h2h, dict):
                odds_home = _american_to_decimal(h2h.get("home"))
                odds_draw = _american_to_decimal(h2h.get("draw"))
                odds_away = _american_to_decimal(h2h.get("away"))
        return odds_home, odds_draw, odds_away

    # The Odds API формат: bookmakers[].markets[].outcomes[]
    for bookmaker in bookmakers:
        markets = bookmaker.get("markets", [])
        for market in markets:
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            for outcome in outcomes:
                name = outcome.get("name", "")
                price = outcome.get("price")
                decimal = _american_to_decimal(price)
                if decimal is None:
                    continue
                name_norm = clean_team_name(name)
                if name_norm == home_team:
                    if odds_home is None or decimal > odds_home:
                        odds_home = decimal
                elif name in ("Draw", "draw"):
                    if odds_draw is None or decimal > odds_draw:
                        odds_draw = decimal
                elif name_norm == away_team:
                    if odds_away is None or decimal > odds_away:
                        odds_away = decimal

    return odds_home, odds_draw, odds_away


def _process_league(sport_key: str, timeout: int) -> tuple:
    """Обрабатывает одну лигу: fetch + parse + upsert.
    Возвращает (sport_key, events_count, stored_count, created, updated, errors).
    """
    results = (sport_key, 0, 0, 0, 0, 0)

    if is_shutdown_requested():
        return results

    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        return results

    # Quota check
    global _quota_remaining
    if _quota_remaining is not None and _quota_remaining < QUOTA_MIN:
        logger.warning(f"Quota low — skip {sport_key}")
        return results

    data = _fetch_odds(sport_key, max_retries=MAX_RETRIES, timeout=timeout)
    if not data:
        return results

    events = data.get("events", [])
    if not events:
        logger.info(f"No events for {sport_key}")
        return (sport_key, 0, 0, 0, 0, 0)

    existing_keys = set(get_all_fields().keys())
    total = len(events)
    stored = 0
    created = 0
    updated = 0
    errors = 0

    # Сортировка по дате
    events.sort(key=lambda ev: ev.get("commence_time", "") or ev.get("start_time", "") or "")

    for ev in events:
        if is_shutdown_requested():
            break

        try:
            event_id = str(ev.get("id", ""))
            if not event_id:
                continue

            home_raw = ev.get("home_team", "")
            away_raw = ev.get("away_team", "")
            if not home_raw or not away_raw:
                continue

            home_team = clean_team_name(home_raw)
            away_team = clean_team_name(away_raw)

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            if date_utc and not is_future_match(date_utc):
                continue

            # Upper bound
            if date_utc:
                try:
                    match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                    if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                        continue
                except (ValueError, TypeError):
                    pass

            # Parse odds (The Odds API format)
            odds_home, odds_draw, odds_away = _parse_h2h_odds(ev, home_team, away_team)

            sport_title = ev.get("sport_title", sport_key)

            # Dry run
            if os.environ.get("DRY_RUN", "0") == "1":
                logger.info(f"[DRY] Would upsert: {home_team} vs {away_team} ({date_utc})")
                stored += 1
                created += 1
                continue

            # Upsert
            try:
                cid = upsert_match(
                    home_team=home_team,
                    away_team=away_team,
                    date_utc=date_utc,
                    competition=sport_title,
                    country="",
                    status="scheduled",
                    source=COLLECTOR_NAME,
                    sources=[COLLECTOR_NAME],
                    source_ids={COLLECTOR_NAME: event_id},
                )
            except Exception as e:
                logger.error(f"[PROPLINE] upsert error for {home_team} vs {away_team}: {e}")
                cid = None

            if cid:
                stored += 1
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
                    run_id = os.environ.get("PROPLINE_RUN_ID", "unknown")
                    idempotency_key = f"{run_id}:{cid}:odds"
                    try:
                        patch_match(cid, "odds", {"current": odds_current},
                                   source=COLLECTOR_NAME, upstream="pinnacle",
                                   idempotency_key=idempotency_key)
                    except Exception as e:
                        logger.error(f"[PROPLINE] patch error for cid={cid}: {e}")
                        errors += 1

        except Exception as e:
            logger.error(f"[PROPLINE] Event error: {e}")
            errors += 1

    logger.info(f"[PROPLINE] {sport_key}: events={total}, stored={stored}, "
               f"created={created}, updated={updated}, errors={errors}")
    return (sport_key, total, stored, created, updated, errors)


def collect_propline() -> Dict[str, Any]:
    """Главная функция коллектора Propline."""

    log_event("propline", "INFO", "Collection started")
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    api_key = os.environ.get("PROPLINE_API_KEY")
    if not api_key:
        logger.error("PROPLINE_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    # §2.6: run_initialization
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    os.environ["PROPLINE_RUN_ID"] = run_id
    if not init_metrics.get("redis_available", False):
        logger.error("Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    logger.info(f"Propline collector started (run_id={run_id}, dry_run={dry_run}, "
                f"workers={MAX_WORKERS}, timeout={HTTP_TIMEOUT}s)")

    # ── Динамическое получение списка лиг ──
    soccer_leagues = _fetch_sports()
    logger.info(f"Total soccer leagues: {len(soccer_leagues)}")

    # ── Параллельный fetch ──
    total_events = 0
    total_stored = 0
    total_created = 0
    total_updated = 0
    total_errors = 0
    processed_leagues = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for sport_key in soccer_leagues:
            if is_shutdown_requested():
                logger.info("Shutdown — остановка отправки задач")
                break
            future = executor.submit(_process_league, sport_key, HTTP_TIMEOUT)
            futures[future] = sport_key

        for future in as_completed(futures):
            sport_key = futures[future]
            try:
                result = future.result()
                if result and result[1] > 0:
                    processed_leagues += 1
                    total_events += result[1]
                    total_stored += result[2]
                    total_created += result[3]
                    total_updated += result[4]
                    total_errors += result[5]
            except Exception as e:
                logger.error(f"[PROPLINE] League {sport_key} failed: {e}")
                total_errors += 1

    logger.info(f"Propline done: leagues={processed_leagues}/{len(soccer_leagues)}, "
               f"events={total_events}, stored={total_stored}, "
               f"created={total_created}, updated={total_updated}, errors={total_errors}")
    if _quota_remaining is not None:
        logger.info(f"Quota remaining: {_quota_remaining}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": total_stored,
        "created": total_created,
        "updated": total_updated,
        "error_count": total_errors,
        "leagues_processed": processed_leagues,
        "leagues_total": len(soccer_leagues),
        "quota_remaining": _quota_remaining,
        "run_id": run_id,
    }
    save_meta(COLLECTOR_NAME, **meta)

    log_event("propline", "INFO", "Collection complete",
              total_events=total_events, stored=total_stored, errors=total_errors)

    return meta


@register_module("propline", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process() -> Dict[str, Any]:
    """Единая точка входа для CI/CD."""
    return collect_propline()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Collector Propline v{__version__}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать в Redis (dry-run)")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
        logger.info("DRY-RUN mode — данные НЕ будут записаны в Redis")

    result = collect_propline()
    logger.info(f"Result: {result}")
