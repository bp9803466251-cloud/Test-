#!/usr/bin/env python3
"""
Diagnostics and flush for Gatekeeper-AI v600-prod Redis.
Works via Upstash REST API.

Usage:
  python redis_diagnostics.py              - standard diagnostics
  python redis_diagnostics.py --history      - detailed history stats
  python redis_diagnostics.py --test        - component test
  python redis_diagnostics.py --flush       - soft flush (selective HDEL)
  python redis_diagnostics.py --flush --yes - auto-confirm soft flush
  python redis_diagnostics.py --flush --hard --yes - hard flush (DANGER)
"""
import sys
import traceback
from datetime import datetime, timezone, timedelta

# Flush stdout immediately
def _print(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

_print("[DEBUG] Python:", sys.version)

try:
    from redis_hub import (
        _execute_upstash_cmd,
        get_all_fields,
        is_redis_available,
        get_circuit_breaker_status,
    )
    _print("[DEBUG] Import redis_hub: OK")
except Exception as e:
    _print("[DEBUG] Import redis_hub FAILED:")
    traceback.print_exc()
    sys.exit(1)

try:
    from gatekeeper_hub import get_matches_by_date_range
    _print("[DEBUG] Import gatekeeper_hub: OK")
except Exception as e:
    _print("[DEBUG] Import gatekeeper_hub FAILED:")
    traceback.print_exc()
    sys.exit(1)

import json

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
    "FootballData": "football_data:meta",
}


def _safe_json_loads(value):
    """Try to parse value as JSON, return as-is if not JSON."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _parse_date_utc(raw):
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (ValueError, TypeError):
        pass
    return None


def hscan_all(hash_name="GatekeeperAI", count=200):
    """
    Scan all fields in a hash using HSCAN with pagination.
    Falls back to HKEYS + HMGET if HSCAN fails.
    Returns dict {field_id: parsed_value}.
    """
    result = {}
    
    # Try HSCAN first
    try:
        cursor = "0"
        iterations = 0
        while True:
            resp = _execute_upstash_cmd(["HSCAN", hash_name, cursor, "COUNT", str(count)])
            if resp is None:
                raise RuntimeError("HSCAN returned None")
            
            # Upstash REST returns [cursor, [k1, v1, k2, v2, ...]]
            if isinstance(resp, list) and len(resp) >= 2:
                next_cursor = resp[0]
                kv_pairs = resp[1]
            elif isinstance(resp, dict):
                next_cursor = resp.get("cursor", "0")
                kv_pairs = resp.get("elements", resp.get("result", []))
            else:
                raise RuntimeError("HSCAN unexpected format: " + str(type(resp)))
            
            # Parse key-value pairs
            if isinstance(kv_pairs, list):
                i = 0
                while i + 1 < len(kv_pairs):
                    key = kv_pairs[i]
                    val_raw = kv_pairs[i + 1]
                    val = _safe_json_loads(val_raw)
                    result[key] = val
                    i += 2
            
            iterations += 1
            _print("[HSCAN] iter=" + str(iterations) + " cursor=" + str(next_cursor) + " fields=" + str(len(result)))
            
            if str(next_cursor) == "0":
                break
            cursor = str(next_cursor)
        
        _print("[HSCAN] Done. iterations=" + str(iterations) + " fields=" + str(len(result)))
        return result
    
    except Exception as e:
        _print("[HSCAN] Failed: " + str(e))
        traceback.print_exc()
    
    # Fallback: HKEYS + HMGET
    _print("[FALLBACK] Using HKEYS + HMGET")
    try:
        keys_resp = _execute_upstash_cmd(["HKEYS", hash_name])
        if keys_resp is None:
            _print("[FALLBACK] HKEYS returned None")
            return {}
        
        if isinstance(keys_resp, list):
            keys = keys_resp
        elif isinstance(keys_resp, dict):
            keys = keys_resp.get("result", keys_resp.get("elements", []))
        else:
            keys = []
        
        _print("[FALLBACK] HKEYS returned " + str(len(keys)) + " keys")
        
        batch_size = 50
        for i in range(0, len(keys), batch_size):
            batch = keys[i:i+batch_size]
            vals_resp = _execute_upstash_cmd(["HMGET", hash_name] + batch)
            if vals_resp is None:
                _print("[FALLBACK] HMGET returned None for batch " + str(i))
                continue
            
            if isinstance(vals_resp, list):
                vals = vals_resp
            elif isinstance(vals_resp, dict):
                vals = vals_resp.get("result", vals_resp.get("elements", []))
            else:
                vals = []
            
            for j, key in enumerate(batch):
                if j < len(vals):
                    result[key] = _safe_json_loads(vals[j])
        
        _print("[FALLBACK] Done. fields=" + str(len(result)))
        return result
    
    except Exception as e:
        _print("[FALLBACK] Failed: " + str(e))
        traceback.print_exc()
        return result


def run_test():
    """Test each component individually."""
    _print("")
    _print("=" * 60)
    _print("[TEST] Component test")
    _print("=" * 60)
    _print("")
    
    # 1. PING
    _print("[TEST] 1. PING...")
    try:
        ping_result = _execute_upstash_cmd(["PING"])
        _print("[TEST]    PING result: " + str(ping_result) + " (type=" + str(type(ping_result).__name__) + ")")
    except Exception as e:
        _print("[TEST]    PING FAILED:")
        traceback.print_exc()
    
    # 2. is_redis_available
    _print("[TEST] 2. is_redis_available()...")
    try:
        avail = is_redis_available()
        _print("[TEST]    is_redis_available: " + str(avail) + " (type=" + str(type(avail).__name__) + ")")
    except Exception as e:
        _print("[TEST]    is_redis_available FAILED:")
        traceback.print_exc()
    
    # 3. HLEN
    _print("[TEST] 3. HLEN GatekeeperAI...")
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _print("[TEST]    HLEN: " + str(hlen) + " (type=" + str(type(hlen).__name__) + ")")
    except Exception as e:
        _print("[TEST]    HLEN FAILED:")
        traceback.print_exc()
    
    # 4. HSCAN (small batch)
    _print("[TEST] 4. HSCAN (10 fields)...")
    try:
        scan_result = _execute_upstash_cmd(["HSCAN", "GatekeeperAI", "0", "COUNT", "10"])
        _print("[TEST]    HSCAN type: " + str(type(scan_result).__name__))
        if isinstance(scan_result, list) and len(scan_result) >= 2:
            cursor = scan_result[0]
            pairs = scan_result[1]
            _print("[TEST]    cursor: " + str(cursor))
            _print("[TEST]    pairs count: " + str(len(pairs) if isinstance(pairs, list) else "N/A"))
            if isinstance(pairs, list) and len(pairs) >= 2:
                _print("[TEST]    first key: " + str(pairs[0]))
                _print("[TEST]    first val type: " + str(type(pairs[1]).__name__))
                val_preview = str(pairs[1])[:200]
                _print("[TEST]    first val preview: " + val_preview)
        elif isinstance(scan_result, dict):
            _print("[TEST]    dict keys: " + str(list(scan_result.keys())))
        elif scan_result is None:
            _print("[TEST]    HSCAN returned None!")
        else:
            _print("[TEST]    HSCAN: " + str(scan_result)[:200])
    except Exception as e:
        _print("[TEST]    HSCAN FAILED:")
        traceback.print_exc()
    
    # 5. HKEYS
    _print("[TEST] 5. HKEYS GatekeeperAI...")
    try:
        hkeys_result = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        if isinstance(hkeys_result, list):
            _print("[TEST]    HKEYS count: " + str(len(hkeys_result)))
            for k in hkeys_result[:5]:
                _print("[TEST]      " + str(k))
        elif isinstance(hkeys_result, dict):
            keys_list = hkeys_result.get("result", hkeys_result.get("elements", []))
            _print("[TEST]    HKEYS count: " + str(len(keys_list)))
            for k in keys_list[:5]:
                _print("[TEST]      " + str(k))
        elif hkeys_result is None:
            _print("[TEST]    HKEYS returned None!")
        else:
            _print("[TEST]    HKEYS: " + str(hkeys_result)[:200])
    except Exception as e:
        _print("[TEST]    HKEYS FAILED:")
        traceback.print_exc()
    
    # 6. HGET one field
    _print("[TEST] 6. HGET single field...")
    try:
        hkeys = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        first_key = None
        if isinstance(hkeys, list) and len(hkeys) > 0:
            first_key = hkeys[0]
        elif isinstance(hkeys, dict):
            kl = hkeys.get("result", hkeys.get("elements", []))
            if len(kl) > 0:
                first_key = kl[0]
        
        if first_key:
            hget_result = _execute_upstash_cmd(["HGET", "GatekeeperAI", first_key])
            _print("[TEST]    HGET key=" + str(first_key) + " type=" + str(type(hget_result).__name__))
            val_parsed = _safe_json_loads(hget_result)
            _print("[TEST]    parsed type: " + str(type(val_parsed).__name__))
            if isinstance(val_parsed, dict):
                _print("[TEST]    dict keys: " + str(list(val_parsed.keys())[:10]))
            else:
                _print("[TEST]    preview: " + str(val_parsed)[:200])
        else:
            _print("[TEST]    No keys to HGET")
    except Exception as e:
        _print("[TEST]    HGET FAILED:")
        traceback.print_exc()
    
    # 7. Circuit breaker
    _print("[TEST] 7. Circuit breaker...")
    try:
        cb = get_circuit_breaker_status()
        _print("[TEST]    CB: " + str(cb))
    except Exception as e:
        _print("[TEST]    Circuit breaker FAILED:")
        traceback.print_exc()
    
    # 8. get_matches_by_date_range
    _print("[TEST] 8. get_matches_by_date_range()...")
    try:
        matches = get_matches_by_date_range()
        if isinstance(matches, dict):
            _print("[TEST]    matches count: " + str(len(matches)))
        else:
            _print("[TEST]    type: " + str(type(matches).__name__) + " len: " + str(len(matches) if hasattr(matches, '__len__') else "N/A"))
    except Exception as e:
        _print("[TEST]    get_matches_by_date_range FAILED:")
        traceback.print_exc()
    
    # 9. get_all_fields
    _print("[TEST] 9. get_all_fields()...")
    try:
        all_fields = get_all_fields()
        _print("[TEST]    get_all_fields count: " + str(len(all_fields)) + " (type=" + str(type(all_fields).__name__) + ")")
    except Exception as e:
        _print("[TEST]    get_all_fields FAILED:")
        traceback.print_exc()
    
    # 10. hscan_all
    _print("[TEST] 10. hscan_all()...")
    try:
        scanned = hscan_all()
        _print("[TEST]    hscan_all count: " + str(len(scanned)))
    except Exception as e:
        _print("[TEST]    hscan_all FAILED:")
        traceback.print_exc()
    
    _print("")
    _print("=" * 60)
    _print("[TEST] Done.")
    _print("=" * 60)


def run_history():
    """Detailed history diagnostics."""
    _print("")
    _print("=" * 60)
    _print("[DIAG] GATEKEEPER-AI v600-prod")
    _print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _print("=" * 60)
    _print("")
    
    # 1. Redis check
    _print("[DIAG] Redis...")
    try:
        if not is_redis_available():
            _print("[DIAG] Redis unavailable (or circuit breaker open).")
            try:
                cb = get_circuit_breaker_status()
                _print("[DIAG] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
            except Exception:
                pass
            _print("=" * 60)
            return
        _print("[DIAG] Redis OK (PING)")
    except Exception as e:
        _print("[DIAG] is_redis_available crashed:")
        traceback.print_exc()
        return
    
    # 2. Count fields
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))
    except Exception as e:
        _print("[DIAG] HLEN failed:")
        traceback.print_exc()
        return
    
    # 3. Load all fields via hscan_all
    _print("[DIAG] Loading all fields via hscan_all()...")
    try:
        all_fields = hscan_all()
    except Exception as e:
        _print("[DIAG] hscan_all crashed:")
        traceback.print_exc()
        return
    
    total_fields = len(all_fields)
    _print("[DIAG] Loaded fields: " + str(total_fields))
    
    # 4. Categorize
    live_matches = {}
    history_matches = {}
    meta_keys = {}
    other_keys = {}
    
    for field_id, value in all_fields.items():
        fid_lower = field_id.lower()
        
        if fid_lower.endswith(":meta") or any(fid_lower == mk.lower() for mk in META_KEYS.values()):
            meta_keys[field_id] = value
            continue
        
        if field_id.startswith("history:match:"):
            if isinstance(value, dict):
                history_matches[field_id] = value
            else:
                parsed = _safe_json_loads(value)
                if isinstance(parsed, dict):
                    history_matches[field_id] = parsed
                else:
                    other_keys[field_id] = value
            continue
        
        if field_id.startswith("match:"):
            if isinstance(value, dict):
                live_matches[field_id] = value
            else:
                parsed = _safe_json_loads(value)
                if isinstance(parsed, dict):
                    live_matches[field_id] = parsed
                else:
                    other_keys[field_id] = value
            continue
        
        if field_id.startswith("match:index:") or field_id.startswith("search:results:") or field_id.startswith("system:"):
            other_keys[field_id] = value
            continue
        
        other_keys[field_id] = value
    
    live_count = len(live_matches)
    history_count = len(history_matches)
    
    # 5. History stats
    _print("")
    _print("--- History Stats ---")
    _print("  History matches: " + str(history_count))
    
    # By season
    by_season = {}
    by_league = {}
    by_season_league = {}
    
    for key, m in history_matches.items():
        season = m.get("season", "unknown")
        league = m.get("league", m.get("league_name", "unknown"))
        
        by_season[season] = by_season.get(season, 0) + 1
        by_league[league] = by_league.get(league, 0) + 1
        
        sl = season + " | " + league
        by_season_league[sl] = by_season_league.get(sl, 0) + 1
    
    _print("")
    _print("  By season:")
    for s, c in sorted(by_season.items()):
        _print("    " + str(s) + ": " + str(c))
    
    _print("")
    _print("  By league (top 20):")
    for l, c in sorted(by_league.items(), key=lambda x: -x[1])[:20]:
        _print("    " + str(l) + ": " + str(c))
    
    _print("")
    _print("  By season+league (top 20):")
    for sl, c in sorted(by_season_league.items(), key=lambda x: -x[1])[:20]:
        _print("    " + str(sl) + ": " + str(c))
    
    # Examples
    _print("")
    _print("  Example history matches (first 5):")
    for i, (key, m) in enumerate(history_matches.items()):
        if i >= 5:
            break
        _print("    " + str(key))
        _print("      season=" + str(m.get("season")) + " league=" + str(m.get("league", m.get("league_name"))) + " date=" + str(m.get("date_utc", m.get("date", ""))))
        _print("      home=" + str(m.get("home_team", m.get("home", ""))) + " away=" + str(m.get("away_team", m.get("away", ""))))
    
    # Football Data Meta
    _print("")
    _print("--- Football Data Meta ---")
    fd_meta = None
    for key, val in meta_keys.items():
        if "football_data" in key.lower():
            fd_meta = val if isinstance(val, dict) else _safe_json_loads(val)
            break
    
    if isinstance(fd_meta, dict):
        _print("  last_run: " + str(fd_meta.get("last_run", "N/A")))
        _print("  total_matches: " + str(fd_meta.get("total_matches", "N/A")))
        _print("  uploaded: " + str(fd_meta.get("uploaded", "N/A")))
        _print("  errors: " + str(fd_meta.get("errors", "N/A")))
        _print("  leagues_done: " + str(fd_meta.get("leagues_done", "N/A")))
        
        fd_total = fd_meta.get("total_matches", 0)
        if isinstance(fd_total, (int, float)) and history_count > 0:
            diff = int(fd_total) - history_count
            _print("")
            _print("  Verification:")
            _print("    Reported by loader: " + str(fd_total))
            _print("    Found in Redis:     " + str(history_count))
            _print("    Difference:         " + str(diff))
            if diff > 0:
                _print("    STATUS: " + str(diff) + " matches missing (possible duplicates overwritten)")
            elif diff == 0:
                _print("    STATUS: OK")
            else:
                _print("    STATUS: " + str(abs(diff)) + " extra matches in Redis")
    else:
        _print("  Not found")
    
    # Other meta
    _print("")
    _print("--- Collector Meta ---")
    for name, key in META_KEYS.items():
        if "football" in name.lower():
            continue
        meta_val = meta_keys.get(key)
        if not meta_val:
            meta_val = all_fields.get(key)
        if meta_val:
            parsed = meta_val if isinstance(meta_val, dict) else _safe_json_loads(meta_val)
            if isinstance(parsed, dict):
                last_run = parsed.get("last_run", parsed.get("last_run_at", "N/A"))
                stored = parsed.get("stored_matches", 0)
                err = parsed.get("error_count", parsed.get("errors", 0))
                _print("  " + name + ": OK " + str(last_run)[:19] + " (stored=" + str(stored) + ", errors=" + str(err) + ")")
            else:
                _print("  " + name + ": data (not dict)")
        else:
            _print("  " + name + ": no data")
    
    # Summary
    _print("")
    _print("--- Summary ---")
    _print("  Total fields: " + str(total_fields))
    _print("  Live matches: " + str(live_count))
    _print("  History matches: " + str(history_count))
    _print("  Meta keys: " + str(len(meta_keys)))
    _print("  Other: " + str(len(other_keys)))
    
    _print("=" * 60)


def run_diagnostics(flush=False, auto_yes=False, hard=False):
    """Standard diagnostics."""
    _print("")
    _print("=" * 60)
    _print("[DIAG] GATEKEEPER-AI v600-prod")
    _print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _print("=" * 60)
    _print("")
    
    # 1. Redis check
    _print("[DIAG] Redis...")
    try:
        if not is_redis_available():
            _print("[DIAG] Redis unavailable (or circuit breaker open).")
            try:
                cb = get_circuit_breaker_status()
                _print("[DIAG] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
            except Exception:
                pass
            _print("=" * 60)
            return
        _print("[DIAG] Redis OK (PING)")
    except Exception as e:
        _print("[DIAG] is_redis_available crashed:")
        traceback.print_exc()
        return
    
    # 2. Count fields
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))
    except Exception as e:
        _print("[DIAG] HLEN failed:")
        traceback.print_exc()
        return
    
    # 3. Load all fields
    _print("[DIAG] Loading all fields via hscan_all()...")
    try:
        all_fields = hscan_all()
    except Exception as e:
        _print("[DIAG] hscan_all crashed:")
        traceback.print_exc()
        return
    
    total_fields = len(all_fields)
    _print("[DIAG] Loaded fields: " + str(total_fields))
    
    # 4. Categorize
    match_count = 0
    history_count = 0
    meta_count = 0
    other_count = 0
    future = 0
    past = 0
    no_date = 0
    with_odds = 0
    sources = {}
    now_utc = datetime.now(timezone.utc)
    
    match_keys_to_delete = []
    search_keys_to_delete = []
    index_keys_to_delete = []
    
    for field_id, value in all_fields.items():
        if not isinstance(value, dict):
            parsed = _safe_json_loads(value)
            if isinstance(parsed, dict):
                value = parsed
            else:
                other_count += 1
                continue
        
        fid_lower = field_id.lower()
        
        if fid_lower.endswith(":meta") or any(fid_lower == mk.lower() for mk in META_KEYS.values()):
            meta_count += 1
            continue
        
        if field_id.startswith("history:match:"):
            history_count += 1
            continue
        
        if field_id.startswith("match:index:"):
            dt = _parse_date_utc(value.get("updated_at", ""))
            if dt is None or (now_utc - dt).total_seconds() > 7200:
                index_keys_to_delete.append(field_id)
            continue
        
        if field_id.startswith("search:results:"):
            search_keys_to_delete.append(field_id)
            continue
        
        if field_id.startswith("match:"):
            match_count += 1
            dt = _parse_date_utc(value.get("date_utc", ""))
            if dt is None:
                no_date += 1
            elif dt < now_utc:
                past += 1
                match_keys_to_delete.append(field_id)
            else:
                future += 1
            odds = value.get("odds", {})
            if isinstance(odds, dict) and odds.get("home") and odds["home"] != "-":
                with_odds += 1
            sources_list = value.get("sources", [])
            if isinstance(sources_list, list) and sources_list:
                for s in sources_list:
                    sources[s] = sources.get(s, 0) + 1
            else:
                src = value.get("raw_source") or value.get("source") or "unknown"
                sources[src] = sources.get(src, 0) + 1
            continue
        
        other_count += 1
    
    # 5. Hub matches count
    try:
        matches_dict = get_matches_by_date_range()
        hub_count = len(matches_dict)
    except Exception:
        hub_count = -1
    
    # 6. Meta status
    meta_status = {}
    for name, key in META_KEYS.items():
        meta_val = all_fields.get(key)
        if meta_val and isinstance(meta_val, dict):
            last_run = meta_val.get("last_run", meta_val.get("last_run_at"))
            if last_run:
                stored = meta_val.get("stored_matches", 0)
                err = meta_val.get("error_count", meta_val.get("errors", 0))
                meta_status[name] = "OK " + str(last_run)[:19] + " (stored=" + str(stored) + ", errors=" + str(err) + ")"
            else:
                meta_status[name] = "no data"
        else:
            meta_status[name] = "no data"
    
    # 7. Circuit breaker
    try:
        cb_status = get_circuit_breaker_status()
    except Exception:
        cb_status = {"open": "?", "error_count": "?", "threshold": "?"}
    
    # 8. Cleanup metrics
    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = _safe_json_loads(health) if health else {}
        if not isinstance(health, dict):
            health = {}
    cleanup_at = health.get("last_cleanup_at", "нет данных")
    cleanup_count = health.get("last_cleanup_count", "нет данных")
    cleanup_finished = health.get("last_cleanup_finished", "нет данных")
    cleanup_expired = health.get("last_cleanup_expired", "нет данных")
    
    # 9. Output
    _print("")
    _print("--- Redis State ---")
    _print("  Total fields:       " + str(total_fields))
    _print("  Matches (match:*):  " + str(match_count))
    _print("    Future:            " + str(future))
    _print("    Past:              " + str(past))
    _print("    No date:            " + str(no_date))
    _print("  History matches:     " + str(history_count))
    _print("  With odds:          " + str(with_odds))
    _print("  Meta keys:          " + str(meta_count))
    _print("  Other:              " + str(other_count))
    _print("  Hub get_matches:    " + str(hub_count))
    
    _print("")
    _print("--- Sources ---")
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            _print("  " + str(src) + ": " + str(count))
    else:
        _print("  (none)")
    
    _print("")
    _print("--- Collector Meta ---")
    for name, status in meta_status.items():
        _print("  " + name + ": " + status)
    
    _print("")
    _print("--- Circuit Breaker ---")
    _print("  Open: " + str(cb_status["open"]) + ", Errors: " + str(cb_status["error_count"]) + "/" + str(cb_status["threshold"]))
    
    _print("")
    _print("--- Cleanup ---")
    _print("  Last cleanup at:        " + str(cleanup_at))
    _print("  Total deleted:          " + str(cleanup_count))
    _print("  Finished matches:       " + str(cleanup_finished))
    _print("  Expired (no date):      " + str(cleanup_expired))
    _print("  Policy: completed + 2 hours (buffer=2h)")
    
    _print("")
    _print("--- Summary ---")
    total_matches = match_count + history_count
    if total_matches > 0:
        _print("  Total: " + str(total_matches) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
        if match_count > 0:
            past_pct = round(past / match_count * 100, 1)
            _print("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
            if past_pct > 50:
                _print("  WARNING: Recommend flush Redis and restart collectors")
        else:
            _print("  Redis empty or no live matches.")
    else:
        _print("  Redis empty or no matches.")
    
    # 10. Flush
    if flush:
        _print("")
        if auto_yes:
            confirm = "y"
            _print("[FLUSH] Auto-confirm (--yes)")
        else:
            try:
                confirm = input("Flush Redis? (y/n): ").strip().lower()
            except EOFError:
                confirm = "n"
                _print("[FLUSH] No terminal - cancelled (use --yes for CI)")
        
        if confirm != "y":
            _print("[FLUSH] Cancelled.")
        else:
            if hard:
                _print("[FLUSH] HARD MODE: DEL GatekeeperAI")
                _print("[FLUSH] WARNING: This will DELETE ALL data including " + str(history_count) + " history matches!")
                if auto_yes:
                    confirm_hard = "DELETE"
                    _print("[FLUSH] Auto-confirm hard mode (--yes)")
                else:
                    try:
                        confirm_hard = input("Type DELETE to confirm: ").strip()
                    except EOFError:
                        confirm_hard = ""
                        _print("[FLUSH] No terminal - cancelled (use --yes for CI)")
                if confirm_hard != "DELETE":
                    _print("[FLUSH] Hard flush cancelled.")
                else:
                    _print("[FLUSH] Fields before: " + str(total_fields))
                    del_result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
                    if del_result is not None:
                        _print("[FLUSH] DEL GatekeeperAI -> deleted: " + str(del_result))
                        hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                        _print("[FLUSH] Fields after: " + str(hlen_after))
                    else:
                        _print("ERROR: DEL GatekeeperAI failed")
            else:
                keys_to_delete = match_keys_to_delete + search_keys_to_delete + index_keys_to_delete
                preserved = total_fields - len(keys_to_delete)
                _print("[FLUSH] SOFT MODE: selective HDEL")
                _print("[FLUSH] Plan: matches=" + str(len(match_keys_to_delete)) +
                      ", search_results=" + str(len(search_keys_to_delete)) +
                      ", index_shards=" + str(len(index_keys_to_delete)) +
                      ", preserved=" + str(preserved) + " (incl. " + str(history_count) + " history)")
                _print("[FLUSH] Fields before: " + str(total_fields))
                
                deleted = 0
                errors = 0
                batch_size = 50
                for i in range(0, len(keys_to_delete), batch_size):
                    batch = keys_to_delete[i:i+batch_size]
                    result = _execute_upstash_cmd(["HDEL", "GatekeeperAI"] + batch)
                    if result is not None:
                        deleted += int(result)
                    else:
                        errors += len(batch)
                
                hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                _print("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                _print("[FLUSH] Fields after: " + str(hlen_after))
                if hlen_after == preserved or hlen_after is None:
                    _print("Redis soft-flushed OK")
                else:
                    _print("WARNING: remaining fields: " + str(hlen_after))
    
    _print("=" * 60)


def main():
    _print("[DEBUG] argv: " + str(sys.argv))
    
    test_mode = "--test" in sys.argv
    flush = "--flush" in sys.argv
    auto_yes = "--yes" in sys.argv
    hard = "--hard" in sys.argv
    history = "--history" in sys.argv
    
    if test_mode:
        _print("[DEBUG] Running TEST mode")
        run_test()
    elif history:
        _print("[DEBUG] Running HISTORY mode")
        run_history()
    elif flush:
        _print("[DEBUG] Running FLUSH mode")
        run_diagnostics(flush=True, auto_yes=auto_yes, hard=hard)
    else:
        _print("[DEBUG] Running DIAGNOSTICS mode")
        run_diagnostics()


if __name__ == "__main__":
    main()
