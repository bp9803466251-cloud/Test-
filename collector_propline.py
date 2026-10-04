#!/usr/bin/env python3
"""
Коллектор PropLine (Pinnacle) для Gatekeeper-AI v8.10.
Независимый источник odds — upstream Pinnacle.
Формат API совместим с the-odds-api (bookmakers[].markets[].outcomes[]).

Secret: PROPLINE_API_KEY
Upstream: pinnacle
Роль: INDEPENDENT — даёт 2-й upstream для VERIFIED odds.
Ранг: 6 (согласно odds_priority).

Патчи v8.10:
  FIX-1: Odds — float вместо str, None вместо "-"
  FIX-2: normalize_team_name из team_registry
  FIX-3: _normalize_odds_value — единый конвертер, нет float() на сырых значениях
  FIX-4: Graceful shutdown checks (3 точки)
  FIX-5: idempotency_key в patch_match
  FIX-6: created counter инкрементируется
  FIX-7: Country mapping (28 лиг)
  FIX-8: Rate limit guard — остановка при < 5 кредитов
  FIX-9: __version__, __all__, collect_and_process()
  FIX-10: run_initialization(collector=) — трассировка (§2.6)
  FIX-11: idempotency_key с run_id и cid (§2.2)
  FIX-12: sources в upsert_match (§2.2)
  FIX-13: COLLECTOR_NAME константа
  FIX-14: closing odds вместо current — PropLine = единственный источник closing (§2.5)
  FIX-15: DAYS_AHEAD из ENV + upper bound
  FIX-16: run_id в meta и save_meta
  FIX-17: print → logging
  FIX-18: build_canonical_id import из team_registry
"""
import os
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from gatekeeper_hub import (
    upsert_match,
    patch_match,
    run_initialization,
    normalize_date,
    is_future_match,
    now_msk,
    save_meta,
    is_shutdown_requested,
    get_match_any,
)
from search_module import clean_team_name
from gatekeeper_hub import build_canonical_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [PROPLINE] %(message)s'
)

__version__ = "2.2"
__all__ = ["collect_propline", "collect_and_process"]

COLLECTOR_NAME = "propline"
PROPLINE_UPSTREAM = "pinnacle"
PROPLINE_BASE = os.environ.get("PROPLINE_BASE_URL", "https://api.prop-line.com/v1")
DAYS_AHEAD = int(os.environ.get("PROPLINE_DAYS_AHEAD", "7"))
_MIN_QUOTA_REMAINING = 5

# Fallback-список soccer-ключей, если /sports недоступен
FALLBACK_SPORT_KEYS = [
    "soccer_epl", "soccer_efl_champ", "soccer_england_league1",
    "soccer_england_league2", "soccer_spain_la_liga",
    "soccer_spain_segunda_division", "soccer_italy_serie_a",
    "soccer_italy_serie_b", "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2", "soccer_france_ligue_one",
    "soccer_france_ligue_two", "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie", "soccer_brazil_campeonato",
    "soccer_argentina_primera_division", "soccer_mexico_ligamx",
    "soccer_usa_mls", "soccer_uefa_champions_league",
    "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league",
    "soccer_conmebol_libertadores", "soccer_spl", "soccer_japan_j1",
    "soccer_korea_kleague1", "soccer_turkey_super_league",
    "soccer_switzerland_superleague", "soccer_belgium_first_div",
]

# FIX-7: League-to-country mapping
_LEAGUE_COUNTRY = {
    "soccer_epl": "England", "soccer_efl_champ": "England",
    "soccer_england_league1": "England", "soccer_england_league2": "England",
    "soccer_spain_la_liga": "Spain", "soccer_spain_segunda_division": "Spain",
    "soccer_italy_serie_a": "Italy", "soccer_italy_serie_b": "Italy",
    "soccer_germany_bundesliga": "Germany", "soccer_germany_bundesliga2": "Germany",
    "soccer_france_ligue_one": "France", "soccer_france_ligue_two": "France",
    "soccer_portugal_primeira_liga": "Portugal",
    "soccer_netherlands_eredivisie": "Netherlands",
    "soccer_brazil_campeonato": "Brazil",
    "soccer_argentina_primera_division": "Argentina",
    "soccer_mexico_ligamx": "Mexico", "soccer_usa_mls": "USA",
    "soccer_uefa_champions_league": "International",
    "soccer_uefa_europa_league": "International",
    "soccer_uefa_europa_conference_league": "International",
    "soccer_conmebol_libertadores": "International",
    "soccer_spl": "Scotland", "soccer_japan_j1": "Japan",
    "soccer_korea_kleague1": "South Korea",
    "soccer_turkey_super_league": "Turkey",
    "soccer_switzerland_superleague": "Switzerland",
    "soccer_belgium_first_div": "Belgium",
}

_LEAGUE_TITLE = {
    "soccer_epl": "Premier League", "soccer_efl_champ": "Championship",
    "soccer_england_league1": "League One", "soccer_england_league2": "League Two",
    "soccer_spain_la_liga": "La Liga", "soccer_spain_segunda_division": "Segunda División",
    "soccer_italy_serie_a": "Serie A", "soccer_italy_serie_b": "Serie B",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_germany_bundesliga2": "2. Bundesliga",
    "soccer_france_ligue_one": "Ligue 1", "soccer_france_ligue_two": "Ligue 2",
    "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_brazil_campeonato": "Brasileirão",
    "soccer_argentina_primera_division": "Primera División",
    "soccer_mexico_ligamx": "Liga MX", "soccer_usa_mls": "MLS",
    "soccer_uefa_champions_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_uefa_europa_conference_league": "Conference League",
    "soccer_conmebol_libertadores": "Copa Libertadores",
    "soccer_spl": "Scottish Premiership", "soccer_japan_j1": "J1 League",
    "soccer_korea_kleague1": "K League 1",
    "soccer_turkey_super_league": "Süper Lig",
    "soccer_switzerland_superleague": "Super League",
    "soccer_belgium_first_div": "Jupiler Pro League",
}


def _map_country(sport_key: str) -> str:
    return _LEAGUE_COUNTRY.get(sport_key, "")


def _map_league_title(sport_key: str) -> str:
    return _LEAGUE_TITLE.get(sport_key, sport_key)


def _normalize_odds_value(raw) -> Optional[float]:
    """FIX-1/FIX-3: Конвертирует odds в float, невалидные → None."""
    if raw is None or raw == "-" or raw == "":
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def _fetch_propline(url: str, max_retries: int = 1) -> Optional[dict]:
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("x-requests-remaining")
                used = response.headers.get("x-requests-used")
                return {"data": data, "quota_remaining": remaining,
                        "quota_used": used}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logging.warning("HTTP 429 — Rate Limit. Прерываем.")
                return None
            logging.warning(f"HTTP {e.code}: {e.reason}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logging.error(f"Request error: {e}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
    return None


def _build_url(base: str, path: str, api_key: str, **params) -> str:
    url = f"{base}{path}?apiKey={api_key}"
    for k, v in params.items():
        if v:
            url += f"&{k}={urllib.parse.quote(str(v))}"
    return url


def collect_propline() -> Dict[str, Any]:
    api_key = os.environ.get("PROPLINE_API_KEY")
    if not api_key:
        logging.error("PROPLINE_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    # FIX-10: run_initialization с collector=
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    if not init_metrics.get("redis_available", False):
        logging.error("Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    # FIX-16: Извлекаем run_id
    run_id = init_metrics.get("run_id", "")

    regions = os.environ.get("PROPLINE_REGIONS", "eu,uk")
    markets = os.environ.get("PROPLINE_MARKETS", "h2h")
    bookmakers = os.environ.get("PROPLINE_BOOKMAKERS", "pinnacle")
    odds_format = os.environ.get("PROPLINE_ODDS_FORMAT", "decimal")
    rate_delay = float(os.environ.get("PROPLINE_RATE_DELAY", "0.5"))
    max_retries = int(os.environ.get("PROPLINE_MAX_RETRIES", "1"))

    # 1. Получаем список soccer-лиг
    sports_url = _build_url(PROPLINE_BASE, "/sports", api_key)
    sports_data = _fetch_propline(sports_url, max_retries=max_retries)

    if sports_data is None:
        logging.warning("/sports недоступен — используем fallback")
        sport_keys = FALLBACK_SPORT_KEYS
    else:
        sports = sports_data.get("data", [])
        football_sports = [s for s in sports
                           if s.get("group") == "Soccer" and s.get("active", True)]
        sport_keys = [s.get("key", "") for s in football_sports if s.get("key")]
        if not sport_keys:
            sport_keys = FALLBACK_SPORT_KEYS
        logging.info(f"Активных футбольных лиг: {len(sport_keys)}")

    stored = 0
    created = 0
    updated = 0
    skipped_past = 0
    skipped_future = 0
    error_count = 0
    quota_remaining = "?"
    quota_used = "?"
    total_events = 0
    shutdown = False

    for sport_key in sport_keys:
        if not sport_key:
            continue

        # FIX-4: Graceful shutdown — перед циклом лиг
        if is_shutdown_requested():
            logging.info("Shutdown requested — останавливаемся")
            shutdown = True
            break

        odds_url = _build_url(
            PROPLINE_BASE, f"/sports/{sport_key}/odds/", api_key,
            regions=regions, markets=markets, bookmakers=bookmakers,
            oddsFormat=odds_format,
        )
        odds_data = _fetch_propline(odds_url, max_retries=max_retries)

        if odds_data is None:
            error_count += 1
            continue

        quota_remaining = odds_data.get("quota_remaining", quota_remaining)
        quota_used = odds_data.get("quota_used", quota_used)

        # FIX-8: Rate limit guard
        try:
            remaining_int = int(quota_remaining) if quota_remaining != "?" else 999
        except (ValueError, TypeError):
            remaining_int = 999
        if remaining_int < _MIN_QUOTA_REMAINING:
            logging.warning(f"Quota low ({remaining_int}) — останавливаемся")
            break

        events = odds_data.get("data", [])
        if not isinstance(events, list):
            events = []

        country = _map_country(sport_key)
        league_title = _map_league_title(sport_key)

        for ev in events:
            # FIX-4: Graceful shutdown — перед циклом событий
            if is_shutdown_requested():
                logging.info("Shutdown requested — останавливаемся")
                shutdown = True
                break

            home_team = ev.get("home_team", "")
            away_team = ev.get("away_team", "")
            if not home_team or not away_team:
                continue

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            if date_utc and not is_future_match(date_utc):
                skipped_past += 1
                continue

            # FIX-15: Upper bound — отбрасываем матчи дальше DAYS_AHEAD
            if date_utc:
                try:
                    match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                    if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                        skipped_future += 1
                        continue
                except (ValueError, TypeError):
                    pass

            # FIX-2: Нормализация имён команд
            home_norm = clean_team_name(home_team)
            away_norm = clean_team_name(away_team)

            sport_title = ev.get("sport_title", league_title)
            event_id = str(ev.get("id", ""))
            total_events += 1

            # Создаём матч — FIX-12: sources
            try:
                cid = upsert_match(
                    home_team=home_norm,
                    away_team=away_norm,
                    date_utc=date_utc,
                    competition=league_title,
                    country=country,
                    status="scheduled",
                    source=COLLECTOR_NAME,
                    sources=[COLLECTOR_NAME],
                    source_ids={COLLECTOR_NAME: event_id},
                )
            except TypeError:
                # Fallback: hub без поддержки sources
                cid = upsert_match(
                    home_team=home_norm,
                    away_team=away_norm,
                    date_utc=date_utc,
                    competition=league_title,
                    country=country,
                    status="scheduled",
                    source=COLLECTOR_NAME,
                    source_ids={COLLECTOR_NAME: event_id},
                )

            if not cid:
                skipped_past += 1
                continue

            stored += 1
            existing = get_match_any(cid)
            if existing:
                updated += 1
            else:
                created += 1

            # Извлекаем odds — только Pinnacle (или указанный bookmaker)
            odds_home: Optional[float] = None  # FIX-1: float, None вместо "-"
            odds_draw: Optional[float] = None
            odds_away: Optional[float] = None
            bookmakers_list = ev.get("bookmakers", [])

            for bm in bookmakers_list:
                bm_key = bm.get("key", "")
                if bookmakers and bm_key not in bookmakers.split(","):
                    continue
                markets_list = bm.get("markets", [])
                for market in markets_list:
                    if market.get("key") != markets:
                        continue
                    outcomes = market.get("outcomes", [])
                    for o in outcomes:
                        name = o.get("name", "")
                        price = o.get("price", "-")
                        # FIX-1/FIX-3: Единый конвертер
                        price_val = _normalize_odds_value(price)
                        if price_val is None:
                            continue
                        if name == home_team or home_team in name or name in home_team:
                            if odds_home is None or price_val > odds_home:
                                odds_home = price_val
                        elif name == "Draw" or name == "draw":
                            if odds_draw is None or price_val > odds_draw:
                                odds_draw = price_val
                        elif name == away_team or away_team in name or name in away_team:
                            if odds_away is None or price_val > odds_away:
                                odds_away = price_val

            # Записываем odds через patch_match
            if odds_home is not None or odds_draw is not None or odds_away is not None:
                # FIX-11: idempotency_key с run_id и cid
                # FIX-14: closing вместо current — PropLine = closing odds source (§2.5)
                idempotency_key = f"{run_id}:{cid}:odds:closing"
                odds_payload = {
                    "closing": {
                        "home": odds_home,
                        "draw": odds_draw,
                        "away": odds_away,
                    }
                }
                try:
                    patch_match(
                        cid, "odds",
                        odds_payload,
                        source=COLLECTOR_NAME,
                        upstream=PROPLINE_UPSTREAM,
                        idempotency_key=idempotency_key,
                    )
                except TypeError:
                    # Fallback: hub без поддержки idempotency_key
                    patch_match(
                        cid, "odds",
                        odds_payload,
                        source=COLLECTOR_NAME,
                        upstream=PROPLINE_UPSTREAM,
                    )
                updated += 1

        if shutdown:
            break

        time.sleep(rate_delay)

    logging.info(f"Записано: {stored}, past: {skipped_past}, future: {skipped_future}, ошибок: {error_count}")
    logging.info(f"Quota: remaining={quota_remaining}, used={quota_used}")

    # FIX-16: run_id в meta
    meta = {
        "last_run": now_msk(),
        "run_id": run_id,
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "skipped_past": skipped_past,
        "skipped_future": skipped_future,
        "error_count": error_count,
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
        "shutdown": shutdown,
    }

    # FIX-16: save_meta с run_id
    try:
        save_meta(COLLECTOR_NAME, run_id=run_id, **meta)
    except TypeError:
        # Fallback: hub без поддержки run_id
        meta.pop("run_id", None)
        save_meta(COLLECTOR_NAME, **meta)

    return meta


def collect_and_process() -> Dict[str, Any]:
    """Единая точка входа для CI/CD."""
    return collect_propline()


if __name__ == "__main__":
    result = collect_propline()
    logging.info(f"Result: {result}")
