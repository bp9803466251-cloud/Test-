#!/usr/bin/env python3
"""
Диагностика и очистка Redis для Gatekeeper-AI v710.
Работает через redis_hub (единый транспорт).

Две модели:
  - Live: хеш GatekeeperAI (match:*, search:*, index:*, system:*)
  - History: отдельные ключи history:match:*, ZSET history:league:*, SET history:team:*

Запуск:
  python redis_diagnostics.py              — диагностика
  python redis_diagnostics.py --flush       — мягкая очистка (selective HDEL)
  python redis_diagnostics.py --flush --yes  — авто-очистка (для CI)
  python redis_diagnostics.py --flush --hard --yes — DEL GatekeeperAI (опасно)
  python redis_diagnostics.py --history-only --yes  — удалить только history (live сохраняется)
  python redis_diagnostics.py --purge --yes  — FLUSHDB (wipe ВСЕХ ключей)
  python redis_diagnostics.py --json         — вывод в JSON для CI
  python redis_diagnostics.py --dry-run      — показать план без выполнения
  python redis_diagnostics.py --reset-breaker — сброс circuit breaker
"""
import sys
import json
import time
from datetime import datetime, timezone, timedelta

from redis_hub import (
    is_redis_available,
    get_circuit_breaker_status,
    reset_circuit_breaker,
    get_all_fields,
    get_key,
    scan_keys,
    delete_keys,
    dbsize,
    zcard_key,
    zrange_key,
    hlen_key,
    _execute_upstash_cmd,
)

MSK_TIMEZONE = timezone(timedelta(hours=3))

META_KEYS = {
    "Bzzoiro": "bzzoiro:meta",
    "SharpAPI": "sharpapi:meta",
    "OddsAPI": "odds_api:meta",
    "FootballData": "football_data:meta",
    "Propline": "propline:meta",
}


def _parse_date_utc(raw):
    """Парсинг даты — 4 формата."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw.strip(), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
    return None


def _print(*args, **kwargs):
    if not getattr(_print, "_json_mode", False):
        print(*args, **kwargs)


def scan_history():
    keys = scan_keys("history:match:*", count=500)
    return keys


def scan_history_leagues():
    keys = scan_keys("history:league:*", count=100)
    result = {}
    for key in keys:
        league_code = key.replace("history:league:", "")
        count = zcard_key(key)
        result[league_code] = count
    return result


def scan_history_teams():
    keys = scan_keys("history:team:*", count=100)
    return len(keys)


def scan_analysis():
    keys = scan_keys("analysis:*", count=500)
    return len(keys)


def get_football_data_meta():
    """Чтение football_data:meta — SET (string key), не HGETALL."""
    raw = get_key("football_data:meta")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def run_diagnostics():
    diag = {
        "version": "v710",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if not is_redis_available():
        cb = get_circuit_breaker_status()
        diag["redis"] = {"available": False, "circuit_breaker": cb}
        return diag

    diag["redis"] = {"available": True}
    total_keys = dbsize()
    diag["redis"]["dbsize"] = total_keys

    all_fields = get_all_fields()
    total_fields = len(all_fields)

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
    search_keys = []
    index_keys = []

    for field_id, value in all_fields.items():
        if not isinstance(value, dict):
            other_count += 1
            continue

        fid_lower = field_id.lower()

        if fid_lower.endswith(":meta") or any(fid_lower == mk.lower() for mk in META_KEYS.values()):
            meta_count += 1
            continue

        if field_id.startswith("match:index:"):
            index_keys.append(field_id)
            continue

        if field_id.startswith("search:results"):
            search_keys.append(field_id)
            continue

        if field_id.startswith("match:"):
            match_count += 1
            dt = _parse_date_utc(value.get("date_utc", ""))
            if dt is None:
                no_date += 1
            elif dt < now_utc:
                past += 1
                if value.get("status") == "completed":
                    match_keys_to_delete.append(field_id)
            else:
                future += 1

            odds = value.get("odds", {})
            if isinstance(odds, dict) and odds.get("1x2"):
                with_odds += 1

            sources_list = value.get("sources", [])
            if isinstance(sources_list, list) and sources_list:
                for s in sources_list:
                    if isinstance(s, dict):
                        src = s.get("source") or s.get("upstream") or "no_source"
                        sources[src] = sources.get(src, 0) + 1
            else:
                src = value.get("raw_source") or value.get("source") or "no_source"
                sources[src] = sources.get(src, 0) + 1
            continue

        other_count += 1

    diag["live"] = {
        "total_fields": total_fields,
        "matches": match_count,
        "future": future,
        "past": past,
        "no_date": no_date,
        "with_odds": with_odds,
        "meta_keys": meta_count,
        "other": other_count,
        "search_keys": len(search_keys),
        "index_keys": len(index_keys),
        "past_completed": len(match_keys_to_delete),
    }

    history_keys = scan_history()
    history_leagues = scan_history_leagues()
    history_teams_count = scan_history_teams()
    analysis_count = scan_analysis()
    football_data_meta = get_football_data_meta()

    diag["history"] = {
        "total_keys": len(history_keys),
        "leagues": history_leagues,
        "team_indexes": history_teams_count,
        "analysis_keys": analysis_count,
    }

    diag["football_data_meta"] = football_data_meta

    meta_status = {}
    for name, key in META_KEYS.items():
        meta_val = all_fields.get(key)
        if meta_val and isinstance(meta_val, dict):
            last_run = meta_val.get("last_run", meta_val.get("last_run_at"))
            if last_run:
                stored = meta_val.get("stored_matches", 0)
                err = meta_val.get("error_count", meta_val.get("errors", 0))
                meta_status[name] = f"OK {str(last_run)[:19]} (stored={stored}, errors={err})"
            else:
                meta_status[name] = "no data"
        else:
            meta_status[name] = "no data"

    diag["collectors"] = meta_status
    diag["circuit_breaker"] = get_circuit_breaker_status()

    health = all_fields.get("system:health", {})
    if not isinstance(health, dict):
        health = {}
    diag["cleanup"] = {
        "last_cleanup_at": health.get("last_cleanup_at", "no data"),
        "last_cleanup_count": health.get("last_cleanup_count", "no data"),
    }

    diag["_flush_keys"] = {
        "match_keys_to_delete": match_keys_to_delete,
        "search_keys": search_keys,
        "index_keys": index_keys,
        "history_keys": history_keys,
    }

    return diag


def do_flush_soft(diag, auto_yes=False, dry_run=False):
    keys_info = diag.get("_flush_keys", {})
    match_keys = keys_info.get("match_keys_to_delete", [])
    search_keys = keys_info.get("search_keys", [])
    index_keys = keys_info.get("index_keys", [])

    keys_to_delete = match_keys + search_keys + index_keys
    if not keys_to_delete:
        _print("[FLUSH] Nothing to delete (soft)")
        return 0

    _print("[FLUSH] SOFT MODE: selective HDEL")
    _print(f"  Matches (completed+past): {len(match_keys)}")
    _print(f"  Search results: {len(search_keys)}")
    _print(f"  Index shards: {len(index_keys)}")

    if dry_run:
        _print("[FLUSH] DRY RUN — nothing deleted")
        return len(keys_to_delete)

    if not auto_yes:
        try:
            confirm = input("Proceed? (y/n): ").strip().lower()
        except EOFError:
            confirm = "n"
        if confirm != "y":
            _print("[FLUSH] Cancelled")
            return 0

    deleted = 0
    errors = 0
    batch_size = 50
    for i in range(0, len(keys_to_delete), batch_size):
        batch = keys_to_delete[i : i + batch_size]
        result = _execute_upstash_cmd(["HDEL", "GatekeeperAI"] + batch)
        if result is not None:
            deleted += int(result) if result else 0
        else:
            errors += len(batch)
        time.sleep(0.1)

    _print(f"[FLUSH] Deleted: {deleted}, Errors: {errors}")
    return deleted


def do_flush_hard(auto_yes=False, dry_run=False):
    hlen = hlen_key("GatekeeperAI")
    _print("[FLUSH] HARD MODE: DEL GatekeeperAI (live only)")
    _print(f"  Fields before: {hlen}")
    _print(f"  NOTE: history:match:* keys will NOT be affected")

    if dry_run:
        _print("[FLUSH] DRY RUN — nothing deleted")
        return hlen

    if not auto_yes:
        try:
            confirm = input("Type DELETE to confirm: ").strip()
        except EOFError:
            confirm = ""
        if confirm != "DELETE":
            _print("[FLUSH] Cancelled")
            return 0

    result = _execute_upstash_cmd(["DEL", "GatekeeperAI"])
    if result is not None:
        _print(f"[FLUSH] DEL GatekeeperAI -> {result}")
        return int(result) if result else 0
    else:
        _print("[FLUSH] ERROR: DEL failed")
        return 0


def do_history_only(auto_yes=False, dry_run=False):
    _print("[HISTORY-ONLY] Scanning history keys...")

    history_keys = scan_keys("history:match:*", count=500)
    league_keys = scan_keys("history:league:*", count=100)
    team_keys = scan_keys("history:team:*", count=100)
    analysis_keys = scan_keys("analysis:*", count=500)

    all_keys = history_keys + league_keys + team_keys + analysis_keys + ["football_data:meta"]

    _print(f"  history:match:* — {len(history_keys)}")
    _print(f"  history:league:* — {len(league_keys)}")
    _print(f"  history:team:* — {len(team_keys)}")
    _print(f"  analysis:* — {len(analysis_keys)}")
    _print(f"  football_data:meta — 1")
    _print(f"  TOTAL: {len(all_keys)} keys to delete")
    _print(f"  Live data (GatekeeperAI hash) will NOT be affected")

    if dry_run:
        _print("[HISTORY-ONLY] DRY RUN — nothing deleted")
        return len(all_keys)

    if not auto_yes:
        try:
            confirm = input("Proceed? (y/n): ").strip().lower()
        except EOFError:
            confirm = "n"
        if confirm != "y":
            _print("[HISTORY-ONLY] Cancelled")
            return 0

    deleted = 0
    errors = 0
    batch_size = 50
    for i in range(0, len(all_keys), batch_size):
        batch = all_keys[i : i + batch_size]
        result = delete_keys(batch)
        deleted += result
        if result < len(batch):
            errors += len(batch) - result
        time.sleep(0.1)

    _print(f"[HISTORY-ONLY] Deleted: {deleted}, Errors: {errors}")
    return deleted


def do_purge(auto_yes=False, dry_run=False):
    _print("[PURGE] GATEKEEPER-AI v710")
    _print(f"  Time: {datetime.now(MSK_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')}")

    if not is_redis_available():
        _print("[PURGE] Redis unavailable")
        return False

    total = dbsize()
    history_keys = scan_keys("history:match:*", count=500)
    _print(f"  DBSIZE: {total}")
    _print(f"  History keys: {len(history_keys)}")
    _print(f"  WARNING: This will DELETE ALL KEYS")

    if dry_run:
        _print("[PURGE] DRY RUN — nothing deleted")
        return True

    if not auto_yes:
        try:
            confirm = input("Type DELETE to confirm FLUSHDB: ").strip()
        except EOFError:
            confirm = ""
        if confirm != "DELETE":
            _print("[PURGE] Cancelled")
            return False

    _print("[PURGE] Executing FLUSHDB...")
    result = _execute_upstash_cmd(["FLUSHDB"])
    if result is not None:
        after = dbsize()
        _print(f"  DBSIZE after: {after}")
        if after == 0:
            _print("[PURGE] SUCCESS — Redis очищен")
        else:
            _print(f"[PURGE] WARNING: {after} keys remaining")
        return True
    else:
        _print("[PURGE] ERROR: FLUSHDB failed")
        return False


def print_diagnostics(diag):
    _print("=" * 60)
    _print(f"[DIAG] GATEKEEPER-AI {diag.get('version', 'v710')}")
    _print(f"  Time: {datetime.now(MSK_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    _print("=" * 60)

    if not diag.get("redis", {}).get("available"):
        _print("\n[DIAG] Redis unavailable (or circuit breaker open)")
        cb = diag.get("circuit_breaker", {})
        _print(f"  Breaker: open={cb.get('open')}, errors={cb.get('error_count')}/{cb.get('threshold')}")
        _print("=" * 60)
        return

    _print(f"\n--- Redis State ---")
    _print(f"  DBSIZE (all keys): {diag['redis'].get('dbsize', '?')}")

    live = diag.get("live", {})
    _print(f"\n--- Live (hash GatekeeperAI) ---")
    _print(f"  Total fields:    {live.get('total_fields', 0)}")
    _print(f"  Matches:         {live.get('matches', 0)}")
    _print(f"    Future:        {live.get('future', 0)}")
    _print(f"    Past:          {live.get('past', 0)}")
    _print(f"    No date:       {live.get('no_date', 0)}")
    _print(f"  With odds:       {live.get('with_odds', 0)}")
    _print(f"  Meta keys:       {live.get('meta_keys', 0)}")
    _print(f"  Search keys:     {live.get('search_keys', 0)}")
    _print(f"  Index keys:      {live.get('index_keys', 0)}")
    _print(f"  Past+completed:  {live.get('past_completed', 0)}")

    hist = diag.get("history", {})
    _print(f"\n--- History (separate keys) ---")
    _print(f"  history:match:* — {hist.get('total_keys', 0)}")
    leagues = hist.get("leagues", {})
    if leagues:
        for lc, cnt in sorted(leagues.items()):
            _print(f"    {lc}: {cnt}")
    _print(f"  history:team:* — {hist.get('team_indexes', 0)}")
    _print(f"  analysis:* — {hist.get('analysis_keys', 0)}")

    fdm = diag.get("football_data_meta", {})
    if fdm:
        _print(f"\n--- Football Data Meta ---")
        for field, meta in fdm.items():
            if isinstance(meta, dict):
                _print(f"  {field}: matches={meta.get('matches')}, errors={meta.get('errors')}, last={str(meta.get('last_run', ''))[:19]}")
            else:
                _print(f"  {field}: {meta}")

    _print(f"\n--- Sources ---")
    sources = {}
    for field_id, value in get_all_fields().items():
        if isinstance(value, dict) and field_id.startswith("match:"):
            sl = value.get("sources", [])
            if isinstance(sl, list):
                for s in sl:
                    if isinstance(s, dict):
                        src = s.get("source") or s.get("upstream") or "no_source"
                        sources[src] = sources.get(src, 0) + 1
    if sources:
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            _print(f"  {src}: {count}")
    else:
        _print("  (none)")

    _print(f"\n--- Collector Meta ---")
    collectors = diag.get("collectors", {})
    for name, status in collectors.items():
        _print(f"  {name}: {status}")

    _print(f"\n--- Circuit Breaker ---")
    cb = diag.get("circuit_breaker", {})
    _print(f"  Open: {cb.get('open')}, Errors: {cb.get('error_count')}/{cb.get('threshold')}")

    _print(f"\n--- Cleanup ---")
    cleanup = diag.get("cleanup", {})
    _print(f"  Last cleanup at: {cleanup.get('last_cleanup_at', 'no data')}")
    _print(f"  Last cleanup count: {cleanup.get('last_cleanup_count', 'no data')}")

    _print(f"\n--- Summary ---")
    total_live = live.get("matches", 0)
    total_history = hist.get("total_keys", 0)
    _print(f"  Live matches: {total_live}")
    _print(f"  History matches: {total_history}")
    _print(f"  Total: {total_live + total_history}")

    _print("=" * 60)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Redis Diagnostics")
    parser.add_argument("--flush", action="store_true", help="Soft flush (selective HDEL)")
    parser.add_argument("--hard", action="store_true", help="Hard flush (DEL GatekeeperAI)")
    parser.add_argument("--history-only", action="store_true",
                        help="Delete only history keys (live preserved)")
    parser.add_argument("--purge", action="store_true", help="FLUSHDB (wipe ALL keys)")
    parser.add_argument("--yes", action="store_true", help="Auto-confirm (for CI)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    parser.add_argument("--json", action="store_true", help="JSON output for CI")
    parser.add_argument("--reset-breaker", action="store_true", help="Reset circuit breaker and exit")
    args = parser.parse_args()

    if args.reset_breaker:
        reset_circuit_breaker()
        print("[DIAG] Circuit breaker reset")
        return 0

    if args.json:
        _print._json_mode = True

    diag = run_diagnostics()

    if args.json:
        diag.pop("_flush_keys", None)
        print(json.dumps(diag, indent=2, ensure_ascii=False, default=str))
        return 0

    print_diagnostics(diag)

    if args.history_only:
        do_history_only(auto_yes=args.yes, dry_run=args.dry_run)
    elif args.purge:
        do_purge(auto_yes=args.yes, dry_run=args.dry_run)
    elif args.flush:
        if args.hard:
            do_flush_hard(auto_yes=args.yes, dry_run=args.dry_run)
        else:
            do_flush_soft(diag, auto_yes=args.yes, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
