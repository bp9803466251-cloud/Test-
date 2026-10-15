#!/usr/bin/env python3
"""
Collector Propline v9.3-debug-keys для Gatekeeper-AI v9.3-audited.
Получает матчи и коэффициенты (h2h, spreads, totals) через PropLine API (Pinnacle),
сохраняет в Redis через gatekeeper_hub, с подробным debug-выводом всех сырых ключей.
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
    from gatekeeper_hub import clean_team_name, build_canonical_id
except ImportError:
    logger.critical("Критическая ошибка: gatekeeper_hub или team_registry недоступны!")
    sys.exit(1)

COLLECTOR_NAME = "propline"
BASE_URL = os.environ.get("PROPLINE_BASE_URL", "https://api.prop-line.com/v1")
DAYS_AHEAD = int(os.environ.get("PROPLINE_DAYS_AHEAD", "1"))
MAX_RETRIES = int(os.environ.get("PROPLINE_MAX_RETRIES", "2"))
HTTP_TIMEOUT = int(os.environ.get("PROPLINE_API_TIMEOUT", "10"))
MAX_WORKERS = int(os.environ.get("PROPLINE_MAX_WORKERS", "8"))
QUOTA_MIN = int(os.environ.get("PROPLINE_QUOTA_MIN", "10"))

FALLBACK_LEAGUES = [
    "soccer_epl", "soccer_la_liga", "soccer_serie_a",
    "soccer_bundesliga", "soccer_ligue_1", "soccer_mls",
    "soccer_portugal_primeira_liga", "soccer_netherlands_eredivisie",
    "soccer_belgium_first_div", "soccer_turkey_super_league",
    "soccer_spain_segunda_division", "soccer_italy_serie_b",
    "soccer_germany_bundesliga2", "soccer_france_ligue2",
    "soccer_england_championship", "soccer_england_league1",
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

__version__ = "9.3-debug-keys"
__all__ = ["collect_propline", "collect_and_process", "__version__"]

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
                return FALLBACK_LEAGUES
    except Exception as e:
        logger.warning(f"GET /sports failed: {e} — using fallback")
        return FALLBACK_LEAGUES


def _fetch_odds(sport_key: str, max_retries: int = 2, timeout: int = 10) -> Optional[dict]:
    """Получает odds от PropLine API для всех основных рынков (h2h, spreads, totals)."""
    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        return None

    url = (f"{BASE_URL}/sports/{sport_key}/odds"
           f"?apiKey={api_key}&markets=h2h,spreads,totals&bookmakers=pinnacle")

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
                remaining = response.headers.get("X-Daily-Remaining")
                if remaining:
                    _quota_remaining = int(remaining)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                time.sleep(retry_after)
                continue
            if e.code == 404:
                return {"events": []}
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"Network error for {sport_key}: {e}")
    return None


def _parse_markets(ev: dict, home_team: str, away_team: str) -> tuple:
    """Парсит h2h, spreads и totals из Pinnacle/The Odds API формата."""
    odds_h2h = {}
    odds_spreads = {}
    odds_totals = {}

    bookmakers = ev.get("bookmakers", [])
    for bookmaker in bookmakers:
        markets = bookmaker.get("markets", [])
        for market in markets:
            m_key = market.get("key")
            outcomes = market.get("outcomes", [])

            if m_key == "h2h":
                for outcome in outcomes:
                    name = outcome.get("name", "")
                    price = outcome.get("price")
                    decimal = _american_to_decimal(price)
                    if decimal is None:
                        continue
                    name_norm = clean_team_name(name)
                    if name_norm == home_team:
                        odds_h2h["home"] = float(decimal)
                    elif name.lower() == "draw":
                        odds_h2h["draw"] = float(decimal)
                    elif name_norm == away_team:
                        odds_h2h["away"] = float(decimal)

            elif m_key == "spreads":
                for outcome in outcomes:
                    name = outcome.get("name", "")
                    price = outcome.get("price")
                    point = outcome.get("point")
                    decimal = _american_to_decimal(price)
                    if decimal is None:
                        continue
                    name_norm = clean_team_name(name)
                    side = "home" if name_norm == home_team else "away" if name_norm == away_team else None
                    if side and point is not None:
                        odds_spreads[side] = {"point": float(point), "price": float(decimal)}

            elif m_key == "totals":
                for outcome in outcomes:
                    name = outcome.get("name", "").lower()
                    price = outcome.get("price")
                    point = outcome.get("point")
                    decimal = _american_to_decimal(price)
                    if decimal is None:
                        continue
                    side = "over" if "over" in name else "under" if "under" in name else None
                    if side and point is not None:
                        odds_totals[side] = {"point": float(point), "price": float(decimal)}

    return odds_h2h, odds_spreads, odds_totals


def _process_league(sport_key: str, timeout: int) -> tuple:
    """Обрабатывает лигу: сбор и обновление рынков в Redis."""
    results = (sport_key, 0, 0, 0, 0, 0)
    if is_shutdown_requested():
        return results

    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        return results

    global _quota_remaining
    if _quota_remaining is not None and _quota_remaining < QUOTA_MIN:
        return results

    data = _fetch_odds(sport_key, max_retries=MAX_RETRIES, timeout=timeout)
    if not data:
        return results

    events = data if isinstance(data, list) else data.get("events", [])
    if not events:
        return (sport_key, 0, 0, 0, 0, 0)

    # ── DEBUG: Логирование всех ключей и структуры первой записи в лиге ──
    if events:
        sample_ev = events[0]
        logger.info(f"\n[DEBUG PROPLINE KEYS] Лига: {sport_key}")
        logger.info(f"[DEBUG PROPLINE KEYS] Список верхнеуровневых ключей события: {list(sample_ev.keys())}")
        logger.info(f"[DEBUG PROPLINE KEYS] Пример сырого объекта события:\n{json.dumps(sample_ev, indent=2, ensure_ascii=False)}")

    existing_keys = set(get_all_fields().keys())
    total = len(events)
    stored = 0
    created = 0
    updated = 0
    errors = 0

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

            if date_utc:
                try:
                    match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                    if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                        continue
                except (ValueError, TypeError):
                    pass

            h2h_data, spreads_data, totals_data = _parse_markets(ev, home_team, away_team)
            sport_title = ev.get("sport_title", sport_key)

            if os.environ.get("DRY_RUN", "0") == "1":
                stored += 1
                created += 1
                continue

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
                logger.error(f"[PROPLINE] upsert error: {e}")
                cid = None

            if cid:
                stored += 1
                if f"match:{cid}" in existing_keys:
                    updated += 1
                else:
                    created += 1

                run_id = os.environ.get("PROPLINE_RUN_ID", "unknown")

                if h2h_data:
                    try:
                        patch_match(cid, "odds", {"current": h2h_data},
                                   source=COLLECTOR_NAME, upstream="pinnacle",
                                   idempotency_key=f"{run_id}:{cid}:odds:1x2")
                    except Exception:
                        errors += 1

                if spreads_data:
                    try:
                        patch_match(cid, "spreads", {"current": spreads_data},
                                   source=COLLECTOR_NAME, upstream="pinnacle",
                                   idempotency_key=f"{run_id}:{cid}:spreads")
                    except Exception:
                        errors += 1

                if totals_data:
                    try:
                        patch_match(cid, "totals", {"current": totals_data},
                                   source=COLLECTOR_NAME, upstream="pinnacle",
                                   idempotency_key=f"{run_id}:{cid}:totals")
                    except Exception:
                        errors += 1

        except Exception as e:
            logger.error(f"[PROPLINE] Event error: {e}")
            errors += 1

    return (sport_key, total, stored, created, updated, errors)


def collect_propline() -> Dict[str, Any]:
    """Главная функция коллектора."""
    log_event("propline", "INFO", "Collection started")
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    api_key = os.environ.get("PROPLINE_API_KEY")
    if not api_key:
        logger.error("PROPLINE_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    os.environ["PROPLINE_RUN_ID"] = run_id
    if not init_metrics.get("redis_available", False):
        logger.error("Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    soccer_leagues = _fetch_sports()

    total_events = 0
    total_stored = 0
    total_created = 0
    total_updated = 0
    total_errors = 0
    processed_leagues = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_process_league, l, HTTP_TIMEOUT): l for l in soccer_leagues}
        for future in as_completed(futures):
            try:
                res = future.result()
                if res and res[1] > 0:
                    processed_leagues += 1
                    total_events += res[1]
                    total_stored += res[2]
                    total_created += res[3]
                    total_updated += res[4]
                    total_errors += res[5]
            except Exception:
                total_errors += 1

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
    log_event("propline", "INFO", "Collection complete", total_events=total_events, stored=total_stored)
    return meta


@register_module("propline", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process() -> Dict[str, Any]:
    return collect_propline()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Collector Propline v{__version__}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
    collect_propline()
