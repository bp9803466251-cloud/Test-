#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics"""

import os
import sys
import json
import time
import traceback
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta


def _p(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Redis backend init (redis_hub → Upstash REST → shared Upstash)
# ---------------------------------------------------------------------------
_REDIS_MODE = None
_REDIS_URL = None
_REDIS_TOKEN = None


def _init_redis():
    """Инициализация Redis: redis_hub (Termux) → Upstash REST API (CI)."""
    global _REDIS_MODE, _REDIS_URL, _REDIS_TOKEN

    try:
        import redis_hub
        redis_hub._execute_upstash_cmd(["PING"])
        _REDIS_MODE = "redis_hub"
        _p("[DIAG] Redis backend: redis_hub")
        return True
    except Exception:
        pass

    _REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[DIAG] Redis backend: Upstash REST API")
        return True

    _REDIS_URL = os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN", "")
    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[DIAG] Redis backend: shared Upstash REST API")
        return True

    _REDIS_MODE = None
    _p("[DIAG] No Redis backend available")
    return False


def _exec_upstash(cmd):
    url = _REDIS_URL.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(str(c), safe="") for c in cmd
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_REDIS_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body).get("result")


def _exec(cmd, max_retries=0):
    for attempt in range(max_retries + 1):
        try:
            if _REDIS_MODE == "redis_hub":
                import redis_hub
                return redis_hub._execute_upstash_cmd(cmd)
            elif _REDIS_MODE == "upstash":
                return _exec_upstash(cmd)
            else:
                _p("[ERROR] No Redis backend initialised")
                return None
        except Exception as e:
            if attempt < max_retries:
                wait = 2 * (attempt + 1)
                _p(f"[REDIS ERROR] {e} (attempt {attempt+1}/{max_retries+1}, retry in {wait}s)")
                time.sleep(wait)
            else:
                _p(f"[REDIS ERROR] {e} (no more retries)")
                return None
    return None


def _safe_json(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def _unwrap(raw):
    """Extract payload from wrapper."""
    if not isinstance(raw, dict):
        return raw
    if "payload" in raw and isinstance(raw["payload"], dict):
        return raw["payload"]
    return raw


def _hscan_all(hash_name, match_pattern=None):
    """Итерация по большому хэшу через HSCAN с retry при таймауте."""
    cursor = "0"
    all_keys = []
    consecutive_failures = 0
    while True:
        cmd = ["HSCAN", hash_name, str(cursor)]
        if match_pattern:
            cmd.append("MATCH")
            cmd.append(match_pattern)
        cmd.append("COUNT")
        cmd.append("100")
        result = _exec(cmd, max_retries=2)
        if result is None:
            consecutive_failures += 1
            _p(f"[WARN] HSCAN returned None at cursor={cursor} (failures={consecutive_failures})")
            if consecutive_failures >= 5:
                _p(f"[WARN] Too many consecutive failures, stopping scan")
                break
            time.sleep(3)
            continue
        consecutive_failures = 0
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                break
        if not isinstance(result, list) or len(result) < 2:
            break
        cursor = str(result[0])
        kv = result[1]
        if isinstance(kv, list):
            field_names = kv[::2]
            all_keys.extend(field_names)
        if cursor == "0" or cursor == 0:
            break
        time.sleep(0.1)
    return all_keys


def load_all_fields():
    """Загружает все поля хэша через HSCAN с retry и маленькими батчами."""
    # Сначала узнаем общий размер
    hlen = _exec(["HLEN", "GatekeeperAI"])
    if hlen is not None:
        _p(f"[LOAD] HLEN = {hlen} fields expected")
    _p("[LOAD] Scanning via HSCAN (COUNT=100, retry=2)...")
    cursor = "0"
    all_fields = {}
    batch_num = 0
    consecutive_failures = 0
    while True:
        cmd = ["HSCAN", "GatekeeperAI", str(cursor), "COUNT", "100"]
        result = _exec(cmd, max_retries=2)
        if result is None:
            consecutive_failures += 1
            _p(f"[WARN] HSCAN returned None at cursor={cursor} (failures={consecutive_failures})")
            if consecutive_failures >= 5:
                _p(f"[WARN] Too many consecutive failures, stopping scan")
                break
            # Ждём подольше перед retry и пробуем с того же курсора
            wait = 5 * consecutive_failures
            _p(f"[LOAD] Waiting {wait}s before retry from cursor={cursor}...")
            time.sleep(wait)
            continue
        consecutive_failures = 0
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                break
        if not isinstance(result, list) or len(result) < 2:
            break
        cursor = str(result[0])
        kv_pairs = result[1]
        if isinstance(kv_pairs, list):
            for i in range(0, len(kv_pairs), 2):
                k = kv_pairs[i]
                v = kv_pairs[i + 1] if i + 1 < len(kv_pairs) else None
                if v is not None:
                    all_fields[k] = _safe_json(v)
        batch_num += 1
        if batch_num % 20 == 0:
            _p(f"[LOAD] Scanned {len(all_fields)} fields so far (batch #{batch_num}, cursor={cursor})...")
        if cursor == "0" or cursor == 0:
            break
        time.sleep(0.1)

    _p(f"[LOAD] Done: {len(all_fields)} fields")
    if hlen is not None:
        hlen_int = int(hlen) if hlen else 0
        if len(all_fields) < hlen_int:
            _p(f"[LOAD] WARNING: loaded {len(all_fields)} but HLEN={hlen_int} — {hlen_int - len(all_fields)} fields missing!")
        elif len(all_fields) == hlen_int:
            _p(f"[LOAD] OK: loaded count matches HLEN")
    return all_fields


def _extract_date(payload):
    if not isinstance(payload, dict):
        return None
    d = payload.get("date_utc") or payload.get("utcDate") or payload.get("date")
    if d:
        return str(d)[:10]
    return None


def _get_season_from_date(date_str):
    if not date_str or len(date_str) < 4:
        return "?"
    try:
        y = int(date_str[:4])
        m = int(date_str[5:7]) if len(date_str) >= 7 else 0
        if m >= 7:
            return f"{y}/{y + 1}"
        else:
            return f"{y - 1}/{y}"
    except Exception:
        return "?"


def _get_season_from_key(key):
    parts = key.split("__")
    if len(parts) >= 3:
        date_part = parts[-1]
        if len(date_part) == 8 and date_part.isdigit():
            ds = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
            return _get_season_from_date(ds), ds
    return None, None


def _get_competition(payload):
    if not isinstance(payload, dict):
        return "?"
    c = payload.get("competition")
    if not c:
        return "?"
    if isinstance(c, dict):
        return c.get("name") or c.get("id") or "?"
    return str(c)


def _get_score(payload):
    if not isinstance(payload, dict):
        return None, None
    s = payload.get("score")
    if not s or not isinstance(s, dict):
        return None, None
    h = s.get("home")
    if h is None:
        h = s.get("homeTeam") or s.get("fullTime", {}).get("homeTeam") if isinstance(s, dict) else None
    a = s.get("away")
    if a is None:
        a = s.get("awayTeam") or s.get("fullTime", {}).get("awayTeam") if isinstance(s, dict) else None
    return h, a


def _get_sender(raw):
    if not isinstance(raw, dict):
        return "?"
    return raw.get("sender_repo") or "?"


def _get_status(payload):
    if not isinstance(payload, dict):
        return "?"
    return payload.get("status") or "?"


# ---------------------------------------------------------------------------
# Odds format check
# ---------------------------------------------------------------------------
def _check_odds_format(match_keys, fields):
    fmt_1x2 = 0
    fmt_current = 0
    fmt_flat = 0
    fmt_empty = 0
    fmt_none = 0
    src_0 = 0
    src_1 = 0
    src_2 = 0
    src_3 = 0
    ver_verified = 0
    ver_warning = 0
    ver_unverified = 0

    for key in match_keys:
        raw = fields.get(key)
        payload = _unwrap(raw)
        if not isinstance(payload, dict):
            continue
        odds = payload.get("odds", {})
        if not isinstance(odds, dict):
            fmt_none += 1
            src_0 += 1
            ver_unverified += 1
            continue

        if "1x2" in odds and isinstance(odds["1x2"], dict):
            section = odds["1x2"]
            current = section.get("current", {})
            if isinstance(current, dict) and current:
                fmt_1x2 += 1
            else:
                fmt_empty += 1
            sources = section.get("sources", [])
            n = len(sources) if isinstance(sources, list) else 0
            if n == 0:
                src_0 += 1
            elif n == 1:
                src_1 += 1
            elif n == 2:
                src_2 += 1
            else:
                src_3 += 1
            v = section.get("_verification", "UNVERIFIED")
            if v == "VERIFIED":
                ver_verified += 1
            elif v == "WARNING":
                ver_warning += 1
            else:
                ver_unverified += 1
        elif "current" in odds and isinstance(odds["current"], dict) and odds["current"]:
            fmt_current += 1
            src_0 += 1
            ver_unverified += 1
        elif odds:
            fmt_flat += 1
            src_0 += 1
            ver_unverified += 1
        else:
            fmt_empty += 1
            src_0 += 1
            ver_unverified += 1

    _p(f"\n--- match:* odds format ---")
    _p(f"  1x2: {fmt_1x2}")
    _p(f"  current: {fmt_current}")
    _p(f"  flat: {fmt_flat}")
    _p(f"  empty: {fmt_empty}")
    _p(f"  none: {fmt_none}")

    _p(f"\n--- match:* sources count ---")
    _p(f"  0+ sources: {src_0}")
    _p(f"  1+ sources: {src_1}")
    _p(f"  2+ sources: {src_2}")
    _p(f"  3+ sources: {src_3}")

    _p(f"\n--- match:* verification ---")
    _p(f"  VERIFIED: {ver_verified}")
    _p(f"  WARNING: {ver_warning}")
    _p(f"  UNVERIFIED: {ver_unverified}")


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------
def run_test():
    _p("=== TEST MODE ===")
    _p("\n--- 1. PING ---")
    r = _exec(["PING"])
    _p(f"  Result: {r!r}")

    _p("\n--- 2. HLEN ---")
    r = _exec(["HLEN", "GatekeeperAI"])
    _p(f"  HLEN: {r}")

    _p("\n--- 3. HSCAN (first batch) ---")
    result = _exec(["HSCAN", "GatekeeperAI", "0", "COUNT", "10"])
    if result is None:
        _p("  HSCAN returned None!")
        return
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = [result, []]
    if isinstance(result, list) and len(result) >= 2:
        kv = result[1] if isinstance(result[1], list) else []
        keys = kv[::2]
        _p(f"  First batch keys: {keys[:10]}")

    _p("\n=== TEST COMPLETE ===")


def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    hist_keys = sorted([k for k in fields if k.startswith("history:match:")])
    match_keys = sorted([k for k in fields if k.startswith("match:")
                         and not k.startswith("history:")
                         and not k.startswith("match:index:")])
    index_keys = sorted([k for k in fields if k.startswith("match:index:")])
    other_keys = sorted([k for k in fields
                         if not k.startswith("history:match:")
                         and not k.startswith("match:")])

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f"  match:index:*   = {len(index_keys)}")
    _p(f"  other           = {len(other_keys)}")

    # --- Parse history ---
    hist_data = {}
    hist_errors = 0
    for key in hist_keys:
        raw = fields[key]
        payload = _unwrap(raw)
        if not isinstance(payload, dict):
            hist_errors += 1
            continue
        cid = payload.get("canonical_id") or key.replace("history:match:", "")
        comp = _get_competition(payload)
        date = _extract_date(payload)
        if not date:
            _, ds = _get_season_from_key(key)
            if ds:
                date = ds
        season = _get_season_from_date(date) if date else "?"
        h, a = _get_score(payload)
        hist_data[cid] = {
            "key": key,
            "competition": comp,
            "date": date,
            "season": season,
            "score_h": h,
            "score_a": a,
            "sender": _get_sender(raw),
            "status": _get_status(payload),
            "home": payload.get("home_team") or "?",
            "away": payload.get("away_team") or "?",
        }

    _p(f"\n--- History parse ---")
    _p(f"  Parsed: {len(hist_data)}, Errors: {hist_errors}")

    # --- By season ---
    seasons = {}
    for cid, d in hist_data.items():
        s = d["season"]
        seasons[s] = seasons.get(s, 0) + 1
    _p(f"\n--- By season ({len(seasons)}) ---")
    for s in sorted(seasons.keys()):
        _p(f"  {s}: {seasons[s]}")

    # --- By league ---
    leagues = {}
    for cid, d in hist_data.items():
        lg = d["competition"]
        leagues[lg] = leagues.get(lg, 0) + 1
    _p(f"\n--- By league ({len(leagues)}) ---")
    for lg in sorted(leagues.keys(), key=lambda x: -leagues[x]):
        _p(f"  {lg}: {leagues[lg]}")

    _p(f"\n  History canonical IDs: {len(hist_data)}")

    # --- Parse match:* ---
    match_data = {}
    match_errors = 0
    non_dict_payloads = []
    for key in match_keys:
        raw = fields[key]
        payload = _unwrap(raw)
        if not isinstance(payload, dict):
            match_errors += 1
            non_dict_payloads.append((key, type(payload).__name__, str(payload)[:100]))
            continue
        cid = payload.get("canonical_id") or key.replace("match:", "")
        comp = _get_competition(payload)
        date = _extract_date(payload)
        if not date:
            _, ds = _get_season_from_key(key)
            if ds:
                date = ds
        season = _get_season_from_date(date) if date else "?"
        h, a = _get_score(payload)
        match_data[cid] = {
            "key": key,
            "competition": comp,
            "date": date,
            "season": season,
            "score_h": h,
            "score_a": a,
            "sender": _get_sender(raw),
            "status": _get_status(payload),
            "home": payload.get("home_team") or "?",
            "away": payload.get("away_team") or "?",
        }

    _p(f"\n--- match:* parse ---")
    _p(f"  Parsed: {len(match_data)}, Errors: {match_errors}")
    if non_dict_payloads:
        _p(f"  Non-dict payloads: {len(non_dict_payloads)}")
        for k, t, v in non_dict_payloads[:5]:
            _p(f"    {k}: type={t}, val={v}")

    # --- Split match:* into past / future / unknown ---
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    past_match = {}
    future_match = {}
    unknown_match = {}
    for cid, d in match_data.items():
        if not d["date"] or d["date"] == "?":
            unknown_match[cid] = d
        elif d["date"] < now_str:
            past_match[cid] = d
        else:
            future_match[cid] = d

    _p(f"\n--- match:* split ---")
    _p(f"  Past    = {len(past_match)}")
    _p(f"  Future  = {len(future_match)}")
    _p(f"  Unknown = {len(unknown_match)}")

    # --- Past match:* by season ---
    past_seasons = {}
    for cid, d in past_match.items():
        s = d["season"]
        past_seasons[s] = past_seasons.get(s, 0) + 1
    _p(f"\n--- Past match:* by season ({len(past_seasons)}) ---")
    for s in sorted(past_seasons.keys()):
        _p(f"  {s}: {past_seasons[s]}")

    # --- Past match:* by league ---
    past_leagues = {}
    for cid, d in past_match.items():
        lg = d["competition"]
        past_leagues[lg] = past_leagues.get(lg, 0) + 1
    _p(f"\n--- Past match:* by league ({len(past_leagues)}) ---")
    for lg in sorted(past_leagues.keys(), key=lambda x: -past_leagues[lg]):
        _p(f"  {lg}: {past_leagues[lg]}")

    # --- LOST MATCHES ---
    hist_cids = set(hist_data.keys())
    lost = {}
    for cid, d in past_match.items():
        if cid not in hist_cids:
            lost[cid] = d

    _p(f"\n{'=' * 60}")
    _p(f"=== LOST MATCHES: {len(lost)} ===")
    _p(f"{'=' * 60}")

    if not lost:
        _p("  No lost matches found — all past match:* are in history:match:*")
    else:
        lost_seasons = {}
        for cid, d in lost.items():
            s = d["season"]
            lost_seasons[s] = lost_seasons.get(s, 0) + 1
        _p(f"\n--- Lost by season ({len(lost_seasons)}) ---")
        for s in sorted(lost_seasons.keys()):
            _p(f"  {s}: {lost_seasons[s]}")

        lost_leagues = {}
        for cid, d in lost.items():
            lg = d["competition"]
            lost_leagues[lg] = lost_leagues.get(lg, 0) + 1
        _p(f"\n--- Lost by league ({len(lost_leagues)}) ---")
        for lg in sorted(lost_leagues.keys(), key=lambda x: -lost_leagues[lg]):
            _p(f"  {lg}: {lost_leagues[lg]}")

        lost_sl = {}
        for cid, d in lost.items():
            sl = f"{d['season']} | {d['competition']}"
            lost_sl[sl] = lost_sl.get(sl, 0) + 1
        _p(f"\n--- Lost by season x league (top 20) ---")
        for sl in sorted(lost_sl.keys(), key=lambda x: -lost_sl[x])[:20]:
            _p(f"  {sl}: {lost_sl[sl]}")

        lost_senders = {}
        for cid, d in lost.items():
            s = d["sender"]
            lost_senders[s] = lost_senders.get(s, 0) + 1
        _p(f"\n--- Lost by sender ---")
        for s in sorted(lost_senders.keys()):
            _p(f"  {s}: {lost_senders[s]}")

        _p(f"\n--- Lost examples (20) ---")
        for i, (cid, d) in enumerate(sorted(lost.items(), key=lambda x: x[1]["date"] or "")[:20]):
            _p(f"  {i + 1}. {d['home']} vs {d['away']} | {d['date']} | {d['competition']} | "
                f"score: {d['score_h']}-{d['score_a']} | status: {d['status']} | sender: {d['sender']}")
            _p(f"     key: {d['key']}")

    # --- Verification ---
    _p(f"\n{'=' * 60}")
    _p(f"=== VERIFICATION ===")
    _p(f"  history:match:*  = {len(hist_data)}")
    _p(f"  match:* past     = {len(past_match)}")
    _p(f"  match:* future   = {len(future_match)}")
    _p(f"  match:* unknown  = {len(unknown_match)}")
    _p(f"  Duplicates       = {len(past_match) - len(lost)}")
    _p(f"  Lost             = {len(lost)}")

    # --- History odds format check ---
    _p(f"\n--- History odds format check ---")
    _check_odds_format(hist_keys, fields)

    _p(f"\n=== HISTORY COMPLETE ===")


def run_purge(hard=False):
    """Полная очистка Redis — удаляет весь хэш GatekeeperAI одной командой DEL."""
    _p("=== PURGE MODE ===")
    _p("[PURGE] This will DELETE ALL keys from GatekeeperAI hash!")

    hlen = _exec(["HLEN", "GatekeeperAI"])
    if hlen is None:
        _p("[FATAL] HLEN returned None — cannot reach Redis")
        return
    hlen = int(hlen) if hlen else 0

    if hlen == 0:
        _p("[PURGE] HLEN=0, already empty.")
        return

    _p(f"[PURGE] HLEN = {hlen} fields")

    _p("[PURGE] Executing DEL GatekeeperAI...")
    result = _exec(["DEL", "GatekeeperAI"])
    if result is None:
        _p("[FATAL] DEL failed")
        return

    _p(f"[PURGE] DEL result: {result}")

    remaining = _exec(["HLEN", "GatekeeperAI"])
    _p(f"[PURGE] HLEN after purge: {remaining}")
    _p("=== PURGE COMPLETE ===")


def run_flush(hard=False):
    """Очистка match:* ключей (hard) или live:* ключей (soft) через HSCAN."""
    if hard:
        _p("=== FLUSH MODE (hard=True) ===")
    else:
        _p("=== FLUSH MODE (hard=False) ===")

    if hard:
        _p("[FLUSH] Scanning for match:* keys via HSCAN...")
        keys = _hscan_all("GatekeeperAI", match_pattern="match:*")
        to_delete = [k for k in keys if k.startswith("match:") and not k.startswith("match:index:")]
        _p(f"[FLUSH] HARD: found {len(to_delete)} match:* keys to delete...")
    else:
        _p("[FLUSH] Scanning for live:* keys via HSCAN...")
        keys = _hscan_all("GatekeeperAI", match_pattern="live:*")
        to_delete = keys
        _p(f"[FLUSH] SOFT: found {len(to_delete)} live:* keys to delete...")

    if not to_delete:
        _p("[FLUSH] Nothing to delete.")
        return

    batch_size = 100
    total = len(to_delete)
    deleted = 0
    batches_done = 0

    for i in range(0, total, batch_size):
        batch = to_delete[i:i + batch_size]
        result = _exec(["HDEL", "GatekeeperAI"] + batch)
        if result is None:
            _p(f"[FLUSH] HDEL batch failed at offset {i}, skipping")
            time.sleep(1)
            continue
        deleted += len(batch)
        batches_done += 1
        if batches_done % 10 == 0 or deleted >= total:
            _p(f"[FLUSH] Deleted {deleted}/{total}...")
        time.sleep(0.05)

    _p(f"[FLUSH] Done: {deleted} keys deleted.")
    _p("=== FLUSH COMPLETE ===")


def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    hist = [k for k in fields if k.startswith("history:match:")]
    match = [k for k in fields if k.startswith("match:")
             and not k.startswith("history:")
             and not k.startswith("match:index:")]
    index = [k for k in fields if k.startswith("match:index:")]
    other = [k for k in fields
             if not k.startswith("history:match:")
             and not k.startswith("match:")]

    _p(f"\nTotal: {len(fields)} (history={len(hist)}, match={len(match)}, "
        f"index={len(index)}, other={len(other)})")
    if other[:10]:
        _p(f"Other keys sample: {other[:10]}")

    if match:
        _check_odds_format(match, fields)

    _p(f"\n=== DIAGNOSTICS COMPLETE ===")


if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] CWD: {os.getcwd()}")
    _p(f"[DIAG] Args: {sys.argv}")

    mode = ""
    flags = set()
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            val = arg[2:]
            if mode == "" and val in ("test", "history", "flush", "purge", "diagnostics"):
                mode = val
            else:
                flags.add(val)

    _p(f"[DIAG] Mode: {mode}")
    if flags:
        _p(f"[DIAG] Flags: {sorted(flags)}")

    if not _init_redis():
        _p("[FATAL] No Redis backend available")
        sys.exit(1)

    try:
        if mode == "test":
            run_test()
        elif mode == "history":
            run_history()
        elif mode == "flush":
            if "yes" not in flags:
                _p("Use --flush --yes to confirm")
            else:
                run_flush(hard="hard" in flags)
        elif mode == "purge":
            if "yes" not in flags:
                _p("Use --purge --yes to confirm")
            else:
                run_purge(hard="hard" in flags)
        else:
            run_diagnostics()
    except Exception as e:
        _p(f"[FATAL] Unhandled exception: {e}")
        traceback.print_exc()
