"""
Диагностика и очистка Redis для Gatekeeper-AI v600-prod.
Работает через Upstash REST API (_execute_upstash_cmd).

Запуск:
  python redis_diagnostics.py              - только диагностика
  python redis_diagnostics.py --history    - детальная статистика по history:match:*
  python redis_diagnostics.py --flush       - мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes  - авто-очистка (для CI/воркера)
  python redis_diagnostics.py --flush --hard --yes - полный DEL (опасно)
"""
import sys
import json
from datetime import datetime, timezone, timedelta

from redis_hub import (
    _execute_upstash_cmd,
    get_all_fields,
    is_redis_available,
    get_circuit_breaker_status,
)
from gatekeeper_hub import get_matches_by_date_range

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
}


def hscan_all(hash_name="GatekeeperAI", batch_size=200):
    """Пагинационное сканирование хэша через HSCAN. Не зависит от get_all_fields()."""
    all_fields = {}
    cursor = "0"
    iterations = 0
    while True:
        iterations += 1
        result = _execute_upstash_cmd(["HSCAN", hash_name, cursor, "COUNT", str(batch_size)])
        if result is None:
            print(f"[HSCAN] Error at cursor={cursor}, iter={iterations}")
            break
        # Upstash REST API возвращает [cursor, [field1, value1, field2, value2, ...]]
        if isinstance(result, list) and len(result) >= 2:
            next_cursor = str(result[0])
            kv_pairs = result[1]
            if isinstance(kv_pairs, list) and len(kv_pairs) >= 2:
                for i in range(0, len(kv_pairs) - 1, 2):
                    field_id = kv_pairs[i]
                    raw_value = kv_pairs[i + 1]
                    if isinstance(raw_value, str):
                        try:
                            all_fields[field_id] = json.loads(raw_value)
                        except (json.JSONDecodeError, TypeError):
                            all_fields[field_id] = raw_value
                    elif isinstance(raw_value, dict):
                        all_fields[field_id] = raw_value
                    else:
                        all_fields[field_id] = raw_value
            if next_cursor == "0" or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            break
    print(f"[HSCAN] Iterations: {iterations}, fields loaded: {len(all_fields)}")
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


def run_diagnostics(flush=False, auto_yes=False, hard=False, history_mode=False):
    print("=" * 60)
    print("[DIAG] GATEKEEPER-AI v600-prod")
    print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    print("=" * 60)

    # 1. Redis check
    print()
    print("[DIAG] Redis...")
    if not is_redis_available():
        print("[DIAG] Redis unavailable (or circuit breaker open).")
        cb = get_circuit_breaker_status()
        print("[DIAG] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
        print("=" * 60)
        return
    print("[DIAG] Redis OK (PING)")

    # 2. Count fields
    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))

    # 3. Load all fields via HSCAN
    all_fields = hscan_all()
    total_fields = len(all_fields)
    print("[DIAG] Loaded via HSCAN: " + str(total_fields))

    if total_fields == 0 and hlen and hlen > 0:
        print("[DIAG] WARNING: HSCAN returned 0 fields but HLEN=" + str(hlen))
        print("[DIAG] Trying get_all_fields() fallback...")
        try:
            all_fields = get_all_fields()
            total_fields = len(all_fields)
            print("[DIAG] Fallback get_all_fields(): " + str(total_fields))
        except Exception as e:
            print("[DIAG] Fallback also failed: " + str(e))

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

    # History stats
    history_seasons = {}
    history_leagues = {}
    history_season_league = {}
    history_samples = []

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
            if history_mode:
                season = value.get("season", "?")
                league = value.get("league_code", value.get("competition", "?"))
                history_seasons[season] = history_seasons.get(season, 0) + 1
                history_leagues[league] = history_leagues.get(league, 0) + 1
                key = str(season) + "_" + str(league)
                history_season_league[key] = history_season_league.get(key, 0) + 1
                if len(history_samples) < 5:
                    history_samples.append({
                        "key": field_id,
                        "home": value.get("home_team", "?"),
                        "away": value.get("away_team", "?"),
                        "date": value.get("date_utc", "?"),
                        "league": value.get("league_code", value.get("competition", "?")),
                        "season": season,
                        "score": value.get("score"),
                    })
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

    # 6b. Football data meta
    fd_meta = all_fields.get("football_data:meta")
    fd_meta_status = "no data"
    if fd_meta and isinstance(fd_meta, dict):
        payload = fd_meta.get("payload", {})
        if payload:
            fd_meta_status = "OK total=" + str(payload.get("total_matches", "?")) + ", uploaded=" + str(payload.get("uploaded", "?")) + ", errors=" + str(payload.get("errors", "?")) + ", leagues=" + str(payload.get("leagues_done", "?"))

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
    print()
    print("--- Redis State ---")
    print("  Total fields:       " + str(total_fields))
    print("  Matches (match:*):  " + str(match_count))
    print("    Future:            " + str(future))
    print("    Past:              " + str(past))
    print("    No date:            " + str(no_date))
    print("  History (history:match:*): " + str(history_count))
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

    print()
    print("--- Football Data Meta ---")
    print("  " + fd_meta_status)

    print()
    print("--- Circuit Breaker ---")
    print("  Open: " + str(cb_status["open"]) + ", Errors: " + str(cb_status["error_count"]) + "/" + str(cb_status["threshold"]))

    print()
    print("--- Cleanup ---")
    print("  Last cleanup at:        " + str(cleanup_at))
    print("  Total deleted:          " + str(cleanup_count))
    print("  Finished matches:       " + str(cleanup_finished))
    print("  Expired (no date):      " + str(cleanup_expired))
    print("  Policy: завершённые + 2 часа (buffer=2h)")

    print()
    print("--- Summary ---")
    print("  Total: " + str(total_fields) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
    if match_count > 0:
        past_pct = round(past / match_count * 100, 1)
        print("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
        if past_pct > 50:
            print("  WARNING: Recommend flush Redis and restart collectors")
    else:
        print("  No live matches in Redis.")

    # 9b. History detail
    if history_mode and history_count > 0:
        print()
        print("=" * 60)
        print("--- History Detail (history:match:*) ---")
        print("=" * 60)

        print()
        print("By Season:")
        for season in sorted(history_seasons.keys()):
            print("  " + str(season) + ": " + str(history_seasons[season]) + " matches")

        print()
        print("By League:")
        for league in sorted(history_leagues.keys(), key=lambda x: -history_leagues[x]):
            print("  " + str(league) + ": " + str(history_leagues[league]) + " matches")

        print()
        print("By Season x League:")
        for key in sorted(history_season_league.keys()):
            print("  " + key + ": " + str(history_season_league[key]))

        print()
        print("Sample matches:")
        for s in history_samples:
            score_str = ""
            if s["score"]:
                score_str = " score=" + str(s["score"])
            print("  " + s["key"])
            print("    " + s["home"] + " vs " + s["away"] + " | " + str(s["league"]) + " | " + str(s["season"]) + " | " + str(s["date"]) + score_str)

        # Verification
        season_sum = sum(history_seasons.values())
        league_sum = sum(history_leagues.values())
        print()
        print("--- Verification ---")
        print("  Season sum:  " + str(season_sum))
        print("  League sum:  " + str(league_sum))
        print("  Total hist:  " + str(history_count))
        if season_sum == history_count and league_sum == history_count:
            print("  Sums match: OK")
        else:
            print("  WARNING: Sums do not match!")
            if season_sum != history_count:
                print("  Season sum (" + str(season_sum) + ") != history_count (" + str(history_count) + ")")
            if league_sum != history_count:
                print("  League sum (" + str(league_sum) + ") != history_count (" + str(history_count) + ")")

        # Discrepancy check
        fd_total = 0
        if fd_meta and isinstance(fd_meta, dict):
            payload = fd_meta.get("payload", {})
            fd_total = payload.get("total_matches", 0)
        if fd_total and fd_total != history_count:
            diff = fd_total - history_count
            print()
            print("--- Discrepancy ---")
            print("  Football-data reported: " + str(fd_total))
            print("  Redis history count:   " + str(history_count))
            print("  Difference:             " + str(diff))
            if diff > 0:
                print("  Likely: " + str(diff) + " duplicates overwritten (same key for different CSV rows)")

    # 10. Flush
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
                print("[FLUSH] No terminal - cancelled (use --yes for CI)")

        if confirm != "y":
            print("[FLUSH] Cancelled.")
        else:
            if hard:
                print("[FLUSH] HARD MODE: DEL GatekeeperAI")
                if history_count > 0:
                    print("[FLUSH] WARNING: This will DELETE ALL data including " + str(history_count) + " history matches!")
                if auto_yes:
                    confirm_hard = "DELETE"
                    print("[FLUSH] Auto-confirm hard mode (--yes)")
                else:
                    try:
                        confirm_hard = input("Type DELETE to confirm: ").strip()
                    except EOFError:
                        confirm_hard = ""
                        print("[FLUSH] No terminal - cancelled (use --yes for CI)")
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
                print("[FLUSH] Plan: matches=" + str(len(match_keys_to_delete)) +
                      ", search_results=" + str(len(search_keys_to_delete)) +
                      ", index_shards=" + str(len(index_keys_to_delete)) +
                      ", preserved=" + str(preserved) +
                      " (incl. " + str(preserved_history) + " history)")
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
                if hlen_after == preserved or hlen_after is None:
                    print("Redis soft-flushed OK")
                else:
                    print("WARNING: remaining fields: " + str(hlen_after))

    print("=" * 60)


def main():
    flush = "--flush" in sys.argv
    auto_yes = "--yes" in sys.argv
    hard = "--hard" in sys.argv
    history_mode = "--history" in sys.argv
    run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard, history_mode=history_mode)


if __name__ == "__main__":
    main()
