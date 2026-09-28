"""
Диагностика и очистка Redis для Gatekeeper-AI v600-prod.
Работает через Upstash REST API (redis_hub._execute_upstash_cmd).

Запуск:
  python redis_diagnostics.py              - только диагностика
  python redis_diagnostics.py --flush       - мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes  - авто-очистка (для CI/воркера)
  python redis_diagnostics.py --flush --hard --yes - полный DEL (опасно)
"""
import sys
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


def run_diagnostics(flush=False, auto_yes=False, hard=False):
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

    # 3. Load all fields
    all_fields = get_all_fields()
    total_fields = len(all_fields)
    print("[DIAG] Loaded via get_all_fields(): " + str(total_fields))

    # 4. Categorize
    match_count = 0
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
            # Шард индекса — проверяем возраст
            dt = _parse_date_utc(value.get("updated_at", ""))
            if dt is None or (now_utc - dt).total_seconds() > 7200:
                index_keys_to_delete.append(field_id)
            continue

        if field_id.startswith("search:results:"):
            # Результаты поиска — удаляем все
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
    if match_count > 0:
        past_pct = round(past / match_count * 100, 1)
        print("  Past matches: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
        if past_pct > 50:
            print("  WARNING: Recommend flush Redis and restart collectors")
    else:
        print("  Redis empty or no matches.")

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
                # Hard flush — DEL entire hash
                print("[FLUSH] HARD MODE: DEL GatekeeperAI")
                # FIX: --yes пропускает второе подтверждение для CI
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
                # Soft flush — selective HDEL
                keys_to_delete = match_keys_to_delete + search_keys_to_delete + index_keys_to_delete
                preserved = total_fields - len(keys_to_delete)
                print("[FLUSH] SOFT MODE: selective HDEL")
                print("[FLUSH] Plan: matches=" + str(len(match_keys_to_delete)) +
                      ", search_results=" + str(len(search_keys_to_delete)) +
                      ", index_shards=" + str(len(index_keys_to_delete)) +
                      ", preserved=" + str(preserved))
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
    run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard)


if __name__ == "__main__":
    main()
