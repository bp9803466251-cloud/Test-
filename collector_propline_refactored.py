#!/usr/bin/env python3
"""
Коллектор PropLine (Pinnacle) для Gatekeeper-AI v700-prod.
Независимый источник odds — upstream Pinnacle.
Формат API совместим с the-odds-api (bookmakers[].markets[].outcomes[]).

Secret: PROPLINE_API_KEY
Upstream: pinnacle
Роль: INDEPENDENT — даёт 2-й upstream для VERIFIED odds.
Запуск: вручную.
"""
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, Any, Optional, List

from gatekeeper_hub import (
    upsert_match,
    patch_match,
    run_initialization,
    normalize_date,
    is_future_match,
    now_msk,
    save_meta,
)

PROPLINE_UPSTREAM = "pinnacle"
PROPLINE_BASE = os.environ.get("PROPLINE_BASE_URL", "https://api.prop-line.com/v1")

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
                print("[PROPLINE] HTTP 429 — Rate Limit. Прерываем.")
                return None
            print(f"[PROPLINE HTTP {e.code}] {e.reason}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"[PROPLINE ERROR] {e}")
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
        print("[PROPLINE] PROPLINE_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    init_metrics = run_initialization()
    if not init_metrics.get("redis_available", False):
        print("[PROPLINE] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

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
        print("[PROPLINE] /sports недоступен — используем fallback")
        sport_keys = FALLBACK_SPORT_KEYS
    else:
        sports = sports_data.get("data", [])
        football_sports = [s for s in sports
                           if s.get("group") == "Soccer" and s.get("active", True)]
        sport_keys = [s.get("key", "") for s in football_sports if s.get("key")]
        if not sport_keys:
            sport_keys = FALLBACK_SPORT_KEYS
        print(f"[PROPLINE] Активных футбольных лиг: {len(sport_keys)}")

    stored = 0
    created = 0
    updated = 0
    skipped_past = 0
    error_count = 0
    quota_remaining = "?"
    quota_used = "?"
    total_events = 0

    for sport_key in sport_keys:
        if not sport_key:
            continue

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

        events = odds_data.get("data", [])
        if not isinstance(events, list):
            events = []

        for ev in events:
            home_team = ev.get("home_team", "")
            away_team = ev.get("away_team", "")
            if not home_team or not away_team:
                continue

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            if date_utc and not is_future_match(date_utc):
                skipped_past += 1
                continue

            sport_title = ev.get("sport_title", sport_key)
            event_id = str(ev.get("id", ""))
            total_events += 1

            # Создаём матч (без odds)
            cid = upsert_match(
                home_team=home_team,
                away_team=away_team,
                date_utc=date_utc,
                competition=sport_title,
                country="",
                status="scheduled",
                source="propline",
                source_ids={"propline": event_id},
            )

            if not cid:
                skipped_past += 1
                continue

            stored += 1

            # Извлекаем odds — только Pinnacle (или указанный bookmaker)
            odds_home = "-"
            odds_draw = "-"
            odds_away = "-"
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
                        try:
                            price_float = float(price) if price != "-" else None
                        except (ValueError, TypeError):
                            price_float = None
                        if price_float is None or price_float <= 0:
                            continue
                        if name == home_team or home_team in name or name in home_team:
                            if odds_home == "-" or price_float > float(odds_home):
                                odds_home = str(price)
                        elif name == "Draw" or name == "draw":
                            if odds_draw == "-" or price_float > float(odds_draw):
                                odds_draw = str(price)
                        elif name == away_team or away_team in name or name in away_team:
                            if odds_away == "-" or price_float > float(odds_away):
                                odds_away = str(price)

            # Записываем odds через patch_match (накопление в sources[])
            if odds_home != "-" or odds_draw != "-" or odds_away != "-":
                patch_match(
                    cid, "odds",
                    {"current": {"home": odds_home, "draw": odds_draw, "away": odds_away}},
                    source="propline",
                    upstream=PROPLINE_UPSTREAM,
                )

        time.sleep(rate_delay)

    print(f"[PROPLINE] Записано: {stored}, past: {skipped_past}, ошибок: {error_count}")
    print(f"[PROPLINE] Quota: remaining={quota_remaining}, used={quota_used}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "skipped_past": skipped_past,
        "error_count": error_count,
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
    }
    save_meta("propline", **meta)
    return meta


if __name__ == "__main__":
    result = collect_propline()
    print(f"[PROPLINE] Result: {result}")
              
