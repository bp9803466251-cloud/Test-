#!/usr/bin/env python3
"""
Football-Data Collector v6.3 (CSV) — ALL columns, GatekeeperAI v710
Источник: football-data.co.uk
Сезоны: --seasons "2425,2526,2627"
Лиги:   --leagues "E0,E1,SP1"

v6.3:
  - SET history:match:* (не HSET) — match hub get_key
  - canonical_id: .replace(" ", "_") — match hub format
  - Team SET keys: .replace(" ", "_") — match hub update_history_indexes
  - save_meta: SET football_data:meta (не HSET) — match diagnostics get_key
  - save_meta: +stored_matches, +error_count, +last_run_at — match diagnostics
  - TEAM_ALIASES: ~130 алиасов — sync с hub v2.1
  - download_csv: 3 попытки (2 retry) с backoff
  - self.errors: инкрементируется при ошибках парсинга
  - history_days: self.skipped += 1 (не silent drop)
  - build_payload: +h2h: {}
  - parse_date: +%Y/%m/%d
  - archive_csv: всегда (даже в dry-run)
  - build_ou25: убран мёртвый параметр suffix
  - Удалены мёртвые импорты: time, timedelta, scan_keys, delete_keys, dbsize

Schema: v710
CSV:    120 колонок (opening + closing odds, O/U 2.5, Asian Handicap)
Stats:  12 метрик + _source + _updated_at
Odds:   1x2 (opening+closing per-bookmaker), O/U 2.5, Asian Handicap
Raw:    ALL CSV columns stored in csv_raw{}
Transport: redis_hub.PipelineBatch (единый транспортный слой)
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Единый транспортный слой
from redis_hub import PipelineBatch, is_redis_available

# ============================================================================
# CONFIG
# ============================================================================

VERSION = "6.3.0"
SCHEMA_VERSION = "v710"

BASE_URL = "https://www.football-data.co.uk/mmz4281"

LEAGUES = {
    "E0":  {"name": "Premier League",       "country": "England",    "division": 1},
    "E1":  {"name": "Championship",          "country": "England",    "division": 2},
    "E2":  {"name": "League One",            "country": "England",    "division": 3},
    "E3":  {"name": "League Two",            "country": "England",    "division": 4},
    "EC":  {"name": "National League",       "country": "England",    "division": 5},
    "SC0": {"name": "Scottish Premiership",  "country": "Scotland",   "division": 1},
    "SC1": {"name": "Scottish Championship", "country": "Scotland",   "division": 2},
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

# Bookmaker prefixes for 1X2 odds (IW, VC removed — dead in CSV 2425+)
BOOKMAKERS_1X2 = [
    ("B365", "bet365"),
    ("BW",   "bwin"),
    ("PS",   "pinnacle"),
    ("WH",   "william_hill"),
    ("BF",   "betfair"),
]

# Team name aliases — синхронизированы с gatekeeper_hub.py v2.1
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
    "real sociedad": "real sociedad",
    "real sociedad de futbol": "real sociedad",
    "athletic bilbao": "athletic bilbao",
    "athletic club": "athletic bilbao",
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
    "fc bayern": "bayern munich",
    "borussia dortmund": "borussia dortmund",
    "dortmund": "borussia dortmund",
    "bayer leverkusen": "bayer leverkusen",
    "leverkusen": "bayer leverkusen",
    "borussia monchengladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
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
    # Ligue 1
    "paris saint-germain": "paris saint-germain",
    "paris saint germain": "paris saint-germain",
    "psg": "paris saint-germain",
    "saint-etienne": "saint-etienne",
    "st etienne": "saint-etienne",
    # Scottish
    "st mirren": "st mirren",
    "st. mirren": "st mirren",
    "st mirren fc": "st mirren",
    "celtic": "celtic",
    "celtic fc": "celtic",
    "rangers": "rangers",
    "rangers fc": "rangers",
    # Other
    "sporting cp": "sporting cp",
    "sporting lisbon": "sporting cp",
    "sporting": "sporting cp",
    "club brugge": "club brugge",
    "brugge": "club brugge",
}


# ============================================================================
# HELPERS
# ============================================================================

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def clean_team_name(name):
    if not name:
        return ""
    n = name.strip().lower()
    return TEAM_ALIASES.get(n, n)

def build_canonical_id(home_team, away_team, date_utc):
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

def safe_float_str(val):
    """Return string if valid float, else None."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s == "NA":
        return None
    try:
        float(s)
        return s
    except (ValueError, TypeError):
        return None

def build_price(row, h_key, d_key, a_key):
    """Build {home, draw, away} from 3 CSV columns, or None."""
    h = safe_float_str(row.get(h_key, ""))
    d = safe_float_str(row.get(d_key, ""))
    a = safe_float_str(row.get(a_key, ""))
    if h and d and a:
        return {"home": h, "draw": d, "away": a}
    return None

def build_ou25(row, b365_prefix, p_prefix, max_prefix, avg_prefix):
    """Build Over/Under 2.5 block."""
    result = {}
    for name, prefix in [("bet365", b365_prefix), ("pinnacle", p_prefix),
                          ("max", max_prefix), ("avg", avg_prefix)]:
        o = safe_float_str(row.get(f"{prefix}>2.5", ""))
        u = safe_float_str(row.get(f"{prefix}<2.5", ""))
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
    size = safe_float_str(row.get(size_key, ""))
    if size:
        result["size"] = size
    for name, prefix in [("bet365", b365_prefix), ("pinnacle", p_prefix),
                          ("max", max_prefix), ("avg", avg_prefix)]:
        h = safe_float_str(row.get(f"{prefix}AHH", ""))
        a = safe_float_str(row.get(f"{prefix}AHA", ""))
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
        "_source": "football_data",
        "_updated_at": ts,
    }

def build_source_map(ts):
    return {
        "odds": {
            "source": "football_data",
            "upstream": "multi_bookmaker",
            "timestamp": ts,
            "independent": True,
            "type": "closing",
        },
        "stats": {
            "source": "football_data",
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
        "types": ["opening", "closing"],
        "sharp_benchmark": "pinnacle",
        "soft_bookmakers": ["bet365", "bwin", "william_hill", "betfair"],
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
    """Build comprehensive odds block: 1x2 (opening+closing), O/U 2.5, Asian Handicap."""
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

    ou25_opening = build_ou25(row, "B365", "P", "Max", "Avg")
    ou25_closing = build_ou25(row, "B365C", "PC", "MaxC", "AvgC")

    ah_opening = build_ah(row, "", "B365", "P", "Max", "Avg")
    ah_closing = build_ah(row, "C", "B365C", "PC", "MaxC", "AvgC")

    current = closing_bm.get("pinnacle") or max_closing
    if not current and closing_bm:
        current = list(closing_bm.values())[0]

    opening = opening_bm.get("pinnacle") or max_opening
    if not opening and opening_bm:
        opening = list(opening_bm.values())[0]

    best = max_closing or max_opening

    sources = []
    if opening_bm.get("pinnacle"):
        sources.append({"source": "football_data", "upstream": "pinnacle",
                        "price": opening_bm["pinnacle"], "timestamp": ts, "type": "opening"})
    if closing_bm.get("pinnacle"):
        sources.append({"source": "football_data", "upstream": "pinnacle",
                        "price": closing_bm["pinnacle"], "timestamp": ts, "type": "closing"})
    if not sources and opening_bm.get("bet365"):
        sources.append({"source": "football_data", "upstream": "bet365",
                        "price": opening_bm["bet365"], "timestamp": ts, "type": "opening"})
    if not sources and closing_bm.get("bet365"):
        sources.append({"source": "football_data", "upstream": "bet365",
                        "price": closing_bm["bet365"], "timestamp": ts, "type": "closing"})

    odds_1x2 = {}
    if current:
        odds_1x2["current"] = current
    if opening:
        odds_1x2["opening"] = opening
    if best:
        odds_1x2["best"] = best
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

def build_payload(row, season, league_code):
    """Build full match payload — ALL CSV columns + structured data."""
    league_info = LEAGUES.get(league_code, {"name": league_code, "country": "Unknown", "division": 0})

    home_team = safe_str(row.get("HomeTeam", ""))
    away_team = safe_str(row.get("AwayTeam", ""))
    date_utc = parse_date(safe_str(row.get("Date", "")))

    if not home_team or not away_team or not date_utc:
        return None

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

    time_utc = parse_time(safe_str(row.get("Time", "")), date_utc)

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
        "time_utc": time_utc,
        "status": "completed",
        "score": {"home": home_score, "away": away_score},
        "half_time_score": {"home": ht_home, "away": ht_away},
        "half_time_result": safe_str(row.get("HTR", "")),
        "full_time_result": safe_str(row.get("FTR", "")),
        "referee": safe_str(row.get("Referee", "")),
        "version": 1,
        "schema_version": SCHEMA_VERSION,
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
    def __init__(self, batch: PipelineBatch, dry_run: bool = False):
        self.batch = batch
        self.dry_run = dry_run
        self.matches_processed = 0
        self.errors = 0
        self.skipped = 0
        self._meta_entries = {}

    def download_csv(self, season, league_code):
        """Download CSV for a season+league from football-data.co.uk. 3 попытки."""
        url = f"{BASE_URL}/{season}/{league_code}.csv"
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"User-Agent": f"FootballDataCollector/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                return content
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print(f"  [SKIP] {season}/{league_code} — сезон ещё не начался (404)")
                    return None
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    print(f"  [RETRY] HTTP {e.code} for {url}, ждём {wait}s...")
                    import time as _time
                    _time.sleep(wait)
                    continue
                print(f"  [ERROR] HTTP {e.code} for {url}")
                return None
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    print(f"  [RETRY] {e} for {url}, ждём {wait}s...")
                    import time as _time
                    _time.sleep(wait)
                    continue
                print(f"  [ERROR] {e} for {url}")
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
        """Parse CSV and write matches to Redis via PipelineBatch."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0
        now = datetime.now(timezone.utc)

        for row in reader:
            if limit and count >= limit:
                break

            try:
                payload = build_payload(row, season, league_code)
            except Exception as e:
                self.errors += 1
                print(f"    [PARSE ERROR] {e}")
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

            key = f"history:match:{payload['canonical_id']}"

            # SET individual key (string, не HSET hash) — match hub get_key
            self.batch.add("SET", key, json.dumps(payload, ensure_ascii=False))

            # ZADD league index
            try:
                dt = datetime.fromisoformat(
                    payload["date_utc"].replace("Z", "+00:00")
                )
                score = dt.timestamp()
            except (ValueError, TypeError):
                score = 0
            self.batch.add("ZADD", f"history:league:{league_code}", str(score),
                            payload["canonical_id"])

            # SADD team indexes (.replace(" ", "_") — match hub format)
            home_clean = payload["home_clean"].replace(" ", "_")
            away_clean = payload["away_clean"].replace(" ", "_")
            self.batch.add("SADD", f"history:team:{home_clean}", payload["canonical_id"])
            self.batch.add("SADD", f"history:team:{away_clean}", payload["canonical_id"])

            count += 1
            self.matches_processed += 1

        # Flush remaining
        self.batch.flush()
        return count

    def save_meta(self, season, league_code, matches, errors):
        """Save metadata about this run (накапливается, пишется через flush_meta)."""
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
        }
        self._meta_entries[field] = meta

    def flush_meta(self):
        """Write all accumulated meta as single SET (string key)."""
        if not self._meta_entries:
            return
        key = "football_data:meta"
        self.batch.add("SET", key, json.dumps(self._meta_entries, ensure_ascii=False))
        self.batch.flush()


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

    print(f"=== Football-Data Collector v{VERSION} (schema {SCHEMA_VERSION}) ===")
    print(f"  Dry run: {args.dry_run}")
    print(f"  Seasons: {args.seasons or 'auto'}")
    print(f"  Leagues: {args.leagues or 'all 22'}")
    print(f"  Limit: {args.limit or 'none'}")
    print(f"  History days: {args.history_days or 'all'}")

    # Redis check
    if not args.dry_run:
        if not is_redis_available():
            print("[FATAL] Redis недоступен. Проверьте UPSTASH_REDIS_REST_URL/TOKEN.")
            sys.exit(1)
        print("  Redis: OK")
    else:
        print("  Redis: dry-run (no writes)")

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

    print(f"  Seasons parsed: {seasons}")
    print(f"  Leagues parsed: {leagues} ({len(leagues)})")

    # Create pipeline batch
    batch = PipelineBatch(dry_run=args.dry_run, max_batch=50, batch_delay=0.15)
    collector = FootballDataCollector(batch, dry_run=args.dry_run)

    total_matches = 0
    total_errors = 0

    for season in seasons:
        print(f"\n--- Season {season} ---")
        for league_code in leagues:
            league_name = LEAGUES.get(league_code, {}).get("name", league_code)
            print(f"  [{league_code}] {league_name}...")

            csv_content = collector.download_csv(season, league_code)
            if csv_content is None:
                total_errors += 1
                continue

            # Archive (always, even in dry-run — полезно для отладки)
            collector.archive_csv(season, league_code, csv_content)

            # Process
            matches = collector.process_csv(
                csv_content, season, league_code,
                limit=args.limit, history_days=args.history_days
            )
            total_matches += matches
            print(f"    Matches: {matches}")

            # Save meta (накапливается)
            collector.save_meta(season, league_code, matches, collector.errors)

    # Flush accumulated meta
    collector.flush_meta()

    # Final flush
    batch.flush()

    print(f"\n=== DONE ===")
    print(f"  Total matches: {total_matches}")
    print(f"  Total errors: {total_errors}")
    print(f"  Collector errors: {collector.errors}")
    print(f"  Skipped (invalid/old): {collector.skipped}")
    print(f"  Pipeline batches: {batch.total_batches}")
    print(f"  Pipeline commands: {batch.total_sent}")
    if args.dry_run:
        print(f"  (dry-run: nothing written to Redis)")

    return 0 if total_errors == 0 and collector.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
