#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics"""

import sys
import json
import time
import traceback
import os
from datetime import datetime, timezone, timedelta

def _p(msg):
    print(msg, flush=True)

try:
    import redis_hub
    _p("[DEBUG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

MSK = timezone(timedelta(hours=3))

def _safe_json(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except:
            return val
    return val

def _unwrap(val):
    """Извлекает payload из обёртки {"version":..., "payload":...}"""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except:
            return val
    if isinstance(val, dict) and "payload" in val:
        return val["payload"]
    return val

def _exec(cmd):
    try:
        return redis_hub._execute_upstash_cmd(cmd)
    except Exception as e:
        _p(f"[ERROR] _execute_upstash_cmd({cmd[0]}) failed: {e}")
        traceback.print_exc()
        return None

def _extract_date_from_key(key):
    """Извлекает дату из ключа вида history:match:home__away__20241214"""
    parts = key.split("__")
    if len(parts) >= 3:
        date_str = parts[-1]
        if date_str == "unknown":
            return None, None
        if len(date_str) == 8 and date_str.isdigit():
            try:
                d = datetime.strptime(date_str, "%Y%m%d")
                return d.strftime("%Y-%m-%d"), d.year
            except:
                pass
    return None, None

def _date_to_season(date_str, year):
    """Преобразует год в сезон (2024 -> 2024/25)"""
    if year is None:
        return None
    # Европейский сезон: август-май, так что матчи с июня+ -> сезон Y/Y+1
    # Но для простоты: если месяц >= 7 (июль+), то сезон Y/(Y+1)
    # Если месяц < 7, то сезон (Y-1)/Y
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            if d.month >= 7:
                return f"{d.year}/{d.year+1}"
            else:
                return f"{d.year-1}/{d.year}"
        except:
            pass
    return f"{year}/{year+1}" if year else None

def load_all_fields():
    _p("[LOAD] Getting HKEYS...")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if keys is None:
        _p("[FATAL] HKEYS returned None")
        return {}
    if isinstance(keys, str):
        try:
            keys = json.loads(keys)
        except:
            keys = [keys]
    _p(f"[LOAD] HKEYS: {len(keys)} keys")
    if not keys:
        return {}

    all_fields = {}
    batch_size = 50
    total = len(keys)

    for i in range(0, total, batch_size):
        batch = keys[i:i+batch_size]
        bn = i // batch_size + 1
        tb = (total + batch_size - 1) // batch_size

        vals = _exec(["HMGET", "GatekeeperAI"] + batch)
        if vals is None:
            _p(f"[WARN] HMGET batch {bn}/{tb} returned None, skipping")
            time.sleep(1)
            continue

        if isinstance(vals, str):
            try:
                vals = json.loads(vals)
            except:
                pass

        if isinstance(vals, list):
            for k, v in zip(batch, vals):
                if v is not None:
                    all_fields[k] = _safe_json(v)

        if bn % 20 == 0 or bn == tb:
            _p(f"[LOAD] Batch {bn}/{tb} — {len(all_fields)} fields loaded")
        time.sleep(0.2)

    _p(f"[LOAD] Done: {len(all_fields)} fields")
    return all_fields

def run_history():
    _p(f"=== HISTORY MODE ===")
    _p(f"   Time: {datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    _p()

    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    # --- Классификация ключей ---
    hist_keys = [k for k in fields if k.startswith("history:match:")]
    match_keys = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    other_keys = [k for k in fields if not k.startswith("match:")]

    _p(f"--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f"  other           = {len(other_keys)}")
    if other_keys:
        _p(f"  other samples: {other_keys[:10]}")
    _p()

    # --- Парсинг history:match:* ---
    _p("--- Parsing history:match:* ---")
    seasons = {}
    leagues = {}
    season_league = {}
    parsed = 0
    errors = 0
    error_keys = []

    for key in hist_keys:
        raw = fields[key]
        val = _unwrap(raw)
        if not isinstance(val, dict):
            errors += 1
            error_keys.append(key)
            continue
        parsed += 1

        # Извлекаем дату и сезон из ключа
        date_str, year = _extract_date_from_key(key)
        season = _date_to_season(date_str, year)

        # Лига из payload
        league = val.get("league") or val.get("league_name") or val.get("competition") or "?"
        if isinstance(league, dict):
            league = league.get("name") or league.get("id") or "?"

        # Если сезон не извлекли из ключа — пробуем из payload
        if not season:
            season = val.get("season") or val.get("season_id") or "?"

        if date_str is None:
            date_str = val.get("date") or val.get("match_date") or val.get("utcDate") or "?"

        home = val.get("home_team") or val.get("homeTeam") or val.get("home") or "?"
        away = val.get("away_team") or val.get("awayTeam") or val.get("away") or "?"
        hs = val.get("home_score") or val.get("score_home") or val.get("home_goals") or "?"
        as_ = val.get("away_score") or val.get("score_away") or val.get("away_goals") or "?"

        seasons[season] = seasons.get(season, 0) + 1
        leagues[league] = leagues.get(league, 0) + 1
        sl = f"{season} | {league}"
        season_league[sl] = season_league.get(sl, 0) + 1

    _p(f"  Parsed: {parsed}")
    _p(f"  Errors: {errors}")
    if error_keys:
        _p(f"  Error keys (first 10): {error_keys[:10]}")
    _p()

    # --- By season ---
    _p(f"--- By season ({len(seasons)} seasons) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")
    _p()

    # --- By league ---
    _p(f"--- By league ({len(leagues)} leagues) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x]):
        _p(f"  {lg}: {leagues[lg]}")
    _p()

    # --- By season x league (top 40) ---
    _p(f"--- By season x league (top 40) ---")
    for sl in sorted(season_league.keys(), key=lambda x: -season_league[x])[:40]:
        _p(f"  {sl}: {season_league[sl]}")
    _p()

    # --- Examples ---
    _p("--- Examples (10 history:match:*) ---")
    for key in hist_keys[:10]:
        raw = fields[key]
        val = _unwrap(raw)
        if not isinstance(val, dict):
            _p(f"  [{key}] PARSE ERROR")
            continue
        date_str, year = _extract_date_from_key(key)
        season = _date_to_season(date_str, year)
        league = val.get("league") or val.get("league_name") or "?"
        if isinstance(league, dict):
            league = league.get("name") or "?"
        home = val.get("home_team") or val.get("homeTeam") or "?"
        away = val.get("away_team") or val.get("awayTeam") or "?"
        hs = val.get("home_score") or val.get("score_home") or "?"
        as_ = val.get("away_score") or val.get("score_away") or "?"
        _p(f"  [{key}]")
        _p(f"    {home} vs {away} | season={season} | league={league} | date={date_str} | score: {hs}-{as_}")
    _p()

    # --- Анализ match:* (без history:) ---
    _p(f"--- Analysis of match:* (no history: prefix) — {len(match_keys)} keys ---")

    now = datetime.now(timezone.utc)
    past_match_keys = []
    future_match_keys = []
    unknown_match_keys = []

    for key in match_keys:
        # Извлекаем канонический ID: match:home__away__date -> home__away__date
        canonical = key[len("match:"):]
        date_str, year = _extract_date_from_key(canonical)
        if date_str is None:
            # Пробуем найти дату в payload
            val = _unwrap(fields[key])
            if isinstance(val, dict):
                d = val.get("date") or val.get("match_date") or val.get("utcDate")
                if d:
                    try:
                        if "T" in d:
                            match_dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                        else:
                            match_dt = datetime.strptime(d, "%Y-%m-%d")
                        if match_dt.replace(tzinfo=timezone.utc) < now:
                            past_match_keys.append(key)
                        else:
                            future_match_keys.append(key)
                        continue
                    except:
                        pass
            unknown_match_keys.append(key)
        else:
            try:
                match_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if match_dt < now:
                    past_match_keys.append(key)
                else:
                    future_match_keys.append(key)
            except:
                unknown_match_keys.append(key)

    _p(f"  Past matches (date < now): {len(past_match_keys)}")
    _p(f"  Future/unknown date: {len(future_match_keys)}")
    _p(f"  Unknown date: {len(unknown_match_keys)}")
    _p()

    # Сезоны для прошлых match:*
    match_seasons = {}
    for key in past_match_keys:
        canonical = key[len("match:"):]
        date_str, year = _extract_date_from_key(canonical)
        season = _date_to_season(date_str, year) or "?"
        match_seasons[season] = match_seasons.get(season, 0) + 1

    _p(f"  Past match:* by season:")
    for s in sorted(match_seasons.keys()):
        _p(f"    {s}: {match_seasons[s]}")
    _p()

    # Примеры прошлых match:*
    _p(f"  Examples (5 past match:*):")
    for key in past_match_keys[:5]:
        val = _unwrap(fields[key])
        if isinstance(val, dict):
            canonical = key[len("match:"):]
            date_str, year = _extract_date_from_key(canonical)
            season = _date_to_season(date_str, year)
            league = val.get("league") or "?"
            if isinstance(league, dict):
                league = league.get("name") or "?"
            home = val.get("home_team") or "?"
            away = val.get("away_team") or "?"
            _p(f"    [{key}]")
            _p(f"      {home} vs {away} | season={season} | league={league} | date={date_str}")
        else:
            _p(f"    [{key}] PARSE ERROR")
    _p()

    # --- Поиск дубликатов ---
    _p("--- Duplicate check (match:* vs history:match:*) ---")
    match_canonicals = set()
    for k in match_keys:
        match_canonicals.add(k[len("match:"):])

    hist_canonicals = set()
    for k in hist_keys:
        hist_canonicals.add(k[len("history:match:"):] if k.startswith("history:match:") else k)

    duplicates = match_canonicals & hist_canonicals
    _p(f"  Duplicates (same canonical_id in both): {len(duplicates)}")
    if duplicates:
        _p(f"  Duplicate examples (first 10):")
        for d in list(duplicates)[:10]:
            _p(f"    match:{d} <-> history:match:{d}")
    _p()

    # --- Проверка: прошлые match:* без дубликата в history ---
    past_no_dup = []
    for key in past_match_keys:
        canonical = key[len("match:"):]
        if canonical not in hist_canonicals:
            past_no_dup.append(key)

    _p(f"  Past match:* WITHOUT history: duplicate: {len(past_no_dup)}")
    if past_no_dup:
        _p(f"  These are likely the 'missing' history matches!")
        _p(f"  Examples (first 10):")
        for key in past_no_dup[:10]:
            val = _unwrap(fields[key])
            if isinstance(val, dict):
                canonical = key[len("match:"):]
                date_str, year = _extract_date_from_key(canonical)
                season = _date_to_season(date_str, year)
                league = val.get("league") or "?"
                if isinstance(league, dict):
                    league = league.get("name") or "?"
                home = val.get("home_team") or "?"
                away = val.get("away_team") or "?"
                _p(f"    [{key}]")
                _p(f"      {home} vs {away} | season={season} | league={league} | date={date_str}")
            else:
                _p(f"    [{key}] PARSE ERROR")
    _p()

    # --- Verification ---
    _p("--- Verification ---")
    total_hist = len(hist_keys)
    total_match = len(match_keys)
    total_all = len(fields)
    _p(f"  history:match:* = {total_hist}")
    _p(f"  match:*         = {total_match}")
    _p(f"  other           = {len(other_keys)}")
    _p(f"  TOTAL           = {total_all}")
    _p(f"  Past match:* without history duplicate = {len(past_no_dup)}")
    _p(f"  Expected history matches: ~17034")
    _p(f"  Actual history:match:* = {total_hist}")
    gap = 17034 - total_hist
    _p(f"  Gap: {gap}")
    _p(f"  Past match:* (no dup) = {len(past_no_dup)}")
    if gap > 0 and len(past_no_dup) >= gap * 0.5:
        _p(f"  >>> LIKELY: {len(past_no_dup)} past matches stuck as match:* should be migrated to history:match:*")
    _p()
    _p("=== HISTORY COMPLETE ===")

def run_test():
    _p("=== TEST MODE ===")
    _p("\n--- 1. PING ---")
    r = _exec(["PING"])
    _p(f"  Result: {r!r} (type={type(r).__name__})")

    _p("\n--- 2. HLEN ---")
    r = _exec(["HLEN", "GatekeeperAI"])
    _p(f"  Result: {r!r}")

    _p("\n--- 3. HKEYS (first 10) ---")
    r = _exec(["HKEYS", "GatekeeperAI"])
    if r is None:
        _p("  HKEYS returned None!")
        return
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except:
            r = [r]
    _p(f"  Total keys: {len(r)}")
    _p(f"  First 10: {r[:10]}")

    _p("\n--- 4. HGET (first key) ---")
    if r:
        first_key = r[0]
        val = _exec(["HGET", "GatekeeperAI", first_key])
        _p(f"  Key: {first_key}")
        _p(f"  Value type: {type(val).__name__}")
        if isinstance(val, str):
            _p(f"  Value (first 300 chars): {val[:300]}")
        else:
            _p(f"  Value: {val!r}")

    _p("\n--- 5. is_redis_available ---")
    try:
        avail = redis_hub.is_redis_available()
        _p(f"  Available: {avail}")
    except Exception as e:
        _p(f"  Error: {e}")
        traceback.print_exc()

    _p("\n--- 6. load_all_fields ---")
    fields = load_all_fields()
    _p(f"  Loaded: {len(fields)} fields")

    # Разбивка
    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    other = [k for k in fields if not k.startswith("match:")]
    _p(f"  history:match:* = {len(hist)}")
    _p(f"  match:*         = {len(match)}")
    _p(f"  other           = {len(other)}")

    # Проверка unwrap
    if hist:
        val = _unwrap(fields[hist[0]])
        _p(f"\n--- 7. Unwrap test ---")
        _p(f"  Key: {hist[0]}")
        _p(f"  Unwrapped type: {type(val).__name__}")
        if isinstance(val, dict):
            _p(f"  Keys in payload: {list(val.keys())[:20]}")

    _p("\n=== TEST COMPLETE ===")

def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return
    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    other = [k for k in fields if not k.startswith("match:")]
    _p(f"\nTotal: {len(fields)} (history={len(hist)}, match={len(match)}, other={len(other)})")
    if other:
        _p(f"\nOther keys: {other[:10]}")
    _p(f"\n=== DIAGNOSTICS COMPLETE ===")

if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] CWD: {os.getcwd() if 'os' in dir() else '.'}")
    _p(f"[DIAG] Args: {sys.argv}")
    mode = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            mode = arg[2:]
    _p(f"[DIAG] Mode: {mode}")

    try:
        if mode == "test":
            run_test()
        elif mode == "history":
            run_history()
        elif mode == "flush":
            if "--yes" in sys.argv:
                hard = "--hard" in sys.argv
                _p(f"Flushing (hard={hard})...")
                keys = _exec(["HKEYS", "GatekeeperAI"])
                if keys:
                    if hard:
                        _p(f"Deleting ALL {len(keys)} keys...")
                        for k in keys:
                            _exec(["HDEL", "GatekeeperAI", k])
                    else:
                        live_keys = [k for k in keys if k.startswith("live:")]
                        _p(f"Deleting {len(live_keys)} live keys...")
                        for k in live_keys:
                            _exec(["HDEL", "GatekeeperAI", k])
                    _p("Flush complete.")
            else:
                _p("Use --flush --yes to confirm")
        else:
            run_diagnostics()
    except Exception as e:
        _p(f"[FATAL] Unhandled exception: {e}")
        traceback.print_exc()
        sys.exit(1)
