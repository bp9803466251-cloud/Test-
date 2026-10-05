"""
Коллектор OddsAPI (The Odds API) для Gatekeeper-AI v700-prod.
Получает матчи и коэффициенты. Роль: VERIFICATION (запускается вручную, реже остальных).

v700.2 (Phase 2):
  - run_initialization(collector="odds_api")
  - idempotency_key в patch_match
  - sources=["odds_api"] в upsert_match
  - team_registry.normalize_team_name
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
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("odds_api")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

from gatekeeper_hub import (
    upsert_match, patch_match, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)
# FIX: get_match_any может отсутствовать — fallback на get_match
try:
    from gatekeeper_hub import get_match_any
except ImportError:
    try:
        from gatekeeper_hub import get_match as get_match_any
    except ImportError:
        get_match_any = lambda cid: None

try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

try:
    from team_registry import normalize_team_name, build_canonical_id, clean_team_name
except ImportError:
    logger.error("team_registry не найден")
    normalize_team_name = lambda x: x.strip().lower() if x else ""
    build_canonical_id = lambda h, a, d: f"{h}__{a}__{d[:10].replace('-','')}"
    clean_team_name = lambda name: name.lower().strip().replace(" ", "_") if name else ""

COLLECTOR_NAME = "odds_api"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DAYS_AHEAD = int(os.environ.get("ODDS_API_DAYS_AHEAD", "3"))

__version__ = "8.11-patched"
__all__ = [
    "collect_odds_api",
    "collect_and_process",
    "__version__",
]


def _fetch_odds_api(url: str, max_retries: int = 1) -> Optional[dict]:
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("x-requests-remaining")
                used = response.headers.get("x-requests-used")
                return {"data": data, "quota_remaining": remaining, "quota_used": used}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning("HTTP 429 — Rate Limit. Прерываем запросы, переходим на кэш.")
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
    rate_delay = float(os.environ.get("ODDS_API_RATE_DELAY", "1.0"))
    max_retries = int(os.environ.get("ODDS_API_MAX_RETRIES", "1"))

    # 1. Получаем события (базовые)
    sports_url = f"{ODDS_API_BASE}/sports/?apiKey={api_key}"
    sports_data = _fetch_odds_api(sports_url, max_retries=max_retries)
    if sports_data is None:
        logger.error("Не удалось получить список спортов")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1, "run_id": run_id}

    sports = sports_data.get("data", [])
    football_sports = [s for s in sports if s.get("group") == "Soccer" and s.get("active", True)]
    logger.info(f"Активных футбольных лиг: {len(football_sports)}")

    stored = 0
    created = 0
    updated = 0
    skipped_past = 0
    skipped_future = 0
    total_events = 0
    quota_remaining = "?"
    quota_used = "?"
    error_count = 0

    for sport in football_sports:
        if is_shutdown_requested():
            logger.info("Graceful shutdown — прерываем цикл спортов")
            break

        sport_key = sport.get("key", "")
        if not sport_key:
            continue

        odds_url = (
            f"{ODDS_API_BASE}/sports/{sport_key}/odds/"
            f"?apiKey={api_key}&regions={regions}&markets={markets}&oddsFormat={odds_format}"
        )
        odds_data = _fetch_odds_api(odds_url, max_retries=max_retries)
        if odds_data is None:
            error_count += 1
            continue

        quota_remaining = odds_data.get("quota_remaining", quota_remaining)
        quota_used = odds_data.get("quota_used", quota_used)

        events = odds_data.get("data", [])
        for ev in events:
            if is_shutdown_requested():
                logger.info("Graceful shutdown — прерываем цикл матчей")
                break

            home_team_raw = ev.get("home_team", "")
            away_team_raw = ev.get("away_team", "")
            if not home_team_raw or not away_team_raw:
                continue

            # §2.4: Нормализация через team_registry
            home_team = clean_team_name(home_team_raw)
            away_team = clean_team_name(away_team_raw)

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            if date_utc and not is_future_match(date_utc):
                skipped_past += 1
                continue

            # Upper bound: отбрасываем матчи дальше DAYS_AHEAD
            if date_utc:
                try:
                    match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                    if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                        skipped_future += 1
                        continue
                except (ValueError, TypeError):
                    pass

            sport_title = ev.get("sport_title", sport.get("title", ""))
            event_id = str(ev.get("id", ""))

            # Извлекаем коэффициенты из bookmakers — как float
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
                        # Сравниваем с нормализованным именем
                        name_norm = clean_team_name(name)
                        if name_norm == home_team or home_team in name_norm or name_norm in home_team:
                            if odds_home is None or price_float > odds_home:
                                odds_home = price_float
                        elif name in ("Draw", "draw", "Ничья"):
                            if odds_draw is None or price_float > odds_draw:
                                odds_draw = price_float
                        elif name_norm == away_team or away_team in name_norm or name_norm in away_team:
                            if odds_away is None or price_float > odds_away:
                                odds_away = price_float

            total_events += 1

            # 1. Создать матч (с sources)
            if dry_run:
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
                logger.error(f"[ODDS_API] upsert error: {e}")
                cid = None

            if cid:
                stored += 1
                existing = get_match_any(cid)
                if existing:
                    updated += 1
                else:
                    created += 1

                # 2. Patch odds как float — только не-None значения
                odds_current = {}
                if odds_home is not None:
                    odds_current["home"] = float(odds_home)
                if odds_draw is not None:
                    odds_current["draw"] = float(odds_draw)
                if odds_away is not None:
                    odds_current["away"] = float(odds_away)
                idempotency_key = f"{run_id}:{cid}:odds"
                try:
                    patch_match(cid, "odds", {"current": odds_current},
                               source=COLLECTOR_NAME, upstream="betradar",
                               idempotency_key=idempotency_key)
                except Exception as e:
                    logger.error(f"[ODDS_API] patch error for cid={cid}: {e}")

        time.sleep(rate_delay)

    logger.info(f"Получено матчей: {total_events}, записано: {stored}, "
                 f"прошлое: {skipped_past}, будущее: {skipped_future}")

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
    }
    save_meta(COLLECTOR_NAME, **meta)

    return meta


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
