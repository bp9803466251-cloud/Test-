#!/usr/bin/env python3
"""
Collector Propline для Gatekeeper-AI v810-patched.
Получает матчи и коэффициенты через PropLine API, сохраняет в Redis через gatekeeper_hub.

FIX v810 (полная переработка):
  - Использует gatekeeper_hub (upsert_match, patch_match, save_meta) — §1.4
  - canonical_id через build_canonical_id (home__away__YYYYMMDD) — §1.6
  - clean_team_name из team_registry — §2.4
  - run_initialization(collector="propline") — §2.6
  - graceful shutdown (is_shutdown_requested) — §2.3
  - american_to_decimal с None-проверкой
  - __version__, __all__
  - --dry-run argparse
  - odds как float (вместо raw american)
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
from typing import Dict, Any, Optional

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

# Graceful shutdown — §2.3
try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

# team_registry — §2.4
try:
    from team_registry import clean_team_name
except ImportError:
    logger.error("team_registry не найден — clean_team_name fallback")
    def clean_team_name(name: str) -> str:
        return name.lower().strip().replace(" ", "_") if name else ""

COLLECTOR_NAME = "propline"
BASE_URL = "https://api.prop-line.com/v1"
SOCCER_LEAGUES = [
    "soccer_epl", "soccer_la_liga", "soccer_serie_a",
    "soccer_bundesliga", "soccer_ligue_1", "soccer_mls"
]
DAYS_AHEAD = int(os.environ.get("PROPLINE_DAYS_AHEAD", "3"))

__version__ = "8.11-patched"
__all__ = ["collect_propline", "collect_and_process", "__version__"]


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


def _fetch_odds(sport_key: str, markets: str = "h2h,spreads,totals",
                max_retries: int = 3, timeout: int = 15) -> Optional[dict]:
    """Получает odds от PropLine API."""
    api_key = os.environ.get("PROPLINE_API_KEY", "")
    if not api_key:
        logger.error("PROPLINE_API_KEY не задан")
        return None

    url = f"{BASE_URL}/sports/{sport_key}/odds?apiKey={api_key}&markets={markets}"

    for attempt in range(max_retries):
        if is_shutdown_requested():
            logger.info("Shutdown requested — остановка fetch")
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GatekeeperAI"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("X-Daily-Remaining")
                if remaining:
                    logger.info(f"Quota remaining: {remaining}")
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                logger.warning(f"Rate limit. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            logger.error(f"HTTP {e.code}: {e.reason}")
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.error(f"Network error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return None


def collect_propline() -> Dict[str, Any]:
    """Главная функция коллектора Propline."""
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    api_key = os.environ.get("PROPLINE_API_KEY")
    if not api_key:
        logger.error("PROPLINE_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    rate_delay = float(os.environ.get("PROPLINE_RATE_DELAY", "0.2"))
    max_retries = int(os.environ.get("PROPLINE_MAX_RETRIES", "3"))
    timeout = int(os.environ.get("PROPLINE_API_TIMEOUT", "15"))

    # §2.6: run_initialization
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    if not init_metrics.get("redis_available", False):
        logger.error("Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    logger.info(f"Propline collector started (run_id={run_id}, dry_run={dry_run})")

    stored = 0
    created = 0
    updated = 0
    skipped_past = 0
    skipped_future = 0
    total_events = 0
    error_count = 0
    deduped = 0
    seen = set()

    for sport_key in SOCCER_LEAGUES:
        if is_shutdown_requested():
            logger.info("Shutdown — остановка цикла лиг")
            break

        logger.info(f"Fetching odds for {sport_key}...")
        data = _fetch_odds(sport_key, max_retries=max_retries, timeout=timeout)

        if not data or "events" not in data:
            logger.info(f"No events for {sport_key}")
            continue

        events = data.get("events", [])
        logger.info(f"Got {len(events)} events for {sport_key}")

        for ev in events:
            if is_shutdown_requested():
                logger.info("Shutdown — остановка цикла матчей")
                break

            event_id = str(ev.get("id", ""))
            if not event_id:
                continue

            home_raw = ev.get("home_team", "")
            away_raw = ev.get("away_team", "")
            if not home_raw or not away_raw:
                continue

            # §2.4: нормализация команд
            home_team = clean_team_name(home_raw)
            away_team = clean_team_name(away_raw)

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            if date_utc and not is_future_match(date_utc):
                skipped_past += 1
                continue

            # Upper bound
            if date_utc:
                try:
                    match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                    if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                        skipped_future += 1
                        continue
                except (ValueError, TypeError):
                    pass

            # Dedup
            dedup_key = f"{home_team}|{away_team}|{date_utc}"
            if dedup_key in seen:
                deduped += 1
                continue
            seen.add(dedup_key)

            total_events += 1

            # Извлечение odds
            odds_home = None
            odds_draw = None
            odds_away = None
            odds_data = ev.get("odds", ev)

            # h2h может быть list [{name, price}] или dict {home, draw, away}
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
                        if name_norm == home_team or home_team in name_norm:
                            if odds_home is None or decimal > odds_home:
                                odds_home = decimal
                        elif name in ("Draw", "draw"):
                            if odds_draw is None or decimal > odds_draw:
                                odds_draw = decimal
                        elif name_norm == away_team or away_team in name_norm:
                            if odds_away is None or decimal > odds_away:
                                odds_away = decimal
                elif isinstance(h2h, dict):
                    odds_home = _american_to_decimal(h2h.get("home"))
                    odds_draw = _american_to_decimal(h2h.get("draw"))
                    odds_away = _american_to_decimal(h2h.get("away"))

            # sport_title для competition
            sport_title = ev.get("sport_title", sport_key)

            if dry_run:
                logger.info(f"[DRY] Would upsert: {home_team} vs {away_team} ({date_utc})")
                stored += 1
                created += 1
                continue

            # §1.4: upsert через хаб
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
                                   source=COLLECTOR_NAME, upstream="propline",
                                   idempotency_key=idempotency_key)
                    except Exception as e:
                        logger.error(f"[PROPLINE] patch error for cid={cid}: {e}")

            time.sleep(rate_delay)

    logger.info(f"Propline done: events={total_events}, stored={stored}, "
                 f"created={created}, past={skipped_past}, future={skipped_future}, "
                 f"deduped={deduped}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "error_count": error_count,
        "skipped_past": skipped_past,
        "skipped_future": skipped_future,
        "deduped": deduped,
        "run_id": run_id,
    }
    if not dry_run:
        save_meta(COLLECTOR_NAME, **meta)

    return meta


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
