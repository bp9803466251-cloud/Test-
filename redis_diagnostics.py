"""
Диагностика и очистка Redis для Gatekeeper-AI v600-prod.
Работает через Upstash REST API.

Запуск:
  python redis_diagnostics.py              - только диагностика
  python redis_diagnostics.py --history     - детальная статистика по history
  python redis_diagnostics.py --test        - тест импортов и Redis
  python redis_diagnostics.py --flush       - мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes - авто-очистка (для CI)
  python redis_diagnostics.py --flush --hard --yes - полный DEL (опасно)
"""
import sys
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

try:
    from redis_hub import (
        _execute_upstash_cmd,
        get_all_fields,
        is_redis_available,
        get_circuit_breaker_status,
    )
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
    # Продолжаем — get_matches_by_date_range может не быть
    get_matches_by_date_range = None

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


def hscan_all(hash_name="GatekeeperAI", batch_size=200, max_fields=None):
    """Сканирует хэш через HSCAN с пагинацией. Fallback на HKEYS+HMGET."""
    print("[HSCAN] Starting scan of " + hash_name + "...")
    all_fields = {}
    cursor = "0"
    iterations = 0

    try:
        while True:
            iterations += 1
            result = _execute_upstash_cmd(["HSCAN", hash_name, cursor, "COUNT", str(batch_size)])

            if result is None:
                print("[HSCAN] HSCAN returned None at iteration " + str(iterations) + ", trying fallback...")
                break

            # Upstash REST возвращает [cursor, [k1,v1,k2,v2,...]] или {result: [...]}
            if isinstance(result, list) and len(result) >= 2:
                cursor = str(result[0])
                kv_pairs = result[1]
            elif isinstance(result, dict) and "result" in result:
                r = result["result"]
                if isinstance(r, list) and len(r) >= 2:
                    cursor = str(r[0])
                    kv_pairs = r[1]
                else:
                    print("[HSCAN] Unexpected dict format: " + str(type(r)))
                    break
            else:
                print("[HSCAN] Unexpected format: " + str(type(result)))
                break

            # kv_pairs может быть списком [k1,v1,k2,v2,...] или dict
            if isinstance(kv_pairs, dict):
                for k, v in kv_pairs.items():
                    if isinstance(v, str):
                        try:
                            all_fields[k] = json.loads(v)
                        except (json.JSONDecodeError, ValueError):
                            all_fields[k] = v
                    else:
                        all_fields[k] = v
            elif isinstance(kv_pairs, list):
                for i in range(0, len(kv_pairs), 2):
                    if i + 1 < len(kv_pairs):
                        k = kv_pairs[i]
                        v = kv_pairs[i + 1]
                        if isinstance(v, str):
                            try:
                                all_fields[k] = json.loads(v)
                            except (json.JSONDecodeError, ValueError):
                                all_fields[k] = v
                        else:
                            all_fields[k] = v

            if max_fields and len(all_fields) >= max_fields:
                break

            if cursor == "0":
                break

            # Защита от бесконечного цикла
            if iterations > 500:
                print("[HSCAN] Too many iterations, stopping")
                break

        print("[HSCAN] Iterations: " + str(iterations) + ", fields loaded: " + str(len(all_fields)))
        if len(all_fields) > 0:
            return all_fields

    except Exception as e:
        print("[HSCAN] Error: " + str(e))
        traceback.print_exc()

    # Fallback: HKEYS + батчинг HMGET
    print("[HSCAN] Fallback to HKEYS + HMGET...")
    try:
        keys_result = _execute_upstash_cmd(["HKEYS", hash_name])
        if keys_result is None:
            print("[HSCAN] HKEYS returned None")
            return {}

        if isinstance(keys_result, dict) and "result" in keys_result:
            keys = keys_result["result"]
        else:
            keys = keys_result

        if not keys:
            print("[HSCAN] No keys found")
            return {}

        print("[HSCAN] HKEYS returned " + str(len(keys)) + " keys")

        for i in range(0, len(keys), 50):
            batch = keys[i:i+50]
            # HMGET возвращает список значений
            vals = _execute_upstash_cmd(["HMGET", hash_name] + batch)
            if vals is None:
                continue

            if isinstance(vals, dict) and "result" in vals:
                vals = vals["result"]

            if isinstance(vals, list):
                for j, v in enumerate(vals):
                    if j < len(batch) and v:
                        if isinstance(v, str):
                            try:
                                all_fields[batch[j]] = json.loads(v)
                            except (json.JSONDecodeError, ValueError):
                                all_fields[batch[j]] = v
                        else:
                            all_fields[batch[j]] = v

            if max_fields and len(all_fields) >= max_fields:
                break

        print("[HSCAN] Fallback loaded: " + str(len(all_fields)) + " fields")
        return all_fields

    except Exception as e:
        print("[HSCAN] Fallback error: " + str(e))
        traceback.print_exc()
        return all_fields


def run_test():
    """Быстрый тест импортов и Redis."""
    print("=" * 60)
    print("[TEST] Gatekeeper-AI v600-prod")
    print("=" * 60)

    print()
    print("[TEST] Python: " + sys.version)
    print("[TEST] Imports: OK")

    print()
    print("[TEST] Redis...")
    if is_redis_available():
        print("[TEST] Redis PING: OK")
    else:
        print("[TEST] Redis PING: FAILED")
        cb = get_circuit_breaker_status()
        print("[TEST] Circuit Breaker: open=" + str(cb["open"]) + ", errors=" + str(cb["error_count"]))
        return

    hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
    print("[TEST] HLEN: " + str(hlen))

    # Тест HSCAN
    print("[TEST] Testing HSCAN...")
    try:
        result = _execute_upstash_cmd(["HSCAN", "GatekeeperAI", "0", "COUNT", "5"])
        print("[TEST] HSCAN raw type: " + str(type(result)))
        if isinstance(result, list):
            print("[TEST] HSCAN len: " + str(len(result)))
            if len(result) >= 2:
                print("[TEST] HSCAN cursor: " + str(result[0]))
                print("[TEST] HSCAN kv type: " + str(type(result[1])))
                if isinstance(result[1], list):
                    print("[TEST] HSCAN kv count: " + str(len(result[1])))
                    if len(result[1]) >= 2:
                        print("[TEST] HSCAN first key: " + str(result[1][0]))
                elif isinstance(result[1], dict):
                    print("[TEST] HSCAN kv keys: " + str(list(result[1].keys())[:3]))
        elif isinstance(result, dict):
            print("[TEST] HSCAN dict keys: " + str(list(result.keys())))
            if "result" in result:
                r = result["result"]
                print("[TEST] HSCAN result type: " + str(type(r)))
                if isinstance(r, list):
                    print("[TEST] HSCAN result len: " + str(len(r)))
        print("[TEST] HSCAN: OK")
    except Exception as e:
        print("[TEST] HSCAN FAILED: " + str(e))
        traceback.print_exc()

    # Тест HKEYS
    print("[TEST] Testing HKEYS...")
    try:
        result = _execute_upstash_cmd(["HKEYS", "GatekeeperAI"])
        print("[TEST] HKEYS type: " + str(type(result)))
        if isinstance(result, list):
            print("[TEST] HKEYS count: " + str(len(result)))
            if result:
                print("[TEST] HKEYS first: " + str(result[0]))
        elif isinstance(result, dict) and "result" in result:
            r = result["result"]
            print("[TEST] HKEYS result type: " + str(type(r)))
            if isinstance(r, list):
                print("[TEST] HKEYS count: " + str(len(r)))
                if r:
                    print("[TEST] HKEYS first: " + str(r[0]))
    except Exception as e:
        print("[TEST] HKEYS FAILED: " + str(e))
        traceback.print_exc()

    print()
    print("[TEST] Done.")
    print("=" * 60)


def run_history_diagnostics(all_fields):
    """Детальная статистика по history:match:*"""
    print()
    print("=" * 60)
    print("[HISTORY] Детальная статистика по history:match:*")
    print("=" * 60)

    history_matches = {}
    for field_id, value in all_fields.items():
        if field_id.startswith("history:match:"):
            history_matches[field_id] = value

    total = len(history_matches)
    print("[HISTORY] Total history matches: " + str(total))

    if total == 0:
        print("[HISTORY] No history matches found!")
        print("=" * 60)
        return

    # Разбивка по сезонам
    seasons = {}
    leagues = {}
    season_league = {}
    no_season = 0
    no_league = 0

    for key, val in history_matches.items():
        season = None
        league = None

        if isinstance(val, dict):
            season = val.get("season")
            league = val.get("league_code") or val.get("league")

        if not season:
            # Пытаемся извлечь из ключа
            parts = key.replace("history:match:", "").split("_")
            if len(parts) >= 1:
                season = parts[0]

        if not league:
            parts = key.replace("history:match:", "").split("_")
            if len(parts) >= 2:
                league = parts[1]

        if season:
            seasons[season] = seasons.get(season, 0) + 1
        else:
            no_season += 1

        if league:
            leagues[league] = leagues.get(league, 0) + 1
        else:
            no_league += 1

        sl = str(season) + "/" + str(league)
        season_league[sl] = season_league.get(sl, 0) + 1

    print()
    print("--- По сезонам ---")
    for s, c in sorted(seasons.items()):
        print("  " + str(s) + ": " + str(c) + " матчей")
    if no_season:
        print("  (без сезона): " + str(no_season))

    print()
    print("--- По лигам ---")
    for l, c in sorted(leagues.items(), key=lambda x: -x[1]):
        print("  " + str(l) + ": " + str(c) + " матчей")
    if no_league:
        print("  (без лиги): " + str(no_league))

    print()
    print("--- Сезон x Лига ---")
    for sl, c in sorted(season_league.items()):
        print("  " + sl + ": " + str(c))

    # Примеры матчей
    print()
    print("--- Примеры матчей (первые 5) ---")
    count = 0
    for key, val in history_matches.items():
        if count >= 5:
            break
        if isinstance(val, dict):
            print("  Key: " + key)
            print("    date: " + str(val.get("date", val.get("date_utc", "?"))))
            print("    home: " + str(val.get("home_team", val.get("home", "?"))))
            print("    away: " + str(val.get("away_team", val.get("away", "?"))))
            print("    score: " + str(val.get("score_home", "?")) + "-" + str(val.get("score_away", "?")))
            print("    season: " + str(val.get("season", "?")))
            print("    league: " + str(val.get("league_code", val.get("league", "?"))))
            print()
        else:
            print("  Key: " + key + " (raw: " + str(type(val)) + ")")
        count += 1

    # Проверка суммы
    print("--- Верификация ---")
    season_sum = sum(seasons.values()) + no_season
    league_sum = sum(leagues.values()) + no_league
    print("  Total: " + str(total))
    print("  By seasons: " + str(season_sum))
    print("  By leagues: " + str(league_sum))
    if season_sum != total:
        print("  WARNING: season sum mismatch!")
    if league_sum != total:
        print("  WARNING: league sum mismatch!")

    # Расхождение с загрузчиком
    meta = all_fields.get("football_data:meta", {})
    if isinstance(meta, dict):
        uploaded = meta.get("total_matches") or meta.get("uploaded") or 0
        diff = int(uploaded) - total
        print()
        print("--- Расхождение ---")
        print("  Загружено (по мета): " + str(uploaded))
        print("  Найдено (в Redis):  " + str(total))
        print("  Разница:            " + str(diff))
        if diff > 0:
            print("  Вероятно: дубликаты ключей (перезапись при совпадении date+teams)")
        elif diff < 0:
            print("  WARNING: в Redis больше, чем загружено (доп. данные?)")

    print("=" * 60)


def run_diagnostics(flush=False, auto_yes=False, hard=False, history=False, test=False):
    if test:
        run_test()
        return

    print("=" * 60)
    print("[DIAG] GATEKEEPER-AI v600-prod")
    print("   Time: " + datetime.now(MSK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S MSK"))
    print("=" * 60)

    try:
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
        print("[DEBUG] After PING")

        # 2. Count fields
        hlen = _execute_upstash_cmd(["HLEN", "GatekeeperAI"])
        print("[DIAG] Fields in hash GatekeeperAI: " + str(hlen))
        print("[DEBUG] After HLEN")

        # 3. Load all fields via HSCAN
        all_fields = hscan_all()
        total_fields = len(all_fields)
        print("[DIAG] Loaded via hscan_all(): " + str(total_fields))
        print("[DEBUG] After hscan_all")

        if total_fields == 0:
            print("[DIAG] WARNING: 0 fields loaded! Trying get_all_fields() fallback...")
            try:
                all_fields = get_all_fields()
                total_fields = len(all_fields)
                print("[DIAG] get_all_fields() returned: " + str(total_fields))
            except Exception as e:
                print("[DIAG] get_all_fields() failed: " + str(e))

        # History diagnostics
        if history:
            run_history_diagnostics(all_fields)
            # Не делаем flush в history mode
            print("=" * 60)
            return

        # 4. Categorize
        print("[DEBUG] Categorizing fields...")
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

            if field_id.startswith("football_data:"):
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

        print("[DEBUG] Categorization done")

        # 5. Hub matches count
        try:
            if get_matches_by_date_range:
                matches_dict = get_matches_by_date_range()
                hub_count = len(matches_dict)
            else:
                hub_count = -1
        except Exception as e:
            print("[DEBUG] get_matches_by_date_range error: " + str(e))
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
        fb_meta = all_fields.get("football_data:meta", {})
        if not isinstance(fb_meta, dict):
            fb_meta = {}

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
        print("  History matches:    " + str(history_count))
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
        if fb_meta:
            for k, v in fb_meta.items():
                print("  " + str(k) + ": " + str(v))
        else:
            print("  (none)")

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
        grand_total = match_count + history_count + meta_count + other_count
        print("  Total: " + str(grand_total) + " (live=" + str(match_count) + ", history=" + str(history_count) + ")")
        if match_count > 0:
            past_pct = round(past / match_count * 100, 1)
            print("  Past live: " + str(past) + " / " + str(match_count) + " (" + str(past_pct) + "%)")
            if past_pct > 50:
                print("  WARNING: Recommend flush Redis and restart collectors")
        else:
            print("  No live matches in Redis.")

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
                    if hlen_after is not None and hlen_after == preserved:
                        print("Redis soft-flushed OK")
                    elif hlen_after is not None:
                        print("WARNING: remaining fields: " + str(hlen_after) + " (expected " + str(preserved) + ")")

    except Exception as e:
        print()
        print("[ERROR] " + str(e))
        print()
        traceback.print_exc()

    print("=" * 60)


def main():
    flush = "--flush" in sys.argv
    auto_yes = "--yes" in sys.argv
    hard = "--hard" in sys.argv
    history = "--history" in sys.argv
    test = "--test" in sys.argv
    run_diagnostics(flush=flush, auto_yes=auto_yes, hard=hard, history=history, test=test)


if __name__ == "__main__":
    main()
