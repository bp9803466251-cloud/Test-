"""
Диагностика и очистка Redis для Gatekeeper-AI v600-prod.
v2: Поддержка больших объёмов (20K+ полей) через HSCAN.

Запуск:
  python redis_diagnostics.py              - только диагностика
  python redis_diagnostics.py --history     - детальная статистика по history:match:*
  python redis_diagnostics.py --flush       - мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes  - авто-очистка (для CI/воркера)
  python redis_diagnostics.py --flush --hard --yes - полный DEL (опасно, удалит ВСЁ включая history)
"""
import sys
import json
from datetime import datetime, timezone, timedelta

from redis_hub import (
    _execute_upstash_cmd,
    is_redis_available,
    get_circuit_breaker_status,
    get_from_cache,
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


def _safe_json_parse(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def hscan_all(batch_size=200, max_iterations=500):
    """Итерация по всем полям через HSCAN (пагинация вместо HGETALL).

    HGETALL падает при 20K+ полей — Upstash REST API не отдаёт такой объём.
    HSCAN возвращает данные порциями (batch_size — рекомендация, не точное число).
    """
    cursor = "0"
    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        result = _execute_upstash_cmd(
            ["HSCAN", "GatekeeperAI", cursor, "COUNT", str(batch_size)]
        )
        if result is None:
            break
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            break
        next_cursor = str(result[0])
        kv_pairs = result[1]
        if kv_pairs is None:
            break
        for i in range(0, len(kv_pairs) - 1, 2):
            field_id = kv_pairs[i]
            raw_value = kv_pairs[i + 1]
            yield field_id, _safe_json_parse(raw_value)
        cursor = next_cursor
        if cursor == "0":
            break


def _get_meta_direct(key):
    """Чтение мета-поля напрямую через HGET (без распаковки конверта)."""
    raw = _execute_upstash_cmd(["HGET", "GatekeeperAI", key])
    return _safe_json_parse(raw)


def _get_football_data_meta():
    """Чтение мета football-data.co.uk (хранится без конверта)."""
    return _get_meta_direct("football_data:meta")


def run_diagnostics(flush=False, auto_yes=False, hard=False, history_only=False):
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
        print("[DIAG] Circuit Breaker: open=" + str(cb["open"]) +
              ", errors=" + str(cb["error_count"]) + "/" + str(cb["threshold"]))
        print("=" * 60)
        return
    print("[DIAG] Redis OK (PING)")

    # 2. Total field count
    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))

    if history_only:
        _run_history_diagnostics()
        print("=" * 60)
        return

    # 3. Scan all fields via HSCAN
    print("[DIAG] Scanning via HSCAN (batch=200)...")

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

    # History breakdown
    hist_by_season = {}
    hist_by_league = {}

    # Meta values (for collector status)
    meta_values = {}
    system_health = {}

    scanned = 0
    for field_id, value in hscan_all(batch_size=200):
        scanned += 1
        fid_lower = field_id.lower()

        # --- History matches ---
        if field_id.startswith("history:match:"):
            history_count += 1
            key_part = field_id[len("history:match:"):]
            parts = key_part.split("_")
            if len(parts) >= 2:
                season = parts[0]
                league = parts[1]
                hist_by_season[season] = hist_by_season.get(season, 0) + 1
                hist_by_league[league] = hist_by_league.get(league, 0) + 1
            continue

        # --- Meta keys ---
        if fid_lower.endswith(":meta") or any(
            fid_lower == mk.lower() for mk in META_KEYS.values()
        ):
            meta_count += 1
            if isinstance(value, dict):
                meta_values[field_id] = value
            continue

        # --- System health ---
        if field_id == "system:health":
            if isinstance(value, dict):
                system_health = value
            other_count += 1
            continue

        # --- Football data meta ---
        if field_id == "football_data:meta":
            other_count += 1
            continue

        # --- Index shards ---
        if field_id.startswith("match:index:"):
            if isinstance(value, dict):
                dt = _parse_date_utc(value.get("updated_at", ""))
                if dt is None or (now_utc - dt).total_seconds() > 7200:
                    index_keys_to_delete.append(field_id)
            other_count += 1
            continue

        # --- Search results ---
        if field_id.startswith("search:results:"):
            search_keys_to_delete.append(field_id)
            other_count += 1
            continue

        # --- Live matches ---
        if field_id.startswith("match:"):
            match_count += 1
            if not isinstance(value, dict):
                other_count += 1
                continue
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

    print("[DIAG] Scanned: " + str(scanned) + " / " + str(hlen) + " fields")

    # 4. Hub matches
    try:
        matches_dict = get_matches_by_date_range()
        hub_count = len(matches_dict)
    except Exception:
        hub_count = -1

    # 5. Meta status (scan results + fallback to HGET)
    meta_status = {}
    for name, key in META_KEYS.items():
        meta_val = meta_values.get(key)
        if not meta_val:
            meta_val = _get_meta_direct(key)
        if meta_val and isinstance(meta_val, dict):
            last_run = meta_val.get("last_run", meta_val.get("last_run_at"))
            if last_run:
                stored = meta_val.get("stored_matches", 0)
                err = meta_val.get("error_count", meta_val.get("errors", 0))
                meta_status[name] = ("OK " + str(last_run)[:19] +
                                     " (stored=" + str(stored) +
                                     ", errors=" + str(err) + ")")
            else:
                meta_status[name] = "no data"
        else:
            meta_status[name] = "no data"

    # 6. Football data meta
    fb_meta = _get_football_data_meta()

    # 7. Circuit breaker
    cb_status = get_circuit_breaker_status()

    # 8. Cleanup metrics
    cleanup_at = system_health.get("last_cleanup_at", "нет данных")
    cleanup_count = system_health.get("last_cleanup_count", "нет данных")
    cleanup_finished = system_health.get("last_cleanup_finished", "нет данных")
    cleanup_expired = system_health.get("last_cleanup_expired", "нет данных")

    # === Output ===
    print()
    print("--- Redis State ---")
    print("  Total fields:       " + str(hlen))
    print("  Scanned fields:     " + str(scanned))
    print("  Live matches:       " + str(match_count))
    print("    Future:            " + str(future))
    print("    Past:              " + str(past))
    print("    No date:           " + str(no_date))
    print("  With odds:           " + str(with_odds))
    print("  History matches:    " + str(history_count))
    print("  Meta keys:          " + str(meta_count))
    print("  Other:              " + str(other_count))
    print("  Hub get_matches:    " + str(hub_count))

    # History breakdown
    if history_count > 0:
        print()
        print("--- History by Season ---")
        for season in sorted(hist_by_season.keys()):
            print("  " + season + ": " + str(hist_by_season[season]))
        print()
        print("--- History by League ---")
        for league in sorted(hist_by_league.keys()):
            print("  " + league + ": " + str(hist_by_league[league]))

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
    if fb_meta and isinstance(fb_meta, dict):
        for k, v in sorted(fb_meta.items()):
            print("  " + str(k) + ": " + str(v))
    else:
        print("  (no data)")

    print()
    print("--- Circuit Breaker ---")
    print("  Open: " + str(cb_status["open"]) +
          ", Errors: " + str(cb_status["error_count"]) +
          "/" + str(cb_status["threshold"]))

    print()
    print("--- Cleanup ---")
    print("  Last cleanup at:        " + str(cleanup_at))
    print("  Total deleted:          " + str(cleanup_count))
    print("  Finished matches:       " + str(cleanup_finished))
    print("  Expired (no date):      " + str(cleanup_expired))
    print("  Policy: завершённые + 2 часа (buffer=2h)")

    print()
    print("--- Summary ---")
    total = match_count + history_count
    if total > 0:
        print("  Total: " + str(total) +
              " (live=" + str(match_count) +
              ", history=" + str(history_count) + ")")
        if match_count > 0:
            past_pct = round(past / match_count * 100, 1)
            print("  Past live: " + str(past) + " / " +
                  str(match_count) + " (" + str(past_pct) + "%)")
            if past_pct > 50:
                print("  WARNING: Recommend flush Redis and restart collectors")
        else:
            print("  No live matches in Redis.")
    else:
        print("  Redis empty or no matches.")

    # === Flush ===
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
                print("[FLUSH] WARNING: This will DELETE ALL data including " +
                      str(history_count) + " history matches!")
                if auto_yes:
                    confirm_hard = "DELETE"
                    print("[FLUSH] Auto-confirm hard mode (--yes)")
                else:
                    try:
                        confirm_hard = input(
                            "Type DELETE to confirm (history will be lost): "
                        ).strip()
                    except EOFError:
                        confirm_hard = ""
                        print("[FLUSH] No terminal - cancelled (use --yes for CI)")
                if confirm_hard != "DELETE":
                    print("[FLUSH] Hard flush cancelled.")
                else:
                    print("[FLUSH] Fields before: " + str(hlen))
                    del_result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
                    if del_result is not None:
                        print("[FLUSH] DEL GatekeeperAI -> deleted: " + str(del_result))
                        hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                        print("[FLUSH] Fields after: " + str(hlen_after))
                    else:
                        print("ERROR: DEL GatekeeperAI failed")
            else:
                # Soft flush — only live matches (past), search results, old index shards
                # History matches are PRESERVED
                keys_to_delete = (match_keys_to_delete +
                                  search_keys_to_delete +
                                  index_keys_to_delete)
                preserved = hlen - len(keys_to_delete)
                print("[FLUSH] SOFT MODE: selective HDEL")
                print("[FLUSH] Plan: live_past=" + str(len(match_keys_to_delete)) +
                      ", search_results=" + str(len(search_keys_to_delete)) +
                      ", index_shards=" + str(len(index_keys_to_delete)) +
                      " | preserved=" + str(preserved) +
                      " (incl. " + str(history_count) + " history)")
                print("[FLUSH] Fields before: " + str(hlen))

                deleted = 0
                errors = 0
                batch_size = 50
                for i in range(0, len(keys_to_delete), batch_size):
                    batch = keys_to_delete[i:i + batch_size]
                    result = _execute_upstash_cmd(
                        ["HDEL", "GatekeeperAI"] + batch
                    )
                    if result is not None:
                        deleted += int(result)
                    else:
                        errors += len(batch)

                hlen_after = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
                print("[FLUSH] Deleted: " + str(deleted) + ", errors: " + str(errors))
                print("[FLUSH] Fields after: " + str(hlen_after))
                if hlen_after is not None and int(hlen_after) == preserved:
                    print("Redis soft-flushed OK (history preserved)")
                else:
                    print("WARNING: remaining fields: " + str(hlen_after))

    print("=" * 60)


def _run_history_diagnostics():
    """Детальная статистика по history:match:*"""
    print()
    print("--- History: football-data.co.uk ---")
    print()

    history_count = 0
    hist_by_season = {}
    hist_by_league = {}
    hist_by_season_league = {}

    # Sample some matches for verification
    samples = []

    for field_id, value in hscan_all(batch_size=500):
        if not field_id.startswith("history:match:"):
            continue
        history_count += 1
        key_part = field_id[len("history:match:"):]
        parts = key_part.split("_")
        if len(parts) >= 2:
            season = parts[0]
            league = parts[1]
            hist_by_season[season] = hist_by_season.get(season, 0) + 1
            hist_by_league[league] = hist_by_league.get(league, 0) + 1
            sl = season + "_" + league
            hist_by_season_league[sl] = hist_by_season_league.get(sl, 0) + 1

        # Collect up to 5 samples
        if len(samples) < 5 and isinstance(value, dict):
            samples.append({
                "key": field_id,
                "home": value.get("home_team", "?"),
                "away": value.get("away_team", "?"),
                "date": value.get("date_utc", "?"),
                "score": value.get("score"),
                "odds": value.get("odds"),
                "season": value.get("season", "?"),
                "league_code": value.get("league_code", "?"),
            })

    print("  Total history matches: " + str(history_count))
    print()

    if history_count == 0:
        print("  No history matches found in Redis.")
        print("  Run football_data_to_redis.py to load historical data.")
        return

    print("--- By Season ---")
    for season in sorted(hist_by_season.keys()):
        print("  " + season + ": " + str(hist_by_season[season]) + " matches")

    print()
    print("--- By League ---")
    for league in sorted(hist_by_league.keys()):
        print("  " + league + ": " + str(hist_by_league[league]) + " matches")

    print()
    print("--- By Season x League ---")
    for key in sorted(hist_by_season_league.keys()):
        print("  " + key + ": " + str(hist_by_season_league[key]))

    # Samples
    if samples:
        print()
        print("--- Sample Matches (5) ---")
        for s in samples:
            print("  " + s["home"] + " vs " + s["away"] +
                  " | " + str(s["date"])[:10] +
                  " | " + s["season"] + " " + s["league_code"] +
                  " | score=" + str(s["score"]) +
                  " | odds=" + str(s["odds"]))

    # Football data meta
    print()
    print("--- Football Data Meta ---")
    fb_meta = _get_football_data_meta()
    if fb_meta and isinstance(fb_meta, dict):
        for k, v in sorted(fb_meta.items()):
            print("  " + str(k) + ": " + str(v))
    else:
        print("  (no meta found)")

    # Verification
    print()
    print("--- Verification ---")
    total_by_season = sum(hist_by_season.values())
    total_by_league = sum(hist_by_league.values())
    print("  Sum by season:  " + str(total_by_season))
    print("  Sum by league:  " + str(total_by_league))
    print("  Direct count:   " + str(history_count))
    if total_by_season == total_by_league == history_count:
        print("  Status: OK (all counts match)")
    else:
        print("  Status: MISMATCH (counts differ!)")

    print()
    print("--- Recommendations ---")
    if history_count > 10000:
        print("  - Large dataset: consider incremental loading for new seasons only")
        print("  - Use HMSET batching to stay within Upstash free tier (10K cmd/day)")
    if history_count > 0 and not fb_meta:
        print("  - football_data:meta not found — run loader to create metadata")
    seasons = sorted(hist_by_season.keys())
    if seasons:
        print("  - Latest season: " + seasons[-1])
        print("  - To add new season: download CSV + run football_data_to_redis.py")


def main():
    flush = "--flush" in sys.argv
    auto_yes = "--yes" in sys.argv
    hard = "--hard" in sys.argv
    history = "--history" in sys.argv
    run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard, history_only=history)


if __name__ == "__main__":
    main()
