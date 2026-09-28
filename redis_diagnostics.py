"""
Диагностика и очистка Redis для Gatekeeper-AI v600-prod.
Работает через Upstash REST API (_execute_upstash_cmd).

Запуск:
  python redis_diagnostics.py              - только диагностика
  python redis_diagnostics.py --history    - детальная статистика по history
  python redis_diagnostics.py --test       - тест компонентов
  python redis_diagnostics.py --flush       - мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes  - авто-очистка (для CI/воркера)
  python redis_diagnostics.py --flush --hard --yes - полный DEL (опасно)
"""
import sys
import os
import json
import traceback
from datetime import datetime, timezone, timedelta

def _p(*args):
    print(*args)
    sys.stdout.flush()

_p("[DEBUG] Python: " + sys.version)
_p("[DEBUG] CWD: " + os.getcwd())
_p("[DEBUG] Args: " + str(sys.argv))

try:
    from redis_hub import (
        _execute_upstash_cmd,
        get_all_fields,
        is_redis_available,
        get_circuit_breaker_status,
    )
    _p("[DEBUG] Import redis_hub: OK")
except Exception as e:
    _p("[DEBUG] Import redis_hub FAILED: " + str(e))
    traceback.print_exc()
    sys.stdout.flush()
    sys.exit(1)

try:
    from gatekeeper_hub import get_matches_by_date_range
    _p("[DEBUG] Import gatekeeper_hub: OK")
except Exception as e:
    _p("[DEBUG] Import gatekeeper_hub FAILED: " + str(e))
    traceback.print_exc()
    sys.stdout.flush()
    sys.exit(1)

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
}


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


def _safe_json_parse(val):
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return {}
    return {}


def load_all_fields():
    """
    Загружает все поля через HKEYS + HMGET (батчами по 50).
    HSCAN нестабилен в Upstash REST при 20K+ полей.
    """
    _p("[LOAD] Getting HKEYS...")
    keys_raw = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])

    if keys_raw is None:
        _p("[LOAD] HKEYS returned None!")
        return {}

    if isinstance(keys_raw, list):
        keys = keys_raw
    elif isinstance(keys_raw, str):
        try:
            keys = json.loads(keys_raw)
        except:
            keys = [keys_raw]
    else:
        keys = list(keys_raw) if keys_raw else []

    total_keys = len(keys)
    _p("[LOAD] HKEYS: " + str(total_keys) + " keys")

    all_fields = {}
    batch_size = 50
    batches = (total_keys + batch_size - 1) // batch_size

    for i in range(0, total_keys, batch_size):
        batch_num = i // batch_size + 1
        batch_keys = keys[i:i + batch_size]

        if batch_num % 20 == 1 or batch_num == batches:
            _p("[LOAD] Batch " + str(batch_num) + "/" + str(batches) +
               " (keys " + str(i + 1) + "-" + str(min(i + batch_size, total_keys)) + ")")

        vals_raw = _execute_upstash_cmd(["HMGET", "GatekeeperAI"] + batch_keys)

        if vals_raw is None:
            _p("[LOAD] WARNING: HMGET returned None for batch " + str(batch_num))
            continue

        if isinstance(vals_raw, list):
            vals = vals_raw
        elif isinstance(vals_raw, str):
            try:
                vals = json.loads(vals_raw)
            except:
                vals = [vals_raw]
        else:
            vals = list(vals_raw) if vals_raw else []

        for j, key in enumerate(batch_keys):
            if j < len(vals):
                val = vals[j]
                if val is not None:
                    parsed = _safe_json_parse(val)
                    if parsed:
                        all_fields[key] = parsed
                    elif isinstance(val, str):
                        all_fields[key] = val

    _p("[LOAD] Loaded: " + str(len(all_fields)) + " fields")
    return all_fields


def run_test():
    _p("=" * 60)
    _p("[TEST] GATEKEEPER-AI v600-prod")
    _p("=" * 60)
    _p()

    # 1. PING
    _p("[TEST] 1. PING...")
    try:
        result = _execute_upstash_cmd(["PING"])
        _p("[TEST]    PING -> " + repr(result))
    except Exception as e:
        _p("[TEST]    PING FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 2. is_redis_available
    _p("[TEST] 2. is_redis_available()...")
    try:
        result = is_redis_available()
        _p("[TEST]    -> " + repr(result))
    except Exception as e:
        _p("[TEST]    FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 3. HLEN
    _p("[TEST] 3. HLEN...")
    try:
        result = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        _p("[TEST]    HLEN -> " + repr(result))
    except Exception as e:
        _p("[TEST]    HLEN FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 4. HKEYS
    _p("[TEST] 4. HKEYS (first 10)...")
    try:
        result = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        if isinstance(result, list):
            _p("[TEST]    HKEYS count: " + str(len(result)))
            _p("[TEST]    First 10: " + str(result[:10]))
        else:
            _p("[TEST]    HKEYS -> " + repr(result))
    except Exception as e:
        _p("[TEST]    HKEYS FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 5. HGET one field
    _p("[TEST] 5. HGET (first field)...")
    try:
        keys = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        if isinstance(keys, list) and keys:
            first_key = keys[0]
            result = _execute_upstash_cmd(["HGET", "GatekeeperAI", first_key])
            val_type = type(result).__name__
            val_preview = str(result)[:200] if result else "None"
            _p("[TEST]    HGET '" + str(first_key) + "' -> type=" + val_type + " val=" + val_preview)
    except Exception as e:
        _p("[TEST]    HGET FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 6. Circuit breaker
    _p("[TEST] 6. Circuit breaker...")
    try:
        cb = get_circuit_breaker_status()
        _p("[TEST]    -> " + str(cb))
    except Exception as e:
        _p("[TEST]    FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 7. get_matches_by_date_range
    _p("[TEST] 7. get_matches_by_date_range()...")
    try:
        matches = get_matches_by_date_range()
        _p("[TEST]    -> " + str(len(matches)) + " matches")
    except Exception as e:
        _p("[TEST]    FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    # 8. load_all_fields
    _p("[TEST] 8. load_all_fields()...")
    try:
        all_fields = load_all_fields()
        _p("[TEST]    -> " + str(len(all_fields)) + " fields")
        # Подсчёт по типам
        match_count = sum(1 for k in all_fields if k.startswith("match:") and not k.startswith("match:index:"))
        history_count = sum(1 for k in all_fields if k.startswith("history:match:"))
        meta_count = sum(1 for k in all_fields if k.endswith(":meta"))
        other_count = len(all_fields) - match_count - history_count - meta_count
        _p("[TEST]    match:* = " + str(match_count) + ", history:match:* = " + str(history_count) +
           ", meta = " + str(meta_count) + ", other = " + str(other_count))
    except Exception as e:
        _p("[TEST]    FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()

    _p()
    _p("=" * 60)
    _p("[TEST] Done.")


def run_diagnostics(flush=False, auto_yes=False, hard=False):
    _p("=" * 60)
    _p("[DIAG] GATEKEEPER-AI v600-prod")
    _p("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _p("=" * 60)

    # 1. Redis check
    _p()
    _p("[DIAG] Redis...")
    try:
        if not is_redis_available():
            _p("[DIAG] Redis unavailable (or circuit breaker open).")
            cb = get_circuit_breaker_status()
            _p("[DIAG] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
            _p("=" * 60)
            return
        _p("[DIAG] Redis OK (PING)")
    except Exception as e:
        _p("[DIAG] Redis check FAILED: " + str(e))
        traceback.print_exc()
        sys.stdout.flush()
        _p("=" * 60)
        return

    # 2. Count fields
    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    _p("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))

    # 3. Load all fields
    all_fields = load_all_fields()
    total_fields = len(all_fields)
    _p("[DIAG] Loaded: " + str(total_fields))

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
            other_count += 1
            continue

        fid_lower = field_id.lower()

        if fid_lower.endswith(":meta") or any(fid_lower == mk.lower() for mk in META_KEYS.values()):
            meta_count += 1
            continue

        if field_id.startswith("match:index:"):
            dt = _parse_date_utc(value.get("updated_at", ""))
            if dt is None or (now_utc - dt).total_seconds() > 7200:
                index_keys_to_delete.append(field_id)
            continue

        if field_id.startswith("search:results:"):
            search_keys_to_delete.append(field_id)
            continue

        if field_id.startswith("history:match:"):
            history_count += 1
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

    # Football Data Meta
    fd_meta = all_fields.get("football_data:meta", {})
    if not isinstance(fd_meta, dict):
        fd_meta = {}

    # 7. Circuit breaker
    cb_status = get_circuit_breaker_status()

    # 8. Cleanup metrics
    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = {}
    cleanup_at = health.get("last_cleanup_at", "нет данных")
    cleanup_count = health.get("last_cleanup_count", "нет данных")
    cleanup_finished = health.get("last_cleanup_finished", "нет данных")
    cleanup_expired = health.get("last_cleanup_expired", "нет данных")

    # 9. Output
    _p()
    _p("--- Redis State ---")
    _p("  Total fields:       " + str(total_fields))
    _p("  Live matches:       " + str(match_count))
    _p("    Future:            " + str(future))
    _p("    Past:              " + str(past))
    _p("    No date:           " + str(no_date))
    _p("  History matches:    " + str(history_count))
    _p("  With odds:          " + str(with_odds))
    _p("  Meta keys:          " + str(meta_count))
    _p("  Other:              " + str(other_count))
    _p("  Hub get_matches:    " + str(hub_count))

    _p()
    _p("--- Sources ---")
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            _p("  " + str(src) + ": " + str(count))
    else:
        _p("  (none)")

    _p()
    _p("--- Collector Meta ---")
    for name, status in meta_status.items():
        _p("  " + name + ": " + status)

    if fd_meta:
        _p()
        _p("--- Football Data Meta ---")
        for k, v in fd_meta.items():
            _p("  " + str(k) + ": " + str(v))

    _p()
    _p("--- Circuit Breaker ---")
    _p("  Open: " + str(cb_status["open"]) + ", Errors: " + str(cb_status["error_count"]) + "/" + str(cb_status["threshold"]))

    _p()
    _p("--- Cleanup ---")
    _p("  Last cleanup at:        " + str(cleanup_at))
    _p("  Total deleted:          " + str(cleanup_count))
    _p("  Finished matches:       " + str(cleanup_finished))
    _p("  Expired (no date):      " + str(cleanup_expired))
    _p("  Policy: завершённые + 2 часа (buffer=2h)")

    _p()
    _p("--- Summary ---")
    grand_total = match_count + history_count
    _p("  Total: " + str(grand_total) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
    if match_count > 0:
        past_pct = round(past / match_count * 100, 1)
        _p("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
        if past_pct > 50:
            _p("  WARNING: Recommend flush Redis and restart collectors")
    else:
        _p("  No live matches.")

    # 10. Flush
    if flush:
        _p()
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
                    _p("[FLUSH] WARNING: will DELETE ALL data including " + str(history_count) + " history matches!")
                    del_result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
                    if del_result is not None:
                        _p("[FLUSH] DEL GatekeeperAI -> deleted: " + str(del_result))
                        hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                        _p("[FLUSH] Fields after: " + str(hlen_after))
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
                    result = _execute_upstash_cmd(["HDEL", "GatekeeperAI"] + batch)
                    if result is not None:
                        deleted += int(result)
                    else:
                        errors += len(batch)

                hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                _p("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                _p("[FLUSH] Fields after: " + str(hlen_after))
                if hlen_after == preserved or hlen_after is None:
                    _p("Redis soft-flushed OK")
                else:
                    _p("WARNING: remaining fields: " + str(hlen_after))

    _p("=" * 60)


def run_history():
    _p("=" * 60)
    _p("[HISTORY] GATEKEEPER-AI v600-prod")
    _p("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    _p("=" * 60)
    _p()

    # Load all fields
    _p("[HISTORY] Loading all fields...")
    all_fields = load_all_fields()
    total = len(all_fields)
    _p("[HISTORY] Loaded: " + str(total) + " fields")
    _p()

    # Filter history matches
    history_matches = {}
    for key, val in all_fields.items():
        if key.startswith("history:match:"):
            if isinstance(val, dict):
                history_matches[key] = val

    hist_count = len(history_matches)
    _p("[HISTORY] History matches: " + str(hist_count))
    _p()

    if hist_count == 0:
        _p("[HISTORY] No history matches found.")
        _p("=" * 60)
        return

    # Stats by season
    by_season = {}
    by_league = {}
    by_season_league = {}
    no_season = 0
    no_league = 0

    for key, val in history_matches.items():
        season = val.get("season", val.get("season_id", ""))
        league = val.get("league", val.get("league_name", val.get("league_id", "")))

        if not season:
            no_season += 1
            season = "(unknown)"
        if not league:
            no_league += 1
            league = "(unknown)"

        by_season[season] = by_season.get(season, 0) + 1
        by_league[league] = by_league.get(league, 0) + 1
        sl_key = str(season) + " | " + str(league)
        by_season_league[sl_key] = by_season_league.get(sl_key, 0) + 1

    # By season
    _p("--- By Season ---")
    for season, count in sorted(by_season.items(), key=lambda x: -x[1]):
        _p("  " + str(season) + ": " + str(count))
    _p("  (no season: " + str(no_season) + ")")
    _p()

    # By league (top 30)
    _p("--- By League (top 30) ---")
    sorted_leagues = sorted(by_league.items(), key=lambda x: -x[1])
    for league, count in sorted_leagues[:30]:
        _p("  " + str(league) + ": " + str(count))
    if len(sorted_leagues) > 30:
        _p("  ... and " + str(len(sorted_leagues) - 30) + " more")
    _p("  (no league: " + str(no_league) + ")")
    _p()

    # By season x league (top 30)
    _p("--- By Season x League (top 30) ---")
    sorted_sl = sorted(by_season_league.items(), key=lambda x: -x[1])
    for sl, count in sorted_sl[:30]:
        _p("  " + str(sl) + ": " + str(count))
    if len(sorted_sl) > 30:
        _p("  ... and " + str(len(sorted_sl) - 30) + " more")
    _p()

    # Examples (5)
    _p("--- Examples (5) ---")
    for i, (key, val) in enumerate(history_matches.items()):
        if i >= 5:
            break
        home = val.get("home_team", val.get("home", "?"))
        away = val.get("away_team", val.get("away", "?"))
        date = val.get("date_utc", val.get("date", "?"))
        season = val.get("season", "?")
        league = val.get("league", val.get("league_name", "?"))
        score = val.get("score", val.get("ft_score", "?"))
        _p("  " + str(i + 1) + ". " + str(home) + " vs " + str(away) +
           " | " + str(date) + " | " + str(season) + " | " + str(league) + " | score: " + str(score))
    _p()

    # Verification
    _p("--- Verification ---")
    _p("  Total history matches: " + str(hist_count))
    _p("  Sum by season: " + str(sum(by_season.values())))
    _p("  Sum by league: " + str(sum(by_league.values())))
    _p("  Sum by season x league: " + str(sum(by_season_league.values())))
    _p("  Unique seasons: " + str(len(by_season)))
    _p("  Unique leagues: " + str(len(by_league)))
    _p("  Unique season x league: " + str(len(by_season_league)))

    # Check football_data:meta
    fd_meta = all_fields.get("football_data:meta", {})
    if isinstance(fd_meta, dict):
        fd_total = fd_meta.get("total_matches", fd_meta.get("uploaded", 0))
        _p("  Football Data Meta total: " + str(fd_total))
        if fd_total and hist_count:
            diff = int(fd_total) - hist_count
            _p("  Difference: " + str(fd_total) + " - " + str(hist_count) + " = " + str(diff))
            if diff > 0:
                _p("  -> " + str(diff) + " matches missing (possible duplicates or failed writes)")
            elif diff < 0:
                _p("  -> " + str(abs(diff)) + " extra matches (possible key collisions)")

    _p()
    _p("--- Recommendations ---")
    if hist_count > 15000:
        _p("  - History is large (" + str(hist_count) + " matches). Consider archiving old seasons.")
    if no_season > 0:
        _p("  - " + str(no_season) + " matches have no season field.")
    if no_league > 0:
        _p("  - " + str(no_league) + " matches have no league field.")

    _p("=" * 60)


def main():
    args = sys.argv[1:]
    flush = "--flush" in args
    auto_yes = "--yes" in args
    hard = "--hard" in args
    history = "--history" in args
    test = "--test" in args

    if test:
        run_test()
    elif history:
        run_history()
    elif flush:
        run_diagnostics(flush=True, auto_yes=auto_yes, hard=hard)
    else:
        run_diagnostics()


if __name__ == "__main__":
    main()
