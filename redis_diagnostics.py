"""
Diagnostics and flush for Redis (Gatekeeper-AI v600-prod).
Works through Upstash REST API.

Usage:
  python redis_diagnostics.py              - standard diagnostics
  python redis_diagnostics.py --test       - test all Redis operations
  python redis_diagnostics.py --history    - detailed history stats
  python redis_diagnostics.py --flush       - soft flush (selective HDEL)
  python redis_diagnostics.py --flush --yes - auto-confirm soft flush
  python redis_diagnostics.py --flush --hard --yes - hard flush (DEL all)
"""

import sys
import json
import traceback
from datetime import datetime, timezone, timedelta

# ── Debug: flush stdout ──
def _p(msg):
    print(msg)
    sys.stdout.flush()

# ── Imports ──
_p("[DEBUG] Python: " + sys.version)
_p("[DEBUG] Importing redis_hub...")

try:
    from redis_hub import (
        _execute_upstash_cmd,
        is_redis_available,
        get_circuit_breaker_status,
    )
    _p("[DEBUG] Import redis_hub: OK")
except Exception as e:
    _p("[DEBUG] Import redis_hub FAILED:")
    traceback.print_exc()
    sys.exit(1)

_p("[DEBUG] Importing gatekeeper_hub...")
try:
    from gatekeeper_hub import get_matches_by_date_range
    _p("[DEBUG] Import gatekeeper_hub: OK")
except Exception as e:
    _p("[DEBUG] Import gatekeeper_hub FAILED:")
    traceback.print_exc()
    sys.exit(1)

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
}


# ── Local hscan_all with fallback ──
def hscan_all(hash_name="GatekeeperAI", batch_size=200):
    """Scan all fields in a hash via HSCAN with fallback to HKEYS+HMGET."""
    _p("[HSCAN] Starting scan of " + hash_name + "...")

    # Try HSCAN first
    try:
        cursor = "0"
        all_fields = {}
        iterations = 0

        while True:
            iterations += 1
            result = _execute_upstash_cmd(["HSCAN", hash_name, cursor, "COUNT", str(batch_size)])

            if result is None:
                _p("[HSCAN] Got None from HSCAN, falling back to HKEYS")
                break

            # Upstash REST returns [cursor, [key1, val1, key2, val2, ...]]
            # or {"cursor": "...", "items": [...]} or other formats
            if isinstance(result, list) and len(result) >= 2:
                next_cursor = str(result[0])
                kv_pairs = result[1]
            elif isinstance(result, dict):
                next_cursor = str(result.get("cursor", "0"))
                kv_pairs = result.get("items", result.get("elements", []))
            else:
                _p("[HSCAN] Unexpected format: " + str(type(result)))
                break

            if isinstance(kv_pairs, list):
                i = 0
                while i + 1 < len(kv_pairs):
                    field_name = kv_pairs[i]
                    field_value = kv_pairs[i + 1]
                    if isinstance(field_value, str):
                        try:
                            field_value = json.loads(field_value)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    all_fields[field_name] = field_value
                    i += 2

            _p("[HSCAN] Iter " + str(iterations) + ": cursor=" + next_cursor + ", fields=" + str(len(all_fields)))

            if next_cursor == "0" or next_cursor == cursor:
                break
            cursor = next_cursor

        if len(all_fields) > 0:
            _p("[HSCAN] Done: " + str(iterations) + " iterations, " + str(len(all_fields)) + " fields")
            return all_fields

    except Exception as e:
        _p("[HSCAN] HSCAN failed: " + str(e))
        traceback.print_exc()

    # Fallback: HKEYS + HMGET
    _p("[HSCAN] Falling back to HKEYS + HMGET...")

    try:
        keys_result = _execute_upstash_cmd(["HKEYS", hash_name])
        if keys_result is None:
            _p("[HSCAN] HKEYS returned None")
            return {}

        if isinstance(keys_result, list):
            all_keys = keys_result
        elif isinstance(keys_result, dict):
            all_keys = keys_result.get("result", keys_result.get("keys", []))
        else:
            _p("[HSCAN] HKEYS unexpected format: " + str(type(keys_result)))
            return {}

        _p("[HSCAN] HKEYS returned " + str(len(all_keys)) + " keys")

        all_fields = {}
        for i in range(0, len(all_keys), 50):
            batch = all_keys[i:i+50]
            vals = _execute_upstash_cmd(["HMGET", hash_name] + batch)
            if vals and isinstance(vals, list):
                for j, key in enumerate(batch):
                    if j < len(vals):
                        val = vals[j]
                        if isinstance(val, str):
                            try:
                                val = json.loads(val)
                            except (json.JSONDecodeError, ValueError):
                                pass
                        all_fields[key] = val

        _p("[HSCAN] Fallback done: " + str(len(all_fields)) + " fields")
        return all_fields

    except Exception as e:
        _p("[HSCAN] Fallback failed: " + str(e))
        traceback.print_exc()
        return {}


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


# ── Test mode ──
def run_test():
    _p("")
    _p("=" * 60)
    _p("[TEST] GATEKEEPER-AI v600-prod - COMPONENT TEST")
    _p("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _p("=" * 60)
    _p("")

    # 1. PING
    _p("[TEST] 1. PING...")
    try:
        ping_result = _execute_upstash_cmd(["PING"])
        _p("[TEST]    PING result: " + str(ping_result) + " (type: " + str(type(ping_result).__name__) + ")")
    except Exception:
        _p("[TEST]    PING FAILED:")
        traceback.print_exc()

    # 2. is_redis_available
    _p("")
    _p("[TEST] 2. is_redis_available()...")
    try:
        avail = is_redis_available()
        _p("[TEST]    is_redis_available: " + str(avail))
    except Exception:
        _p("[TEST]    is_redis_available FAILED:")
        traceback.print_exc()

    # 3. HLEN
    _p("")
    _p("[TEST] 3. HLEN GatekeeperAI...")
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _p("[TEST]    HLEN: " + str(hlen) + " (type: " + str(type(hlen).__name__) + ")")
    except Exception:
        _p("[TEST]    HLEN FAILED:")
        traceback.print_exc()

    # 4. HSCAN first batch
    _p("")
    _p("[TEST] 4. HSCAN first batch...")
    try:
        scan_result = _execute_upstash_cmd(["HSCAN", "GatekeeperAI", "0", "COUNT", "10"])
        _p("[TEST]    HSCAN type: " + str(type(scan_result).__name__))
        if scan_result is None:
            _p("[TEST]    HSCAN returned None")
        elif isinstance(scan_result, list):
            _p("[TEST]    HSCAN list length: " + str(len(scan_result)))
            if len(scan_result) >= 1:
                _p("[TEST]    cursor: " + str(scan_result[0]))
            if len(scan_result) >= 2:
                pairs = scan_result[1]
                _p("[TEST]    pairs type: " + str(type(pairs).__name__) + ", length: " + str(len(pairs) if hasattr(pairs, '__len__') else 'N/A'))
                if isinstance(pairs, list) and len(pairs) > 0:
                    _p("[TEST]    first key: " + str(pairs[0]))
        elif isinstance(scan_result, dict):
            _p("[TEST]    HSCAN keys: " + str(list(scan_result.keys())))
        else:
            _p("[TEST]    HSCAN raw: " + str(scan_result)[:500])
    except Exception:
        _p("[TEST]    HSCAN FAILED:")
        traceback.print_exc()

    # 5. HKEYS
    _p("")
    _p("[TEST] 5. HKEYS GatekeeperAI...")
    try:
        keys_result = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        if keys_result is None:
            _p("[TEST]    HKEYS returned None")
        elif isinstance(keys_result, list):
            _p("[TEST]    HKEYS count: " + str(len(keys_result)))
            if len(keys_result) > 0:
                _p("[TEST]    first 5 keys:")
                for k in keys_result[:5]:
                    _p("[TEST]      " + str(k))
        else:
            _p("[TEST]    HKEYS type: " + str(type(keys_result).__name__))
            _p("[TEST]    HKEYS raw: " + str(keys_result)[:500])
    except Exception:
        _p("[TEST]    HKEYS FAILED:")
        traceback.print_exc()

    # 6. HGET one key
    _p("")
    _p("[TEST] 6. HGET single field...")
    try:
        keys_result = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        if keys_result and isinstance(keys_result, list) and len(keys_result) > 0:
            first_key = keys_result[0]
            val = _execute_upstash_cmd(["HGET", "GatekeeperAI", first_key])
            _p("[TEST]    HGET " + str(first_key) + ": type=" + str(type(val).__name__))
            _p("[TEST]    value preview: " + str(val)[:300])
    except Exception:
        _p("[TEST]    HGET FAILED:")
        traceback.print_exc()

    # 7. Circuit breaker
    _p("")
    _p("[TEST] 7. Circuit breaker...")
    try:
        cb = get_circuit_breaker_status()
        _p("[TEST]    CB: open=" + str(cb.get("open")) + ", errors=" + str(cb.get("error_count")) + "/" + str(cb.get("threshold")))
    except Exception:
        _p("[TEST]    Circuit breaker FAILED:")
        traceback.print_exc()

    # 8. get_matches_by_date_range
    _p("")
    _p("[TEST] 8. get_matches_by_date_range()...")
    try:
        matches = get_matches_by_date_range()
        _p("[TEST]    matches count: " + str(len(matches) if matches else 0))
    except Exception:
        _p("[TEST]    get_matches_by_date_range FAILED:")
        traceback.print_exc()

    # 9. Full HSCAN test
    _p("")
    _p("[TEST] 9. Full hscan_all()...")
    try:
        all_fields = hscan_all()
        _p("[TEST]    hscan_all returned: " + str(len(all_fields)) + " fields")
    except Exception:
        _p("[TEST]    hscan_all FAILED:")
        traceback.print_exc()

    _p("")
    _p("[TEST] Done.")
    _p("=" * 60)


# ── History mode ──
def run_history():
    _p("")
    _p("=" * 60)
    _p("[DIAG] GATEKEEPER-AI v600-prod - HISTORY")
    _p("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _p("=" * 60)
    _p("")

    # 1. Redis check
    _p("[DIAG] Redis check...")
    try:
        if not is_redis_available():
            _p("[DIAG] Redis unavailable!")
            return
        _p("[DIAG] Redis OK (PING)")
    except Exception:
        _p("[DIAG] is_redis_available FAILED:")
        traceback.print_exc()
        return

    # 2. HLEN
    _p("")
    _p("[DIAG] HLEN...")
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _p("[DIAG] HLEN: " + str(hlen))
    except Exception:
        _p("[DIAG] HLEN FAILED:")
        traceback.print_exc()
        return

    # 3. Scan all
    _p("")
    _p("[DIAG] Scanning all fields...")
    try:
        all_fields = hscan_all()
        total = len(all_fields)
        _p("[DIAG] Loaded: " + str(total) + " fields")
    except Exception:
        _p("[DIAG] Scan FAILED:")
        traceback.print_exc()
        return

    if total == 0:
        _p("[DIAG] No fields found!")
        return

    # 4. Categorize
    live_matches = {}
    history_matches = {}
    meta_fields = {}
    other = {}

    for field_id, value in all_fields.items():
        fid = str(field_id)

        if fid.startswith("history:match:"):
            if isinstance(value, dict):
                history_matches[fid] = value
            else:
                other[fid] = value
        elif fid.startswith("match:"):
            if isinstance(value, dict):
                live_matches[fid] = value
            else:
                other[fid] = value
        elif fid.endswith(":meta") or fid == "football_data:meta" or fid == "system:health":
            meta_fields[fid] = value
        else:
            other[fid] = value

    _p("")
    _p("--- Summary ---")
    _p("  Total fields:    " + str(total))
    _p("  Live matches:    " + str(len(live_matches)))
    _p("  History matches: " + str(len(history_matches)))
    _p("  Meta fields:     " + str(len(meta_fields)))
    _p("  Other:           " + str(len(other)))

    # 5. History by season
    _p("")
    _p("--- History by Season ---")
    by_season = {}
    for fid, val in history_matches.items():
        season = val.get("season", "unknown")
        if isinstance(season, str) and season:
            by_season[season] = by_season.get(season, 0) + 1
        else:
            by_season["no_season"] = by_season.get("no_season", 0) + 1

    for season, count in sorted(by_season.items(), key=lambda x: -x[1]):
        _p("  " + str(season) + ": " + str(count))

    # 6. History by league
    _p("")
    _p("--- History by League (top 30) ---")
    by_league = {}
    for fid, val in history_matches.items():
        league = val.get("league_code", val.get("league", "unknown"))
        if isinstance(league, str) and league:
            by_league[league] = by_league.get(league, 0) + 1
        else:
            by_league["no_league"] = by_league.get("no_league", 0) + 1

    for league, count in sorted(by_league.items(), key=lambda x: -x[1])[:30]:
        _p("  " + str(league) + ": " + str(count))

    if len(by_league) > 30:
        _p("  ... and " + str(len(by_league) - 30) + " more leagues")

    # 7. Season x League matrix
    _p("")
    _p("--- Season x League (top 20 by count) ---")
    by_sl = {}
    for fid, val in history_matches.items():
        season = val.get("season", "?")
        league = val.get("league_code", val.get("league", "?"))
        key = str(season) + " | " + str(league)
        by_sl[key] = by_sl.get(key, 0) + 1

    for key, count in sorted(by_sl.items(), key=lambda x: -x[1])[:20]:
        _p("  " + key + ": " + str(count))

    # 8. Sample matches
    _p("")
    _p("--- Sample History Matches (first 5) ---")
    for i, (fid, val) in enumerate(history_matches.items()):
        if i >= 5:
            break
        home = val.get("home_team", val.get("home", "?"))
        away = val.get("away_team", val.get("away", "?"))
        date = val.get("date_utc", val.get("date", "?"))
        season = val.get("season", "?")
        league = val.get("league_code", val.get("league", "?"))
        status = val.get("status", "?")
        score = val.get("score", val.get("result", "?"))
        _p("  " + str(i+1) + ". " + str(home) + " vs " + str(away))
        _p("     date=" + str(date) + ", season=" + str(season) + ", league=" + str(league))
        _p("     status=" + str(status) + ", score=" + str(score))
        _p("     key=" + fid)

    # 9. Football Data Meta
    _p("")
    _p("--- Football Data Meta ---")
    fd_meta = all_fields.get("football_data:meta")
    if fd_meta and isinstance(fd_meta, dict):
        _p("  last_run:    " + str(fd_meta.get("last_run", fd_meta.get("timestamp", "?"))))
        _p("  total:       " + str(fd_meta.get("total_matches", "?")))
        _p("  uploaded:    " + str(fd_meta.get("uploaded", "?")))
        _p("  errors:      " + str(fd_meta.get("errors", "?")))
        _p("  leagues:     " + str(fd_meta.get("leagues_done", "?")))
        _p("  version:     " + str(fd_meta.get("version", "?")))
        _p("  sender_repo: " + str(fd_meta.get("sender_repo", "?")))
    else:
        _p("  Not found or not a dict: " + str(type(fd_meta)))

    # 10. Verification
    _p("")
    _p("--- Verification ---")
    fd_total = None
    if fd_meta and isinstance(fd_meta, dict):
        fd_total = fd_meta.get("total_matches")
    actual = len(history_matches)
    _p("  Football Data reported: " + str(fd_total))
    _p("  Actual in Redis:         " + str(actual))
    if fd_total:
        diff = int(fd_total) - actual
        _p("  Difference:              " + str(diff))
        if diff > 0:
            _p("  Status: " + str(diff) + " matches missing (possible duplicates or key collisions)")
        elif diff < 0:
            _p("  Status: " + str(abs(diff)) + " extra matches (possible duplicate keys or reruns)")
        else:
            _p("  Status: OK - exact match")

    _p("")
    _p("--- Circuit Breaker ---")
    try:
        cb = get_circuit_breaker_status()
        _p("  Open: " + str(cb.get("open")) + ", Errors: " + str(cb.get("error_count")) + "/" + str(cb.get("threshold")))
    except Exception:
        _p("  Failed to get CB status")
        traceback.print_exc()

    _p("")
    _p("=" * 60)


# ── Standard diagnostics ──
def run_diagnostics(flush=False, auto_yes=False, hard=False):
    _p("")
    _p("=" * 60)
    _p("[DIAG] GATEKEEPER-AI v600-prod")
    _p("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _p("=" * 60)
    _p("")

    # 1. Redis check
    _p("[DIAG] Redis check...")
    try:
        if not is_redis_available():
            _p("[DIAG] Redis unavailable (or circuit breaker open).")
            try:
                cb = get_circuit_breaker_status()
                _p("[DIAG] Circuit Breaker: open=" + str(cb.get("open")) + ", errors=" + str(cb.get("error_count")) + "/" + str(cb.get("threshold")))
            except Exception:
                pass
            _p("=" * 60)
            return
        _p("[DIAG] Redis OK (PING)")
    except Exception:
        _p("[DIAG] is_redis_available FAILED:")
        traceback.print_exc()
        return

    # 2. Count fields
    try:
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _p("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))
    except Exception:
        _p("[DIAG] HLEN FAILED:")
        traceback.print_exc()
        hlen = -1

    # 3. Scan all
    _p("")
    _p("[DIAG] Scanning all fields...")
    try:
        all_fields = hscan_all()
        total_fields = len(all_fields)
        _p("[DIAG] Loaded: " + str(total_fields) + " fields")
    except Exception:
        _p("[DIAG] Scan FAILED:")
        traceback.print_exc()
        return

    if total_fields == 0:
        _p("[DIAG] No fields found!")
        _p("=" * 60)
        return

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
        fid = str(field_id)

        if not isinstance(value, dict):
            other_count += 1
            continue

        fid_lower = fid.lower()

        if fid_lower.endswith(":meta") or fid == "system:health" or fid == "football_data:meta":
            meta_count += 1
            continue

        if fid.startswith("match:index:"):
            dt = _parse_date_utc(value.get("updated_at", ""))
            if dt is None or (now_utc - dt).total_seconds() > 7200:
                index_keys_to_delete.append(fid)
            continue

        if fid.startswith("search:results:"):
            search_keys_to_delete.append(fid)
            continue

        if fid.startswith("history:match:"):
            history_count += 1
            continue

        if fid.startswith("match:"):
            match_count += 1
            dt = _parse_date_utc(value.get("date_utc", ""))
            if dt is None:
                no_date += 1
            elif dt < now_utc:
                past += 1
                match_keys_to_delete.append(fid)
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

    # 5. Hub matches
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

    # 7. Football Data Meta
    fd_meta = all_fields.get("football_data:meta")
    fd_status = "no data"
    if fd_meta and isinstance(fd_meta, dict):
        fd_status = "OK " + str(fd_meta.get("last_run", fd_meta.get("timestamp", "?")))[:19]
        fd_status += " (total=" + str(fd_meta.get("total_matches", "?"))
        fd_status += ", uploaded=" + str(fd_meta.get("uploaded", "?"))
        fd_status += ", errors=" + str(fd_meta.get("errors", "?"))
        fd_status += ", leagues=" + str(fd_meta.get("leagues_done", "?")) + ")"

    # 8. Circuit breaker
    cb_status = {}
    try:
        cb_status = get_circuit_breaker_status()
    except Exception:
        cb_status = {"open": "?", "error_count": "?", "threshold": "?"}

    # 9. Cleanup metrics
    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = {}
    cleanup_at = health.get("last_cleanup_at", "нет данных")
    cleanup_count = health.get("last_cleanup_count", "нет данных")
    cleanup_finished = health.get("last_cleanup_finished", "нет данных")
    cleanup_expired = health.get("last_cleanup_expired", "нет данных")

    # 10. Output
    _p("")
    _p("--- Redis State ---")
    _p("  Total fields:       " + str(total_fields))
    _p("  Matches (match:*):  " + str(match_count))
    _p("    Future:            " + str(future))
    _p("    Past:              " + str(past))
    _p("    No date:           " + str(no_date))
    _p("  History:            " + str(history_count))
    _p("  With odds:          " + str(with_odds))
    _p("  Meta keys:          " + str(meta_count))
    _p("  Other:              " + str(other_count))
    _p("  Hub get_matches:    " + str(hub_count))

    _p("")
    _p("--- Sources ---")
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            _p("  " + str(src) + ": " + str(count))
    else:
        _p("  (none)")

    _p("")
    _p("--- Collector Meta ---")
    for name, status in meta_status.items():
        _p("  " + name + ": " + status)

    _p("")
    _p("--- Football Data Meta ---")
    _p("  " + fd_status)

    _p("")
    _p("--- Circuit Breaker ---")
    _p("  Open: " + str(cb_status.get("open")) + ", Errors: " + str(cb_status.get("error_count")) + "/" + str(cb_status.get("threshold")))

    _p("")
    _p("--- Cleanup ---")
    _p("  Last cleanup at:        " + str(cleanup_at))
    _p("  Total deleted:          " + str(cleanup_count))
    _p("  Finished matches:       " + str(cleanup_finished))
    _p("  Expired (no date):      " + str(cleanup_expired))
    _p("  Policy: завершённые + 2 часа (buffer=2h)")

    _p("")
    _p("--- Summary ---")
    total_matches = match_count + history_count
    _p("  Total: " + str(total_matches) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
    if match_count > 0:
        past_pct = round(past / match_count * 100, 1)
        _p("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
        if past_pct > 50:
            _p("  WARNING: Recommend flush Redis and restart collectors")
    else:
        _p("  No live matches in Redis.")

    # 11. Flush
    if flush:
        _p("")
        if auto_yes:
            confirm = "y"
            _p("[FLUSH] Auto-confirm (--yes)")
        else:
            try:
                confirm = input("Flush Redis? (y/n): ").strip().lower()
            except EOFError:
                confirm = "n"
                _p("[FLUSH] No terminal - cancelled (use --yes for CI)")

        if confirm != "y":
            _p("[FLUSH] Cancelled.")
        else:
            if hard:
                _p("[FLUSH] HARD MODE: DEL GatekeeperAI")
                _p("[FLUSH] WARNING: This will DELETE ALL data including " + str(history_count) + " history matches!")
                if auto_yes:
                    confirm_hard = "DELETE"
                    _p("[FLUSH] Auto-confirm hard mode (--yes)")
                else:
                    try:
                        confirm_hard = input("Type DELETE to confirm: ").strip()
                    except EOFError:
                        confirm_hard = ""
                        _p("[FLUSH] No terminal - cancelled (use --yes for CI)")
                if confirm_hard != "DELETE":
                    _p("[FLUSH] Hard flush cancelled.")
                else:
                    _p("[FLUSH] Fields before: " + str(total_fields))
                    del_result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
                    if del_result is not None:
                        _p("[FLUSH] DEL GatekeeperAI -> deleted: " + str(del_result))
                        try:
                            hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                            _p("[FLUSH] Fields after: " + str(hlen_after))
                        except Exception:
                            _p("[FLUSH] Could not get HLEN after")
                    else:
                        _p("ERROR: DEL GatekeeperAI failed")
            else:
                keys_to_delete = match_keys_to_delete + search_keys_to_delete + index_keys_to_delete
                preserved = total_fields - len(keys_to_delete)
                _p("[FLUSH] SOFT MODE: selective HDEL")
                _p("[FLUSH] Plan: matches=" + str(len(match_keys_to_delete)) +
                      ", search_results=" + str(len(search_keys_to_delete)) +
                      ", index_shards=" + str(len(index_keys_to_delete)) +
                      ", preserved=" + str(preserved) + " (incl. " + str(history_count) + " history)")
                _p("[FLUSH] Fields before: " + str(total_fields))

                deleted = 0
                errors = 0
                batch_size = 50
                for i in range(0, len(keys_to_delete), batch_size):
                    batch = keys_to_delete[i:i+batch_size]
                    try:
                        result = _execute_upstash_cmd(["HDEL", "GatekeeperAI"] + batch)
                        if result is not None:
                            deleted += int(result)
                        else:
                            errors += len(batch)
                    except Exception:
                        errors += len(batch)

                try:
                    hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                    _p("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                    _p("[FLUSH] Fields after: " + str(hlen_after))
                except Exception:
                    _p("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                    _p("[FLUSH] Could not get HLEN after")

    _p("=" * 60)


# ── Main ──
def main():
    flush = "--flush" in sys.argv
    auto_yes = "--yes" in sys.argv
    hard = "--hard" in sys.argv
    test = "--test" in sys.argv
    history = "--history" in sys.argv

    if test:
        run_test()
    elif history:
        run_history()
    else:
        run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard)


if __name__ == "__main__":
    main()
