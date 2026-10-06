#!/usr/bin/env python3
"""
Football-Data Collector v7.0 (CSV) — ALL columns, GatekeeperAI v710
Источник: football-data.co.uk
Сезоны: --seasons "2425,2526,2627"
Лиги:   --leagues "E0,E1,SP1"

v7.0 (Phase 2):
  - upsert_match() через хаб (не прямой SET) — match hub envelope
  - source/sources в payload — хаб видит football_data
  - idempotency_key — защита от дублей при повторном CI
  - team_registry.clean_team_name — единый реестр (257+ алиасов)
  - team_registry.build_canonical_id — единый canonical_id (257+ алиасов)
  - gatekeeper_config.now_msk — единое MSK-время
  - flush_meta после каждой лиги (не в конце) — crash-safe
  - run_id из хаба — трассировка

v6.3 (предыдущая):
  - SET history:match:* (не HSET) — match hub get_key
  - canonical_id: .replace(" ", "_") — match hub format
  - Team SET keys: .replace(" ", "_") — match hub update_history_indexes
  - save_meta: SET football_data:meta (не HSET) — match diagnostics get_key
  - TEAM_ALIASES: ~130 алиасов — fallback
  - download_csv: 3 попытки (2 retry) с backoff
  - safe_float: return float (FIX-1)
  - build_ou25: убран мёртвый параметр suffix
  - Graceful shutdown через is_shutdown_requested()

Schema: v710
CSV:    120 колонок (opening + closing odds, O/U 2.5, Asian Handicap)
Stats:  12 метрик + _source + _updated_at
Odds:   1x2 (opening+closing per-bookmaker), O/U 2.5, Asian Handicap
Raw:    ALL CSV columns stored in csv_raw{}
Transport: gatekeeper_hub.upsert_match (единый транспортный слой через хаб)
"""
import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Единый транспортный слой — хаб
try:
    from gatekeeper_hub import (
        upsert_match,
        upsert_history_match,
        is_shutdown_requested,
        run_initialization,
        UPSTREAM_MAP,
    )
    _HUB_AVAILABLE = True
except ImportError:
    _HUB_AVAILABLE = False
    UPSTREAM_MAP = {}

try:
    from gatekeeper_hub import register_module, log_event
except ImportError:
    def register_module(name, **kwargs):
        def deco(func):
            return func
        return deco
    def log_event(source, level, message, **kwargs):
        pass

    def upsert_match(payload, source=None, idempotency_key=None, dry_run=False):
        """Fallback: прямой SET через redis_hub PipelineBatch."""
        from redis_hub import PipelineBatch
        if not hasattr(upsert_match, "_batch"):
            upsert_match._batch = PipelineBatch(dry_run=dry_run, max_batch=50, batch_delay=0.15)
        key = f"history:match:{payload['canonical_id']}"
        upsert_match._batch.add("SET", key, json.dumps(payload, ensure_ascii=False))
        # ZADD league index
        try:
            dt = datetime.fromisoformat(payload["date_utc"].replace("Z", "+00:00"))
            score = dt.timestamp()
        except (ValueError, TypeError):
            score = 0
        league_code = payload.get("league_code", "")
        if league_code:
            upsert_match._batch.add("ZADD", f"history:league:{league_code}",
                                    str(score), payload["canonical_id"])
        # SADD team indexes
        home_clean = payload.get("home_clean", "").replace(" ", "_")
        away_clean = payload.get("away_clean", "").replace(" ", "_")
        if home_clean:
            upsert_match._batch.add("SADD", f"history:team:{home_clean}",
                                   payload["canonical_id"])
        if away_clean:
            upsert_match._batch.add("SADD", f"history:team:{away_clean}",
                                   payload["canonical_id"])
        upsert_match._batch.flush()
        return True

    def is_shutdown_requested():
        return False

    def run_initialization(collector=None):
        from redis_hub import is_redis_available
        return {"redis_available": is_redis_available(), "run_id": "fallback"}

# Единый реестр команд
try:
    from team_registry import clean_team_name as _registry_normalize
    from team_registry import build_canonical_id as _registry_build_cid
    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False

# Единое MSK-время
try:
    from gatekeeper_config import now_msk as _now_msk
    _NOW_MSK_AVAILABLE = True
except ImportError:
    _NOW_MSK_AVAILABLE = False

# redis_hub для fallback и is_redis_available
try:
    from redis_hub import PipelineBatch, is_redis_available
    _PIPELINE_BATCH_AVAILABLE = True
except ImportError:
    from redis_hub import is_redis_available
    _PIPELINE_BATCH_AVAILABLE = False

    class PipelineBatch:
        """Fallback: simple batch using redis_hub.set_key."""
        def __init__(self, dry_run=False, max_batch=50, batch_delay=0.15):
            self.dry_run = dry_run
            self._ops = []
        def add(self, *args):
            self._ops.append(args)
        def flush(self):
            from redis_hub import set_key as _sk
            if self.dry_run:
                self._ops.clear()
                return
            for op in self._ops:
                if op[0] == "SET":
                    _sk(op[1], op[2])
            self._ops.clear()

# ============================================================================
# CONFIG
# ============================================================================

VERSION = "8.11-patched"
__version__ = "8.11-patched"

# FIX-3: Убран logging.basicConfig
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

__all__ = [
    "FootballDataCollector",
    "PipelineBatch",
    "collect_and_process",
    "build_payload",
    "build_canonical_id",
    "clean_team_name",
    "__version__",
]
SCHEMA_VERSION = "v710"
SOURCE_NAME = "football_data"

BASE_URL = "https://www.football-data.co.uk/mmz4281"

LEAGUES = {
    "E0":  {"name": "Premier League",       "country": "England",    "division": 1},
    "E1":  {"name": "Championship",          "country": "England",    "division": 2},
    "E2":  {"name": "League One",            "country": "England",    "division": 3},
    "E3":  {"name": "League Two",           "country": "England",    "division": 4},
    "EC":  {"name": "National League",      "country": "England",    "division": 5},
    "SC0": {"name": "Scottish Premiership",  "country": "Scotland",   "division": 1},
    "SC1": {"name": "Scottish Championship", "country": "Scotland",  "division": 2},
    "SC2": {"name": "Scottish League One",   "country": "Scotland",   "division": 3},
    "SC3": {"name": "Scottish League Two",   "country": "Scotland",   "division": 4},
    "D1":  {"name": "Bundesliga",            "country": "Germany",     "division": 1},
    "D2":  {"name": "2. Bundesliga",         "country": "Germany",     "division": 2},
    "I1":  {"name": "Serie A",               "country": "Italy",      "division": 1},
    "I2":  {"name": "Serie B",               "country": "Italy",      "division": 2},
    "SP1": {"name": "La Liga",               "country": "Spain",      "division": 1},
    "SP2": {"name": "La Liga 2",             "country": "Spain",      "division": 2},
    "F1":  {"name": "Ligue 1",              "country": "France",      "division": 1},
    "F2":  {"name": "Ligue 2",              "country": "France",      "division": 2},
    "N1":  {"name": "Eredivisie",            "country": "Netherlands", "division": 1},
    "B1":  {"name": "Jupiler Pro League",    "country": "Belgium",    "division": 1},
    "P1":  {"name": "Primeira Liga",         "country": "Portugal",   "division": 1},
    "T1":  {"name": "Süper Lig",             "country": "Turkey",     "division": 1},
    "G1":  {"name": "Super League",          "country": "Greece",     "division": 1},
}

# Bookmaker prefixes for 1X2 odds (IW, VC, WH removed — dead in CSV 2425+)
# PS → PP (Pinnacle rebranded prefix), BF → BFD (Betfair 3-letter prefix)
BOOKMAKERS_1X2 = [
    ("B365", "bet365"),
    ("BW",   "bwin"),
    ("PP",   "pinnacle"),
    ("BFD",  "betfair"),
]

# Team name aliases — fallback если team_registry недоступен (130 алиасов)
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
    "brighton": "brighton hove albion",
    "brighton & hove albion": "brighton hove albion",
    "aston villa": "aston villa",
    "crystal palace": "crystal palace",
    "brentford": "brentford",
    "fulham": "fulham",
    "everton": "everton",
    "liverpool": "liverpool",
    "chelsea": "chelsea",
    "arsenal": "arsenal",
    "leicester city": "leicester city",
    "leicester": "leicester city",
    "leeds united": "leeds united",
    "leeds": "leeds united",
    "southampton": "southampton",
    "bournemouth": "bournemouth",
    "burnley": "burnley",
    "luton town": "luton town",
    "luton": "luton town",
    "sheffield united": "sheffield united",
    "sheffield utd": "sheffield united",
    # Championship
    "norwich city": "norwich city",
    "norwich": "norwich city",
    "watford": "watford",
    "middlesbrough": "middlesbrough",
    "birmingham city": "birmingham city",
    "birmingham": "birmingham city",
    "hull city": "hull city",
    "hull": "hull city",
    "stoke city": "stoke city",
    "stoke": "stoke city",
    "swansea city": "swansea city",
    "swansea": "swansea city",
    "cardiff city": "cardiff city",
    "cardiff": "cardiff city",
    "derby county": "derby county",
    "derby": "derby county",
    "preston north end": "preston north end",
    "preston": "preston north end",
    "queens park rangers": "queens park rangers",
    "qpr": "queens park rangers",
    "blackburn rovers": "blackburn rovers",
    "blackburn": "blackburn rovers",
    "bristol city": "bristol city",
    "coventry city": "coventry city",
    "coventry": "coventry city",
    "huddersfield town": "huddersfield town",
    "huddersfield": "huddersfield town",
    "millwall": "millwall",
    "rotherham united": "rotherham united",
    "rotherham": "rotherham united",
    "plymouth argyle": "plymouth argyle",
    "plymouth": "plymouth argyle",
    "ipswich town": "ipswich town",
    "ipswich": "ipswich town",
    "sunderland": "sunderland",
    "west bromwich albion": "west bromwich albion",
    "west brom": "west bromwich albion",
    "wba": "west bromwich albion",
    # Bundesliga
    "bayern munich": "bayern munich",
    "bayern munchen": "bayern munich",
    "borussia dortmund": "borussia dortmund",
    "dortmund": "borussia dortmund",
    "bayer leverkusen": "bayer leverkusen",
    "leverkusen": "bayer leverkusen",
    "rb leipzig": "rb leipzig",
    "eintracht frankfurt": "eintracht frankfurt",
    "frankfurt": "eintracht frankfurt",
    "vfb stuttgart": "vfb stuttgart",
    "stuttgart": "vfb stuttgart",
    "vfl wolfsburg": "vfl wolfsburg",
    "wolfsburg": "vfl wolfsburg",
    "borussia monchengladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "sc freiburg": "sc freiburg",
    "freiburg": "sc freiburg",
    "fc augsburg": "fc augsburg",
    "augsburg": "fc augsburg",
    "hoffenheim": "tsg hoffenheim",
    "tsg hoffenheim": "tsg hoffenheim",
    "union berlin": "union berlin",
    "werder bremen": "werder bremen",
    "bremen": "werder bremen",
    "vfl bochum": "vfl bochum",
    "bochum": "vfl bochum",
    "mainz": "mainz 05",
    "mainz 05": "mainz 05",
    "darmstadt": "sv darmstadt 98",
    "sv darmstadt 98": "sv darmstadt 98",
    # Serie A
    "juventus": "juventus",
    "inter milan": "inter milan",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "ac milan": "ac milan",
    "milan": "ac milan",
    "napoli": "napoli",
    "ssc napoli": "napoli",
    "roma": "roma",
    "as roma": "roma",
    "lazio": "lazio",
    "ss lazio": "lazio",
    "atalanta": "atalanta",
    "fiorentina": "fiorentina",
    "bologna": "bologna",
    "torino": "torino",
    "udinese": "udinese",
    "sassuolo": "sassuolo",
    "monza": "monza",
    "hellas verona": "hellas verona",
    "verona": "hellas verona",
    "cagliari": "cagliari",
    "lecce": "lecce",
    "salernitana": "salernitana",
    "frosinone": "frosinone",
    "genoa": "genoa",
    "empoli": "empoli",
    "como": "como",
    "parma": "parma",
    "venezia": "venezia",
    # La Liga
    "real madrid": "real madrid",
    "barcelona": "barcelona",
    "fc barcelona": "barcelona",
    "atletico madrid": "atletico madrid",
    "athletic bilbao": "athletic bilbao",
    "athletic club": "athletic bilbao",
    "real sociedad": "real sociedad",
    "real betis": "real betis",
    "betis": "real betis",
    "villarreal": "villarreal",
    "valencia": "valencia",
    "sevilla": "sevilla",
    "celta vigo": "celta vigo",
    "celta": "celta vigo",
    "getafe": "getafe",
    "osasuna": "osasuna",
    "rayo vallecano": "rayo vallecano",
    "rayo": "rayo vallecano",
    "mallorca": "mallorca",
    "almeria": "almeria",
    "cadiz": "cadiz",
    "granada": "granada",
    "las palmas": "las palmas",
    "girona": "girona",
    "alaves": "alaves",
    "deportivo alaves": "alaves",
    "espanyol": "espanyol",
    "leganes": "leganes",
    "valladolid": "valladolid",
    # Ligue 1
    "paris saint germain": "paris saint germain",
    "psg": "paris saint germain",
    "paris saint-germain": "paris saint germain",
    "marseille": "marseille",
    "olympique marseille": "marseille",
    "monaco": "monaco",
    "as monaco": "monaco",
    "lyon": "lyon",
    "olympique lyonnais": "lyon",
    "lille": "lille",
    "nice": "nice",
    "rennes": "rennes",
    "lens": "lens",
    "nantes": "nantes",
    "strasbourg": "strasbourg",
    "montpellier": "montpellier",
    "toulouse": "toulouse",
    "brest": "brest",
    "le havre": "le havre",
    "reims": "reims",
    "auxerre": "auxerre",
    "angers": "angers",
    "lorient": "lorient",
    "metz": "metz",
    "clermont": "clermont",
    # Eredivisie
    "ajax": "ajax",
    "psv eindhoven": "psv eindhoven",
    "psv": "psv eindhoven",
    "feyenoord": "feyenoord",
    "az alkmaar": "az alkmaar",
    "az": "az alkmaar",
    "twente": "twente",
    "fc twente": "twente",
    "utrecht": "utrecht",
    "fc utrecht": "utrecht",
    "vitesse": "vitesse",
    "heerenveen": "heerenveen",
    "sc heerenveen": "heerenveen",
    "groningen": "groningen",
    "fc groningen": "groningen",
    "sparta rotterdam": "sparta rotterdam",
    "sparta": "sparta rotterdam",
    "fortuna sittard": "fortuna sittard",
    "nec nijmegen": "nec nijmegen",
    "nec": "nec nijmegen",
    "pec zwolle": "pec zwolle",
    "zwolle": "pec zwolle",
    "willem ii": "willem ii",
    "willem ii tilburg": "willem ii",
    "almere city": "almere city",
    "rkc waalwijk": "rkc waalwijk",
    "rkc": "rkc waalwijk",
    "go ahead eagles": "go ahead eagles",
    "go-ahead eagles": "go ahead eagles",
    "heracles": "heracles",
    "heracles almelo": "heracles",
}

# Upstream из UPSTREAM_MAP хаба, не хардкод
_FD_UPSTREAM = UPSTREAM_MAP.get("football_data", "bet365")
_PINNACLE_UPSTREAM = UPSTREAM_MAP.get("propline", "pinnacle")


# ============================================================================
# HELPERS
# ============================================================================

def now_msk():
    """Единое MSK-время через gatekeeper_config. Fallback: UTC+3."""
    if _NOW_MSK_AVAILABLE:
        return _now_msk()
    # Fallback
    from datetime import timedelta
    return datetime.now(timezone(timedelta(hours=3)))


def now_iso():
    """ISO timestamp для created_at/updated_at."""
    ts = now_msk()
    if isinstance(ts, str):
        return ts.replace("+03:00", "Z") if "+03:00" in ts else ts
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_team_name(name):
    """Делегирует в team_registry.clean_team_name() — единый реестр (257+ алиасов).
    Fallback: локальные TEAM_ALIASES (130 алиасов)."""
    if not name:
        return ""
    # Приоритет — team_registry (единый реестр)
    if _REGISTRY_AVAILABLE:
        try:
            result = _registry_normalize(name)
            if result:
                return result
        except Exception:
            pass
    # Fallback — локальные TEAM_ALIASES
    n = name.strip().lower()
    return TEAM_ALIASES.get(n, n)


def build_canonical_id(home_team, away_team, date_utc):
    """Единый canonical_id. Приоритет — team_registry, fallback — локальный."""
    if _REGISTRY_AVAILABLE:
        try:
            return _registry_build_cid(home_team, away_team, date_utc)
        except Exception:
            pass
    # Fallback — локальная реализация (совместима с хабом)
    home_clean = clean_team_name(home_team).replace(" ", "_")
    away_clean = clean_team_name(away_team).replace(" ", "_")
    date_short = date_utc[:10].replace("-", "")
    return f"{home_clean}__{away_clean}__{date_short}"


def parse_date(raw):
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
        except (ValueError, TypeError):
            continue
    return None


def parse_time(raw, date_utc):
    """Parse Time column (HH:MM) -> ISO 8601 combined with date."""
    if not raw or not date_utc:
        return None
    raw = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            date_part = date_utc[:10]
            return f"{date_part}T{t.strftime('%H:%M:%S')}Z"
        except (ValueError, TypeError):
            continue
    return None


def safe_int(val):
    if val is None or val == "" or val == "NA":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def safe_str(val):
    if val is None:
        return ""
    return str(val).strip()


def safe_float(val):
    """Return float if valid, else None. Odds сохраняются как числа (FIX-1)."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s == "NA":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def build_price(row, h_key, d_key, a_key):
    """Build {home, draw, away} from 3 CSV columns, or None."""
    h = safe_float(row.get(h_key, ""))
    d = safe_float(row.get(d_key, ""))
    a = safe_float(row.get(a_key, ""))
    if h and d and a:
        return {"home": h, "draw": d, "away": a}
    return None


def build_ou25(row, b365_prefix, p_prefix, max_prefix, avg_prefix):
    """Build Over/Under 2.5 block."""
    result = {}
    for name, prefix in [("bet365", b365_prefix), ("pinnacle", p_prefix),
                          ("max", max_prefix), ("avg", avg_prefix)]:
        o = safe_float(row.get(f"{prefix}>2.5", ""))
        u = safe_float(row.get(f"{prefix}<2.5", ""))
        if o and u:
            result[name] = {"over": o, "under": u}
    return result if result else None


def build_ah(row, suffix, b365_prefix, p_prefix, max_prefix, avg_prefix):
    """Build Asian Handicap block."""
    result = {}
    if suffix == "C":
        size_key = "AHCh"
    else:
        size_key = "AHh"
    size = safe_float(row.get(size_key, ""))
    if size:
        result["size"] = size
    for name, prefix in [("bet365", b365_prefix), ("pinnacle", p_prefix),
                          ("max", max_prefix), ("avg", avg_prefix)]:
        h = safe_float(row.get(f"{prefix}AHH", ""))
        a = safe_float(row.get(f"{prefix}AHA", ""))
        if h and a:
            result[name] = {"home": h, "away": a}
    return result if result else None


def detect_flags(stats, score):
    total_goals = score["home"] + score["away"]
    extreme = total_goals >= 7
    abnormal = total_goals >= 8
    red_driven = stats.get("red_home", 0) > 0 or stats.get("red_away", 0) > 0
    return {
        "extreme_result": extreme,
        "abnormal_score": abnormal,
        "red_card_driven": red_driven,
    }


def build_stats(row, ts):
    return {
        "shots_home": safe_int(row.get("HS", 0)),
        "shots_away": safe_int(row.get("AS", 0)),
        "shots_on_target_home": safe_int(row.get("HST", 0)),
        "shots_on_target_away": safe_int(row.get("AST", 0)),
        "corners_home": safe_int(row.get("HC", 0)),
        "corners_away": safe_int(row.get("AC", 0)),
        "fouls_home": safe_int(row.get("HF", 0)),
        "fouls_away": safe_int(row.get("AF", 0)),
        "yellow_home": safe_int(row.get("HY", 0)),
        "yellow_away": safe_int(row.get("AY", 0)),
        "red_home": safe_int(row.get("HR", 0)),
        "red_away": safe_int(row.get("AR", 0)),
        "_source": SOURCE_NAME,
        "_updated_at": ts,
    }


def build_source_map(ts):
    return {
        "odds": {
            "source": SOURCE_NAME,
            "upstream": _FD_UPSTREAM,
            "timestamp": ts,
            "independent": True,
            "type": "closing",
        },
        "stats": {
            "source": SOURCE_NAME,
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
        "types": ["opening", "closing"],
        "sharp_benchmark": _PINNACLE_UPSTREAM,
        "soft_bookmakers": ["bet365", "bwin", "betfair"],
    }


def build_csv_raw(row):
    """Store ALL CSV columns as raw values — nothing is lost."""
    raw = {}
    for key, val in row.items():
        if key is None:
            continue
        k = key.strip()
        if not k:
            continue
        raw[k] = safe_str(val)
    return raw


def build_odds_block(row, ts):
    """Build comprehensive odds block: 1x2 (opening+closing), O/U 2.5, Asian Handicap.
    Все коэффициенты — float (FIX-1)."""
    opening_bm = {}
    closing_bm = {}
    for prefix, name in BOOKMAKERS_1X2:
        op = build_price(row, f"{prefix}H", f"{prefix}D", f"{prefix}A")
        if op:
            opening_bm[name] = op
        cl = build_price(row, f"{prefix}CH", f"{prefix}CD", f"{prefix}CA")
        if cl:
            closing_bm[name] = cl

    max_opening = build_price(row, "MaxH", "MaxD", "MaxA")
    avg_opening = build_price(row, "AvgH", "AvgD", "AvgA")
    max_closing = build_price(row, "MaxCH", "MaxCD", "MaxCA")
    avg_closing = build_price(row, "AvgCH", "AvgCD", "AvgCA")

    ou25_opening = build_ou25(row, "B365", "PP", "Max", "Avg")
    ou25_closing = build_ou25(row, "B365C", "PPC", "MaxC", "AvgC")

    ah_opening = build_ah(row, "", "B365", "PP", "Max", "Avg")
    ah_closing = build_ah(row, "C", "B365C", "PPC", "MaxC", "AvgC")

    current = closing_bm.get("pinnacle") or max_closing
    if not current and closing_bm:
        current = list(closing_bm.values())[0]

    opening = opening_bm.get("pinnacle") or max_opening
    if not opening and opening_bm:
        opening = list(opening_bm.values())[0]

    best = max_closing or max_opening

    # FIX-1: closing
    closing = max_closing or closing_bm.get("pinnacle") or (list(closing_bm.values())[0] if closing_bm else None)

    sources = []
    if opening_bm.get("pinnacle"):
        sources.append({"source": SOURCE_NAME, "upstream": _PINNACLE_UPSTREAM,
                        "price": opening_bm["pinnacle"], "timestamp": ts, "type": "opening"})
    if closing_bm.get("pinnacle"):
        sources.append({"source": SOURCE_NAME, "upstream": _PINNACLE_UPSTREAM,
                        "price": closing_bm["pinnacle"], "timestamp": ts, "type": "closing"})
    if not sources and opening_bm.get("bet365"):
        sources.append({"source": SOURCE_NAME, "upstream": _FD_UPSTREAM,
                        "price": opening_bm["bet365"], "timestamp": ts, "type": "opening"})
    if not sources and closing_bm.get("bet365"):
        sources.append({"source": SOURCE_NAME, "upstream": _FD_UPSTREAM,
                        "price": closing_bm["bet365"], "timestamp": ts, "type": "closing"})

    odds_1x2 = {}
    if current:
        odds_1x2["current"] = current
    if opening:
        odds_1x2["opening"] = opening
    if best:
        odds_1x2["best"] = best
    if closing:
        odds_1x2["closing"] = closing
    if sources:
        odds_1x2["sources"] = sources
    if opening_bm:
        odds_1x2["bookmakers_opening"] = opening_bm
    if closing_bm:
        odds_1x2["bookmakers_closing"] = closing_bm
    if max_opening:
        odds_1x2["max_opening"] = max_opening
    if avg_opening:
        odds_1x2["avg_opening"] = avg_opening
    if max_closing:
        odds_1x2["max_closing"] = max_closing
    if avg_closing:
        odds_1x2["avg_closing"] = avg_closing

    odds_block = {}
    if odds_1x2:
        odds_block["1x2"] = odds_1x2

    if ou25_opening or ou25_closing:
        ou25 = {}
        if ou25_opening:
            ou25["opening"] = ou25_opening
        if ou25_closing:
            ou25["closing"] = ou25_closing
        odds_block["over_under_25"] = ou25

    if ah_opening or ah_closing:
        ah = {}
        if ah_opening:
            ah["opening"] = ah_opening
        if ah_closing:
            ah["closing"] = ah_closing
        odds_block["asian_handicap"] = ah

    return odds_block


def build_payload(row, season, league_code, run_id=None):
    """Build full match payload — ALL CSV columns + structured data.
    v7.0: +source, +sources, +run_id для хаба."""
    league_info = LEAGUES.get(league_code, {"name": league_code, "country": "Unknown", "division": 0})

    home_team = safe_str(row.get("HomeTeam", ""))
    away_team = safe_str(row.get("AwayTeam", ""))
    date_raw = parse_date(safe_str(row.get("Date", "")))
    time_raw = parse_time(safe_str(row.get("Time", "")), date_raw) if date_raw else None

    if not home_team or not away_team or not date_raw:
        return None

    # date_utc включает время, если доступно
    date_utc = time_raw if time_raw else date_raw

    home_score = safe_int(row.get("FTHG", 0))
    away_score = safe_int(row.get("FTAG", 0))

    ht_home = safe_int(row.get("HTHG", 0))
    ht_away = safe_int(row.get("HTAG", 0))

    ts = now_iso()

    stats = build_stats(row, ts)
    odds_block = build_odds_block(row, ts)
    source_map = build_source_map(ts)
    csv_raw = build_csv_raw(row)

    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    canonical_id = build_canonical_id(home_team, away_team, date_utc)

    flags = detect_flags(stats, {"home": home_score, "away": away_score})

    payload = {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_clean,
        "away_clean": away_clean,
        "competition": league_info["name"],
        "country": league_info["country"],
        "season": str(season),
        "league_code": league_code,
        "division": league_info["division"],
        "date_utc": date_utc,
        "time_utc": time_raw,
        "status": "completed",
        "score": {"home": home_score, "away": away_score},
        "half_time_score": {"home": ht_home, "away": ht_away},
        "half_time_result": safe_str(row.get("HTR", "")),
        "full_time_result": safe_str(row.get("FTR", "")),
        "referee": safe_str(row.get("Referee", "")),
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "sources": [SOURCE_NAME],
        "odds": odds_block,
        "predictions": {},
        "value_analysis": {},
        "h2h": {},
        "stats": stats,
        "source_map": source_map,
        "flags": flags,
        "csv_raw": csv_raw,
        "created_at": ts,
        "updated_at": ts,
    }

    return payload


# ============================================================================
# COLLECTOR
# ============================================================================

class FootballDataCollector:
    def __init__(self, dry_run=False, run_id=None):
        self.dry_run = dry_run
        self.run_id = run_id or "manual"
        self.matches_processed = 0
        self.errors = 0
        self.skipped = 0
        self._meta_entries = {}
        self._batch = PipelineBatch(dry_run=dry_run, max_batch=50, batch_delay=0.15) if (not _HUB_AVAILABLE and _PIPELINE_BATCH_AVAILABLE) else None

    def download_csv(self, season, league_code):
        """Download CSV for a season+league from football-data.co.uk. 3 попытки."""
        url = f"{BASE_URL}/{season}/{league_code}.csv"
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"User-Agent": f"FootballDataCollector/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    content = resp.read().decode("utf-8-sig", errors="replace")
                return content
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.info(f"  [SKIP] {season}/{league_code} — сезон ещё не начался (404)")
                    return None
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    logger.info(f"  [RETRY] HTTP {e.code} for {url}, ждём {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"HTTP {e.code} for {url}")
                return None
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    logger.info(f"  [RETRY] {e} for {url}, ждём {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"Error {e} for {url}")
                return None
        return None

    def archive_csv(self, season, league_code, content):
        """Save raw CSV to /tmp for archival (all 22 leagues, always — even dry-run)."""
        archive_dir = os.environ.get("ARCHIVE_DIR", "/tmp/football_data_archive")
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"{season}_{league_code}.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def process_csv(self, csv_content, season, league_code, limit=0, history_days=0):
        """Parse CSV and write matches to Redis through gatekeeper_hub.upsert_match.
        v7.0: через хаб (не прямой SET), с idempotency_key."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0
        now = datetime.now(timezone.utc)

        for row in reader:
            if is_shutdown_requested():
                logger.info("    [SHUTDOWN] Graceful shutdown — прерываем CSV-обработку")
                break
            if limit and count >= limit:
                break

            try:
                payload = build_payload(row, season, league_code, run_id=self.run_id)
            except Exception as e:
                self.errors += 1
                logger.error(f"Parse error: {e}")
                continue

            if payload is None:
                self.skipped += 1
                continue

            # History filter
            if history_days > 0:
                try:
                    match_dt = datetime.fromisoformat(
                        payload["date_utc"].replace("Z", "+00:00")
                    )
                    if (now - match_dt).days > history_days:
                        self.skipped += 1
                        continue
                except (ValueError, TypeError):
                    pass

            # v7.0: idempotency_key — защита от дублей при повторном CI
            idempotency_key = f"{self.run_id}:{payload['canonical_id']}"

            # v7.0: Запись через хаб (upsert_match), не прямой SET
            try:
                if _HUB_AVAILABLE:
                    upsert_history_match(payload["canonical_id"], payload)
                else:
                    upsert_match(
                        payload,
                        source=SOURCE_NAME,
                        idempotency_key=idempotency_key,
                        dry_run=self.dry_run,
                    )
            except Exception as e:
                self.errors += 1
                logger.error(f"Write error: {e}")
                continue

            count += 1
            self.matches_processed += 1

        return count

    def save_meta(self, season, league_code, matches, errors):
        """Save metadata about this run. v7.0: пишется после каждой лиги (crash-safe)."""
        ts = now_iso()
        field = f"{season}_{league_code}"
        meta = {
            "season": season,
            "league_code": league_code,
            "matches": matches,
            "stored_matches": matches,
            "errors": errors,
            "error_count": errors,
            "last_run": ts,
            "last_run_at": ts,
            "version": VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
        }
        self._meta_entries[field] = meta
        # v7.0: flush_meta после каждой лиги (не в конце) — crash-safe
        self._flush_meta()

    def _flush_meta(self):
        """Write accumulated meta. v7.0: вызывается после каждой лиги (crash-safe).
        FIX §1.27: merge с существующей meta — не перезаписывать предыдущие запуски."""
        if not self._meta_entries:
            return
        key = "football_data:meta"

        if _HUB_AVAILABLE:
            try:
                from gatekeeper_hub import save_to_cache as _hub_save_cache
                from gatekeeper_hub import get_from_cache as _hub_get_cache
                # §1.27 FIX: читаем существующую meta, мерджим новые записи
                existing = _hub_get_cache(key) or {}
                if not isinstance(existing, dict):
                    existing = {}
                existing.update(self._meta_entries)
                _hub_save_cache(key, existing)
            except Exception:
                # Fallback на прямой SET через redis_hub
                try:
                    from redis_hub import get_key, set_key
                    existing_raw = get_key(key) if not self.dry_run else None
                    existing = json.loads(existing_raw) if existing_raw else {}
                    if not isinstance(existing, dict):
                        existing = {}
                    existing.update(self._meta_entries)
                    if not self.dry_run:
                        set_key(key, json.dumps(existing, ensure_ascii=False))
                except Exception as e:
                    logger.error(f"flush_meta fallback failed: {e}")
        else:
            try:
                from redis_hub import get_key, set_key
                existing_raw = get_key(key) if not self.dry_run else None
                existing = json.loads(existing_raw) if existing_raw else {}
                if not isinstance(existing, dict):
                    existing = {}
                existing.update(self._meta_entries)
                if not self.dry_run:
                    set_key(key, json.dumps(existing, ensure_ascii=False))
            except Exception as e:
                logger.error(f"flush_meta failed: {e}")

    def flush_remaining(self):
        """Финальный flush для fallback batch."""
        if self._batch:
            self._batch.flush()


# ============================================================================
# COLLECT_AND_PROCESS — единая точка входа для CI/CD
# ============================================================================

def collect_and_process(seasons: str = "", leagues: str = "",
                       limit: int = 0, history_days: int = 0,
                       dry_run: bool = False) -> dict:
    """
    Единая точка входа для CI/CD.
    Возвращает dict с метриками запуска.
    """
    # Parse seasons
    if seasons:
        season_list = [s.strip() for s in seasons.split(",") if s.strip()]
    else:
        now = datetime.now(timezone.utc)
        season_list = [f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"]

    # Parse leagues
    if leagues:
        league_list = [l.strip() for l in leagues.split(",") if l.strip()]
    else:
        league_list = list(LEAGUES.keys())

    # Инициализация хаба
    init_metrics = run_initialization(collector=SOURCE_NAME)
    run_id = init_metrics.get("run_id", "manual")

    if not dry_run:
        if not init_metrics.get("redis_available", False) and not is_redis_available():
            return {"error": "Redis недоступен", "stored_matches": 0, "run_id": run_id}

    collector = FootballDataCollector(dry_run=dry_run, run_id=run_id)

    total_matches = 0
    total_errors = 0

    for season in season_list:
        for league_code in league_list:
            if is_shutdown_requested():
                break

            csv_content = collector.download_csv(season, league_code)
            if csv_content is None:
                total_errors += 1
                continue

            collector.archive_csv(season, league_code, csv_content)
            matches = collector.process_csv(
                csv_content, season, league_code,
                limit=limit, history_days=history_days
            )
            total_matches += matches
            # v7.0: save_meta после каждой лиги (crash-safe)
            collector.save_meta(season, league_code, matches, collector.errors)

        if is_shutdown_requested():
            break

    collector.flush_remaining()

    return {
        "stored_matches": total_matches,
        "total_errors": total_errors,
        "collector_errors": collector.errors,
        "skipped": collector.skipped,
        "dry_run": dry_run,
        "run_id": run_id,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=f"Football-Data Collector v{VERSION}")
    parser.add_argument("--seasons", default="", help="Seasons comma-separated (e.g. 2425,2526)")
    parser.add_argument("--leagues", default="", help="League codes (empty = all 22)")
    parser.add_argument("--limit", type=int, default=0, help="Max matches per league (0 = all)")
    parser.add_argument("--history-days", type=int, default=0,
                        help="Only matches within N days (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Redis")
    args = parser.parse_args()

    logger.info(f"=== Football-Data Collector v{VERSION} (schema {SCHEMA_VERSION}) ===")
    logger.info(f"  Dry run: {args.dry_run}")
    logger.info(f"  Seasons: {args.seasons or 'auto'}")
    logger.info(f"  Leagues: {args.leagues or 'all 22'}")
    logger.info(f"  Limit: {args.limit or 'none'}")
    logger.info(f"  History days: {args.history_days or 'all'}")
    logger.info(f"  Hub: {'available' if _HUB_AVAILABLE else 'fallback (direct redis_hub)'}")
    logger.info(f"  Registry: {'available' if _REGISTRY_AVAILABLE else 'fallback (local TEAM_ALIASES)'}")

    # Инициализация хаба
    init_metrics = run_initialization(collector=SOURCE_NAME)
    run_id = init_metrics.get("run_id", "manual")

    if not init_metrics.get("redis_available", False) and not args.dry_run:
        if not is_redis_available():
            logger.info("[FATAL] Redis недоступен. Проверьте SHARED_UPSTASH_REDIS_REST_URL/TOKEN.")
            sys.exit(1)
        logger.info("  Redis: OK (direct)")
    else:
        logger.info(f"  Redis: {'OK (hub)' if init_metrics.get('redis_available') else 'dry-run'}")
        logger.info(f"  Run ID: {run_id}")

    # Parse seasons
    if args.seasons:
        seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    else:
        now = datetime.now(timezone.utc)
        seasons = [f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"]

    # Parse leagues
    if args.leagues:
        leagues = [l.strip() for l in args.leagues.split(",") if l.strip()]
    else:
        leagues = list(LEAGUES.keys())

    logger.info(f"  Seasons parsed: {seasons}")
    logger.info(f"  Leagues parsed: {leagues} ({len(leagues)})")

    collector = FootballDataCollector(dry_run=args.dry_run, run_id=run_id)

    total_matches = 0
    total_errors = 0

    for season in seasons:
        logger.info(f"\n--- Season {season} ---")
        for league_code in leagues:
            league_name = LEAGUES.get(league_code, {}).get("name", league_code)
            logger.info(f"  [{league_code}] {league_name}...")

            csv_content = collector.download_csv(season, league_code)
            if csv_content is None:
                total_errors += 1
                continue

            collector.archive_csv(season, league_code, csv_content)

            matches = collector.process_csv(
                csv_content, season, league_code,
                limit=args.limit, history_days=args.history_days
            )
            total_matches += matches
            logger.info(f"    Matches: {matches}")

            # v7.0: save_meta после каждой лиги (crash-safe)
            collector.save_meta(season, league_code, matches, collector.errors)

        if is_shutdown_requested():
            logger.info("\n  [SHUTDOWN] Graceful shutdown — прерываем")
            break

    collector.flush_remaining()

    logger.info(f"\n=== DONE ===")
    logger.info(f"  Total matches: {total_matches}")
    logger.error(f"  Total errors: {total_errors}")
    logger.error(f"  Collector errors: {collector.errors}")
    logger.info(f"  Skipped (invalid/old): {collector.skipped}")
    if args.dry_run:
        logger.info(f"  (dry-run: nothing written to Redis)")

    return 0 if total_errors == 0 and collector.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
