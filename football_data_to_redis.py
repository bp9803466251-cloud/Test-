#!/usr/bin/env python3
"""
Football-Data Collector v5.1 (CSV)
Источник: football-data.co.uk
Сезоны: --seasons "2425,2526,2627"
Лиги:   --leagues "E0,E1,SP1"
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import hashlib
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ============================================================================
# CONFIG
# ============================================================================

VERSION = "5.1.0"
SCHEMA_VERSION = "v700"

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

# Odds fallback chain: B365 -> BbAv -> IW -> LB -> WH -> VC
ODDS_CHAIN = [
    {"prefix": "B365",  "upstream": "bet365"},
    {"prefix": "BbAv",  "upstream": "betbrain_avg"},
    {"prefix": "IW",    "upstream": "interwetten"},
    {"prefix": "LB",    "upstream": "ladbrokes"},
    {"prefix": "WH",    "upstream": "william_hill"},
    {"prefix": "VC",    "upstream": "vc_bet"},
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
# CIRCUIT BREAKER (with higher threshold)
# ============================================================================

class CircuitBreaker:
    def __init__(self, max_errors=10, reset_timeout=60):
        self.max_errors = max_errors
        self.reset_timeout = reset_timeout
        self.error_count = 0
        self.last_error_time = 0
        self.state = "closed"  # closed, open, half_open

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
            print(f"[REDIS WARNING] Circuit Breaker активен — запросы заблокированы (осталось {remaining}s)")

    def reset(self):
        self.error_count = 0
        self.state = "closed"

    def remaining_cooldown(self):
        if self.state != "open":
            return 0
        elapsed = time.time() - self.last_error_time
        return max(0, int(self.reset_timeout - elapsed))


# ============================================================================
# REDIS CLIENT (Upstash REST with batching)
# ============================================================================

class RedisClient:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        self.cb = CircuitBreaker(max_errors=10, reset_timeout=60)
        self._pipeline = []
        self._pipeline_size = 0
        self.MAX_PIPELINE = 50  # max commands per pipeline
        self.BATCH_DELAY = 0.15  # 150ms between batches

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, body):
        if self.cb.is_open():
            wait = self.cb.remaining_cooldown()
            if wait > 0:
                print(f"[REDIS WARNING] Circuit Breaker активен — ожидание {wait}s")
                time.sleep(wait)
                self.cb.state = "half_open"

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                self.cb.record_success()
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            self.cb.record_failure()
            print(f"[REDIS ERROR] {e}")
            return None

    def _post_pipeline(self, commands):
        """Send pipeline of commands as single Upstash REST call."""
        if not commands:
            return None
        body = commands  # Upstash REST pipeline: array of [command, args...]
        return self._post(body)

    def pipe_add(self, command, *args):
        """Add command to pipeline. Auto-flush when full."""
        self._pipeline.append([command] + list(args))
        self._pipeline_size += 1
        if self._pipeline_size >= self.MAX_PIPELINE:
            return self.flush()
        return None

    def flush(self):
        """Flush pending pipeline commands to Redis."""
        if not self._pipeline:
            return None
        commands = self._pipeline
        self._pipeline = []
        self._pipeline_size = 0

        result = self._post_pipeline(commands)
        time.sleep(self.BATCH_DELAY)  # throttle between batches
        return result

    def set(self, key, value):
        """Single set with retry."""
        if self.cb.is_open():
            return None
        body = ["SET", key, value]
        return self._post(body)

    def get(self, key):
        """Single get."""
        if self.cb.is_open():
            return None
        body = ["GET", key]
        result = self._post(body)
        if result and "result" in result:
            return result["result"]
        return None

    def hset(self, key, field, value):
        """Add HSET to pipeline."""
        return self.pipe_add("HSET", key, field, value)

    def sadd(self, key, *members):
        """Add SADD to pipeline."""
        return self.pipe_add("SADD", key, *members)

    def zadd(self, key, score, member):
        """Add ZADD to pipeline."""
        return self.pipe_add("ZADD", key, str(score), member)

    def hlen(self, key):
        """Get HLEN."""
        if self.cb.is_open():
            return None
        body = ["HLEN", key]
        result = self._post(body)
        if result and "result" in result:
            return result["result"]
        return None


# ============================================================================
# HELPERS
# ============================================================================

def clean_team_name(name):
    """Lowercase, strip, apply aliases."""
    if not name:
        return ""
    n = name.strip().lower()
    return TEAM_ALIASES.get(n, n)

def build_canonical_id(home_team, away_team, date_utc):
    """Format: {home_clean}__{away_clean}__{YYYYMMDD}"""
    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    date_short = date_utc[:10].replace("-", "")
    return f"{home_clean}__{away_clean}__{date_short}"

def parse_date(raw):
    """Parse DD/MM/YYYY -> ISO 8601 UTC."""
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
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

def resolve_odds(row):
    """Try odds chain: B365 -> BbAv -> IW -> LB -> WH -> VC."""
    for source in ODDS_CHAIN:
        prefix = source["prefix"]
        h_key = f"{prefix}H"
        d_key = f"{prefix}D"
        a_key = f"{prefix}A"
        h = safe_str(row.get(h_key, ""))
        d = safe_str(row.get(d_key, ""))
        a = safe_str(row.get(a_key, ""))
        if h and d and a and h != "NA" and d != "NA" and a != "NA":
            try:
                float(h)
                float(d)
                float(a)
                return {
                    "home": h,
                    "draw": d,
                    "away": a,
                    "upstream": source["upstream"],
                }
            except (ValueError, TypeError):
                continue
    return None

def detect_flags(stats, score):
    """Detect extreme_result, abnormal_score, red_card_driven."""
    total_goals = score["home"] + score["away"]
    extreme = total_goals >= 7
    abnormal = total_goals >= 8
    red_driven = stats.get("red_home", 0) > 0 or stats.get("red_away", 0) > 0
    return {
        "extreme_result": extreme,
        "abnormal_score": abnormal,
        "red_card_driven": red_driven,
    }

def build_payload(row, season, league_code):
    """Build full match payload from CSV row."""
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
    }

    odds_data = resolve_odds(row)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    odds_block = {}
    if odds_data:
        price = {
            "home": odds_data["home"],
            "draw": odds_data["draw"],
            "away": odds_data["away"],
        }
        odds_block = {
            "1x2": {
                "current": price,
                "opening": price,
                "best": price,
                "sources": [{
                    "source": "football_data",
                    "upstream": odds_data["upstream"],
                    "price": price,
                    "timestamp": now_iso,
                    "type": "closing",
                }],
            }
        }

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
        "date_utc": date_utc,
        "status": "completed",
        "score": {"home": home_score, "away": away_score},
        "half_time_score": {"home": ht_home, "away": ht_away},
        "full_time_result": safe_str(row.get("FTR", "")),
        "referee": safe_str(row.get("Referee", "")),
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "odds": odds_block,
        "predictions": {},
        "stats": stats,
        "h2h": {},
        "source_map": {
            "odds": {"source": "football_data", "upstream": odds_data["upstream"] if odds_data else "", "type": "closing"},
            "stats": {"source": "football_data", "upstream": "match_data"},
        },
        "source_ids": {"football_data": league_code},
        "sources": ["football_data"],
        "section_history": [],
        "flags": flags,
    }

    return payload, canonical_id, date_utc, home_clean, away_clean


# ============================================================================
# CSV DOWNLOADER
# ============================================================================

def download_csv(season, league_code, max_retries=3):
    url = f"{BASE_URL}/{season}/{league_code}.csv"
    for attempt in range(1, max_retries + 1):
        print(f"[CSV] Скачивание {url} (попытка {attempt}/{max_retries})")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FD-Collector/5.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if len(data) < 50:
                    print(f"[CSV] Файл слишком маленький ({len(data)} байт), пропускаем")
                    return None
                print(f"[CSV] Скачано: {season}_{league_code}.csv ({len(data)} байт)")
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            print(f"[CSV] Ошибка: {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)
    return None


def archive_csv(season, league_code, data):
    """Save CSV to archive directory."""
    archive_dir = "archive"
    if not os.path.exists(archive_dir):
        os.makedirs(archive_dir)
    path = os.path.join(archive_dir, f"{season}_{league_code}.csv")
    with open(path, "wb") as f:
        f.write(data)
    print(f"[CSV] Архивирован: {path}")


# ============================================================================
# MAIN COLLECTOR
# ============================================================================

class FootballDataCollector:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.total_matches = 0
        self.total_skipped = 0
        self.total_errors = 0

    def process_csv(self, csv_data, season, league_code, limit=0):
        """Parse CSV and write matches + indexes to Redis in batches."""
        text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
        reader = csv.DictReader(io.StringIO(text))

        league_info = LEAGUES.get(league_code, {"name": league_code})
        print(f"[CSV] Обработка: {season}_{league_code}.csv (season={season}, league={league_code})")

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

                # --- BATCH WRITE: match + indexes in same pipeline ---
                match_key = f"history:match:{canonical_id}"
                self.redis.hset(match_key, "data", payload_json)

                # Team index: history:team:{team_clean} -> set of canonical_ids
                self.redis.sadd(f"history:team:{home_clean}", canonical_id)
                self.redis.sadd(f"history:team:{away_clean}", canonical_id)

                # League index: history:league:{league_code} -> sorted set by date
                date_score = int(date_utc[:10].replace("-", ""))
                self.redis.zadd(f"history:league:{league_code}", date_score, canonical_id)

                matches_in_league += 1

            except Exception as e:
                errors += 1
                print(f"[CSV] Ошибка обработки строки {reader.line_num}: {e}")
                continue

        # Flush remaining pipeline
        self.redis.flush()

        # Save meta
        self._save_meta(season, league_code, matches_in_league, skipped, errors)

        self.total_matches += matches_in_league
        self.total_skipped += skipped
        self.total_errors += errors

        print(f"[CSV] Готово: {matches_in_league} матчей, {skipped} пропущено, {errors} ошибок")

        if league_code in ["E0", "E1", "E2", "E3", "D1", "I1", "SP1", "F1"]:
            archive_csv(season, league_code, csv_data)

    def _save_meta(self, season, league_code, matches, skipped, errors):
        """Save metadata to Redis."""
        meta_key = "football_data:meta"
        meta_entry = json.dumps({
            "season": str(season),
            "league": league_code,
            "matches": matches,
            "skipped": skipped,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, ensure_ascii=False)

        # Add to pipeline instead of separate call
        self.redis.hset(meta_key, f"{season}_{league_code}", meta_entry)
        self.redis.flush()

    def print_summary(self):
        print("[FD] " + "=" * 50)
        print("[FD] ИТОГО")
        print("[FD] " + "=" * 50)
        print(f"[FD] Всего строк:  {self.total_matches + self.total_skipped}")
        print(f"[FD] Записано:     {self.total_matches}")
        print(f"[FD] Пропущено:    {self.total_skipped}")
        print(f"[FD] Ошибок:       {self.total_errors}")
        print("[FD] Done.")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Football-Data Collector v5.1")
    parser.add_argument("--seasons", type=str, default="", help='Сезоны через запятую, например "2425,2526"')
    parser.add_argument("--leagues", type=str, default="", help='Лиги через запятую, например "E0,E1"')
    parser.add_argument("--season", type=str, default="", help="Один сезон (legacy)")
    parser.add_argument("--league", type=str, default="", help="Одна лига (legacy)")
    parser.add_argument("--history-days", type=int, default=0, help="Окно истории в днях (0 = весь CSV)")
    parser.add_argument("--limit", type=int, default=0, help="Лимит матчей на лигу (0 = все)")
    parser.add_argument("--dry-run", action="store_true", help="Без записи в Redis")
    args = parser.parse_args()

    # Normalize legacy args
    seasons_str = args.seasons or args.season or ""
    leagues_str = args.leagues or args.league or ""

    # Default season = current
    if not seasons_str:
        now = datetime.now(timezone.utc)
        seasons_str = f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"
        print(f"[FD] Сезон не указан, используем текущий: {seasons_str}")

    seasons = [s.strip() for s in seasons_str.split(",") if s.strip()]
    if leagues_str:
        leagues = [l.strip() for l in leagues_str.split(",") if l.strip()]
    else:
        leagues = list(LEAGUES.keys())

    print(f"[FD] Football-Data Collector v{VERSION} (CSV) started")
    print(f"[FD] Источник: football-data.co.uk")
    print(f"[FD] Сезоны: {', '.join(seasons)}")
    print(f"[FD] Лиг: {len(leagues)}")

    if args.dry_run:
        print("[FD] DRY RUN — запись в Redis отключена")
        redis = None
    else:
        if not REDIS_URL or not REDIS_TOKEN:
            print("[FD] ОШИБКА: UPSTASH_REDIS_REST_URL и UPSTASH_REDIS_REST_TOKEN не заданы")
            sys.exit(1)
        redis = RedisClient(REDIS_URL, REDIS_TOKEN)

    collector = FootballDataCollector(redis)

    for season in seasons:
        print(f"[FD] === Сезон {season} ===")
        for league_code in leagues:
            if league_code not in LEAGUES:
                print(f"[FD] Неизвестная лига: {league_code}, пропускаем")
                continue

            league_name = LEAGUES[league_code]["name"]
            print(f"[FD] --- {league_name} ({league_code}) ---")

            csv_data = download_csv(season, league_code)
            if csv_data is None:
                print(f"[FD] CSV не скачан, пропускаем {league_code}")
                continue

            if args.dry_run:
                # Just parse, don't write
                collector.process_csv(csv_data, season, league_code, limit=args.limit)
            else:
                collector.process_csv(csv_data, season, league_code, limit=args.limit)

            # Check circuit breaker
            if redis and redis.cb.is_open():
                wait = redis.cb.remaining_cooldown()
                if wait > 0:
                    print(f"[FD] Circuit Breaker активен, ждём {wait}s перед следующей лигой")
                    time.sleep(wait)
                    redis.cb.reset()

    collector.print_summary()

    # Final flush
    if redis:
        redis.flush()

    print(f"\n=== Football Data Loader Summary ===")
    print(f"Seasons: {','.join(seasons)}")
    print(f"Leagues: {','.join(leagues)}")
    print(f"History days: {args.history_days}")


if __name__ == "__main__":
    main()
