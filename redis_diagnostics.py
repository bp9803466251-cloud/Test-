#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics"""

import sys
import json
import time
import traceback

def _p(msg):
    print(msg, flush=True)

try:
    import redis_hub
    _p("[DEBUG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    import gatekeeper_hub
    _p("[DEBUG] Import gatekeeper_hub: OK")
except Exception as e:
    _p(f"[WARN] Cannot import gatekeeper_hub: {e}")

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
        traceback.print_exc()
        return None

def _unwrap(val):
    """Извлекает payload из обёртки {"version":..., "payload":...}"""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except:
            return val
    if isinstance(val, dict) and "payload" in val:
        return val["payload"]
    return val

def _get_field(data, *keys):
    """Ищет поле по нескольким возможным именам."""
    if not isinstance(data, dict):
        return None
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return None

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

# ─── Modes ───

def run_test():
    _p("=== TEST MODE ===")

    _p("\n--- 1. PING ---")
    r = _exec(["PING"])
    _p(f"  Result: {r!r}")

    _p("\n--- 2. HLEN ---")
    r = _exec(["HLEN", "GatekeeperAI"])
    _p(f"  Result: {r!r}")

    _p("\n--- 3. HKEYS ---")
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
        _p(f"  Raw value type: {type(val).__name__}")
        if isinstance(val, str):
            _p(f"  Raw (first 300 chars): {val[:300]}")
            try:
                parsed = json.loads(val)
                _p(f"  Parsed type: {type(parsed).__name__}")
                if isinstance(parsed, dict):
                    _p(f"  Top-level keys: {list(parsed.keys())}")
                    if "payload" in parsed:
                        payload = parsed["payload"]
                        if isinstance(payload, dict):
                            _p(f"  Payload keys: {list(payload.keys())}")
                            _p(f"  Payload (first 500 chars): {json.dumps(payload, ensure_ascii=False)[:500]}")
            except Exception as e:
                _p(f"  JSON parse error: {e}")

    _p("\n--- 5. Unwrap test (first 3 history keys) ---")
    hist_keys = [k for k in r if k.startswith("history:")]
    for key in hist_keys[:3]:
        val = _exec(["HGET", "GatekeeperAI", key])
        unwrapped = _unwrap(val)
        if isinstance(unwrapped, dict):
            _p(f"  [{key}]")
            _p(f"    season={_get_field(unwrapped, 'season', 'season_id')}")
            _p(f"    league={_get_field(unwrapped, 'league', 'league_id', 'competition')}")
            _p(f"    home={_get_field(unwrapped, 'home_team', 'homeTeam', 'home')}")
            _p(f"    away={_get_field(unwrapped, 'away_team', 'awayTeam', 'away')}")
            _p(f"    date={_get_field(unwrapped, 'date', 'match_date', 'utcDate')}")
        else:
            _p(f"  [{key}] unwrapped type={type(unwrapped).__name__}")

    _p("\n--- 6. is_redis_available ---")
    try:
        avail = redis_hub.is_redis_available()
        _p(f"  Available: {avail}")
    except Exception as e:
        _p(f"  Error: {e}")

    _p("\n--- 7. load_all_fields ---")
    fields = load_all_fields()
    live = [k for k in fields if k.startswith("live:")]
    hist = [k for k in fields if k.startswith("history:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    meta = [k for k in fields if k.startswith("meta")]
    other = [k for k in fields if k not in live + hist + match + meta]
    _p(f"  Total: {len(fields)} (live={len(live)}, history={len(hist)}, match={len(match)}, meta={len(meta)}, other={len(other)})")

    _p("\n--- 8. Unwrap check (first 5 history) ---")
    for key in hist[:5]:
        val = fields[key]
        unwrapped = _unwrap(val)
        if isinstance(unwrapped, dict):
            _p(f"  [{key}] keys={list(unwrapped.keys())[:15]}")
        else:
            _p(f"  [{key}] type={type(unwrapped).__name__}")

    _p("\n=== TEST COMPLETE ===")

def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    hist_keys = [k for k in fields if k.startswith("history:")]
    match_keys = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")

    seasons = {}
    leagues = {}
    season_league = {}
    parsed = 0
    errors = 0

    for key in hist_keys:
        val = fields[key]
        unwrapped = _unwrap(val)

        if not isinstance(unwrapped, dict):
            errors += 1
            continue

        parsed += 1
        season = _get_field(unwrapped, "season", "season_id", "seasonId")
        league = _get_field(unwrapped, "league", "league_id", "leagueId", "competition", "competition_id")

        if isinstance(league, dict):
            league = league.get("name") or league.get("id") or "?"
        if isinstance(season, dict):
            season = season.get("name") or season.get("id") or "?"

        if not season:
            season = "?"
        if not league:
            league = "?"

        seasons[season] = seasons.get(season, 0) + 1
        leagues[league] = leagues.get(league, 0) + 1
        sl = f"{season} | {league}"
        season_league[sl] = season_league.get(sl, 0) + 1

    _p(f"\n--- Parse results ---")
    _p(f"  Parsed: {parsed}")
    _p(f"  Errors: {errors}")

    _p(f"\n--- By season ({len(seasons)} seasons) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    _p(f"\n--- By league ({len(leagues)} leagues, top 30) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x])[:30]:
        _p(f"  {lg}: {leagues[lg]}")

    _p(f"\n--- By season x league (top 30) ---")
    for sl in sorted(season_league.keys(), key=lambda x: -season_league[x])[:30]:
        _p(f"  {sl}: {season_league[sl]}")

    _p(f"\n--- Examples (10 history:match:*) ---")
    for key in hist_keys[:10]:
        val = fields[key]
        unwrapped = _unwrap(val)
        if isinstance(unwrapped, dict):
            home = _get_field(unwrapped, "home_team", "homeTeam", "home")
            away = _get_field(unwrapped, "away_team", "awayTeam", "away")
            season = _get_field(unwrapped, "season", "season_id")
            league = _get_field(unwrapped, "league", "league_id", "competition")
            date = _get_field(unwrapped, "date", "match_date", "utcDate")
            hs = _get_field(unwrapped, "home_score", "homeScore", "score_home", "full_time_home_score")
            as_ = _get_field(unwrapped, "away_score", "awayScore", "score_away", "full_time_away_score")
            _p(f"  [{key}]")
            _p(f"    {home} vs {away} | season={season} | league={league} | date={date} | score: {hs}-{as_}")

    _p(f"\n--- match:* (no history prefix) breakdown ---")
    m_seasons = {}
    m_parsed = 0
    m_errors = 0
    for key in match_keys:
        val = fields[key]
        unwrapped = _unwrap(val)
        if not isinstance(unwrapped, dict):
            m_errors += 1
            continue
        m_parsed += 1
        season = _get_field(unwrapped, "season", "season_id", "seasonId")
        if not season:
            season = "?"
        m_seasons[season] = m_seasons.get(season, 0) + 1

    _p(f"  match:* total: {len(match_keys)} (parsed={m_parsed}, errors={m_errors})")
    _p(f"  match:* by season:")
    for s in sorted(m_seasons.keys()):
        _p(f"    {s}: {m_seasons[s]}")

    _p(f"\n--- match:* examples (5) ---")
    for key in match_keys[:5]:
        val = fields[key]
        unwrapped = _unwrap(val)
        if isinstance(unwrapped, dict):
            home = _get_field(unwrapped, "home_team", "homeTeam", "home")
            away = _get_field(unwrapped, "away_team", "awayTeam", "away")
            season = _get_field(unwrapped, "season", "season_id")
            league = _get_field(unwrapped, "league", "league_id", "competition")
            _p(f"  [{key}]")
            _p(f"    {home} vs {away} | season={season} | league={league}")

    _p(f"\n--- Verification ---")
    _p(f"  history:match:* total: {len(hist_keys)}")
    _p(f"  match:* total:         {len(match_keys)}")
    _p(f"  Combined:             {len(hist_keys) + len(match_keys)}")

    _p(f"\n=== HISTORY COMPLETE ===")

def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return
    live = [k for k in fields if k.startswith("live:")]
    hist = [k for k in fields if k.startswith("history:")]
    match = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    meta = [k for k in fields if k.startswith("meta")]
    other = [k for k in fields if k not in live + hist + match + meta]
    _p(f"\nTotal: {len(fields)} (live={len(live)}, history={len(hist)}, match={len(match)}, meta={len(meta)}, other={len(other)})")
    if live:
        _p(f"\nLive keys sample: {live[:5]}")
    if other:
        _p(f"\nOther keys sample: {other[:10]}")
    _p(f"\n=== DIAGNOSTICS COMPLETE ===")

if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] CWD: {__import__('os').getcwd()}")
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
            hard = "--hard" in sys.argv
            _p(f"Flushing (hard={hard})...")
            keys = _exec(["HKEYS", "GatekeeperAI"])
            if keys:
                live_keys = [k for k in keys if k.startswith("live:")]
                _p(f"Deleting {len(live_keys)} live keys...")
                for k in live_keys:
                    _exec(["HDEL", "GatekeeperAI", k])
                _p("Flush complete.")
        else:
            _p("Use --flush --yes to confirm")
    else:
        run_diagnostics()
