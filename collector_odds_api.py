"""
Коллектор OddsAPI (The Odds API) для Gatekeeper-AI v600-prod.
Получает матчи и коэффициенты.
"""
import os
import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

from gatekeeper_hub import (
    batch_upsert_matches, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def _fetch_odds_api(url: str, max_retries: int = 1) -> Optional[dict]:
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                # Сохраняем quota из headers
                remaining = response.headers.get("x-requests-remaining")
                used = response.headers.get("x-requests-used")
                return {"data": data, "quota_remaining": remaining, "quota_used": used}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("[ODDS_API] HTTP 429 — Rate Limit. Прерываем запросы, переходим на кэш.")
                return None
            print(f"[ODDS_API HTTP {e.code}] {e.reason}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"[ODDS_API ERROR] {e}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None
    return None


def collect_odds_api() -> Dict[str, Any]:
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("[ODDS_API] ODDS_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    init_metrics = run_initialization()
    if not init_metrics.get("redis_available", False):
        print("[ODDS_API] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    # Настройки из env (передаются через workflow)
    regions = os.environ.get("ODDS_API_REGIONS", "eu,uk")
    markets = os.environ.get("ODDS_API_MARKETS", "h2h")
    odds_format = os.environ.get("ODDS_API_ODDS_FORMAT", "decimal")
    rate_delay = float(os.environ.get("ODDS_API_RATE_DELAY", "1.0"))
    max_retries = int(os.environ.get("ODDS_API_MAX_RETRIES", "1"))

    # 1. Получаем события (базовые)
    sports_url = f"{ODDS_API_BASE}/sports/?apiKey={api_key}"
    sports_data = _fetch_odds_api(sports_url, max_retries=max_retries)
    if sports_data is None:
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    sports = sports_data.get("data", [])
    # Фильтруем только активные футбол
    football_sports = [s for s in sports if s.get("group") == "Soccer" and s.get("active", True)]
    print(f"[ODDS_API] Активных футбольных лиг: {len(football_sports)}")

    all_matches = []
    quota_remaining = "?"
    quota_used = "?"
    error_count = 0

    for sport in football_sports:
        sport_key = sport.get("key", "")
        if not sport_key:
            continue

        # 2. Получаем odds для каждой лиги
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
            home_team = ev.get("home_team", "")
            away_team = ev.get("away_team", "")
            if not home_team or not away_team:
                continue

            raw_date = ev.get("commence_time", "") or ev.get("start_time", "")
            date_utc = normalize_date(raw_date)

            # Правило 1.5: матчи без даты считаются будущими — сохраняем.
            # Пропускаем только матчи с датой в прошлом.
            if date_utc and not is_future_match(date_utc):
                continue

            sport_title = ev.get("sport_title", sport.get("title", ""))
            event_id = str(ev.get("id", ""))

            # Извлекаем коэффициенты из bookmakers
            odds_home = "-"
            odds_draw = "-"
            odds_away = "-"
            bookmakers = ev.get("bookmakers", [])
            for bm in bookmakers:
                markets_list = bm.get("markets", [])
                for market in markets_list:
                    if market.get("key") != "h2h":
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

            match_data = {
                "home_team": home_team,
                "away_team": away_team,
                "date_utc": date_utc,
                "competition": sport_title,
                "country": "",
                "status": "scheduled",
                "source_ids": {"odds_api": event_id},
                "odds": {
                    "home": odds_home,
                    "draw": odds_draw,
                    "away": odds_away,
                    "source": "odds_api",
                    "updated_at": now_msk(),
                },
            }
            all_matches.append(match_data)

        # Rate limit между лигами
        time.sleep(rate_delay)

    total_events = len(all_matches)
    print(f"[ODDS_API] Получено матчей с коэффициентами: {total_events}")

    # 3. Batch upsert
    if all_matches:
        upsert_result = batch_upsert_matches(all_matches, source="odds_api")
        created = upsert_result.get("created", 0)
        updated = upsert_result.get("updated", 0)
        deduped = upsert_result.get("deduped", 0)
        skipped_past = upsert_result.get("skipped_past", 0)
        stored = created + updated + deduped
    else:
        stored = created = updated = deduped = skipped_past = 0

    print(f"[ODDS_API] Записано: {stored} (создано {created}, обновлено {updated}, дубликатов {deduped})")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "deduped": deduped,
        "skipped_past": skipped_past,
        "error_count": error_count,
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
    }
    save_meta("odds_api", **meta)

    return meta


if __name__ == "__main__":
    result = collect_odds_api()
    print(f"[ODDS_API] Result: {result}")
