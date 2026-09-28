"""
Коллектор SharpAPI для Gatekeeper-AI v600-prod.
Получает матчи и коэффициенты, сохраняет в Redis через gatekeeper_hub.
Хелперы normalize_date, is_future_match, now_msk, save_meta импортируются из хаба (правило 1.11).
"""
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

from gatekeeper_hub import (
    upsert_match, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)
from redis_hub import get_all_fields, delete_from_cache
SHARP_API_BASE = "https://api.sharpapi.io/api/v1"


# ---------------------------------------------------------------------------
# Словари для парсинга слага лиги SharpAPI
# Формат: country_slug_-_league_slug (например, 'argentina_-_primera_a')
# ---------------------------------------------------------------------------
COUNTRY_MAP = {
    "argentina": "Argentina", "england": "England", "spain": "Spain",
    "italy": "Italy", "germany": "Germany", "france": "France",
    "netherlands": "Netherlands", "portugal": "Portugal", "brazil": "Brazil",
    "mexico": "Mexico", "usa": "USA", "paraguay": "Paraguay", "chile": "Chile",
    "wales": "Wales", "turkey": "Turkey", "japan": "Japan",
    "south_korea": "South Korea", "china": "China", "australia": "Australia",
    "scotland": "Scotland", "belgium": "Belgium", "austria": "Austria",
    "switzerland": "Switzerland", "greece": "Greece", "poland": "Poland",
    "denmark": "Denmark", "sweden": "Sweden", "norway": "Norway",
    "finland": "Finland", "ireland": "Ireland", "colombia": "Colombia",
    "uruguay": "Uruguay", "ecuador": "Ecuador", "peru": "Peru",
    "bolivia": "Bolivia", "venezuela": "Venezuela", "canada": "Canada",
    "international": "International", "world": "International", "europe": "International",
    "africa": "Africa", "asia": "Asia", "russia": "Russia", "ukraine": "Ukraine",
    "romania": "Romania", "croatia": "Croatia", "serbia": "Serbia",
    "czech_republic": "Czech Republic", "hungary": "Hungary", "israel": "Israel",
    "saudi_arabia": "Saudi Arabia", "qatar": "Qatar", "uae": "UAE",
    "northern_ireland": "Northern Ireland", "iceland": "Iceland",
    "slovakia": "Slovakia", "slovenia": "Slovenia", "bulgaria": "Bulgaria",
    "cyprus": "Cyprus", "lithuania": "Lithuania", "latvia": "Latvia",
    "estonia": "Estonia", "luxembourg": "Luxembourg", "malta": "Malta",
    "albania": "Albania", "kosovo": "Kosovo", "montenegro": "Montenegro",
    "bosnia": "Bosnia", "moldova": "Moldova", "georgia": "Georgia",
    "armenia": "Armenia", "azerbaijan": "Azerbaijan", "kazakhstan": "Kazakhstan",
    "south_africa": "South Africa", "nigeria": "Nigeria", "egypt": "Egypt",
    "morocco": "Morocco", "algeria": "Algeria", "tunisia": "Tunisia",
    "senegal": "Senegal", "ivory_coast": "Ivory Coast", "ghana": "Ghana",
    "cameroon": "Cameroon", "mali": "Mali", "burkina_faso": "Burkina Faso",
    "benin": "Benin", "cabo_verde": "Cabo Verde", "guinea": "Guinea",
    "kenya": "Kenya", "tanzania": "Tanzania", "ethiopia": "Ethiopia",
    "thailand": "Thailand", "vietnam": "Vietnam", "indonesia": "Indonesia",
    "malaysia": "Malaysia", "singapore": "Singapore", "philippines": "Philippines",
    "india": "India",
}

LEAGUE_NAME_MAP = {
    "primera_a": "Primera División", "primera_division": "Primera División",
    "premier_league": "Premier League", "championship": "Championship",
    "league_one": "League One", "league_two": "League Two",
    "national_league": "National League", "fa_cup": "FA Cup",
    "efl_cup": "EFL Cup", "la_liga": "La Liga",
    "segunda_division": "Segunda División", "copa_del_rey": "Copa del Rey",
    "serie_a": "Serie A", "serie_b": "Serie B", "coppa_italia": "Coppa Italia",
    "bundesliga": "Bundesliga", "bundesliga_2": "2. Bundesliga",
    "dfb_pokal": "DFB-Pokal", "ligue_1": "Ligue 1", "ligue_2": "Ligue 2",
    "coupe_de_france": "Coupe de France", "eredivisie": "Eredivisie",
    "primeira_liga": "Primeira Liga", "brasileirao": "Brasileirão",
    "serie_a_brazil": "Série A", "campeonato_brasileiro": "Brasileirão",
    "liga_mx": "Liga MX", "mls": "MLS",
    "primera_division_py": "Primera División", "paraguayan_primera": "Primera División",
    "primera_division_cl": "Primera División", "chilean_primera": "Primera División",
    "welsh_premier": "Welsh Premier League", "welsh_cup": "Welsh Cup",
    "super_lig": "Süper Lig", "j1_league": "J1 League",
    "k_league_1": "K League 1", "chinese_super_league": "Chinese Super League",
    "a_league": "A-League", "scottish_premiership": "Premiership",
    "jupiler_pro_league": "Jupiler Pro League", "austrian_bundesliga": "Bundesliga",
    "swiss_super_league": "Super League", "super_league_greece": "Super League",
    "ekstraklasa": "Ekstraklasa", "superligaen": "Superliga",
    "allsvenskan": "Allsvenskan", "eliteserien": "Eliteserien",
    "veikkausliiga": "Veikkausliiga", "premier_division_ie": "Premier Division",
    "primera_a_colombia": "Primera A", "primera_division_uy": "Primera División",
    "primera_a_ecuador": "Primera A", "liga_1_peru": "Liga 1",
    "primera_division_bo": "Primera División", "primera_division_ve": "Primera División",
    "canadian_premier_league": "Canadian Premier League",
    "friendlies": "Friendlies", "international_friendlies": "Friendlies",
    "world_cup_qualifiers_uefa": "WC Qualifiers UEFA",
    "euro_qualifiers": "Euro Qualifiers", "nations_league": "Nations League",
    "fifa_world_cup_qualifying": "WC Qualifiers",
    "afcon_qualifiers": "AFCON Qualifiers",
    "africa_cup_nations_qualifying": "AFCON Qualifiers",
    "premier_liga": "Premier Liga", "efl_championship": "Championship",
    "efl_league_one": "League One", "efl_league_two": "League Two",
    "womens_super_league": "WSL", "fa_wsl": "WSL",
    "copa_libertadores": "Copa Libertadores", "copa_sudamericana": "Copa Sudamericana",
    "uefa_champions_league": "Champions League", "champions_league": "Champions League",
    "europa_league": "Europa League", "conference_league": "Conference League",
}


def _resolve_league(league_slug: str, row: dict) -> tuple:
    """
    Возвращает (country, competition_name) из слага лиги SharpAPI.
    Формат: country_slug_-_league_slug (например, 'argentina_-_primera_a').
    """
    if not league_slug:
        return ("", "")
    slug_lower = league_slug.lower().strip()

    if "_-_" in slug_lower:
        parts = slug_lower.split("_-_", 1)
        country_slug = parts[0].strip()
        league_part = parts[1].strip() if len(parts) > 1 else ""

        country = COUNTRY_MAP.get(country_slug, country_slug.replace("_", " ").title())
        league_name = LEAGUE_NAME_MAP.get(league_part, league_part.replace("_", " ").title())
        return (country, league_name)

    return ("", slug_lower.replace("_", " ").title())


def _fetch_sharpapi(url, headers):
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("[SHARPAPI] HTTP 429 — Rate Limit. Прерываем запросы, переходим на кэш.")
            return None
        print(f"[SHARPAPI HTTP {e.code}] {e.reason}")
        try:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[SHARPAPI HTTP body] {body[:500]}")
        except Exception:
            pass
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"[SHARPAPI ERROR] {e}")
        return None


def _flush_old_matches():
    """Удаляет все match:* ключи перед свежим сбором."""
    all_fields = get_all_fields()
    deleted = 0
    for key in all_fields:
        if key.startswith("match:"):
            delete_from_cache(key)
            deleted += 1
    print(f"[SHARPAPI] Flush: удалено {deleted} старых ключей match:*")
    return deleted


def _fetch_odds_pages(headers, max_pages, limit, rate_delay):
    """Одна фаза — только /odds с cursor-пагинацией."""
    all_rows = []
    pages = 0
    cursor = None
    debug_printed = False

    for page in range(max_pages):
        url = f"{SHARP_API_BASE}/odds?sport=soccer&market=moneyline&limit={limit}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        print(f"[SHARPAPI] Запрос /odds: page={page + 1}, cursor={'да' if cursor else 'нет'}")

        data = _fetch_sharpapi(url, headers)
        if data is None:
            break

        rows = data.get("data", [])
        if isinstance(rows, list):
            all_rows.extend(rows)
            print(f"[SHARPAPI] Страница {page + 1}: {len(rows)} записей")

            if not debug_printed and rows:
                sample = rows[0]
                print(f"[SHARPAPI DEBUG] Образец строки:")
                print(f"  event_id: {sample.get('event_id', 'НЕТ')}")
                print(f"  selection_type: {sample.get('selection_type', 'НЕТ')}")
                print(f"  odds_decimal: {sample.get('odds_decimal', 'НЕТ')}")
                print(f"  home_team: {sample.get('home_team', 'НЕТ')}")
                print(f"  away_team: {sample.get('away_team', 'НЕТ')}")
                print(f"  event_start_time: {sample.get('event_start_time', 'НЕТ')}")
                print(f"  league: {sample.get('league', 'НЕТ')}")
                print(f"  market_type: {sample.get('market_type', 'НЕТ')}")
                print(f"  Все ключи: {list(sample.keys())}")
                debug_printed = True
        else:
            print(f"[SHARPAPI] Страница {page + 1}: data не список ({type(rows)})")

        pages += 1

        if len(rows) < limit:
            print(f"[SHARPAPI] Конец данных на странице {page + 1}")
            break

        pagination = data.get("pagination", {})
        if not isinstance(pagination, dict):
            break

        if not pagination.get("has_more", False):
            break

        next_cursor = pagination.get("next_cursor")
        if next_cursor:
            cursor = next_cursor
        else:
            break

        time.sleep(rate_delay)

    return all_rows, pages


def collect_sharpapi():
    api_key = os.environ.get("SHARP_API_KEY")
    if not api_key:
        print("[SHARPAPI] SHARP_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    rate_delay = float(os.environ.get("SHARPAPI_RATE_LIMIT_DELAY", "6"))
    max_pages = int(os.environ.get("SHARPAPI_MAX_PAGES", "60"))
    limit = int(os.environ.get("SHARPAPI_LIMIT", "200"))
    flush_old = os.environ.get("SHARPAPI_FLUSH_OLD", "0") == "1"

    init_metrics = run_initialization()
    if not init_metrics.get("redis_available", False):
        print("[SHARPAPI] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    if flush_old:
        _flush_old_matches()

    print("[SHARPAPI] Сбор матчей из /odds?sport=soccer&market=moneyline ...")
    odds_rows, pages = _fetch_odds_pages(headers, max_pages, limit, rate_delay)
    print(f"[SHARPAPI] Получено строк odds: {len(odds_rows)} (страниц: {pages})")

    if not odds_rows:
        meta = {
            "last_run": now_msk(),
            "total_events": 0,
            "stored_matches": 0,
            "created": 0,
            "updated": 0,
            "error_count": 1,
            "pages_fetched": pages,
            "skipped_past": 0,
            "deduped": 0,
        }
        save_meta("sharpapi", **meta)
        return meta

    # --- Группировать odds по event_id, выбрать лучшие ---
    events_map = {}

    for row in odds_rows:
        eid = row.get("event_id", "")
        sel_type = row.get("selection_type", "")
        odds_dec = row.get("odds_decimal")
        if not eid or not sel_type or odds_dec is None:
            continue

        try:
            odds_dec = float(odds_dec)
        except (ValueError, TypeError):
            continue

        if odds_dec <= 1.0:
            continue

        if eid not in events_map:
            league_slug = row.get("league", "")
            country, comp_name = _resolve_league(league_slug, row)
            events_map[eid] = {
                "home_team": row.get("home_team", ""),
                "away_team": row.get("away_team", ""),
                "start_time": row.get("event_start_time", "") or row.get("start_time", ""),
                "league": comp_name or league_slug,
                "country": country,
                "odds": {},
            }

        current = events_map[eid]["odds"].get(sel_type)
        if current is None or odds_dec > current:
            events_map[eid]["odds"][sel_type] = odds_dec

    sel_types_found = set()
    for ev in events_map.values():
        sel_types_found.update(ev["odds"].keys())
    print(f"[SHARPAPI] Найдено selection_type: {sel_types_found}")

    # --- Записать в Redis ---
    stored = 0
    skipped_past = 0
    deduped = 0
    seen_keys = set()
    existing_keys = set(get_all_fields().keys())
    created = 0
    updated = 0

    for eid, ev in events_map.items():
        home_team = ev["home_team"]
        away_team = ev["away_team"]
        if not home_team or not away_team:
            continue

        dedup_key = f"{home_team}|{away_team}|{eid}"
        if dedup_key in seen_keys:
            deduped += 1
            continue
        seen_keys.add(dedup_key)

        date_utc = normalize_date(ev["start_time"])

        if date_utc and not is_future_match(date_utc):
            skipped_past += 1
            continue

        ev_odds = ev["odds"]
        odds = {
            "home": str(ev_odds["home"]) if "home" in ev_odds else "-",
            "draw": str(ev_odds["draw"]) if "draw" in ev_odds else "-",
            "away": str(ev_odds["away"]) if "away" in ev_odds else "-",
            "source": "SharpAPI",
            "updated_at": now_msk(),
        }

        source_ids = {"sharpapi": str(eid)}

        extra = {
            "odds": odds,
            "source_ids": source_ids,
        }

        cid = upsert_match(
            home_team=home_team,
            away_team=away_team,
            date_utc=date_utc,
            competition=ev["league"],
            country=ev.get("country", ""),
            status="scheduled",
            source="SharpAPI",
            **extra,
        )

        if cid:
            stored += 1
            if cid in existing_keys:
                updated += 1
            else:
                created += 1

    total_events = len(events_map)
    print(f"[SHARPAPI] Записано: {stored}, создано: {created}, обновлено: {updated}, пропущено (прошедшие): {skipped_past}, дедупликатов: {deduped}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "error_count": 0,
        "pages_fetched": pages,
        "skipped_past": skipped_past,
        "deduped": deduped,
    }
    save_meta("sharpapi", **meta)

    return meta


if __name__ == "__main__":
    result = collect_sharpapi()
    print(f"[SHARPAPI] Result: {result}")
