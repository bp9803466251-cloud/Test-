#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics & flush tool"""

import sys, os, json, time, traceback
from datetime import datetime, timezone

def _p(msg):
    print(msg, flush=True)

# ─── Imports ───
try:
    import redis_hub
    _p("[DIAG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

# ─── Helpers ───
def _safe_json(val):
    if val is None: return None
    if isinstance(val, (dict, list)): return val
    if isinstance(val, str):
        try: return json.loads(val)
        except: return val
    return val

def _unwrap(raw):
    if not isinstance(raw, dict): return raw
    if "payload" in raw and isinstance(raw["payload"], dict):
        return raw["payload"]
    return raw

def _exec(cmd):
    try:
        return redis_hub._execute_upstash_cmd(cmd)
    except Exception as e:
        _p(f"[ERROR] _execute_upstash_cmd({cmd[0]}) failed: {e}")
        return None

def _season_from_date(date_str):
    if not date_str or not isinstance(date_str, str):
        return "?"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        y, m = dt.year, dt.month
        if m >= 7:
            return f"{y}/{y+1}"
        else:
            return f"{y-1}/{y}"
    except:
        return "?"

def _season_from_key(key):
    parts = key.split("__")
    if len(parts) >= 3:
        date_part = parts[-1]
        if len(date_part) == 8 and date_part.isdigit():
            y = int(date_part[:4]); m = int(date_part[4:6])
            if m >= 7: return f"{y}/{y+1}"
            else: return f"{y-1}/{y}"
    return "?"

def _date_from_key(key):
    parts = key.split("__")
    if len(parts) >= 3:
        date_part = parts[-1]
        if len(date_part) == 8 and date_part.isdigit():
            return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
    return None

def load_all_fields():
    _p("[LOAD] Getting HKEYS...")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if keys is None:
        _p("[FATAL] HKEYS returned None")
        return {}
    if isinstance(keys, str):
        try: keys = json.loads(keys)
        except: keys = [keys]
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
            _p(f"[WARN] HMGET batch {bn}/{tb} returned None")
            time.sleep(1)
            continue
        if isinstance(vals, str):
            try: vals = json.loads(vals)
            except: pass
        if isinstance(vals, list):
            for k, v in zip(batch, vals):
                if v is not None:
                    all_fields[k] = _safe_json(v)
        if bn % 20 == 0 or bn == tb:
            _p(f"[LOAD] Batch {bn}/{tb} — {len(all_fields)} fields loaded")
        time.sleep(0.15)
    _p(f"[LOAD] Done: {len(all_fields)} fields")
    return all_fields

def _get_comp(payload):
    c = payload.get("competition")
    if isinstance(c, dict):
        return c.get("name") or c.get("id") or "?"
    if c: return str(c)
    return "?"

def _get_score(payload):
    s = payload.get("score")
    if not s: return None
    if isinstance(s, dict):
        h = s.get("home", s.get("homeTeam", s.get("fullTime", {}).get("homeTeam"))) if isinstance(s.get("fullTime"), dict) else s.get("home", s.get("homeTeam"))
        a = s.get("away", s.get("awayTeam", s.get("fullTime", {}).get("awayTeam"))) if isinstance(s.get("fullTime"), dict) else s.get("away", s.get("awayTeam"))
        # simpler:
        h = s.get("home")
        a = s.get("away")
        if h is None and isinstance(s.get("fullTime"), dict):
            h = s["fullTime"].get("homeTeam")
            a = s["fullTime"].get("awayTeam")
        return f"{h}-{a}"
    return str(s)

def _get_date(payload, key):
    d = payload.get("date_utc") or payload.get("utcDate") or payload.get("date")
    if d: return d
    return _date_from_key(key)

# ─── Modes ───
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
        try: r = json.loads(r)
        except: r = [r]
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
    _p("\n--- 5. is_redis_available ---")
    try:
        _p(f"  Available: {redis_hub.is_redis_available()}")
    except Exception as e:
        _p(f"  Error: {e}")
    _p("\n--- 6. load_all_fields ---")
    fields = load_all_fields()
    live = [k for k in fields if k.startswith("live:")]
    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("match:meta")]
    other = [k for k in fields if k not in live and k not in hist and k not in match]
    _p(f"\n  Total: {len(fields)}")
    _p(f"  history:match:* = {len(hist)}")
    _p(f"  match:*         = {len(match)}")
    _p(f"  live:*          = {len(live)}")
    _p(f"  other           = {len(other)}")
    if other[:10]:
        _p(f"  other samples: {other[:10]}")
    _p("\n=== TEST COMPLETE ===")

def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    # ── Key breakdown ──
    hist_keys = sorted([k for k in fields if k.startswith("history:match:")])
    match_keys = sorted([k for k in fields if k.startswith("match:") and not k.startswith("match:meta")])
    meta_keys = [k for k in fields if k.startswith("meta") or k.startswith("match:meta")]
    other_keys = [k for k in fields if k not in hist_keys and k not in match_keys and k not in meta_keys]

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f"  meta            = {len(meta_keys)}")
    _p(f"  other           = {len(other_keys)}")

    # ── 2 payload dumps only ──
    _p(f"\n--- PAYLOAD DUMP (2 examples) ---")
    for label, keys in [("HISTORY", hist_keys[:1]), ("MATCH", match_keys[:1])]:
        if not keys:
            continue
        k = keys[0]
        raw = fields[k]
        payload = _unwrap(raw)
        _p(f"\n  [{label}] {k}")
        _p(f"  RAW type: {type(raw).__name__}")
        if isinstance(raw, dict) and "payload" in raw:
            _p(f"  Wrapper: version={raw.get('version')}, sender={raw.get('sender_repo')}, ts={raw.get('timestamp')}")
        if isinstance(payload, dict):
            _p(f"  Payload keys ({len(payload)}): {sorted(payload.keys())}")
            _p(f"  competition: {payload.get('competition')!r}")
            _p(f"  date_utc: {payload.get('date_utc')!r}")
            _p(f"  score: {payload.get('score')!r}")
            _p(f"  status: {payload.get('status')!r}")
            _p(f"  country: {payload.get('country')!r}")

    # ── Parse history:match:* ──
    _p(f"\n--- Parsing history:match:* ---")
    seasons = {}
    leagues = {}
    season_league = {}
    parsed = 0
    errors = 0

    for k in hist_keys:
        payload = _unwrap(fields[k])
        if not isinstance(payload, dict):
            errors += 1
            continue
        parsed += 1
        comp = _get_comp(payload)
        date = _get_date(payload, k)
        season = _season_from_date(date) if date else _season_from_key(k)
        seasons[season] = seasons.get(season, 0) + 1
        leagues[comp] = leagues.get(comp, 0) + 1
        sl = f"{season} | {comp}"
        season_league[sl] = season_league.get(sl, 0) + 1

    _p(f"  Parsed: {parsed}, Errors: {errors}")

    # ── By season ──
    _p(f"\n--- By season ({len(seasons)} seasons) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    # ── By league ──
    _p(f"\n--- By league ({len(leagues)} leagues) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x]):
        _p(f"  {lg}: {leagues[lg]}")

    # ── By season x league (top 40) ──
    _p(f"\n--- By season x league (top 40) ---")
    for sl in sorted(season_league.keys(), key=lambda x: -season_league[x])[:40]:
        _p(f"  {sl}: {season_league[sl]}")

    # ── Examples ──
    _p(f"\n--- Examples (10 history:match:*) ---")
    for k in hist_keys[:10]:
        payload = _unwrap(fields[k])
        if not isinstance(payload, dict):
            _p(f"  [{k}] PARSE ERROR")
            continue
        comp = _get_comp(payload)
        date = _get_date(payload, k)
        season = _season_from_date(date) if date else _season_from_key(k)
        score = _get_score(payload)
        status = payload.get("status", "?")
        home = payload.get("home_team", "?")
        away = payload.get("away_team", "?")
        _p(f"  [{k}]")
        _p(f"    {home} vs {away} | {season} | {comp} | {date} | score: {score} | {status}")

    # ── Analyze match:* ──
    _p(f"\n--- Analyzing match:* ({len(match_keys)} keys) ---")
    now = datetime.now(timezone.utc)
    past_match = []
    future_match = []
    unknown_match = []

    for k in match_keys:
        payload = _unwrap(fields[k])
        if not isinstance(payload, dict):
            unknown_match.append(k)
            continue
        date = _get_date(payload, k)
        if date:
            try:
                dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if dt < now:
                    past_match.append(k)
                else:
                    future_match.append(k)
            except:
                unknown_match.append(k)
        else:
            unknown_match.append(k)

    _p(f"  Past (date < now):   {len(past_match)}")
    _p(f"  Future (date >= now): {len(future_match)}")
    _p(f"  Unknown date:        {len(unknown_match)}")

    # ── Past match:* by season ──
    if past_match:
        pm_seasons = {}
        pm_leagues = {}
        for k in past_match:
            payload = _unwrap(fields[k])
            if not isinstance(payload, dict): continue
            comp = _get_comp(payload)
            date = _get_date(payload, k)
            season = _season_from_date(date) if date else _season_from_key(k)
            pm_seasons[season] = pm_seasons.get(season, 0) + 1
            pm_leagues[comp] = pm_leagues.get(comp, 0) + 1
        _p(f"\n  Past match:* by season:")
        for s in sorted(pm_seasons.keys()):
            _p(f"    {s}: {pm_seasons[s]}")
        _p(f"\n  Past match:* by league:")
        for lg in sorted(pm_leagues.keys(), key=lambda x: -pm_leagues[x]):
            _p(f"    {lg}: {pm_leagues[lg]}")
        _p(f"\n  Past match:* examples (10):")
        for k in sorted(past_match)[:10]:
            payload = _unwrap(fields[k])
            if not isinstance(payload, dict):
                _p(f"    [{k}] PARSE ERROR")
                continue
            comp = _get_comp(payload)
            date = _get_date(payload, k)
            season = _season_from_date(date) if date else _season_from_key(k)
            score = _get_score(payload)
            home = payload.get("home_team", "?")
            away = payload.get("away_team", "?")
            _p(f"    [{k}]")
            _p(f"      {home} vs {away} | {season} | {comp} | {date} | score: {score}")

    # ── Unknown match:* examples ──
    if unknown_match:
        _p(f"\n  Unknown date match:* examples (10):")
        for k in sorted(unknown_match)[:10]:
            payload = _unwrap(fields[k])
            if isinstance(payload, dict):
                _p(f"    [{k}] comp={payload.get('competition')}, date_utc={payload.get('date_utc')}, status={payload.get('status')}")
            else:
                _p(f"    [{k}] type={type(payload).__name__}")

    # ── Find lost matches ──
    _p(f"\n--- Lost matches analysis ---")
    hist_ids = set()
    for k in hist_keys:
        payload = _unwrap(fields[k])
        if isinstance(payload, dict):
            cid = payload.get("canonical_id")
            if cid:
                hist_ids.add(cid)
        # also try from key
        parts = k.replace("history:match:", "").split("__")
        if len(parts) >= 3:
            hist_ids.add("__".join(parts))

    lost = []
    duplicates = []
    for k in past_match:
        payload = _unwrap(fields[k])
        cid = None
        if isinstance(payload, dict):
            cid = payload.get("canonical_id")
        if not cid:
            # extract from key
            parts = k.replace("match:", "").split("__")
            if len(parts) >= 3:
                cid = "__".join(parts)
        if cid and cid in hist_ids:
            duplicates.append(k)
        elif cid:
            lost.append(k)

    _p(f"  Past match:* total:     {len(past_match)}")
    _p(f"  Duplicates in history:  {len(duplicates)}")
    _p(f"  Lost (not in history):  {len(lost)}")

    if lost:
        lost_seasons = {}
        lost_leagues = {}
        for k in lost:
            payload = _unwrap(fields[k])
            if not isinstance(payload, dict): continue
            comp = _get_comp(payload)
            date = _get_date(payload, k)
            season = _season_from_date(date) if date else _season_from_key(k)
            lost_seasons[season] = lost_seasons.get(season, 0) + 1
            lost_leagues[comp] = lost_leagues.get(comp, 0) + 1
        _p(f"\n  Lost by season:")
        for s in sorted(lost_seasons.keys()):
            _p(f"    {s}: {lost_seasons[s]}")
        _p(f"\n  Lost by league:")
        for lg in sorted(lost_leagues.keys(), key=lambda x: -lost_leagues[x]):
            _p(f"    {lg}: {lost_leagues[lg]}")
        _p(f"\n  Lost examples (20):")
        for k in sorted(lost)[:20]:
            payload = _unwrap(fields[k])
            if not isinstance(payload, dict):
                _p(f"    [{k}] PARSE ERROR")
                continue
            comp = _get_comp(payload)
            date = _get_date(payload, k)
            season = _season_from_date(date) if date else _season_from_key(k)
            score = _get_score(payload)
            home = payload.get("home_team", "?")
            away = payload.get("away_team", "?")
            _p(f"    [{k}]")
            _p(f"      {home} vs {away} | {season} | {comp} | {date} | score: {score}")

    # ── Verification ──
    _p(f"\n--- Verification ---")
    _p(f"  history:match:*       = {len(hist_keys)}")
    _p(f"  match:* (past, lost) = {len(lost)}")
    _p(f"  match:* (past, dup)  = {len(duplicates)}")
    _p(f"  match:* (future)     = {len(future_match)}")
    _p(f"  match:* (unknown)    = {len(unknown_match)}")
    _p(f"  Expected total history: 17034")
    _p(f"  Actual (history + lost): {len(hist_keys) + len(lost)}")

    _p(f"\n=== HISTORY COMPLETE ===")

def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return
    live = [k for k in fields if k.startswith("live:")]
    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("match:meta")]
    other = [k for k in fields if k not in live and k not in hist and k not in match]
    _p(f"\nTotal: {len(fields)} (live={len(live)}, history={len(hist)}, match={len(match)}, other={len(other)})")
    if live:
        _p(f"\nLive keys sample: {live[:5]}")
    if other:
        _p(f"\nOther keys: {other[:20]}")
    _p(f"\n=== DIAGNOSTICS COMPLETE ===")

def run_flush(hard=False):
    _p("=== FLUSH MODE ===")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if not keys:
        _p("[FATAL] HKEYS returned None")
        return
    if isinstance(keys, str):
        try: keys = json.loads(keys)
        except: keys = [keys]

    if hard:
        _p(f"[FLUSH] HARD: Deleting ALL {len(keys)} keys...")
        deleted = 0
        for i in range(0, len(keys), 50):
            batch = keys[i:i+50]
            for k in batch:
                _exec(["HDEL", "GatekeeperAI", k])
                deleted += 1
            if deleted % 500 == 0 or deleted == len(keys):
                _p(f"[FLUSH] Deleted {deleted}/{len(keys)}")
            time.sleep(0.1)
        _p(f"[FLUSH] Done: {deleted} keys deleted")
    else:
        live_keys = [k for k in keys if k.startswith("live:")]
        _p(f"[FLUSH] SOFT: Deleting {len(live_keys)} live:* keys...")
        for i, k in enumerate(live_keys):
            _exec(["HDEL", "GatekeeperAI", k])
            if (i+1) % 100 == 0 or i+1 == len(live_keys):
                _p(f"[FLUSH] Deleted {i+1}/{len(live_keys)}")
            time.sleep(0.05)
        _p(f"[FLUSH] Done: {len(live_keys)} live keys deleted")
    _p("=== FLUSH COMPLETE ===")

if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] Args: {sys.argv}")
    mode = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            mode = arg[2:]
    _p(f"[DIAG] Mode: {mode}")

    if mode == "test":
        run_test()
    elif mode == "history":
        run_history()
    elif mode == "flush":
        if "--yes" in sys.argv:
            run_flush(hard="--hard" in sys.argv)
        else:
            _p("Use --flush --yes to confirm")
    else:
        run_diagnostics()
