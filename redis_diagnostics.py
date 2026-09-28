#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics"""

import sys
import json
import time
import traceback
from datetime import datetime, timezone

def _p(msg):
    print(msg, flush=True)

try:
    import redis_hub
    _p("[DIAG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

# ─── Helpers ───

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

def _exec(cmd):
    try:
        return redis_hub._execute_upstash_cmd(cmd)
    except Exception as e:
        _p(f"[ERROR] _execute_upstash_cmd({cmd[0]}) failed: {e}")
        return None

def _unwrap(raw):
    """Extract payload from wrapper if present."""
    if isinstance(raw, dict) and "payload" in raw and "version" in raw:
        return raw["payload"]
    return raw

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

def _extract_date(payload):
    """Extract date string from payload."""
    d = payload.get("date_utc") or payload.get("utcDate") or payload.get("date")
    return d

def _extract_season(payload):
    """Extract season from date_utc or key."""
    d = _extract_date(payload)
    if d and isinstance(d, str) and len(d) >= 4:
        try:
            year = int(d[:4])
            month = int(d[5:7]) if len(d) >= 7 else 1
            if month >= 7:
                return f"{year}/{year+1}"
            else:
                return f"{year-1}/{year}"
        except:
            pass
    return None

def _extract_season_from_key(key):
    """Extract season from key like history:match:home__away__20241214."""
    parts = key.split("__")
    if parts:
        last = parts[-1]
        if len(last) >= 4 and last[:4].isdigit():
            year = int(last[:4])
            month = int(last[4:6]) if len(last) >= 6 else 1
            if month >= 7:
                return f"{year}/{year+1}"
            else:
                return f"{year-1}/{year}"
    return None

def _extract_league(payload):
    """Extract league name from payload."""
    lg = payload.get("competition") or payload.get("league")
    if isinstance(lg, dict):
        return lg.get("name") or lg.get("id") or "?"
    return lg

def _extract_score(payload):
    """Extract score string from payload."""
    sc = payload.get("score")
    if not sc:
        return None
    if isinstance(sc, dict):
        h = sc.get("home") or sc.get("homeTeam") or sc.get("fullTime", {}).get("homeTeam") if isinstance(sc.get("fullTime"), dict) else None
        a = sc.get("away") or sc.get("awayTeam") or sc.get("fullTime", {}).get("awayTeam") if isinstance(sc.get("fullTime"), dict) else None
        if h is not None and a is not None:
            return f"{h}-{a}"
    return None

def _extract_canonical_id(payload, key):
    """Extract canonical_id from payload or key."""
    cid = payload.get("canonical_id")
    if cid:
        return cid
    # Extract from key
    if ":" in key:
        parts = key.split(":")
        if len(parts) >= 3:
            return parts[2]
    return key

def _parse_payload(raw_val, key):
    """Parse raw value and return payload dict."""
    if isinstance(raw_val, str):
        try:
            raw_val = json.loads(raw_val)
        except:
            return None
    if not isinstance(raw_val, dict):
        return None
    return _unwrap(raw_val)

# ─── Main modes ───

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

    _p("\n--- 4. HGET (first key) ---")
    if r:
        first_key = r[0]
        val = _exec(["HGET", "GatekeeperAI", first_key])
        _p(f"  Key: {first_key}")
        if isinstance(val, str):
            _p(f"  Value (first 200 chars): {val[:200]}")

    _p("\n--- 5. is_redis_available ---")
    try:
        avail = redis_hub.is_redis_available()
        _p(f"  Available: {avail}")
    except Exception as e:
        _p(f"  Error: {e}")

    _p("\n=== TEST COMPLETE ===")

def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    # ── Key breakdown ──
    hist_keys = sorted([k for k in fields if k.startswith("history:match:")])
    match_keys = sorted([k for k in fields if k.startswith("match:") and not k.startswith("history:")])
    other_keys = sorted([k for k in fields if not k.startswith("history:match:") and not k.startswith("match:")])

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f"  other           = {len(other_keys)}")

    # ── Parse history ──
    hist_payloads = {}
    hist_errors = 0
    for key in hist_keys:
        payload = _parse_payload(fields[key], key)
        if payload is None:
            hist_errors += 1
            continue
        hist_payloads[key] = payload

    _p(f"\n--- History parse ---")
    _p(f"  Parsed: {len(hist_payloads)}, Errors: {hist_errors}")

    # ── Brief season summary ──
    seasons = {}
    for key, p in hist_payloads.items():
        s = _extract_season(p) or _extract_season_from_key(key) or "?"
        seasons[s] = seasons.get(s, 0) + 1

    _p(f"\n--- By season ({len(seasons)}) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    # ── Brief league summary ──
    leagues = {}
    for key, p in hist_payloads.items():
        lg = _extract_league(p) or "?"
        leagues[lg] = leagues.get(lg, 0) + 1

    _p(f"\n--- By league ({len(leagues)}) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x]):
        _p(f"  {lg}: {leagues[lg]}")

    # ── Build canonical ID set for history ──
    hist_cids = set()
    for key, p in hist_payloads.items():
        cid = _extract_canonical_id(p, key)
        hist_cids.add(cid)

    _p(f"\n  History canonical IDs: {len(hist_cids)}")

    # ── Parse match:* ──
    match_payloads = {}
    match_errors = 0
    for key in match_keys:
        payload = _parse_payload(fields[key], key)
        if payload is None:
            match_errors += 1
            continue
        match_payloads[key] = payload

    _p(f"\n--- match:* parse ---")
    _p(f"  Parsed: {len(match_payloads)}, Errors: {match_errors}")

    # ── Split match:* into past / future / unknown ──
    now_utc = datetime.now(timezone.utc)
    past_matches = {}
    future_matches = {}
    unknown_matches = {}

    for key, p in match_payloads.items():
        d = _extract_date(p)
        if d and isinstance(d, str) and len(d) >= 10:
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                if dt < now_utc:
                    past_matches[key] = p
                else:
                    future_matches[key] = p
            except:
                # Try from key
                s = _extract_season_from_key(key)
                if s:
                    past_matches[key] = p
                else:
                    unknown_matches[key] = p
        else:
            s = _extract_season_from_key(key)
            if s:
                # Check if season is in the past
                try:
                    year = int(s.split("/")[0])
                    if year < now_utc.year or (year == now_utc.year and now_utc.month < 7):
                        past_matches[key] = p
                    else:
                        unknown_matches[key] = p
                except:
                    unknown_matches[key] = p
            else:
                unknown_matches[key] = p

    _p(f"\n--- match:* split ---")
    _p(f"  Past    = {len(past_matches)}")
    _p(f"  Future  = {len(future_matches)}")
    _p(f"  Unknown = {len(unknown_matches)}")

    # ── Past match:* by season ──
    past_seasons = {}
    for key, p in past_matches.items():
        s = _extract_season(p) or _extract_season_from_key(key) or "?"
        past_seasons[s] = past_seasons.get(s, 0) + 1

    _p(f"\n--- Past match:* by season ---")
    for s in sorted(past_seasons.keys()):
        _p(f"  {s}: {past_seasons[s]}")

    # ── Past match:* by league ──
    past_leagues = {}
    for key, p in past_matches.items():
        lg = _extract_league(p) or "?"
        past_leagues[lg] = past_leagues.get(lg, 0) + 1

    _p(f"\n--- Past match:* by league ---")
    for lg in sorted(past_leagues.keys(), key=lambda x: -past_leagues[x]):
        _p(f"  {lg}: {past_leagues[lg]}")

    # ── Find LOST matches: past match:* not in history ──
    lost = {}
    duplicates = 0
    for key, p in past_matches.items():
        cid = _extract_canonical_id(p, key)
        if cid in hist_cids:
            duplicates += 1
        else:
            lost[key] = p

    _p(f"\n{'='*60}")
    _p(f"  LOST MATCHES: {len(lost)}")
    _p(f"  Duplicates (in both match:* and history:*): {duplicates}")
    _p(f"{'='*60}")

    # ── Lost by season ──
    lost_seasons = {}
    for key, p in lost.items():
        s = _extract_season(p) or _extract_season_from_key(key) or "?"
        lost_seasons[s] = lost_seasons.get(s, 0) + 1

    _p(f"\n--- Lost by season ---")
    for s in sorted(lost_seasons.keys()):
        _p(f"  {s}: {lost_seasons[s]}")

    # ── Lost by league ──
    lost_leagues = {}
    for key, p in lost.items():
        lg = _extract_league(p) or "?"
        lost_leagues[lg] = lost_leagues.get(lg, 0) + 1

    _p(f"\n--- Lost by league ---")
    for lg in sorted(lost_leagues.keys(), key=lambda x: -lost_leagues[x]):
        _p(f"  {lg}: {lost_leagues[lg]}")

    # ── Lost by season x league ──
    lost_sl = {}
    for key, p in lost.items():
        s = _extract_season(p) or _extract_season_from_key(key) or "?"
        lg = _extract_league(p) or "?"
        sl = f"{s} | {lg}"
        lost_sl[sl] = lost_sl.get(sl, 0) + 1

    _p(f"\n--- Lost by season x league (top 20) ---")
    for sl in sorted(lost_sl.keys(), key=lambda x: -lost_sl[x])[:20]:
        _p(f"  {sl}: {lost_sl[sl]}")

    # ── Lost examples ──
    _p(f"\n--- Lost examples (20) ---")
    for i, (key, p) in enumerate(sorted(lost.items())[:20]):
        home = p.get("home_team") or "?"
        away = p.get("away_team") or "?"
        lg = _extract_league(p) or "?"
        d = _extract_date(p) or "?"
        sc = _extract_score(p) or "?"
        st = p.get("status") or "?"
        sender = "?"
        raw = fields.get(key, {})
        if isinstance(raw, dict) and "sender_repo" in raw:
            sender = raw["sender_repo"]
        _p(f"  {i+1}. [{key}]")
        _p(f"     {home} vs {away} | {lg} | {d} | score: {sc} | status: {st} | sender: {sender}")

    # ── Future match:* examples ──
    if future_matches:
        _p(f"\n--- Future match:* examples (5) ---")
        for i, (key, p) in enumerate(sorted(future_matches.items())[:5]):
            home = p.get("home_team") or "?"
            away = p.get("away_team") or "?"
            lg = _extract_league(p) or "?"
            d = _extract_date(p) or "?"
            _p(f"  {i+1}. {home} vs {away} | {lg} | {d}")

    # ── Verification ──
    _p(f"\n{'='*60}")
    _p(f"  VERIFICATION")
    _p(f"{'='*60}")
    _p(f"  history:match:*  = {len(hist_keys)}")
    _p(f"  match:* (past)   = {len(past_matches)}")
    _p(f"  match:* (future) = {len(future_matches)}")
    _p(f"  match:* (unkn)   = {len(unknown_matches)}")
    _p(f"  Duplicates        = {duplicates}")
    _p(f"  LOST              = {len(lost)}")
    _p(f"  history + lost    = {len(hist_keys) + len(lost)}")
    _p(f"  Expected          = 17034")
    _p(f"  Difference        = {17034 - (len(hist_keys) + len(lost))}")

    # ── Sender breakdown for lost ──
    lost_senders = {}
    for key in lost:
        raw = fields.get(key, {})
        if isinstance(raw, dict) and "sender_repo" in raw:
            s = raw["sender_repo"]
        else:
            s = "?"
        lost_senders[s] = lost_senders.get(s, 0) + 1

    _p(f"\n--- Lost by sender ---")
    for s in sorted(lost_senders.keys(), key=lambda x: -lost_senders[x]):
        _p(f"  {s}: {lost_senders[s]}")

    _p(f"\n=== HISTORY COMPLETE ===")

def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return
    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    other = [k for k in fields if not k.startswith("history:match:") and not k.startswith("match:")]
    _p(f"\nTotal: {len(fields)} (history={len(hist)}, match={len(match)}, other={len(other)})")
    _p(f"\n=== DIAGNOSTICS COMPLETE ===")

if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
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
                        try:
                            keys = json.loads(keys)
                        except:
                            keys = [keys]
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
