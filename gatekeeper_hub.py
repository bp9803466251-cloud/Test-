# gatekeeper_hub.py
"""
Единый хаб Gatekeeper-AI v710.
Все операции с данными матчей проходят только через этот модуль.

v2.1:
  - patch_match: restored positional (section, data) API + keyword API
  - copy.deepcopy in _merge_odds, _merge_1x2, patch_match (CAS safe)
  - _init_match_object: +home_clean, +away_clean, +competition, +country,
    +league_code, +season, +time_utc, +h2h, +csv_raw, +flags
  - upsert_match: +competition/country/league_code/season params, past filter
  - patch_match: +h2h param
  - cleanup_expired: date-based check (not only status==completed)
  - batch_upsert_matches: returns dict with stats
  - get_matches_count: dedup via set
  - upsert_history_match: enrichment + indexes + merge existing
  - migrate_to_history: strips live-only fields, always updates indexes
  - _merge_odds O/U+AH: "price" field (not "data"), deepcopy
  - _merge_1x2: proper type ("opening"/"live"), deepcopy
  - _normalize_incoming_full_odds: `is not None` (not `or`)
  - save_meta/save_health: set_key (not hash)
  - Added: get_match_by_teams, patch_match_by_teams, find_match_fuzzy,
    get_history_by_team, get_analysis_by_date, _resolve_upstream,
    _remove_from_index, _extract_opening_closing
  - Removed dead code: _validate_section, _prices_agree, _mad_outlier,
    MERGE_SECTIONS, MAX_HISTORY_ENTRIES
  - Removed unused imports: os, json, batch_get_from_cache, delete_keys,
    zrem_key, srem_key, Set
  - _make_canonical_id: "nodate" (not "unknown") for missing dates
"""
import copy
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from redis_hub import (
    # Hash model (live)
    save_to_cache,
    get_from_cache,
    delete_from_cache,
    get_all_fields,
    is_redis_available,
    reset_circuit_breaker,
    get_circuit_breaker_status,
    # Key model (history/analysis)
    get_key,
    set_key,
    delete_key,
    scan_keys,
    # ZSET
    zadd_key,
    zcard_key,
    zrange_key,
    # SET
    sadd_key,
    smembers_key,
    # System
    dbsize,
)

# Fallback for clean_team_name if search_module is unavailable
try:
    from search_module import clean_team_name as _base_clean_team_name
except ImportError:
    _base_clean_team_name = None

SCHEMA_VERSION = "v710"
MSK_TIMEZONE = timezone(timedelta(hours=3))
MATCH_PREFIX = "match:"
HISTORY_PREFIX = "history:match:"
ANALYSIS_PREFIX = "analysis:"
INDEX_FIELD = "match:index"
MAX_CAS_RETRIES = 3
MAX_ODDS_SNAPSHOTS = 10

MATCH_FINISH_BUFFER_HOURS = 2
INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7

_pending_metrics: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# TEAM_ALIASES — синхронизированы с football_data_to_redis.py v6.2
# ---------------------------------------------------------------------------
TEAM_ALIASES = {
    # Premier League
    "manchester united": "manchester united",
    "man united": "manchester united",
    "man utd": "manchester united",
    "manchester city": "manchester city",
    "man city": "manchester city",
    "tottenham hotspur": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "spurs": "tottenham hotspur",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers": "wolverhampton wanderers",
    "wolves": "wolverhampton wanderers",
    "newcastle united": "newcastle united",
    "newcastle": "newcastle united",
    "west ham united": "west ham united",
    "west ham": "west ham united",
    "nottm forest": "nottingham forest",
    "nottingham forest": "nottingham forest",
    "nott'm forest": "nottingham forest",
    "nottingham": "nottingham forest",
    "brighton hove albion": "brighton hove albion",
    "brighton & hove albion": "brighton hove albion",
    "brighton": "brighton hove albion",
    "leicester city": "leicester city",
    "leicester": "leicester city",
    "norwich city": "norwich city",
    "norwich": "norwich city",
    # La Liga
    "atletico madrid": "atletico madrid",
    "atletico": "atletico madrid",
    "athletico madrid": "atletico madrid",
    "real betis": "real betis",
    "betis": "real betis",
    "rayo vallecano": "rayo vallecano",
    # Serie A
    "internazionale": "internazionale",
    "inter milan": "internazionale",
    "inter": "internazionale",
    "ac milan": "ac milan",
    "milan": "ac milan",
    "hellas verona": "hellas verona",
    "verona": "hellas verona",
    # Bundesliga
    "bayern munich": "bayern munich",
    "bayern": "bayern munich",
    "borussia dortmund": "borussia dortmund",
    "dortmund": "borussia dortmund",
    "bayer leverkusen": "bayer leverkusen",
    "leverkusen": "bayer leverkusen",
    "borussia monchengladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    # Ligue 1
    "paris saint germain": "paris saint-germain",
    "paris saint-germain": "paris saint-germain",
    "psg": "paris saint-germain",
    "saint etienne": "saint-etienne",
    "st etienne": "saint-etienne",
    # Scottish
    "st mirren fc": "st mirren",
    "st. mirren": "st mirren",
    "st mirren": "st mirren",
    "celtic fc": "celtic",
    "celtic": "celtic",
    "rangers fc": "rangers",
    "rangers": "rangers",
    # Other
    "sporting cp": "sporting cp",
    "sporting lisbon": "sporting cp",
    "sporting": "sporting cp",
    "club brugge": "club brugge",
    "brugge": "club brugge",
    "fc bayern": "bayern munich",
    "real sociedad de futbol": "real sociedad",
    "athletic club": "athletic bilbao",
    "athletic bilbao": "athletic bilbao",
    "vfl wolfsburg": "vfl wolfsburg",
    "wolfsburg": "vfl wolfsburg",
    "sc freiburg": "sc freiburg",
    "freiburg": "sc freiburg",
    "vfb stuttgart": "vfb stuttgart",
    "stuttgart": "vfb stuttgart",
    "1 fc union berlin": "1 fc union berlin",
    "1. fc union berlin": "1 fc union berlin",
    "union berlin": "1 fc union berlin",
    "1 fc koln": "1 fc koln",
    "1. fc koln": "1 fc koln",
    "koln": "1 fc koln",
    "fc augsburg": "fc augsburg",
    "augsburg": "fc augsburg",
    "vfl bochum": "vfl bochum",
    "bochum": "vfl bochum",
    "sv werder bremen": "sv werder bremen",
    "werder bremen": "sv werder bremen",
    "bremen": "sv werder bremen",
    "tsg hoffenheim": "tsg hoffenheim",
    "hoffenheim": "tsg hoffenheim",
    "fc schalke 04": "fc schalke 04",
    "schalke": "fc schalke 04",
    "hertha bsc": "hertha bsc",
    "hertha": "hertha bsc",
    "hamburger sv": "hamburger sv",
    "hamburg": "hamburger sv",
}


def clean_team_name(raw: str) -> str:
    """Очистка имени команды + разрешение алиасов."""
    if not raw:
        return ""
    raw_lower = raw.strip().lower()

    # 1. Прямой алиас
    if raw_lower in TEAM_ALIASES:
        return TEAM_ALIASES[raw_lower]

    # 2. Базовая очистка из search_module (с fallback)
    if _base_clean_team_name is not None:
        cleaned = _base_clean_team_name(raw)
        if cleaned is None:
            cleaned = ""
        cleaned_lower = cleaned.strip().lower()
        if cleaned_lower in TEAM_ALIASES:
            return TEAM_ALIASES[cleaned_lower]
        return cleaned_lower if cleaned_lower else raw_lower
    else:
        # Fallback: простая очистка
        return raw_lower


# ---------------------------------------------------------------------------
# UPSTREAM_MAP
# ---------------------------------------------------------------------------
UPSTREAM_MAP = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "propline": "pinnacle",
    "bzzoiro": "opta",
    "football_data": "multi_bookmaker",
    "unknown": "unknown",
}


def detect_upstream(source: str) -> str:
    return UPSTREAM_MAP.get(source, "unknown")


def _resolve_upstream(source: str) -> str:
    """Backward compat alias for detect_upstream."""
    return detect_upstream(source)


# ---------------------------------------------------------------------------
# Временные утилиты
# ---------------------------------------------------------------------------
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _now_msk() -> str:
    return datetime.now(MSK_TIMEZONE).isoformat()

def now_msk() -> str:
    """Backward compat — возвращает MSK ISO."""
    return _now_msk()

def now_utc() -> str:
    """Возвращает UTC ISO."""
    return _now_utc()


# ---------------------------------------------------------------------------
# Canonical ID
# ---------------------------------------------------------------------------
def _make_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    home_c = clean_team_name(home_team).replace(" ", "_")
    away_c = clean_team_name(away_team).replace(" ", "_")
    if not date_utc:
        date_str = "nodate"
    else:
        try:
            dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y%m%d")
        except (ValueError, TypeError):
            date_str = "nodate"
    return f"{home_c}__{away_c}__{date_str}"


def build_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    """Публичная обёртка для коллекторов."""
    return _make_canonical_id(home_team, away_team, date_utc)


def _make_field_name(canonical_id: str) -> str:
    return f"{MATCH_PREFIX}{canonical_id}"


def _is_future_match(date_utc: str) -> bool:
    if not date_utc:
        return True
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def is_future_match(date_utc: str) -> bool:
    return _is_future_match(date_utc)


def normalize_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        if isinstance(date_str, (int, float)):
            dt = datetime.fromtimestamp(float(date_str), tz=timezone.utc)
        elif isinstance(date_str, str):
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return ""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Системные метрики
# ---------------------------------------------------------------------------
def _update_health_metric(key: str, value: Any) -> None:
    global _pending_metrics
    if isinstance(value, (int, float)):
        _pending_metrics[key] = value
    elif isinstance(value, dict):
        _pending_metrics.update(value)
    else:
        _pending_metrics[key] = str(value)


def _flush_health_metrics() -> None:
    global _pending_metrics
    if not _pending_metrics:
        return
    health = get_key("system:health") or {}
    if not isinstance(health, dict):
        health = {}
    health.update(_pending_metrics)
    set_key("system:health", health)
    _pending_metrics = {}


def _increment_metric(key: str) -> None:
    global _pending_metrics
    _pending_metrics[key] = _pending_metrics.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------
def _max_per_selection(prices: List[dict]) -> dict:
    result = {}
    for key in ("home", "draw", "away"):
        values = []
        for p in prices:
            val = p.get(key)
            if val is None or val == "-" or val == "":
                continue
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                continue
        result[key] = str(max(values)) if values else ""
    return result


def _is_numeric(val) -> bool:
    if val is None or val == "-" or val == "":
        return False
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def _normalize_incoming_odds(raw_odds: Any) -> Optional[dict]:
    """Нормализация любого входа 1x2 → плоский {home, draw, away}."""
    if raw_odds is None:
        return None
    if isinstance(raw_odds, dict):
        if "home" in raw_odds and "draw" in raw_odds and "away" in raw_odds:
            return {"home": raw_odds["home"], "draw": raw_odds["draw"], "away": raw_odds["away"]}
        if "1x2" in raw_odds:
            inner = raw_odds["1x2"]
            if isinstance(inner, dict):
                if "current" in inner:
                    return inner["current"]
                if "home" in inner:
                    return inner
    return None


def _extract_opening_closing(raw_odds: Any) -> dict:
    """Извлекает opening/closing из входящего odds-блока для football_data."""
    result = {}
    if raw_odds is None or not isinstance(raw_odds, dict):
        return result
    if "1x2" in raw_odds:
        inner = raw_odds["1x2"]
        if isinstance(inner, dict):
            if "opening" in inner:
                result["opening_1x2"] = inner["opening"]
            if "closing" in inner:
                result["closing_1x2"] = inner["closing"]
    if "over_under_25" in raw_odds:
        inner = raw_odds["over_under_25"]
        if isinstance(inner, dict):
            if "opening" in inner:
                result["opening_ou25"] = inner["opening"]
            if "closing" in inner:
                result["closing_ou25"] = inner["closing"]
    if "asian_handicap" in raw_odds:
        inner = raw_odds["asian_handicap"]
        if isinstance(inner, dict):
            if "opening" in inner:
                result["opening_ah"] = inner["opening"]
            if "closing" in inner:
                result["closing_ah"] = inner["closing"]
    return result


def _normalize_incoming_full_odds(raw_odds: Any) -> Optional[dict]:
    """Нормализация полного odds-блока: 1x2 + O/U 2.5 + AH."""
    if raw_odds is None or not isinstance(raw_odds, dict):
        return None

    result = {}

    # 1x2
    flat_1x2 = _normalize_incoming_odds(raw_odds)
    if flat_1x2:
        result["1x2"] = flat_1x2

    # O/U 2.5
    ou25 = raw_odds.get("over_under_25")
    if ou25 is None:
        ou25 = raw_odds.get("ou25")
    if ou25 is not None and isinstance(ou25, dict):
        result["over_under_25"] = ou25

    # Asian Handicap
    ah = raw_odds.get("asian_handicap")
    if ah is None:
        ah = raw_odds.get("ah")
    if ah is not None and isinstance(ah, dict):
        result["asian_handicap"] = ah

    return result if result else None


def _count_independent(sources: List[dict]) -> int:
    """Подсчёт уникальных upstream в sources[]."""
    upstreams = set()
    for s in sources:
        if isinstance(s, dict):
            up = s.get("upstream") or s.get("source") or "unknown"
            upstreams.add(up)
    return len(upstreams)


def _build_1x2(price: dict, source: str, upstream: str, ts: str) -> dict:
    """Создание начального 1x2 блока."""
    return {
        "current": price,
        "opening": price,
        "best": price,
        "sources": [
            {
                "source": source,
                "upstream": upstream,
                "price": price,
                "timestamp": ts,
                "type": "opening",
            }
        ],
        "snapshots": [
            {
                "price": price,
                "timestamp": ts,
                "source": source,
            }
        ],
    }


def _merge_odds(existing: dict, new_odds: dict, source: str, upstream: str, ts: str) -> dict:
    """Слияние odds: сохраняет 1x2, O/U 2.5, AH."""
    new_full = _normalize_incoming_full_odds(new_odds)
    if new_full is None:
        flat = _normalize_incoming_odds(new_odds)
        if flat is None:
            return copy.deepcopy(existing) if existing else {}
        new_full = {"1x2": flat}

    result = copy.deepcopy(existing) if existing else {}

    # --- 1x2 ---
    new_1x2 = new_full.get("1x2")
    if new_1x2:
        existing_1x2 = result.get("1x2", {})
        if not existing_1x2:
            result["1x2"] = _build_1x2(copy.deepcopy(new_1x2), source, upstream, ts)
        else:
            result["1x2"] = _merge_1x2(existing_1x2, copy.deepcopy(new_1x2), source, upstream, ts)

    # --- O/U 2.5 ---
    new_ou25 = new_full.get("over_under_25")
    if new_ou25:
        new_ou25 = copy.deepcopy(new_ou25)
        existing_ou = result.get("over_under_25", {})
        if not existing_ou:
            result["over_under_25"] = {
                "current": new_ou25,
                "sources": [{"source": source, "upstream": upstream, "price": new_ou25, "timestamp": ts}],
            }
        else:
            sources = list(existing_ou.get("sources", []))
            sources.append({"source": source, "upstream": upstream, "price": new_ou25, "timestamp": ts})
            existing_ou["sources"] = sources[-MAX_ODDS_SNAPSHOTS:]
            existing_ou["current"] = new_ou25
            result["over_under_25"] = existing_ou

    # --- Asian Handicap ---
    new_ah = new_full.get("asian_handicap")
    if new_ah:
        new_ah = copy.deepcopy(new_ah)
        existing_ah = result.get("asian_handicap", {})
        if not existing_ah:
            result["asian_handicap"] = {
                "current": new_ah,
                "sources": [{"source": source, "upstream": upstream, "price": new_ah, "timestamp": ts}],
            }
        else:
            sources = list(existing_ah.get("sources", []))
            sources.append({"source": source, "upstream": upstream, "price": new_ah, "timestamp": ts})
            existing_ah["sources"] = sources[-MAX_ODDS_SNAPSHOTS:]
            existing_ah["current"] = new_ah
            result["asian_handicap"] = existing_ah

    return result


def _merge_1x2(existing_1x2: dict, new_price: dict, source: str, upstream: str, ts: str) -> dict:
    """Слияние 1x2: накопление в sources[], обновление current/best."""
    result = copy.deepcopy(existing_1x2)

    # Determine type: first source = opening, subsequent = live
    sources = result.get("sources", [])
    otype = "opening" if not sources else "live"

    # Update current
    result["current"] = copy.deepcopy(new_price)

    # Update best (max per selection)
    old_best = result.get("best", {})
    result["best"] = _max_per_selection([old_best, new_price]) if old_best else copy.deepcopy(new_price)

    # Append to sources
    sources = list(sources)
    sources.append({
        "source": source,
        "upstream": upstream,
        "price": copy.deepcopy(new_price),
        "timestamp": ts,
        "type": otype,
    })
    result["sources"] = sources[-MAX_ODDS_SNAPSHOTS:]

    # Append to snapshots
    snapshots = list(result.get("snapshots", []))
    snapshots.append({"price": copy.deepcopy(new_price), "timestamp": ts, "source": source})
    result["snapshots"] = snapshots[-MAX_ODDS_SNAPSHOTS:]

    return result


# ---------------------------------------------------------------------------
# Init match
# ---------------------------------------------------------------------------
def _init_match_object(
    home_team: str, away_team: str, date_utc: str, source: str, upstream: str,
    competition: str = "", country: str = "", league_code: str = "", season: str = "",
) -> dict:
    ts = _now_utc()
    cid = _make_canonical_id(home_team, away_team, date_utc)
    home_c = clean_team_name(home_team)
    away_c = clean_team_name(away_team)
    time_utc = ""
    if date_utc:
        try:
            dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
            time_utc = dt.strftime("%H:%M:%SZ")
        except (ValueError, TypeError):
            pass
    return {
        "canonical_id": cid,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_c,
        "away_clean": away_c,
        "competition": competition,
        "country": country,
        "league_code": league_code,
        "season": season,
        "date_utc": date_utc,
        "time_utc": time_utc,
        "status": "scheduled",
        "score": {},
        "odds": {},
        "stats": {},
        "sources": [],
        "source_ids": {},
        "predictions": {},
        "h2h": {},
        "value_analysis": {},
        "csv_raw": {},
        "flags": {},
        "schema_version": SCHEMA_VERSION,
        "version": 1,
        "created_at": ts,
        "updated_at": ts,
        "source_map": {
            "odds": {
                "source": source,
                "upstream": upstream,
                "timestamp": ts,
                "independent": True,
            },
        },
    }


# ---------------------------------------------------------------------------
# Public: upsert_match
# ---------------------------------------------------------------------------
def upsert_match(
    home_team: str,
    away_team: str,
    date_utc: str,
    source: str,
    *,
    score: Optional[dict] = None,
    odds: Optional[dict] = None,
    stats: Optional[dict] = None,
    extra: Optional[dict] = None,
    source_id: Optional[str] = None,
    predictions: Optional[dict] = None,
    h2h: Optional[dict] = None,
    competition: str = "",
    country: str = "",
    league_code: str = "",
    season: str = "",
) -> str:
    """Создать или обновить матч (live, в хеше). Только предстоящие матчи."""
    # FIX #7: Filter past matches
    if not _is_future_match(date_utc):
        return ""

    upstream = detect_upstream(source)
    cid = _make_canonical_id(home_team, away_team, date_utc)
    field_id = _make_field_name(cid)
    ts = _now_utc()

    existing = get_from_cache(field_id)

    if existing is None:
        match = _init_match_object(
            home_team, away_team, date_utc, source, upstream,
            competition=competition, country=country,
            league_code=league_code, season=season,
        )
    else:
        match = existing
        # Update fields if provided
        if competition and not match.get("competition"):
            match["competition"] = competition
        if country and not match.get("country"):
            match["country"] = country
        if league_code and not match.get("league_code"):
            match["league_code"] = league_code
        if season and not match.get("season"):
            match["season"] = season

    # Score
    if score:
        match["score"] = score
        if score.get("home") is not None and score.get("away") is not None:
            match["status"] = "completed"

    # Odds
    if odds:
        match["odds"] = _merge_odds(match.get("odds", {}), odds, source, upstream, ts)

    # Stats
    if stats:
        match["stats"] = stats

    # Extra
    if extra:
        if not match.get("extra"):
            match["extra"] = {}
        match["extra"].update(extra)

    # Source IDs
    if source_id:
        if not match.get("source_ids"):
            match["source_ids"] = {}
        match["source_ids"][source] = source_id

    # Predictions
    if predictions:
        match["predictions"] = predictions

    # H2H
    if h2h:
        match["h2h"] = h2h

    # Sources list
    sources_list = match.get("sources", [])
    if source not in [s.get("source") for s in sources_list if isinstance(s, dict)]:
        sources_list.append({"source": source, "upstream": upstream, "timestamp": ts})
        match["sources"] = sources_list

    match["updated_at"] = ts

    save_to_cache(field_id, match)
    _update_index(cid, date_utc)
    return cid


# ---------------------------------------------------------------------------
# Public: patch_match (CAS)
# ---------------------------------------------------------------------------
def patch_match(
    canonical_id: str,
    section: Optional[str] = None,
    data: Optional[dict] = None,
    *,
    score: Optional[dict] = None,
    odds: Optional[dict] = None,
    stats: Optional[dict] = None,
    extra: Optional[dict] = None,
    predictions: Optional[dict] = None,
    h2h: Optional[dict] = None,
    value_analysis: Optional[dict] = None,
    upstream: Optional[str] = None,
    source_map: Optional[dict] = None,
    source: str = "unknown",
) -> bool:
    """CAS-обновление матча. Поддерживает positional (section, data) и keyword API."""
    # Support positional API: patch_match(cid, "odds", {...}, source=, upstream=)
    if section is not None and data is not None:
        if section == "odds":
            odds = data
        elif section == "stats":
            stats = data
        elif section == "score":
            score = data
        elif section == "predictions":
            predictions = data
        elif section == "h2h":
            h2h = data
        elif section == "extra":
            extra = data
        elif section == "value_analysis":
            value_analysis = data
        elif section == "source_map":
            source_map = data

    field_id = _make_field_name(canonical_id)
    ts = _now_utc()

    for attempt in range(MAX_CAS_RETRIES):
        existing = get_from_cache(field_id)
        if existing is None:
            return False

        match = copy.deepcopy(existing)

        if score:
            match["score"] = score
            if score.get("home") is not None and score.get("away") is not None:
                match["status"] = "completed"

        if odds:
            up = upstream or detect_upstream(source)
            match["odds"] = _merge_odds(match.get("odds", {}), odds, source, up, ts)

        if stats:
            match["stats"] = stats

        if extra:
            if not match.get("extra"):
                match["extra"] = {}
            match["extra"].update(extra)

        if predictions:
            match["predictions"] = predictions

        if h2h:
            match["h2h"] = h2h

        if value_analysis:
            match["value_analysis"] = value_analysis

        if source_map:
            if not match.get("source_map"):
                match["source_map"] = {}
            match["source_map"].update(source_map)

        # Ensure required fields
        if "schema_version" not in match:
            match["schema_version"] = SCHEMA_VERSION
        if "value_analysis" not in match:
            match["value_analysis"] = {}
        if "h2h" not in match:
            match["h2h"] = {}

        old_version = match.get("version", 1)
        match["version"] = old_version + 1
        match["updated_at"] = ts

        if save_to_cache(field_id, match):
            return True

        if attempt < MAX_CAS_RETRIES - 1:
            time.sleep(0.5 * (attempt + 1))

    return False


# ---------------------------------------------------------------------------
# Public: get_match / get_all_matches / get_matches_by_date_range
# ---------------------------------------------------------------------------
def get_match(canonical_id: str) -> Optional[dict]:
    field_id = _make_field_name(canonical_id)
    return get_from_cache(field_id)


def get_all_matches() -> Dict[str, dict]:
    """Все матчи — union flat + sharded index."""
    all_fields = get_all_fields()
    matches = {}

    # From flat index
    flat_index = all_fields.get(INDEX_FIELD, {})
    if isinstance(flat_index, dict):
        for cid in flat_index:
            m = all_fields.get(_make_field_name(cid))
            if m and isinstance(m, dict):
                matches[cid] = m

    # From sharded index
    now = datetime.now(timezone.utc)
    for delta_days in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        d = now + timedelta(days=delta_days)
        date_fmt = d.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = all_fields.get(shard_key)
        if isinstance(shard, dict):
            for cid in shard:
                if cid not in matches:
                    m = all_fields.get(_make_field_name(cid))
                    if m and isinstance(m, dict):
                        matches[cid] = m

    # Fallback: scan all match:* fields directly
    if not matches:
        for field_id, value in all_fields.items():
            if field_id.startswith(MATCH_PREFIX) and isinstance(value, dict):
                cid = value.get("canonical_id", field_id[len(MATCH_PREFIX):])
                matches[cid] = value

    return matches


def get_matches_by_date_range(
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, dict]:
    """Чтение через шарды по датам."""
    all_fields = get_all_fields()
    matches = {}

    if start and end:
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            start_dt = datetime.now(timezone.utc) - timedelta(days=7)
            end_dt = datetime.now(timezone.utc) + timedelta(days=7)
    else:
        start_dt = datetime.now(timezone.utc) - timedelta(days=7)
        end_dt = datetime.now(timezone.utc) + timedelta(days=7)

    # Sharded index
    current = start_dt
    while current <= end_dt:
        date_fmt = current.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = all_fields.get(shard_key)
        if isinstance(shard, dict):
            for cid in shard:
                if cid not in matches:
                    m = all_fields.get(_make_field_name(cid))
                    if m and isinstance(m, dict):
                        matches[cid] = m
        current += timedelta(days=1)

    # Fallback to flat index
    if not matches:
        flat_index = all_fields.get(INDEX_FIELD, {})
        if isinstance(flat_index, dict):
            for cid in flat_index:
                m = all_fields.get(_make_field_name(cid))
                if m and isinstance(m, dict):
                    matches[cid] = m

    return matches


def get_matches_count() -> int:
    """Количество матчей — union flat + sharded (с дедупликацией)."""
    all_fields = get_all_fields()
    seen = set()

    flat_index = all_fields.get(INDEX_FIELD, {})
    if isinstance(flat_index, dict):
        seen.update(flat_index.keys())

    now = datetime.now(timezone.utc)
    for delta_days in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        d = now + timedelta(days=delta_days)
        date_fmt = d.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = all_fields.get(shard_key)
        if isinstance(shard, dict):
            seen.update(shard.keys())

    return len(seen)


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------
def _update_index(canonical_id: str, date_utc: str) -> None:
    """Обновление плоского и шардированного индекса."""
    # Flat index
    flat = get_from_cache(INDEX_FIELD) or {}
    if not isinstance(flat, dict):
        flat = {}
    flat[canonical_id] = _now_utc()
    save_to_cache(INDEX_FIELD, flat)

    # Sharded index
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        date_fmt = dt.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = get_from_cache(shard_key) or {}
        if not isinstance(shard, dict):
            shard = {}
        shard[canonical_id] = _now_utc()
        save_to_cache(shard_key, shard)
    except (ValueError, TypeError):
        pass


# ---------------------------------------------------------------------------
# History (отдельные ключи)
# ---------------------------------------------------------------------------
def upsert_history_match(payload: dict) -> bool:
    """Записать history-матч в отдельный ключ (не в хеш). С enrichment + indexes."""
    cid = payload.get("canonical_id")
    if not cid:
        return False

    # Enrichment: ensure required fields
    home_team = payload.get("home_team", "")
    away_team = payload.get("away_team", "")
    if "home_clean" not in payload or not payload["home_clean"]:
        payload["home_clean"] = clean_team_name(home_team)
    if "away_clean" not in payload or not payload["away_clean"]:
        payload["away_clean"] = clean_team_name(away_team)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("version", 1)
    payload.setdefault("value_analysis", {})
    payload.setdefault("predictions", {})

    key = f"{HISTORY_PREFIX}{cid}"

    # Merge with existing (preserve enrichments from value_engine etc.)
    existing = get_key(key)
    if existing and isinstance(existing, dict):
        for enrich_field in ("value_analysis", "predictions", "analysis"):
            if existing.get(enrich_field) and not payload.get(enrich_field):
                payload[enrich_field] = existing[enrich_field]

    if not set_key(key, payload):
        return False

    # Update indexes
    league_code = payload.get("league_code", "")
    date_utc = payload.get("date_utc", "")
    if league_code or date_utc:
        update_history_indexes(cid, league_code, date_utc, home_team, away_team)

    return True


def get_history_match(canonical_id: str) -> Optional[dict]:
    """Прочитать history-матч из отдельного ключа."""
    return get_key(f"{HISTORY_PREFIX}{canonical_id}")


def get_history_count() -> int:
    """Количество history-матчей."""
    keys = scan_keys(f"{HISTORY_PREFIX}*")
    return len(keys)


def get_history_by_league(league_code: str, start: int = 0, end: int = -1) -> List[str]:
    """Список canonical_id по лиге (из ZSET)."""
    return zrange_key(f"history:league:{league_code}", start, end)


def get_history_league_count(league_code: str) -> int:
    """Количество матчей по лиге (из ZCARD)."""
    return zcard_key(f"history:league:{league_code}")


def update_history_indexes(canonical_id: str, league_code: str, date_utc: str,
                           home_team: str, away_team: str) -> None:
    """Обновление индексов history:league:* (ZSET) и history:team:* (SET)."""
    # ZSET league index (only if league_code is non-empty)
    if league_code:
        try:
            dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
            score = dt.timestamp()
        except (ValueError, TypeError):
            score = 0
        zadd_key(f"history:league:{league_code}", score, canonical_id)

    # SET team indexes
    if home_team:
        home_clean = clean_team_name(home_team).replace(" ", "_")
        sadd_key(f"history:team:{home_clean}", canonical_id)
    if away_team:
        away_clean = clean_team_name(away_team).replace(" ", "_")
        sadd_key(f"history:team:{away_clean}", canonical_id)


def migrate_to_history(canonical_id: str) -> bool:
    """Перенос live-матча в history (при cleanup)."""
    field_id = _make_field_name(canonical_id)
    match = get_from_cache(field_id)
    if match is None:
        return False

    # Strip live-only fields
    payload = copy.deepcopy(match)
    for live_field in ("created_at", "updated_at"):
        payload.pop(live_field, None)

    # Ensure home_clean/away_clean
    if not payload.get("home_clean"):
        payload["home_clean"] = clean_team_name(payload.get("home_team", ""))
    if not payload.get("away_clean"):
        payload["away_clean"] = clean_team_name(payload.get("away_team", ""))

    # Записать в отдельный ключ
    key = f"{HISTORY_PREFIX}{canonical_id}"
    if not set_key(key, payload):
        return False

    # Удалить из хеша
    delete_from_cache(field_id)

    # Обновить индексы
    league_code = payload.get("league_code", "")
    date_utc = payload.get("date_utc", "")
    home_team = payload.get("home_team", "")
    away_team = payload.get("away_team", "")
    update_history_indexes(canonical_id, league_code, date_utc, home_team, away_team)

    return True


# ---------------------------------------------------------------------------
# Analysis (отдельные ключи, без двойной обёртки)
# ---------------------------------------------------------------------------
def save_analysis(canonical_id: str, payload: dict) -> bool:
    """Сохранить анализ в отдельный ключ (raw JSON, без конверта)."""
    key = f"{ANALYSIS_PREFIX}{canonical_id}"
    existing = get_key(key)
    analysis_version = 1
    if existing and isinstance(existing, dict):
        analysis_version = existing.get("analysis_version", 0) + 1
    payload["analysis_version"] = analysis_version
    payload["saved_at"] = _now_utc()
    return set_key(key, payload)


def get_analysis(canonical_id: str) -> Optional[dict]:
    """Прочитать анализ из отдельного ключа."""
    return get_key(f"{ANALYSIS_PREFIX}{canonical_id}")


# ---------------------------------------------------------------------------
# Search results (без двойной обёртки)
# ---------------------------------------------------------------------------
def save_search_results(results: list, query: str = "") -> bool:
    """Сохранить результаты поиска в отдельный ключ."""
    payload = {
        "data": results,
        "query": query,
        "timestamp": _now_utc(),
    }
    return set_key("search:results:latest", payload)


def get_search_results() -> Optional[dict]:
    """Прочитать последние результаты поиска."""
    return get_key("search:results:latest")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
def save_meta(collector: str, **kwargs) -> None:
    if not collector:
        return
    key = f"{collector}:meta"
    meta = get_key(key) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["last_run"] = _now_utc()
    meta.update(kwargs)
    set_key(key, meta)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_expired() -> int:
    """Удаление завершённых матчей старше buffer. Возвращает количество удалённых."""
    all_fields = get_all_fields()
    now = datetime.now(timezone.utc)
    deleted = 0

    for field_id, value in all_fields.items():
        if not field_id.startswith(MATCH_PREFIX):
            continue
        if not isinstance(value, dict):
            continue

        status = value.get("status", "")
        date_utc = value.get("date_utc", "")
        should_delete = False

        # Check 1: completed + buffer passed
        if status == "completed" and date_utc:
            try:
                dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() > MATCH_FINISH_BUFFER_HOURS * 3600:
                    should_delete = True
            except (ValueError, TypeError):
                pass

        # Check 2: date in the past + buffer passed (regardless of status)
        if not should_delete and date_utc:
            try:
                dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() > MATCH_FINISH_BUFFER_HOURS * 3600:
                    should_delete = True
            except (ValueError, TypeError):
                pass

        if should_delete:
            cid = value.get("canonical_id", field_id[len(MATCH_PREFIX):])
            if migrate_to_history(cid):
                deleted += 1
                # Clean up index entries
                _remove_from_index(cid, date_utc)

    if deleted > 0:
        _update_health_metric("last_cleanup_count", deleted)
        _update_health_metric("last_cleanup_at", _now_utc())
        _flush_health_metrics()

    return deleted


def _remove_from_index(canonical_id: str, date_utc: str) -> None:
    """Удаление canonical_id из flat и sharded индексов."""
    flat = get_from_cache(INDEX_FIELD) or {}
    if isinstance(flat, dict):
        flat.pop(canonical_id, None)
        save_to_cache(INDEX_FIELD, flat)

    if date_utc:
        try:
            dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
            date_fmt = dt.strftime("%Y%m%d")
            shard_key = f"match:index:{date_fmt}"
            shard = get_from_cache(shard_key) or {}
            if isinstance(shard, dict):
                shard.pop(canonical_id, None)
                save_to_cache(shard_key, shard)
        except (ValueError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Find by source ID
# ---------------------------------------------------------------------------
def find_match_by_source_id(source: str, source_id: str) -> Optional[dict]:
    """Поиск матча по source_id. Сначала через SCAN, fallback — полный скан."""
    # Fast path: scan history keys
    history_keys = scan_keys(f"{HISTORY_PREFIX}*", count=500)
    for key in history_keys:
        data = get_key(key)
        if data and isinstance(data, dict):
            source_ids = data.get("source_ids", {})
            if isinstance(source_ids, dict) and source_ids.get(source) == source_id:
                return data

    # Fallback: scan live matches in hash
    all_fields = get_all_fields()
    for field_id, value in all_fields.items():
        if not field_id.startswith(MATCH_PREFIX):
            continue
        if not isinstance(value, dict):
            continue
        source_ids = value.get("source_ids", {})
        if isinstance(source_ids, dict) and source_ids.get(source) == source_id:
            return value

    return None


# ---------------------------------------------------------------------------
# Batch upsert
# ---------------------------------------------------------------------------
def batch_upsert_matches(matches: List[dict], source: str = "unknown") -> dict:
    """Массовое создание/обновление матчей. Возвращает статистику."""
    result = {"total": len(matches), "created": 0, "updated": 0, "skipped_past": 0, "errors": 0}
    for m in matches:
        try:
            date_utc = m.get("date_utc", "")
            # Check past match
            if date_utc and not _is_future_match(date_utc):
                result["skipped_past"] += 1
                continue

            cid = upsert_match(
                home_team=m.get("home_team", ""),
                away_team=m.get("away_team", ""),
                date_utc=date_utc,
                source=source,
                score=m.get("score"),
                odds=m.get("odds"),
                stats=m.get("stats"),
                extra=m.get("extra"),
                source_id=m.get("source_id"),
                predictions=m.get("predictions"),
                h2h=m.get("h2h"),
                competition=m.get("competition", ""),
                country=m.get("country", ""),
                league_code=m.get("league_code", ""),
                season=m.get("season", ""),
            )
            if cid:
                # Check if it was created or updated
                field_id = _make_field_name(cid)
                existing = get_from_cache(field_id)
                if existing and existing.get("version", 1) > 1:
                    result["updated"] += 1
                else:
                    result["created"] += 1
            else:
                result["skipped_past"] += 1
        except Exception as e:
            print(f"[HUB] batch_upsert error: {e}")
            result["errors"] += 1
    return result


# ---------------------------------------------------------------------------
# Odds accessors
# ---------------------------------------------------------------------------
def get_current_odds(canonical_id: str) -> Optional[dict]:
    """Текущие odds для матча (обёртка обратной совместимости)."""
    match = get_match(canonical_id)
    if not match:
        return None
    odds = match.get("odds", {})
    return odds.get("1x2", {}).get("current") if isinstance(odds, dict) else None


def get_all_odds(canonical_id: str) -> Optional[dict]:
    """Полные odds-данные одним вызовом."""
    match = get_match(canonical_id)
    if not match:
        return None
    return match.get("odds", {})


def get_odds_metadata(canonical_id: str) -> dict:
    """Метаданные odds: verification, sources, movement."""
    match = get_match(canonical_id)
    if not match:
        return {}
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return {}

    result = {}
    odds_1x2 = odds.get("1x2", {})
    if isinstance(odds_1x2, dict):
        sources = odds_1x2.get("sources", [])
        result["source_count"] = len(sources)
        result["independent_sources"] = _count_independent(sources)
        result["snapshots"] = len(odds_1x2.get("snapshots", []))
        result["has_opening"] = any(s.get("type") == "opening" for s in sources)
        result["has_closing"] = any(s.get("type") == "closing" for s in sources)
        result["has_live"] = any(s.get("type") == "live" for s in sources)

    result["has_ou25"] = "over_under_25" in odds
    result["has_ah"] = "asian_handicap" in odds

    return result


# ---------------------------------------------------------------------------
# Find by teams / fuzzy
# ---------------------------------------------------------------------------
def get_match_by_teams(home_team: str, away_team: str, date_utc: str) -> Optional[dict]:
    """Найти матч по командам и дате."""
    cid = _make_canonical_id(home_team, away_team, date_utc)
    return get_match(cid)


def patch_match_by_teams(
    home_team: str, away_team: str, date_utc: str,
    section: Optional[str] = None, data: Optional[dict] = None,
    **kwargs,
) -> bool:
    """Патч матча по командам и дате (обёртка над patch_match)."""
    cid = _make_canonical_id(home_team, away_team, date_utc)
    return patch_match(cid, section, data, **kwargs)


def find_match_fuzzy(home_team: str, away_team: str) -> Optional[dict]:
    """Нечёткий поиск матча по командам (без даты)."""
    home_c = clean_team_name(home_team)
    away_c = clean_team_name(away_team)
    all_matches = get_all_matches()
    for cid, match in all_matches.items():
        if not isinstance(match, dict):
            continue
        m_home = match.get("home_clean", "")
        m_away = match.get("away_clean", "")
        if not m_home:
            m_home = clean_team_name(match.get("home_team", ""))
        if not m_away:
            m_away = clean_team_name(match.get("away_team", ""))
        if m_home == home_c and m_away == away_c:
            return match
    return None


def get_history_by_team(team_clean: str) -> List[str]:
    """History-матчи команды (из SET)."""
    team_key = clean_team_name(team_clean).replace(" ", "_")
    return list(smembers_key(f"history:team:{team_key}"))


def get_analysis_by_date(date_utc: str) -> List[dict]:
    """Все analysis за указанную дату."""
    try:
        target_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        target_date = target_dt.strftime("%Y%m%d")
    except (ValueError, TypeError):
        return []

    results = []
    analysis_keys = scan_keys(f"{ANALYSIS_PREFIX}*", count=500)
    for key in analysis_keys:
        data = get_key(key)
        if not data or not isinstance(data, dict):
            continue
        saved_at = data.get("saved_at", "")
        if saved_at and target_date in saved_at:
            results.append(data)
    return results


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
def run_initialization() -> dict:
    """Инициализация: health-check + cleanup + metrics."""
    result = {
        "redis_available": False,
        "cleanup_deleted": 0,
        "matches_count": 0,
        "history_count": 0,
        "circuit_breaker": get_circuit_breaker_status(),
    }

    if not is_redis_available():
        print("[HUB] Redis недоступен")
        return result

    result["redis_available"] = True
    reset_circuit_breaker()

    # Cleanup expired matches
    try:
        deleted = cleanup_expired()
        result["cleanup_deleted"] = deleted
        print(f"[HUB] Cleanup: удалено {deleted} матчей")
    except Exception as e:
        print(f"[HUB] Cleanup error: {e}")

    # Count matches
    try:
        result["matches_count"] = get_matches_count()
    except Exception as e:
        print(f"[HUB] get_matches_count error: {e}")

    # Count history
    try:
        result["history_count"] = get_history_count()
    except Exception as e:
        print(f"[HUB] get_history_count error: {e}")

    _flush_health_metrics()
    print(f"[HUB] Init: redis={'OK' if result['redis_available'] else 'FAIL'}, "
          f"matches={result['matches_count']}, history={result['history_count']}")

    return result


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
def migrate_schema() -> int:
    """Миграция старых полей к v710."""
    all_fields = get_all_fields()
    migrated = 0

    for field_id, value in all_fields.items():
        if not field_id.startswith(MATCH_PREFIX):
            continue
        if not isinstance(value, dict):
            continue

        changed = False

        # Добавить schema_version
        if "schema_version" not in value:
            value["schema_version"] = SCHEMA_VERSION
            changed = True

        # Добавить value_analysis
        if "value_analysis" not in value:
            value["value_analysis"] = {}
            changed = True

        # Обновить старый формат odds
        odds = value.get("odds", {})
        if isinstance(odds, dict) and "home" in odds and "1x2" not in odds:
            value["odds"] = {"1x2": _build_1x2(odds, "migrated", "unknown", _now_utc())}
            changed = True

        if changed:
            save_to_cache(field_id, value)
            migrated += 1

    if migrated > 0:
        print(f"[HUB] Migrated {migrated} matches to {SCHEMA_VERSION}")

    return migrated
