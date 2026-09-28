#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics v5"""

import sys
import os
import json
import time
import traceback
from datetime import datetime, date

def _p(msg):
    print(msg, flush=True)

try:
    import redis_hub
    _p("[DIAG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

# ─── helpers ──────────────────────────────────────────────────────────────────

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
    """Extract payload from {version, sender_repo, timestamp, payload} wrapper."""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except:
            return val
    if isinstance(val, dict):
        if "payload" in val and isinstance(val["payload"], (dict, list)):
            return val["payload"]
    return val

def _exec(cmd):
    try:
        return redis_hub._execute_upstash_cmd(cmd)
    except Exception as e:
        _p(f"[ERROR] _execute_upstash_cmd({cmd[0]}) failed: {e}")
        traceback.print_exc()
        return None

def _parse_date_from_key(key):
    try:
        parts = key.split("__")
        if len(parts) >= 3:
            date_str = parts[-1]
            if len(date_str) == 8 and date_str.isdigit():
                return date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except:
        pass
    return None

def _date_to_season(d):
    if d is None:
        return "?"
    y, m = d.year, d.month
    return f"{y}/{y+1}" if m >= 7 else f"{y-1}/{y}"

def _extract_season(val):
    if not isinstance(val, dict):
        return None
    s = val.get("season")
    if isinstance(s, str) and s.strip():
        return s.strip()
    if isinstance(s, dict):
        sd = s.get("startDate") or s.get("start_date")
        if sd:
            try:
                y, m = int(sd[:4]), int(sd[5:7])
                return f"{y}/{y+1}" if m >= 7 else f"{y-1}/{y}"
            except:
                pass
        nm = s.get("name") or s.get("year")
        if nm:
            return str(nm)
    sid = val.get("season_id")
    if sid is not None:
        return str(sid)
    return None

def _extract_league(val):
    if not isinstance(val, dict):
        return None
    lg = val.get("league") or val.get("competition") or val.get("league_name")
    if isinstance(lg, dict):
        return lg.get("name") or lg.get("id") or "?"
    if isinstance(lg, str) and lg.strip():
        return lg.strip()
    lid = val.get("league_id") or val.get("competition_id")
    if lid is not None:
        return str(lid)
    return None

def _extract_date(val):
    if not isinstance(val, dict):
        return None
    d = val.get("date") or val.get("utcDate") or val.get("match_date") or val.get("utc_date") or val.get("datetime")
    if isinstance(d, str) and d.strip():
        return d.strip()[:10]
    return None

def _extract_score(val):
    if not isinstance(val, dict):
        return None
    sc = val.get("score")
    if isinstance(sc, dict):
        ft = sc.get("fullTime") or sc.get("full_time") or sc
        if isinstance(ft, dict):
            h = ft.get("homeTeam") or ft.get("home_team") or ft.get("home")
            a = ft.get("awayTeam") or ft.get("away_team") or ft.get("away")
            if h is not None or a is not None:
                return f"{h}-{a}"
    h = val.get("home_score") or val.get("home_goals") or val.get("ft_home")
    a = val.get("away_score") or val.get("away_goals") or val.get("ft_away")
    if h is not None or a is not None:
        return f"{h}-{a}"
    return None

def _extract_teams(val):
    if not isinstance(val, dict):
        return None, None
    h = val.get("home_team") or val.get("homeTeam") or val.get("home")
    a = val.get("away_team") or val.get("awayTeam") or val.get("away")
    if isinstance(h, dict):
        h = h.get("name") or h.get("id")
    if isinstance(a, dict):
        a = a.get("name") or a.get("id")
    return h, a

# ─── load_all_fields ──────────────────────────────────────────────────────────

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

# ─── DUMP ─────────────────────────────────────────────────────────────────────

def dump_payloads(fields, n=5):
    _p("\n--- PAYLOAD DUMPS (history:match:*) ---")
    hist_keys = sorted([k for k in fields if k.startswith("history:match:")])
    seen = set()
    for key in hist_keys:
        if len(seen) >= n:
            break
        raw = fields[key]
        _p(f"\n  [{key}]")
        _p(f"  RAW type: {type(raw).__name__}")
        if isinstance(raw, dict) and "payload" in raw:
            _p(f"  Wrapper keys: {list(raw.keys())}")
            _p(f"  version={raw.get('version')}, sender={raw.get('sender_repo')}, ts={raw.get('timestamp')}")
            payload = raw["payload"]
            if isinstance(payload, dict):
                pk = tuple(sorted(payload.keys()))
                _p(f"  PAYLOAD keys ({len(pk)}): {list(pk)}")
                if pk not in seen:
                    seen.add(pk)
                    _p(f"  PAYLOAD FULL:")
                    try:
                        for line in json.dumps(payload, indent=2, ensure_ascii=False, default=str).split("\n"):
                            _p(f"    {line}")
                    except:
                        _p(f"    {payload!r}")
                else:
                    _p(f"  (structure already shown)")
        elif isinstance(raw, dict):
            pk = tuple(sorted(raw.keys()))
            _p(f"  Keys ({len(pk)}): {list(pk)}")
            if pk not in seen:
                seen.add(pk)
                _p(f"  FULL:")
                try:
                    for line in json.dumps(raw, indent=2, ensure_ascii=False, default=str).split("\n"):
                        _p(f"    {line}")
                except:
                    _p(f"    {raw!r}")
        else:
            _p(f"  Value: {str(raw)[:300]}")

    _p(f"\n--- PAYLOAD DUMPS (match:* without history:) ---")
    match_keys = sorted([k for k in fields if k.startswith("match:") and not k.startswith("history:")])
    seen2 = set()
    for key in match_keys[:n]:
        raw = fields[key]
        _p(f"\n  [{key}]")
        _p(f"  RAW type: {type(raw).__name__}")
        if isinstance(raw, dict) and "payload" in raw:
            _p(f"  Wrapper keys: {list(raw.keys())}")
            payload = raw["payload"]
            if isinstance(payload, dict):
                pk = tuple(sorted(payload.keys()))
                _p(f"  PAYLOAD keys ({len(pk)}): {list(pk)}")
                if pk not in seen2:
                    seen2.add(pk)
                    _p(f"  PAYLOAD FULL:")
                    try:
                        for line in json.dumps(payload, indent=2, ensure_ascii=False, default=str).split("\n"):
                            _p(f"    {line}")
                    except:
                        _p(f"    {payload!r}")
        elif isinstance(raw, dict):
            _p(f"  Keys: {list(raw.keys())}")
        else:
            _p(f"  Value: {str(raw)[:300]}")

    _p(f"\n--- META KEYS ---")
    meta_keys = [k for k in fields if "meta" in k.lower() or k in ("meta", "_meta", "stats", "info")]
    for key in meta_keys:
        raw = fields[key]
        _p(f"  [{key}]: {json.dumps(raw, ensure_ascii=False, default=str)[:300] if isinstance(raw, (dict, list)) else str(raw)[:300]}")

    _p(f"\n--- OTHER KEYS (not match/history/meta) ---")
    other_keys = [k for k in fields
                  if not k.startswith("history:match:")
                  and not (k.startswith("match:") and not k.startswith("history:"))
                  and "meta" not in k.lower()
                  and k not in ("meta", "_meta", "stats", "info")]
    for key in sorted(other_keys)[:15]:
        raw = fields[key]
        _p(f"  [{key}]: {str(raw)[:300]}")

# ─── HISTORY MODE ─────────────────────────────────────────────────────────────

def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    hist_keys = [k for k in fields if k.startswith("history:match:")]
    match_keys = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    meta_keys = [k for k in fields if "meta" in k.lower() or k in ("meta", "_meta", "stats", "info")]
    other_keys = [k for k in fields if k not in set(hist_keys + match_keys + meta_keys)]

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f"  meta            = {len(meta_keys)}")
    _p(f"  other           = {len(other_keys)}")

    dump_payloads(fields, n=5)

    # Parse history matches
    _p("\n\n--- PARSING history:match:* ---")
    seasons = {}
    leagues = {}
    season_league = {}
    parsed = 0
    errors = 0
    error_keys = []

    for key in hist_keys:
        raw = fields[key]
        payload = _unwrap(raw)
        if not isinstance(payload, dict):
            errors += 1
            error_keys.append(key)
            continue
        parsed += 1
        season = _extract_season(payload)
        league = _extract_league(payload)
        if season is None or season == "?":
            d = _parse_date_from_key(key)
            if d:
                season = _date_to_season(d)
        if season is None:
            season = "?"
        if league is None:
            league = "?"
        seasons[season] = seasons.get(season, 0) + 1
        leagues[league] = leagues.get(league, 0) + 1
        sl = f"{season} | {league}"
        season_league[sl] = season_league.get(sl, 0) + 1

    _p(f"  Parsed: {parsed}")
    _p(f"  Errors: {errors}")
    if error_keys:
        _p(f"  Error keys (first 10): {error_keys[:10]}")

    _p(f"\n--- By season ({len(seasons)} seasons) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    _p(f"\n--- By league ({len(leagues)} leagues) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x]):
        _p(f"  {lg}: {leagues[lg]}")

    _p(f"\n--- By season x league (top 30) ---")
    for sl in sorted(season_league.keys(), key=lambda x: -season_league[x])[:30]:
        _p(f"  {sl}: {season_league[sl]}")

    # Examples
    _p(f"\n--- Examples (10 history:match:*) ---")
    for key in sorted(hist_keys)[:10]:
        raw = fields[key]
        payload = _unwrap(raw)
        if not isinstance(payload, dict):
            _p(f"  [{key}] (parse error)")
            continue
        h, a = _extract_teams(payload)
        season = _extract_season(payload)
        league = _extract_league(payload)
        d = _extract_date(payload)
        score = _extract_score(payload)
        key_date = _parse_date_from_key(key)
        _p(f"  [{key}]")
        _p(f"    {h} vs {a} | season={season} | league={league} | date={d} | key_date={key_date} | score: {score}")

    # Analyze match:* (no prefix)
    _p(f"\n--- ANALYZING match:* ({len(match_keys)} keys) ---")
    match_past = []
    match_future = []
    match_unknown = []
    now = date.today()
    for key in match_keys:
        d = _parse_date_from_key(key)
        if d is None:
            match_unknown.append(key)
        elif d < now:
            match_past.append(key)
        else:
            match_future.append(key)

    _p(f"  Past (date < {now}):  {len(match_past)}")
    _p(f"  Future (date >= {now}): {len(match_future)}")
    _p(f"  Unknown date:        {len(match_unknown)}")

    if match_past:
        past_seasons = {}
        for key in match_past:
            d = _parse_date_from_key(key)
            s = _date_to_season(d) if d else "?"
            past_seasons[s] = past_seasons.get(s, 0) + 1
        _p(f"\n  Past match:* by season:")
        for s in sorted(past_seasons.keys()):
            _p(f"    {s}: {past_seasons[s]}")
        _p(f"\n  Past match:* examples (first 10):")
        for key in sorted(match_past)[:10]:
            raw = fields[key]
            payload = _unwrap(raw)
            d = _parse_date_from_key(key)
            if isinstance(payload, dict):
                h, a = _extract_teams(payload)
                league = _extract_league(payload)
                score = _extract_score(payload)
                _p(f"    [{key}] {h} vs {a} | league={league} | date={d} | score={score}")
            else:
                _p(f"    [{key}] (raw={str(raw)[:100]})")

    if match_unknown:
        _p(f"\n  Unknown date match:* examples (first 10):")
        for key in sorted(match_unknown)[:10]:
            raw = fields[key]
            payload = _unwrap(raw)
            if isinstance(payload, dict):
                h, a = _extract_teams(payload)
                league = _extract_league(payload)
                _p(f"    [{key}] {h} vs {a} | league={league}")
            else:
                _p(f"    [{key}] (raw={str(raw)[:100]})")

    # Duplicates
    _p(f"\n--- DUPLICATE CHECK ---")
    def canonical_from_key(key):
        if key.startswith("history:match:"):
            return key[len("history:match:"):]
        if key.startswith("match:"):
            return key[len("match:"):]
        return key

    hist_ids = set(canonical_from_key(k) for k in hist_keys)
    match_ids = set(canonical_from_key(k) for k in match_keys)
    dups = hist_ids & match_ids
    _p(f"  history:match:* unique IDs: {len(hist_ids)}")
    _p(f"  match:* unique IDs: {len(match_ids)}")
    _p(f"  Duplicates (in both): {len(dups)}")
    if dups:
        _p(f"  Duplicate examples (first 10):")
        for d in sorted(dups)[:10]:
            _p(f"    match:{d}  ==  history:match:{d}")

    # Lost matches
    _p(f"\n--- LOST MATCHES (past match:* not in history:*) ---")
    lost = []
    for k in match_past:
        cid = canonical_from_key(k)
        if cid not in hist_ids:
            lost.append(k)
    _p(f"  Count: {len(lost)}")
    if lost:
        _p(f"  Examples (first 20):")
        for key in sorted(lost)[:20]:
            raw = fields[key]
            payload = _unwrap(raw)
            d = _parse_date_from_key(key)
            if isinstance(payload, dict):
                h, a = _extract_teams(payload)
                league = _extract_league(payload)
                score = _extract_score(payload)
                _p(f"    [{key}] {h} vs {a} | league={league} | date={d} | score={score}")
            else:
                _p(f"    [{key}] (raw={str(raw)[:100]})")

    # Verification
    _p(f"\n--- VERIFICATION ---")
    _p(f"  history:match:* total:   {len(hist_keys)}")
    _p(f"  match:* total:           {len(match_keys)}")
    _p(f"  match:* past:            {len(match_past)}")
    _p(f"  match:* future:          {len(match_future)}")
    _p(f"  match:* unknown date:    {len(match_unknown)}")
    _p(f"  Duplicates:              {len(dups)}")
    _p(f"  Lost (past, no history): {len(lost)}")
    _p(f"  Expected:                17034")
    _p(f"  Diff (expected - actual): {17034 - len(hist_keys)}")
    _p(f"  history + lost:          {len(hist_keys) + len(lost)}")

    _p(f"\n--- META ---")
    for key in meta_keys:
        raw = fields[key]
        _p(f"  [{key}]: {json.dumps(raw, ensure_ascii=False, default=str)[:300] if isinstance(raw, (dict, list)) else str(raw)[:300]}")

    _p(f"\n=== HISTORY COMPLETE ===")

# ─── TEST MODE ─────────────────────────────────────────────────────────────────

def run_test():
    _p("=== TEST MODE ===")
    _p("\n--- 1. PING ---")
    r = _exec(["PING"])
    _p(f"  Result: {r!r}")
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
    _p("\n--- 4. HGET (first key, full dump) ---")
    if r:
        first_key = r[0]
        val = _exec(["HGET", "GatekeeperAI", first_key])
        _p(f"  Key: {first_key}")
        _p(f"  Value type: {type(val).__name__}")
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                _p(f"  Parsed JSON keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'not a dict'}")
                if isinstance(parsed, dict) and "payload" in parsed:
                    _p(f"  Payload keys: {list(parsed['payload'].keys()) if isinstance(parsed['payload'], dict) else type(parsed['payload']).__name__}")
                    _p(f"  Payload FULL:")
                    for line in json.dumps(parsed["payload"], indent=2, ensure_ascii=False, default=str).split("\n"):
                        _p(f"    {line}")
                else:
                    _p(f"  Full parsed:")
                    for line in json.dumps(parsed, indent=2, ensure_ascii=False, default=str).split("\n"):
                        _p(f"    {line}")
            except:
                _p(f"  Value (first 500 chars): {val[:500]}")
        else:
            _p(f"  Value: {val!r}")
    _p("\n--- 5. is_redis_available ---")
    try:
        avail = redis_hub.is_redis_available()
        _p(f"  Available: {avail}")
    except Exception as e:
        _p(f"  Error: {e}")
    _p("\n=== TEST COMPLETE ===")

# ─── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] CWD: {os.getcwd()}")
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
                    if isinstance(keys, str):
                        keys = json.loads(keys)
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
            _p("=== DIAGNOSTICS ===")
            fields = load_all_fields()
            if not fields:
                _p("[FATAL] No fields loaded")
                sys.exit(1)
            hist = [k for k in fields if k.startswith("history:match:")]
            match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
            other = [k for k in fields if k not in set(hist + match)]
            _p(f"Total: {len(fields)} (history={len(hist)}, match={len(match)}, other={len(other)})")
            _p("=== DIAGNOSTICS COMPLETE ===")
    except Exception as e:
        _p(f"[FATAL] Unhandled exception: {e}")
        traceback.print_exc()
        sys.exit(1)
