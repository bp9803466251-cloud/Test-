#!/usr/bin/env python3
"""
Коллектор SharpAPI для Gatekeeper-AI v9.1-argparse.
Получает матчи и коэффициенты, сохраняет в Redis через gatekeeper_hub.

v9.0-delta:
  - Delta endpoint /odds/delta?since=... для инкрементальных обновлений
  - X-RateLimit-Remaining: динамический self-throttle
  - 429 retry с экспоненциальным backoff
  - HTTP timeout 15с (env: SHARPAPI_HTTP_TIMEOUT)
  - Removed[] handling: удаление устаревших odds из Redis
  - Watermark storage: meta.last_odds_timestamp для delta-цепочки
  - Overflow detection: fallback на полный /odds scan
  - Progress logging: книги, markets, events summary
  - Fallback: если delta недоступен → полный scan через /odds

v8.11 (предыдущая):
  - Bulk /odds с cursor pagination
  - group_by event_id (ручная группировка)
  - selection_type: home/draw/away напрямую
  - odds_decimal: десятичный формат (без конвертации)
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

from gatekeeper_hub import (
    upsert_match, patch_match, run_initialization,
    normalize_date, is_future_match, now_msk, save_meta,
)
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
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

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

__version__ = "9.1-argparse"
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
    "primera_a": "Primera Divisi\u00f3n", "primera_division": "Primera Divisi\u00f3n",
    "premier_league": "Premier League", "championship": "Championship",
    "league_one": "League One", "league_two": "League Two",
    "national_league": "National League", "fa_cup": "FA Cup",
    "efl_cup": "EFL Cup", "la_liga": "La Liga",
    "segunda_division": "Segunda Divisi\u00f3n", "copa_del_rey": "Copa del Rey",
    "serie_a": "Serie A", "serie_b": "Serie B", "coppa_italia": "Coppa Italia",
    "bundesliga": "Bundesliga", "bundesliga_2": "2. Bundesliga",
    "dfb_pokal": "DFB-Pokal", "ligue_1": "Ligue 1", "ligue_2": "Ligue 2",
    "coupe_de_france": "Coupe de France", "eredivisie": "Eredivisie",
    "primeira_liga": "Primeira Liga", "brasileirao": "Brasileir\u00e3o",
    "serie_a_brazil": "S\u00e9rie A", "campeonato_brasileiro": "Brasileir\u00e3o",
    "liga_mx": "Liga MX", "mls": "MLS",
    "primera_division_py": "Primera Divisi\u00f3n", "paraguayan_primera": "Primera Divisi\u00f3n",
    "primera_division_cl": "Primera Divisi\u00f3n", "chilean_primera": "Primera Divisi\u00f3n",
    "welsh_premier": "Welsh Premier League", "welsh_cup": "Welsh Cup",
    "super_lig": "S\u00fcper Lig", "j1_league": "J1 League",
    "k_league_1": "K League 1", "chinese_super_league": "Chinese Super League",
    "a_league": "A-League", "scottish_premiership": "Premiership",
    "jupiler_pro_league": "Jupiler Pro League", "austrian_bundesliga": "Bundesliga",
    "swiss_super_league": "Super League", "super_league_greece": "Super League",
    "ekstraklasa": "Ekstraklasa", "superligaen": "Superliga",
    "allsvenskan": "Allsvenskan", "eliteserien": "Eliteserien",
    "veikkausliitto": "Veikkausliiga", "premier_division_ie": "Premier Division",
    "primera_a_colombia": "Primera A", "primera_division_uy": "Primera Divisi\u00f3n",
    "primera_a_ecuador": "Primera A", "liga_1_peru": "Liga 1",
    "primera_division_bo": "Primera Divisi\u00f3n", "primera_division_ve": "Primera Divisi\u00f3n",
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


# ---------------------------------------------------------------------------
# HTTP fetch с retry, rate-limit awareness
# ---------------------------------------------------------------------------

_last_rate_remaining = None
_last_rate_limit = None


def _fetch_sharpapi(url, headers, timeout=None):
    """HTTP GET с retry на 429 и чтением rate-limit заголовков."""
    global _last_rate_remaining, _last_rate_limit
    if timeout is None:
        timeout = int(os.environ.get("SHARPAPI_HTTP_TIMEOUT", "15"))

    max_retries = int(os.environ.get("SHARPAPI_MAX_RETRIES", "2"))

    for attempt in range(max_retries + 1):
        if is_shutdown_requested():
            logger.info("[SHARPAPI] Shutdown requested \u2014 остановка fetch")
            return None
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                # Чтение rate-limit заголовков
                _last_rate_remaining = response.headers.get("X-RateLimit-Remaining")
                _last_rate_limit = response.headers.get("X-RateLimit-Limit")
                data_delay = response.headers.get("X-Data-Delay", "?")

                body = json.loads(response.read().decode("utf-8"))

                if _last_rate_remaining:
                    logger.debug(f"[SHARPAPI] Rate: {_last_rate_remaining}/{_last_rate_limit} remaining, delay={data_delay}s")

                return body

        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Retry с backoff
                retry_after = int(e.headers.get("Retry-After", "0"))
                reset_ts = e.headers.get("X-RateLimit-Reset")
                if retry_after > 0:
                    wait = retry_after
                elif reset_ts:
                    try:
                        wait = max(1, int(reset_ts) - int(time.time()))
                    except (ValueError, TypeError):
                        wait = 5
                else:
                    wait = 2 ** (attempt + 2)

                if attempt < max_retries:
                    logger.warning(f"[SHARPAPI] HTTP 429 \u2014 Rate Limit. Waiting {wait}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(min(wait, 60))
                    continue
                else:
                    logger.error(f"[SHARPAPI] HTTP 429 \u2014 исчерпаны retries")
                    return None

            logger.warning(f"[SHARPAPI HTTP {e.code}] {e.reason}")
            try:
                body = e.read().decode("utf-8", errors="replace")
                logger.warning(f"[SHARPAPI HTTP body] {body[:500]}")
            except Exception:
                pass
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"[SHARPAPI] Network error (attempt {attempt + 1}): {e}, retry in {wait}s")
                time.sleep(wait)
                continue
            logger.error(f"[SHARPAPI] Network error: {e}")
            return None

    return None


def _flush_old_matches():
    """\u00a71.12: Flush-режим \u2014 использует cleanup_expired из хаба (\u00a71.4)."""
    from gatekeeper_hub import cleanup_expired
    result = cleanup_expired(dry_run=False, auto_migrate=True)
    logger.info(f"[SHARPAPI] Flush: cleanup_expired удалено {result.get('count', 0)} ключей")
    return result.get('count', 0)


# ---------------------------------------------------------------------------
# Полный scan через /odds с cursor pagination
# ---------------------------------------------------------------------------

def _fetch_odds_full(headers, max_pages, limit, rate_delay):
    """Полный scan через /odds?sport=soccer&market=moneyline с cursor pagination."""
    all_rows = []
    pages = 0
    cursor = None
    debug_printed = False
    global _last_rate_remaining

    for page in range(max_pages):
        if is_shutdown_requested():
            logger.info(f"[SHARPAPI] Получен SIGTERM, останавливаем сбор страниц")
            break

        url = f"{SHARP_API_BASE}/odds?sport=soccer&market=moneyline&limit={limit}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        logger.info(f"[SHARPAPI] /odds: page={page + 1}, cursor={'да' if cursor else 'нет'}")

        data = _fetch_sharpapi(url, headers)
        if data is None:
            break

        rows = data.get("data", [])
        if isinstance(rows, list):
            all_rows.extend(rows)
            logger.info(f"[SHARPAPI] Страница {page + 1}: {len(rows)} записей (всего: {len(all_rows)})")

            if not debug_printed and rows:
                sample = rows[0]
                logger.debug(f"[SHARPAPI DEBUG] Образец: event_id={sample.get('event_id', '?')}, "
                    f"sel={sample.get('selection_type', '?')}, "
                    f"odds={sample.get('odds_decimal', '?')}, "
                    f"home={sample.get('home_team', '?')}, "
                    f"away={sample.get('away_team', '?')}, "
                    f"league={sample.get('league', '?')}, "
                    f"book={sample.get('sportsbook', '?')}")
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

        # Dynamic rate delay: если remaining < 3, удваиваем delay
        effective_delay = rate_delay
        if _last_rate_remaining:
            try:
                remaining = int(_last_rate_remaining)
                if remaining <= 3:
                    effective_delay = rate_delay * 2
                    logger.warning(f"[SHARPAPI] Rate limit low ({remaining}), delay={effective_delay}s")
            except (ValueError, TypeError):
                pass

        time.sleep(effective_delay)

    # Watermark из последнего ответа
    updated_at = data.get("updated_at", "") if data else ""
    return all_rows, pages, updated_at


# ---------------------------------------------------------------------------
# Delta scan через /odds/delta?since=... с offset pagination
# ---------------------------------------------------------------------------

def _fetch_odds_delta(headers, since_ts, limit, rate_delay):
    """Инкрементальный scan через /odds/delta?since=... с offset pagination."""
    all_rows = []
    removed_ids = []
    pages = 0
    offset = 0
    max_offset = 500
    new_watermark = since_ts
    overflow_detected = False
    debug_printed = False

    while True:
        if is_shutdown_requested():
            logger.info(f"[SHARPAPI] SIGTERM \u2014 остановка delta scan")
            break

        url = (f"{SHARP_API_BASE}/odds/delta?since={urllib.parse.quote(since_ts)}"
               f"&sport=soccer&market=moneyline&limit={limit}&offset={offset}")

        logger.info(f"[SHARPAPI] /odds/delta: offset={offset}, since={since_ts[:19]}")

        data = _fetch_sharpapi(url, headers)
        if data is None:
            break

        rows = data.get("data", [])
        removed = data.get("removed", [])

        if isinstance(rows, list):
            all_rows.extend(rows)
            if removed:
                removed_ids.extend(removed)

            logger.info(f"[SHARPAPI] Delta: {len(rows)} изменений, {len(removed)} удалено (всего: {len(all_rows)})")

            if not debug_printed and rows:
                sample = rows[0]
                logger.debug(f"[SHARPAPI DELTA DEBUG] event_id={sample.get('event_id', '?')}, "
                    f"sel={sample.get('selection_type', '?')}, "
                    f"odds={sample.get('odds_decimal', '?')}, "
                    f"timestamp={sample.get('timestamp', '?')}")
                debug_printed = True
        else:
            logger.warning(f"[SHARPAPI] Delta: data не список ({type(rows)})")

        pages += 1

        pagination = data.get("pagination", {})
        if not isinstance(pagination, dict):
            break

        # Проверка overflow
        if data.get("overflow", False):
            overflow_detected = True
            logger.warning(f"[SHARPAPI] Delta overflow detected \u2014 bounded scan limit")

        has_more = pagination.get("has_more", False)
        next_offset = pagination.get("next_offset")

        if not has_more:
            # Terminal page \u2014 берём watermark
            if overflow_detected:
                logger.warning(f"[SHARPAPI] Delta overflow на terminal page \u2014 re-bootstrap нужен")
                new_watermark = data.get("meta", {}).get("server_time", "") or data.get("updated_at", "")
                return all_rows, pages, new_watermark, removed_ids, True  # overflow=True

            new_watermark = data.get("meta", {}).get("server_time", "") or data.get("updated_at", "")
            break

        if next_offset is None:
            # Window too large \u2014 re-bootstrap
            logger.warning(f"[SHARPAPI] Delta: next_offset=null при has_more=true \u2014 re-bootstrap")
            return all_rows, pages, new_watermark, removed_ids, True  # overflow=True

        offset = next_offset
        if offset > max_offset:
            logger.warning(f"[SHARPAPI] Delta: offset > {max_offset} \u2014 re-bootstrap")
            return all_rows, pages, new_watermark, removed_ids, True

        time.sleep(rate_delay)

    return all_rows, pages, new_watermark, removed_ids, overflow_detected


# ---------------------------------------------------------------------------
# Группировка odds по event_id (общая для full и delta)
# ---------------------------------------------------------------------------

def _group_odds_by_event(odds_rows):
    """Группирует плоский список odds rows по event_id, берёт лучшие коэффициенты."""
    events_map = {}
    books_seen = set()
    markets_seen = set()

    for row in odds_rows:
        eid = row.get("event_id", "")
        sel_type = row.get("selection_type", "")
        odds_dec = row.get("odds_decimal")
        book = row.get("sportsbook", "")
        market = row.get("market_type", "")

        if book:
            books_seen.add(book)
        if market:
            markets_seen.add(market)

        if not eid or not sel_type or odds_dec is None:
            continue

        try:
            odds_dec = float(odds_dec)
        except (ValueError, TypeError):
            continue

        if odds_dec <= 1.0:
            continue

        # Только home/draw/away для 1x2
        if sel_type not in ("home", "draw", "away"):
            continue

        if eid not in events_map:
            league_slug = row.get("league", "")
            country, comp_name = _resolve_league(league_slug, row)
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

    logger.info(f"[SHARPAPI] Книги: {sorted(books_seen)}, markets: {sorted(markets_seen)}")

    sel_types_found = set()
    for ev in events_map.values():
        sel_types_found.update(ev["odds"].keys())
    logger.info(f"[SHARPAPI] Selection types: {sel_types_found}")

    return events_map


# ---------------------------------------------------------------------------
# Главный коллектор
# ---------------------------------------------------------------------------

def collect_sharpapi():
    api_key = os.environ.get("SHARPAPI_API_KEY") or os.environ.get("SHARP_API_KEY")
    if not api_key:
        logger.error("[SHARPAPI] SHARP_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    rate_delay = float(os.environ.get("SHARPAPI_RATE_LIMIT_DELAY", "5"))
    max_pages = int(os.environ.get("SHARPAPI_MAX_PAGES", "60"))
    limit = int(os.environ.get("SHARPAPI_LIMIT", "200"))
    flush_old = os.environ.get("SHARPAPI_FLUSH_OLD", "0") == "1"
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    use_delta = os.environ.get("SHARPAPI_USE_DELTA", "1") == "1"

    # \u00a72.6: run_initialization
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    if not init_metrics.get("redis_available", False):
        logger.info(f"[SHARPAPI] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    if flush_old:
        _flush_old_matches()

    # Чтение watermark из meta (для delta)
    last_watermark = ""
    try:
        from gatekeeper_hub import get_meta
        prev_meta = get_meta(COLLECTOR_NAME)
        if prev_meta:
            last_watermark = prev_meta.get("last_odds_timestamp", "")
    except Exception:
        pass

    # Решение: delta или full scan
    use_delta_path = use_delta and last_watermark and not flush_old

    if use_delta_path:
        logger.info(f"[SHARPAPI] Delta scan: since={last_watermark[:19]}")
        log_event("sharpapi", "INFO", "Delta scan started", run_id=run_id, since=last_watermark)

        odds_rows, pages, new_watermark, removed_ids, overflow = _fetch_odds_delta(
            headers, last_watermark, limit, rate_delay
        )

        if overflow:
            logger.warning(f"[SHARPAPI] Delta overflow \u2014 fallback на полный scan")
            use_delta_path = False
        else:
            logger.info(f"[SHARPAPI] Delta: {len(odds_rows)} строк, {len(removed_ids)} удалено, "
                  f"watermark={new_watermark[:19] if new_watermark else '?'}")

    if not use_delta_path:
        logger.info(f"[SHARPAPI] Полный scan: /odds?sport=soccer&market=moneyline")
        log_event("sharpapi", "INFO", "Full scan started", run_id=run_id, max_pages=max_pages)

        odds_rows, pages, new_watermark = _fetch_odds_full(headers, max_pages, limit, rate_delay)
        removed_ids = []

        logger.info(f"[SHARPAPI] Full: {len(odds_rows)} строк (страниц: {pages}), "
              f"watermark={new_watermark[:19] if new_watermark else '?'}")

    if not odds_rows and not removed_ids:
        meta = {
            "last_run": now_msk(),
            "total_events": 0,
            "stored_matches": 0,
            "created": 0,
            "updated": 0,
            "error_count": 0,
            "pages_fetched": pages,
            "skipped_past": 0,
            "deduped": 0,
            "scan_mode": "delta" if use_delta_path else "full",
            "last_odds_timestamp": new_watermark or last_watermark,
            "run_id": run_id,
        }
        save_meta(COLLECTOR_NAME, **meta)
        logger.info(f"[SHARPAPI] Нет данных для записи")
        return meta

    # Группировка odds по event_id
    events_map = _group_odds_by_event(odds_rows) if odds_rows else {}

    # Запись в Redis
    stored = 0
    skipped_past = 0
    skipped_future = 0
    deduped = 0
    seen_keys = set()
    created = 0
    updated = 0
    existing_keys = set(get_all_fields().keys())

    def _event_date_key(item):
        eid, ev = item
        dt_str = ev.get("start_time", "")
        return dt_str or ""

    sorted_events = sorted(events_map.items(), key=_event_date_key)

    for eid, ev in sorted_events:
        if is_shutdown_requested():
            logger.info(f"[SHARPAPI] SIGTERM \u2014 остановка записи")
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

        if date_utc:
            try:
                match_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                if match_dt > datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD):
                    skipped_future += 1
                    continue
            except (ValueError, TypeError):
                pass

        ev_odds = ev["odds"]
        idempotency_key = f"{run_id}:{eid}:odds"

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
            if f"match:{cid}" in existing_keys:
                updated += 1
            else:
                created += 1

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

    # Обработка removed[] \u2014 логирование (удаление из Redis опционально)
    removed_count = len(removed_ids) if removed_ids else 0
    if removed_count > 0:
        logger.info(f"[SHARPAPI] Removed odds: {removed_count} (лог, удаление из Redis опционально)")

    total_events = len(events_map)
    scan_mode = "delta" if use_delta_path else "full"

    logger.info(f"[SHARPAPI] Готово: {stored} записано, {created} создано, {updated} обновлено, "
          f"прошлое: {skipped_past}, будущее: {skipped_future}, дедуп: {deduped}, "
          f"removed: {removed_count}, mode: {scan_mode}")

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
        "scan_mode": scan_mode,
        "last_odds_timestamp": new_watermark or last_watermark,
        "removed_count": removed_count,
        "run_id": run_id,
    }
    save_meta(COLLECTOR_NAME, **meta)

    return meta


@register_module("sharpapi", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process():
    """Единая точка входа для CI/CD."""
    return collect_sharpapi()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SharpAPI Collector v9.0-delta")
    parser.add_argument("--dry-run", action="store_true", help="Без записи в Redis")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    )

    result = collect_and_process()
    print(f"\n=== SharpAPI Collector Summary ===")
    for k, v in sorted(result.items()):
        print(f"  {k}: {v}")
