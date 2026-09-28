"""
Diagnostics and flush for Gatekeeper-AI v600-prod.
Works with Upstash REST API directly.
"""
import sys
import json
import traceback
from datetime import datetime, timezone, timedelta

print("[DEBUG] Python: " + sys.version)

try:
    from redis_hub import _execute_upstash_cmd, is_redis_available, get_circuit_breaker_status
    print("[DEBUG] Import redis_hub: OK")
except Exception as e:
    print("[DEBUG] Import redis_hub FAILED: " + str(e))
    traceback.print_exc()
    sys.exit(1)

try:
    from gatekeeper_hub import get_matches_by_date_range
    print("[DEBUG] Import gatekeeper_hub: OK")
except Exception as e:
    print("[DEBUG] Import gatekeeper_hub FAILED: " + str(e))
    traceback.print_exc()
    get_matches_by_date_range = None

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
}


def hscan_all():
    """Scan all fields in GatekeeperAI hash using HSCAN with pagination."""
    all_fields = {}
    cursor = "0"
    iteration = 0
    
    while True:
        iteration += 1
        print("[DEBUG] HSCAN iteration " + str(iteration) + ", cursor=" + str(cursor))
        result = _execute_upstash_cmd(["HSCAN", "GatekeeperAI", cursor, "COUNT", "200"])
        
        if result is None:
            print("[DEBUG] HSCAN returned None, trying fallback HKEYS")
            return hkeys_fallback()
        
        # Upstash returns [cursor, [k1,v1,k2,v2,...]]
        if isinstance(result, list) and len(result) >= 2:
            next_cursor = str(result[0])
            kv_pairs = result[1]
            
            if isinstance(kv_pairs, list):
                i = 0
                while i + 1 < len(kv_pairs):
                    key = kv_pairs[i]
                    val_raw = kv_pairs[i + 1]
                    try:
                        if isinstance(val_raw, str):
                            val = json.loads(val_raw)
                        else:
                            val = val_raw
                    except (json.JSONDecodeError, TypeError):
                        val = val_raw
                    all_fields[key] = val
                    i += 2
            elif isinstance(kv_pairs, dict):
                for k, v in kv_pairs.items():
                    try:
                        if isinstance(v, str):
                            all_fields[k] = json.loads(v)
                        else:
                            all_fields[k] = v
                    except (json.JSONDecodeError, TypeError):
                        all_fields[k] = v
        elif isinstance(result, dict):
            next_cursor = str(result.get("cursor", result.get("0", "0")))
            kv_pairs = result.get("result", result.get("data", []))
            if isinstance(kv_pairs, list):
                i = 0
                while i + 1 < len(kv_pairs):
                    key = kv_pairs[i]
                    val_raw = kv_pairs[i + 1]
                    try:
                        if isinstance(val_raw, str):
                            val = json.loads(val_raw)
                        else:
                            val = val_raw
                    except (json.JSONDecodeError, TypeError):
                        val = val_raw
                    all_fields[key] = val
                    i += 2
        else:
            print("[DEBUG] HSCAN unexpected format: " + str(type(result)))
            print("[DEBUG] HSCAN raw: " + str(result)[:500])
            return hkeys_fallback()
        
        if next_cursor == "0" or next_cursor == cursor:
            break
        cursor = next_cursor
    
    print("[HSCAN] Iterations: " + str(iteration) + ", fields loaded: " + str(len(all_fields)))
    return all_fields


def hkeys_fallback():
    """Fallback: HKEYS + HMGET in batches."""
    print("[DEBUG] Using HKEYS fallback")
    keys = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
    if keys is None:
        print("[ERROR] HKEYS returned None")
        return {}
    
    if not isinstance(keys, list):
        print("[DEBUG] HKEYS format: " + str(type(keys)))
        keys = list(keys) if hasattr(keys, '__iter__') else []
    
    print("[DEBUG] HKEYS: " + str(len(keys)) + " keys")
    
    all_fields = {}
    batch_size = 50
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i+batch_size]
        vals = _execute_upstash_cmd(["HMGET", "GatekeeperAI"] + batch)
        if vals and isinstance(vals, list):
            for j, key in enumerate(batch):
                if j < len(vals):
                    try:
                        if isinstance(vals[j], str):
                            all_fields[key] = json.loads(vals[j])
                        else:
                            all_fields[key] = vals[j]
                    except (json.JSONDecodeError, TypeError):
                        all_fields[key] = vals[j]
    
    print("[HKEYS] Fields loaded: " + str(len(all_fields)))
    return all_fields


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


def run_test():
    """Quick test of imports and Redis connectivity."""
    print("=" * 60)
    print("[TEST] Gatekeeper-AI connectivity test")
    print("=" * 60)
    
    print()
    print("[TEST] Redis PING...")
    try:
        ping = _execute_upstash_cmd(["PING"])
        print("[TEST] PING result: " + str(ping))
    except Exception as e:
        print("[TEST] PING failed: " + str(e))
        traceback.print_exc()
        return
    
    print()
    print("[TEST] HLEN...")
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        print("[TEST] HLEN result: " + str(hlen))
    except Exception as e:
        print("[TEST] HLEN failed: " + str(e))
        traceback.print_exc()
        return
    
    print()
    print("[TEST] HSCAN test (COUNT=5)...")
    try:
        scan = _execute_upstash_cmd(["HSCAN", "GatekeeperAI", "0", "COUNT", "5"])
        print("[TEST] HSCAN type: " + str(type(scan)))
        print("[TEST] HSCAN raw: " + str(scan)[:500])
    except Exception as e:
        print("[TEST] HSCAN failed: " + str(e))
        traceback.print_exc()
    
    print()
    print("[TEST] HKEYS test (first 5)...")
    try:
        keys = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        print("[TEST] HKEYS type: " + str(type(keys)))
        if isinstance(keys, list):
            print("[TEST] HKEYS count: " + str(len(keys)))
            for k in keys[:5]:
                print("[TEST]   key: " + str(k))
    except Exception as e:
        print("[TEST] HKEYS failed: " + str(e))
        traceback.print_exc()
    
    print("=" * 60)


def run_history():
    """Detailed history diagnostics."""
    print("=" * 60)
    print("[DIAG] GATEKEEPER-AI v600-prod -- HISTORY")
    print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    print("=" * 60)
    
    # Redis check
    print()
    print("[DEBUG] Redis PING...")
    try:
        ping = _execute_upstash_cmd(["PING"])
        print("[DEBUG] PING: " + str(ping))
    except Exception as e:
        print("[ERROR] PING failed: " + str(e))
        traceback.print_exc()
        return
    
    # HLEN
    print("[DEBUG] HLEN...")
    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    print("[DEBUG] HLEN: " + str(hlen))
    
    # Scan all
    print("[DEBUG] Scanning all fields...")
    all_fields = hscan_all()
    total = len(all_fields)
    print("[DEBUG] Total fields: " + str(total))
    
    # Categorize
    live_matches = {}
    history_matches = {}
    meta_keys = {}
    other = {}
    
    for key, val in all_fields.items():
        if not isinstance(val, dict):
            other[key] = val
            continue
        
        if key.startswith("history:match:"):
            history_matches[key] = val
        elif key.startswith("match:"):
            live_matches[key] = val
        elif key.endswith(":meta") or key.startswith("system:"):
            meta_keys[key] = val
        elif key.startswith("search:"):
            other[key] = val
        else:
            other[key] = val
    
    print()
    print("--- Overview ---")
    print("  Total fields:     " + str(total))
    print("  Live matches:      " + str(len(live_matches)))
    print("  History matches:   " + str(len(history_matches)))
    print("  Meta/system keys:  " + str(len(meta_keys)))
    print("  Other:             " + str(len(other)))
    
    # History by season
    by_season = {}
    by_league = {}
    by_season_league = {}
    
    for key, val in history_matches.items():
        season = val.get("season", "unknown")
        league = val.get("division", val.get("league", "unknown"))
        
        by_season[season] = by_season.get(season, 0) + 1
        by_league[league] = by_league.get(league, 0) + 1
        sl = season + " | " + league
        by_season_league[sl] = by_season_league.get(sl, 0) + 1
    
    print()
    print("--- History by Season ---")
    for s in sorted(by_season.keys()):
        print("  " + str(s) + ": " + str(by_season[s]))
    print("  TOTAL: " + str(sum(by_season.values())))
    
    print()
    print("--- History by League (top 20) ---")
    for lg, cnt in sorted(by_league.items(), key=lambda x: -x[1])[:20]:
        print("  " + str(lg) + ": " + str(cnt))
    print("  Total leagues: " + str(len(by_league)))
    
    print()
    print("--- History by Season x League (top 30) ---")
    for sl, cnt in sorted(by_season_league.items(), key=lambda x: -x[1])[:30]:
        print("  " + str(sl) + ": " + str(cnt))
    print("  Total combos: " + str(len(by_season_league)))
    
    # Examples
    print()
    print("--- Sample History Matches (5) ---")
    for i, (key, val) in enumerate(history_matches.items()):
        if i >= 5:
            break
        print("  Key: " + str(key))
        print("  Date: " + str(val.get("date_utc", val.get("date", "?"))))
        print("  Home: " + str(val.get("home_team", val.get("home", "?"))))
        print("  Away: " + str(val.get("away_team", val.get("away", "?"))))
        print("  Score: " + str(val.get("fthg", "?")) + "-" + str(val.get("ftag", "?")))
        print("  Season: " + str(val.get("season", "?")))
        print("  League: " + str(val.get("division", val.get("league", "?"))))
        print()
    
    # Football data meta
    fd_meta = meta_keys.get("football_data:meta")
    if fd_meta:
        print("--- Football Data Meta ---")
        if isinstance(fd_meta, dict):
            for k, v in fd_meta.items():
                print("  " + str(k) + ": " + str(v))
        else:
            print("  " + str(fd_meta)[:200])
    else:
        print("--- Football Data Meta: not found ---")
    
    # Verification
    print()
    print("--- Verification ---")
    fd_total = 0
    if fd_meta and isinstance(fd_meta, dict):
        fd_total = fd_meta.get("total_matches", fd_meta.get("uploaded", 0))
    redis_total = len(history_matches)
    diff = fd_total - redis_total
    print("  Loader reported: " + str(fd_total))
    print("  Redis contains:  " + str(redis_total))
    print("  Difference:      " + str(diff))
    if diff > 0:
        print("  Status: " + str(diff) + " matches missing (likely duplicates overwritten)")
    elif diff == 0:
        print("  Status: exact match")
    else:
        print("  Status: Redis has " + str(abs(diff)) + " extra matches")
    
    print("=" * 60)


def run_diagnostics(flush=False, auto_yes=False, hard=False):
    print("=" * 60)
    print("[DIAG] GATEKEEPER-AI v600-prod")
    print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    print("=" * 60)
    
    print()
    print("[DEBUG] Redis PING...")
    try:
        if not is_redis_available():
            print("[DIAG] Redis unavailable (or circuit breaker open).")
            cb = get_circuit_breaker_status()
            print("[DIAG] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
            print("=" * 60)
            return
        print("[DIAG] Redis OK (PING)")
    except Exception as e:
        print("[ERROR] Redis check failed: " + str(e))
        traceback.print_exc()
        return
    
    print("[DEBUG] HLEN...")
    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))
    
    print("[DEBUG] Scanning all fields...")
    all_fields = hscan_all()
    total_fields = len(all_fields)
    print("[DIAG] Fields loaded: " + str(total_fields))
    
    # Categorize
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
    
    # Hub matches
    try:
        if get_matches_by_date_range:
            matches_dict = get_matches_by_date_range()
            hub_count = len(matches_dict)
        else:
            hub_count = -1
    except Exception as e:
        print("[DEBUG] Hub get_matches error: " + str(e))
        hub_count = -1
    
    # Meta status
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
    
    # Football data meta
    fd_meta = all_fields.get("football_data:meta")
    fd_status = "no data"
    if fd_meta and isinstance(fd_meta, dict):
        fd_status = "OK " + str(fd_meta.get("last_run", "?"))[:19] + " (matches=" + str(fd_meta.get("total_matches", fd_meta.get("uploaded", 0))) + ", errors=" + str(fd_meta.get("errors", 0)) + ")"
    
    # Circuit breaker
    cb_status = get_circuit_breaker_status()
    
    # Cleanup
    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = {}
    cleanup_at = health.get("last_cleanup_at", "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445")
    cleanup_count = health.get("last_cleanup_count", "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445")
    cleanup_finished = health.get("last_cleanup_finished", "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445")
    cleanup_expired = health.get("last_cleanup_expired", "\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445")
    
    # Output
    print()
    print("--- Redis State ---")
    print("  Total fields:       " + str(total_fields))
    print("  Live matches:       " + str(match_count))
    print("  History matches:   " + str(history_count))
    print("    Future:           " + str(future))
    print("    Past:             " + str(past))
    print("    No date:          " + str(no_date))
    print("  With odds:          " + str(with_odds))
    print("  Meta keys:          " + str(meta_count))
    print("  Other:              " + str(other_count))
    print("  Hub get_matches:    " + str(hub_count))
    
    print()
    print("--- Sources ---")
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            print("  " + str(src) + ": " + str(count))
    else:
        print("  (none)")
    
    print()
    print("--- Collector Meta ---")
    for name, status in meta_status.items():
        print("  " + name + ": " + status)
    print("  FootballData: " + fd_status)
    
    print()
    print("--- Circuit Breaker ---")
    print("  Open: " + str(cb_status["open"]) + ", Errors: " + str(cb_status["error_count"]) + "/" + str(cb_status["threshold"]))
    
    print()
    print("--- Cleanup ---")
    print("  Last cleanup at:        " + str(cleanup_at))
    print("  Total deleted:          " + str(cleanup_count))
    print("  Finished matches:       " + str(cleanup_finished))
    print("  Expired (no date):      " + str(cleanup_expired))
    print("  Policy: finished + 2h (buffer=2h)")
    
    print()
    print("--- Summary ---")
    grand_total = match_count + history_count
    if grand_total > 0:
        print("  Total: " + str(grand_total) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
        if match_count > 0:
            past_pct = round(past / match_count * 100, 1)
            print("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
            if past_pct > 50:
                print("  WARNING: Recommend flush Redis and restart collectors")
    else:
        print("  Redis empty or no matches.")
    
    # Flush
    if flush:
        print()
        if auto_yes:
            confirm = "y"
            print("[FLUSH] Auto-confirm (--yes)")
        else:
            try:
                confirm = input("Flush Redis? (y/n): ").strip().lower()
            except EOFError:
                confirm = "n"
                print("[FLUSH] No terminal - cancelled")
        
        if confirm != "y":
            print("[FLUSH] Cancelled.")
        else:
            if hard:
                print("[FLUSH] HARD MODE: DEL GatekeeperAI")
                print("[FLUSH] WARNING: will DELETE ALL data including " + str(history_count) + " history matches!")
                if auto_yes:
                    confirm_hard = "DELETE"
                    print("[FLUSH] Auto-confirm hard mode (--yes)")
                else:
                    try:
                        confirm_hard = input("Type DELETE to confirm: ").strip()
                    except EOFError:
                        confirm_hard = ""
                        print("[FLUSH] No terminal - cancelled")
                if confirm_hard != "DELETE":
                    print("[FLUSH] Hard flush cancelled.")
                else:
                    print("[FLUSH] Fields before: " + str(total_fields))
                    del_result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
                    if del_result is not None:
                        print("[FLUSH] DEL GatekeeperAI -> deleted: " + str(del_result))
                        hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                        print("[FLUSH] Fields after: " + str(hlen_after))
                    else:
                        print("ERROR: DEL GatekeeperAI failed")
            else:
                keys_to_delete = match_keys_to_delete + search_keys_to_delete + index_keys_to_delete
                preserved = total_fields - len(keys_to_delete)
                preserved_history = history_count
                print("[FLUSH] SOFT MODE: selective HDEL")
                print("[FLUSH] Plan: live_past=" + str(len(match_keys_to_delete)) +
                      ", search=" + str(len(search_keys_to_delete)) +
                      ", index=" + str(len(index_keys_to_delete)) +
                      ", preserved=" + str(preserved) + " (incl. " + str(preserved_history) + " history)")
                print("[FLUSH] Fields before: " + str(total_fields))
                
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
                print("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                print("[FLUSH] Fields after: " + str(hlen_after))
                if hlen_after is not None and hlen_after == preserved:
                    print("Redis soft-flushed OK")
                elif hlen_after is not None:
                    print("WARNING: remaining fields: " + str(hlen_after))
    
    print("=" * 60)


def main():
    args = sys.argv[1:]
    
    if "--test" in args:
        run_test()
        return
    
    if "--history" in args:
        run_history()
        return
    
    flush = "--flush" in args
    auto_yes = "--yes" in args
    hard = "--hard" in args
    run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard)


if __name__ == "__main__":
    main()
