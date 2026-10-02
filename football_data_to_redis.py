#!/usr/bin/env python3
"""
Football-Data Collector v6.1 (CSV) — ALL columns, GatekeeperAI v5.7.18
Источник: football-data.co.uk
Сезоны: --seasons "2425,2526,2627"
Лиги:   --leagues "E0,E1,SP1"

Schema: v710-prod
CSV:    120 колонок (opening + closing odds, O/U 2.5, Asian Handicap)
Stats:  12 метрик + _source + _updated_at
Odds:   1x2 (opening+closing per-bookmaker), O/U 2.5, Asian Handicap
Raw:    ALL CSV columns stored in csv_raw{}
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ============================================================================
# CONFIG
# ============================================================================

VERSION = "6.1.0"
SCHEMA_VERSION = "v710"

REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

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

# Bookmaker prefixes for 1X2 odds
BOOKMAKERS_1X2 = [
    ("B365", "bet365"),
    ("BW",   "bwin"),
    ("BF",   "betfair"),
    ("PS",   "pinnacle"),
    ("WH",   "william_hill"),
]

# Team name aliases for canonical_id
TEAM_ALIASES = {
    "man united": "manchester united",
    "man utd": "manchester united",
    "newcastle": "newcastle united",
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "nott'm forest": "nottingham forest",
    "nottingham": "nottingham forest",
}

# ============================================================================
# CIRCUIT BREAKER
# ============================================================================

class CircuitBreaker:
    def __init__(self, max_errors=10, reset_timeout=60):
        self.max_errors = max_errors
        self.reset_timeout = reset_timeout
        self.error_count = 0
        self.last_error_time = 0
        self.state = "closed"

    def is_open(self):
        if self.state == "open":
            elapsed = time.time() - self.last_error_time
            if elapsed >= self.reset_timeout:
                self.state = "half_open"
                return False
            return True
        return False

    def record_success(self):
        if self.state == "half_open":
            self.state = "closed"
        self.error_count = 0

    def record_failure(self):
        self.error_count += 1
        self.last_error_time = time.time()
        if self.error_count >= self.max_errors:
            self.state = "open"
            remaining = int(self.reset_timeout - (time.time() - self.last_error_time))
            print(f"[REDIS WARNING] Circuit Breaker active (remaining {remaining}s)")

    def reset(self):
        self.error_count = 0
        self.state = "closed"

    def remaining_cooldown(self):
        if self.state != "open":
            return 0
        elapsed = time.time() - self.last_error_time
        return max(0, int(self.reset_timeout - elapsed))


# ============================================================================
# REDIS CLIENT
# ============================================================================

class RedisClient:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        self.cb = CircuitBreaker(max_errors=10, reset_timeout=60)
        self._pipeline = []
        self._pipeline_size = 0
        self.MAX_PIPELINE = 50
        self.BATCH_DELAY = 0.15

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, body, path=""):
        if self.cb.is_open():
            wait = self.cb.remaining_cooldown()
            if wait > 0:
                print(f"[REDIS WARNING] Circuit Breaker wait {wait}s")
                time.sleep(wait)
                self.cb.state = "half_open"

        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                self.cb.record_success()
                return data
        except urllib.error.HTTPError as e:
            self.cb.record_failure()
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"[REDIS ERROR] HTTP {e.code}: {err_body}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.cb.record_failure()
            print(f"[REDIS ERROR] {e}")
            return None

    def _post_pipeline(self, commands):
        if not commands:
            return None
        return self._post(commands, "/pipeline")

    def pipe_add(self, command, *args):
        self._pipeline.append([command] + [str(a) for a in args])
        self._pipeline_size += 1
        if self._pipeline_size >= self.MAX_PIPELINE:
            return self.flush()
        return None

    def flush(self):
        if not self._pipeline:
            return None
        commands = self._pipeline
        self._pipeline = []
        self._pipeline_size = 0
        result = self._post_pipeline(commands)
        time.sleep(self.BATCH_DELAY)
        return result

    def hset(self, key, field, value):
        return self.pipe_add("HSET", key, field, value)

    def sadd(self, key, *members):
        return self.pipe_add("SADD", key, *members)

    def zadd(self, key, score, member):
        return self.pipe_add("ZADD", key, str(score), member)

    def dbsize(self):
        if self.cb.is_open():
            return None
        result = self._post(["DBSIZE"])
        if result and "result" in result:
            return result["result"]
        return None


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
    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    date_short = date_utc[:10].replace("-", "")
    return f"{home_clean}__{away_clean}__{date_short}"

def parse_date(raw):
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
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

def build_ou25(row, suffix, b365_prefix, p_prefix, max_prefix, avg_prefix):
    """Build Over/Under 2.5 block."""
    result = {}
    b365 = {}
    o = safe_float_str(row.get(f"{b365_prefix}>2.5", ""))
    u = safe_float_str(row.get(f"{b365_prefix}<2.5", ""))
    if o and u:
        b365 = {"over": o, "under": u}
    pin = {}
    o = safe_float_str(row.get(f"{p_prefix}>2.5", ""))
    u = safe_float_str(row.get(f"{p_prefix}<2.5", ""))
    if o and u:
        pin = {"over": o, "under": u}
    mx = {}
    o = safe_float_str(row.get(f"{max_prefix}>2.5", ""))
    u = safe_float_str(row.get(f"{max_prefix}<2.5", ""))
    if o and u:
        mx = {"over": o, "under": u}
    avg = {}
    o = safe_float_str(row.get(f"{avg_prefix}>2.5", ""))
    u = safe_float_str(row.get(f"{avg_prefix}<2.5", ""))
    if o and u:
        avg = {"over": o, "under": u}
    if b365:
        result["bet365"] = b365
    if pin:
        result["pinnacle"] = pin
    if mx:
        result["max"] = mx
    if avg:
        result["avg"] = avg
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
    b365 = {}
    h = safe_float_str(row.get(f"{b365_prefix}AHH", ""))
    a = safe_float_str(row.get(f"{b365_prefix}AHA", ""))
    if h and a:
        b365 = {"home": h, "away": a}
    pin = {}
    h = safe_float_str(row.get(f"{p_prefix}AHH", ""))
    a = safe_float_str(row.get(f"{p_prefix}AHA", ""))
    if h and a:
        pin = {"home": h, "away": a}
    mx = {}
    h = safe_float_str(row.get(f"{max_prefix}AHH", ""))
    a = safe_float_str(row.get(f"{max_prefix}AHA", ""))
    if h and a:
        mx = {"home": h, "away": a}
    avg = {}
    h = safe_float_str(row.get(f"{avg_prefix}AHH", ""))
    a = safe_float_str(row.get(f"{avg_prefix}AHA", ""))
    if h and a:
        avg = {"home": h, "away": a}
    if b365:
        result["bet365"] = b365
    if pin:
        result["pinnacle"] = pin
    if mx:
        result["max"] = mx
    if avg:
        result["avg"] = avg
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
    stats = {
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
    return stats

def build_source_map(ts):
    return {
        "odds": {
            "source": "football_data",
            "upstream": "pinnacle",
            "timestamp": ts,
            "independent": True,
            "types": ["opening", "closing"],
            "sharp_benchmark": "pinnacle",
            "soft_bookmakers": ["bet365", "bwin", "william_hill", "betfair"],
        },
        "stats": {
            "source": "football_data",
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
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
    # --- 1X2 per-bookmaker (opening) ---
    opening_bm = {}
    closing_bm = {}
    for prefix, name in BOOKMAKERS_1X2:
        op = build_price(row, f"{prefix}H", f"{prefix}D", f"{prefix}A")
        if op:
            opening_bm[name] = op
        cl = build_price(row, f"{prefix}CH", f"{prefix}CD", f"{prefix}CA")
        if cl:
            closing_bm[name] = cl

    # --- Max/Avg ---
    max_opening = build_price(row, "MaxH", "MaxD", "MaxA")
    avg_opening = build_price(row, "AvgH", "AvgD", "AvgA")
    max_closing = build_price(row, "MaxCH", "MaxCD", "MaxCA")
    avg_closing = build_price(row, "AvgCH", "AvgCD", "AvgCA")

    # --- O/U 2.5 ---
    ou25_opening = build_ou25(row, "", "B365", "P", "Max", "Avg")
    ou25_closing = build_ou25(row, "C", "B365C", "PC", "MaxC", "AvgC")

    # --- Asian Handicap ---
    ah_opening = build_ah(row, "", "B365", "P", "Max", "Avg")
    ah_closing = build_ah(row, "C", "B365C", "PC", "MaxC", "AvgC")

    # --- Determine current/opening/best ---
    # current = Pinnacle closing (sharpest), fallback to max closing, fallback to any closing
    current = closing_bm.get("pinnacle") or max_closing
    if not current and closing_bm:
        current = list(closing_bm.values())[0]

    # opening = Pinnacle opening, fallback to max opening, fallback to any opening
    opening = opening_bm.get("pinnacle") or max_opening
    if not opening and opening_bm:
        opening = list(opening_bm.values())[0]

    # best = max closing (best price across bookmakers, closing)
    best = max_closing or max_opening

    # --- Sources ---
    sources = []
    if opening_bm.get("pinnacle"):
        sources.append({
            "source": "football_data",
            "upstream": "pinnacle",
            "price": opening_bm["pinnacle"],
            "timestamp": ts,
            "type": "opening",
        })
    if closing_bm.get("pinnacle"):
        sources.append({
            "source": "football_data",
            "upstream": "pinnacle",
            "price": closing_bm["pinnacle"],
            "timestamp": ts,
            "type": "closing",
        })
    if not sources and opening_bm.get("bet365"):
        sources.append({
            "source": "football_data",
            "upstream": "bet365",
            "price": opening_bm["bet365"],
            "timestamp": ts,
            "type": "opening",
        })
    if not sources and closing_bm.get("bet365"):
        sources.append({
            "source": "football_data",
            "upstream": "bet365",
            "price": closing_bm["bet365"],
            "timestamp": ts,
            "type": "closing",
        })

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
        "stats": stats,
        "h2h": {},
        "form": None,
        "ratings": None,
        "context": None,
        "source_map": source_map,
        "source_ids": {"football_data": league_code},
        "sources": ["football_data"],
        "section_history": [],
        "flags": flags,
        "created_at": ts,
        "updated_at": ts,
        "csv_raw": csv_raw,
    }

    return payload, canonical_id, date_utc, home_clean, away_clean


# ============================================================================
# CSV DOWNLOADER
# ============================================================================

def download_csv(season, league_code, max_retries=3):
    url = f"{BASE_URL}/{season}/{league_code}.csv"
    for attempt in range(1, max_retries + 1):
        print(f"[CSV] Download {url} (attempt {attempt}/{max_retries})")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FD-Collector/6.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if len(data) < 50:
                    print(f"[CSV] File too small ({len(data)} bytes), skip")
                    return None
                print(f"[CSV] Downloaded: {season}_{league_code}.csv ({len(data)} bytes)")
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            print(f"[CSV] Error: {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)
    return None

def archive_csv(season, league_code, data):
    archive_dir = "archive"
    if not os.path.exists(archive_dir):
        os.makedirs(archive_dir)
    path = os.path.join(archive_dir, f"{season}_{league_code}.csv")
    with open(path, "wb") as f:
        f.write(data)
    print(f"[CSV] Archived: {path}")


# ============================================================================
# MAIN COLLECTOR
# ============================================================================

class FootballDataCollector:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.total_matches = 0
        self.total_skipped = 0
        self.total_errors = 0
        self.total_csv_columns = 0

    def process_csv(self, csv_data, season, league_code, limit=0):
        text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
        reader = csv.DictReader(io.StringIO(text))

        print(f"[CSV] Processing: {season}_{league_code}.csv (season={season}, league={league_code})")

        if reader.fieldnames:
            col_count = len(reader.fieldnames)
            print(f"[CSV] Columns in CSV: {col_count}")
            if col_count > self.total_csv_columns:
                self.total_csv_columns = col_count

        matches_in_league = 0
        skipped = 0
        errors = 0

        for row in reader:
            if limit and matches_in_league >= limit:
                break

            try:
                result = build_payload(row, season, league_code)
                if result is None:
                    skipped += 1
                    continue

                payload, canonical_id, date_utc, home_clean, away_clean = result
                payload_json = json.dumps(payload, ensure_ascii=False)

                match_key = f"history:match:{canonical_id}"
                self.redis.hset(match_key, "data", payload_json)

                self.redis.sadd(f"history:team:{home_clean}", canonical_id)
                self.redis.sadd(f"history:team:{away_clean}", canonical_id)

                date_score = int(date_utc[:10].replace("-", ""))
                self.redis.zadd(f"history:league:{league_code}", date_score, canonical_id)

                matches_in_league += 1

            except Exception as e:
                errors += 1
                print(f"[CSV] Error row {reader.line_num}: {e}")
                continue

        self.redis.flush()
        self._save_meta(season, league_code, matches_in_league, skipped, errors)

        self.total_matches += matches_in_league
        self.total_skipped += skipped
        self.total_errors += errors

        print(f"[CSV] Done: {matches_in_league} matches, {skipped} skipped, {errors} errors")

        if league_code in ["E0", "E1", "E2", "E3", "EC", "SC0", "SC1", "D1", "D2", "I1", "I2", "SP1", "SP2", "F1", "F2", "N1", "B1", "P1", "T1", "G1"]:
            archive_csv(season, league_code, csv_data)

    def _save_meta(self, season, league_code, matches, skipped, errors):
        meta_key = "football_data:meta"
        meta_entry = json.dumps({
            "season": str(season),
            "league": league_code,
            "matches": matches,
            "skipped": skipped,
            "errors": errors,
            "csv_columns": self.total_csv_columns,
            "timestamp": now_iso(),
        }, ensure_ascii=False)

        self.redis.hset(meta_key, f"{season}_{league_code}", meta_entry)
        self.redis.flush()

    def print_summary(self):
        print("[FD] " + "=" * 50)
        print("[FD] SUMMARY")
        print("[FD] " + "=" * 50)
        print(f"[FD] Total rows:    {self.total_matches + self.total_skipped}")
        print(f"[FD] Written:       {self.total_matches}")
        print(f"[FD] Skipped:       {self.total_skipped}")
        print(f"[FD] Errors:        {self.total_errors}")
        print(f"[FD] CSV columns:   {self.total_csv_columns}")
        print("[FD] Done.")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Football-Data Collector v6.1")
    parser.add_argument("--seasons", type=str, default="", help="Seasons comma-separated")
    parser.add_argument("--leagues", type=str, default="", help="Leagues comma-separated")
    parser.add_argument("--season", type=str, default="", help="Single season (legacy)")
    parser.add_argument("--league", type=str, default="", help="Single league (legacy)")
    parser.add_argument("--history-days", type=int, default=0, help="History window days")
    parser.add_argument("--limit", type=int, default=0, help="Match limit per league")
    parser.add_argument("--dry-run", action="store_true", help="No Redis write")
    args = parser.parse_args()

    seasons_str = args.seasons or args.season or ""
    leagues_str = args.leagues or args.league or ""

    if not seasons_str:
        now = datetime.now(timezone.utc)
        seasons_str = f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"
        print(f"[FD] No season specified, using current: {seasons_str}")

    seasons = [s.strip() for s in seasons_str.split(",") if s.strip()]
    if leagues_str:
        leagues = [l.strip() for l in leagues_str.split(",") if l.strip()]
    else:
        leagues = list(LEAGUES.keys())

    print(f"[FD] Football-Data Collector v{VERSION} (CSV) started")
    print(f"[FD] Schema: {SCHEMA_VERSION} (GatekeeperAI v5.7.18)")
    print(f"[FD] Source: football-data.co.uk")
    print(f"[FD] Seasons: {', '.join(seasons)}")
    print(f"[FD] Leagues: {len(leagues)}")

    if args.dry_run:
        print("[FD] DRY RUN — no Redis write")
        redis = None
    else:
        if not REDIS_URL or not REDIS_TOKEN:
            print("[FD] ERROR: UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN not set")
            sys.exit(1)
        redis = RedisClient(REDIS_URL, REDIS_TOKEN)

    collector = FootballDataCollector(redis)

    for season in seasons:
        print(f"[FD] === Season {season} ===")
        for league_code in leagues:
            if league_code not in LEAGUES:
                print(f"[FD] Unknown league: {league_code}, skip")
                continue

            league_name = LEAGUES[league_code]["name"]
            print(f"[FD] --- {league_name} ({league_code}) ---")

            csv_data = download_csv(season, league_code)
            if csv_data is None:
                print(f"[FD] CSV not downloaded, skip {league_code}")
                continue

            collector.process_csv(csv_data, season, league_code, limit=args.limit)

            if redis and redis.cb.is_open():
                wait = redis.cb.remaining_cooldown()
                if wait > 0:
                    print(f"[FD] Circuit Breaker active, wait {wait}s")
                    time.sleep(wait)
                    redis.cb.reset()

    collector.print_summary()

    if redis:
        redis.flush()

    print(f"\n=== Football Data Loader Summary ===")
    print(f"Seasons: {','.join(seasons)}")
    print(f"Leagues: {','.join(leagues)}")
    print(f"History days: {args.history_days}")


if __name__ == "__main__":
    main()
