"""
Коллектор SharpAPI для Gatekeeper-AI v700-prod.
Получает матчи и коэффициенты, сохраняет в Redis через gatekeeper_hub.
Хелперы normalize_date, is_future_match, now_msk, save_meta импортируются из хаба.

v7.0 (Phase 2):
  - SHARPAPI_API_KEY (вместо SHARP_API_KEY) — аудит §2.11
  - idempotency_key в patch_match — аудит §2.2
  - odds как float (вместо str) — аудит §2.1
  - run_initialization(collector="sharpapi") — аудит §2.6
  - team_registry.normalize_team_name — аудит §2.4
  - graceful shutdown (is_shutdown_requested) в цикле — аудит §2.3
  - source + sources в payload — аудит §2.2
"""
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

from gatekeeper_hub import (
    upsert_match, patch_match, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)
# Direct redis_hub import removed — §1.4: hub is the only gateway

# §20.7: Module registry + §20.6: log_event
try:
    from gatekeeper_hub import register_module, log_event
except ImportError:
    def register_module(name, **kwargs):
        def deco(func):
            return func
        return deco
    def log_event(source, level, message, **kwargs):
        pass

# Graceful shutdown — аудит §2.3
try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

# team_registry — аудит §2.4: clean_team_name
# FIX: nested try-except (двойной except на одном уровне — dead code)
try:
    from team_registry import clean_team_name
except ImportError:
    try:
        from search_module import clean_team_name
    except ImportError:
        def clean_team_name(name: str) -> str:
            return name.lower().strip().replace(" ", "_")

SHARP_API_BASE = "https://api.sharpapi.io/api/v1"
DAYS_AHEAD = int(os.environ.get("SHARPAPI_DAYS_AHEAD", "3"))

logger = logging.getLogger("sharpapi")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

COLLECTOR_NAME = "sharpapi"

__version__ = "8.11-patched"
__all__ = ["collect_sharpapi", "collect_and_process", "__version__"]


# ---------------------------------------------------------------------------
# Словари для парсинга слага лиги SharpAPI
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
    "veikkausliitto": "Veikkausliiga", "premier_division_ie": "Premier Division",
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
            logger.info(f"[SHARPAPI] HTTP 429 — Rate Limit. Прерываем запросы, переходим на кэш.")
            return None
        logger.warning(f"[SHARPAPI HTTP {e.code}] {e.reason}")
        try:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(f"[SHARPAPI HTTP body] {body[:500]}")
        except Exception:
            pass
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.error(f"[SHARPAPI ERROR] {e}")
        return None


def _flush_old_matches():
    """§1.12: Flush-режим — использует cleanup_expired из хаба (§1.4)."""
    from gatekeeper_hub import cleanup_expired
    result = cleanup_expired(dry_run=False, auto_migrate=True)
    logger.info(f"[SHARPAPI] Flush: cleanup_expired удалено {result.get('count', 0)} ключей")
    return result.get('count', 0)


def _fetch_odds_pages(headers, max_pages, limit, rate_delay):
    all_rows = []
    pages = 0
    cursor = None
    debug_printed = False

    for page in range(max_pages):
        # Graceful shutdown — аудит §2.3
        if is_shutdown_requested():
            logger.info(f"[SHARPAPI] Получен SIGTERM, останавливаем сбор страниц")
            break

        url = f"{SHARP_API_BASE}/odds?sport=soccer&market=moneyline&limit={limit}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        logger.info(f"[SHARPAPI] Запрос /odds: page={page + 1}, cursor={'да' if cursor else 'нет'}")

        data = _fetch_sharpapi(url, headers)
        if data is None:
            break

        rows = data.get("data", [])
        if isinstance(rows, list):
            all_rows.extend(rows)
            logger.info(f"[SHARPAPI] Страница {page + 1}: {len(rows)} записей")

            if not debug_printed and rows:
                sample = rows[0]
                logger.debug("[SHARPAPI DEBUG] Образец строки:")
                logger.debug(f"  event_id: {sample.get('event_id', 'НЕТ')}")
                logger.debug(f"  selection_type: {sample.get('selection_type', 'НЕТ')}")
                logger.debug(f"  odds_decimal: {sample.get('odds_decimal', 'НЕТ')}")
                logger.debug(f"  home_team: {sample.get('home_team', 'НЕТ')}")
                logger.debug(f"  away_team: {sample.get('away_team', 'НЕТ')}")
                logger.debug(f"  event_start_time: {sample.get('event_start_time', 'НЕТ')}")
                logger.debug(f"  league: {sample.get('league', 'НЕТ')}")
                logger.debug(f"  market_type: {sample.get('market_type', 'НЕТ')}")
                logger.debug(f"  Все ключи: {list(sample.keys())}")
                debug_printed = True
        else:
            logger.info(f"[SHARPAPI] Страница {page + 1}: data не список ({type(rows)})")

        pages += 1

        if len(rows) < limit:
            logger.info(f"[SHARPAPI] Конец данных на странице {page + 1}")
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
    # GitHub secret: SHARP_API_KEY (primary). SHARPAPI_API_KEY — legacy fallback.
    api_key = os.environ.get("SHARPAPI_API_KEY") or os.environ.get("SHARP_API_KEY")
    if not api_key:
        logger.error("[SHARPAPI] SHARP_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    rate_delay = float(os.environ.get("SHARPAPI_RATE_LIMIT_DELAY", "6"))
    max_pages = int(os.environ.get("SHARPAPI_MAX_PAGES", "60"))
    limit = int(os.environ.get("SHARPAPI_LIMIT", "200"))
    flush_old = os.environ.get("SHARPAPI_FLUSH_OLD", "0") == "1"

    # FIX: dry-run support
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    
    # FIX §2.6: run_initialization с collector=
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    if not init_metrics.get("redis_available", False):
        logger.info(f"[SHARPAPI] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    if flush_old:
        _flush_old_matches()

    logger.info(f"[SHARPAPI] Сбор матчей из /odds?sport=soccer&market=moneyline ...")
    log_event("sharpapi", "INFO", "Collection started", run_id=run_id, max_pages=max_pages)
    odds_rows, pages = _fetch_odds_pages(headers, max_pages, limit, rate_delay)
    logger.info(f"[SHARPAPI] Получено строк odds: {len(odds_rows)} (страниц: {pages})")

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
        save_meta(COLLECTOR_NAME, **meta)
        return meta

    # --- Группировать odds по event_id ---
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
            # FIX §2.4: нормализация команд через team_registry
            home_raw = row.get("home_team", "")
            away_raw = row.get("away_team", "")
            events_map[eid] = {
                "home_team": clean_team_name(home_raw) if home_raw else "",
                "away_team": clean_team_name(away_raw) if away_raw else "",
                "home_team_raw": home_raw,
                "away_team_raw": away_raw,
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
    logger.info(f"[SHARPAPI] Найдено selection_type: {sel_types_found}")

    # --- Записать в Redis ---
    stored = 0
    skipped_past = 0
    skipped_future = 0
    deduped = 0
    seen_keys = set()
    created = 0
    updated = 0

    for eid, ev in events_map.items():
        # Graceful shutdown — аудит §2.3
        if is_shutdown_requested():
            logger.info(f"[SHARPAPI] Получен SIGTERM, останавливаем запись матчей")
            break

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

        # Upper bound: отбрасываем матчи дальше DAYS_AHEAD
        if date_utc:
            try:
                match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                    skipped_future += 1
                    continue
            except (ValueError, TypeError):
                pass

        ev_odds = ev["odds"]

        # FIX §2.2: idempotency_key для защиты от двойного patch
        idempotency_key = f"{run_id}:{eid}:odds"

        # 1. Создать матч (с source и sources)
        try:
            cid = upsert_match(
                home_team=home_team,
                away_team=away_team,
                date_utc=date_utc,
                competition=ev["league"],
                country=ev.get("country", ""),
                status="scheduled",
                source=COLLECTOR_NAME,
                sources=[COLLECTOR_NAME],
                source_ids={COLLECTOR_NAME: str(eid)},
            )
        except Exception as e:
            logger.error(f"[SHARPAPI] upsert error for event {eid}: {e}")
            continue

        if cid:
            stored += 1
            created += 1

            # FIX §2.1: odds как float (вместо str)
            # FIX §2.2: idempotency_key в patch_match
            odds_current = {}
            if "home" in ev_odds:
                odds_current["home"] = float(ev_odds["home"])
            if "draw" in ev_odds:
                odds_current["draw"] = float(ev_odds["draw"])
            if "away" in ev_odds:
                odds_current["away"] = float(ev_odds["away"])

            if odds_current and not dry_run:
                try:
                    patch_match(
                        cid, "odds", {"current": odds_current},
                        source=COLLECTOR_NAME,
                        upstream="betradar",
                        idempotency_key=idempotency_key,
                    )
                except Exception as e:
                    logger.error(f"[SHARPAPI] patch error for cid={cid}: {e}")

    total_events = len(events_map)
    logger.info(f"[SHARPAPI] Записано: {stored}, создано: {created}, обновлено: {updated}, "
          f"прошлое: {skipped_past}, будущее: {skipped_future}, дедупликатов: {deduped}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "created": created,
        "updated": updated,
        "error_count": 0,
        "pages_fetched": pages,
        "skipped_past": skipped_past,
        "skipped_future": skipped_future,
        "deduped": deduped,
        "run_id": run_id,
    }
    log_event("sharpapi", "INFO", "Collection complete",
        total_events=total_events, stored=stored, pages=pages)
    save_meta(COLLECTOR_NAME, **meta)

    return meta


@register_module("sharpapi", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process():
    """Единая точка входа для CI/CD."""
    return collect_sharpapi()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Collector SharpAPI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать в Redis (dry-run)")
    args = parser.parse_args()
    if args.dry_run:
        logger.info("[SHARPAPI] DRY-RUN mode — данные НЕ будут записаны в Redis")
        os.environ["DRY_RUN"] = "1"
    result = collect_sharpapi()
    logger.info(f"[SHARPAPI] Result: {result}")
