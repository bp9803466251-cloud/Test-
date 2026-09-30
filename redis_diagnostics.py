
#!/usr/bin/env python3
"""redis_diagnostics.py — GATEKEEPER-AI diagnostics"""

import os
import sys
import json
import time
import traceback

def _p(msg):
    print(msg, flush=True)

try:
    import redis_hub
    _p("[DIAG] Import redis_hub: OK")
except Exception as e:
    _p(f"[FATAL] Cannot import redis_hub: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    import gatekeeper_hub
    _p("[DIAG] Import gatekeeper_hub: OK")
except Exception as e:
    _p(f"[WARN] Cannot import gatekeeper_hub: {e}")


def _safe_json(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except:
            return val
    return val


def _unwrap(raw):
    if not isinstance(raw, dict):
        return raw
    if "payload" in raw and isinstance(raw["payload"], dict):
        return raw["payload"]
    return raw


def _exec(cmd):
    try:
        return redis_hub._execute_upstash_cmd(cmd)
    except Exception as e:
        _p(f"[ERROR] _execute_upstash_cmd({cmd[0]}) failed: {e}")
        traceback.print_exc()
        return None


def _parse_args(argv):
    """Parse argv: first --mode sets mode, rest are flags."""
    mode = ""
    flags = set()
    valid_modes = ("test", "history", "flush", "diagnostics", "purge")
    for arg in argv[1:]:
        if arg.startswith("--"):
            val = arg[2:]
            if mode == "" and val in valid_modes:
                mode = val
            else:
                flags.add(val)
    return mode, flags


def load_all_fields():
    _p("[LOAD] Getting HKEYS...")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if keys is None:
        _p("[FATAL] HKEYS returned None")
        return {}
    if isinstance(keys, str):
        try:
            keys = json.loads(keys)
        except:
            keys = [keys]
    _p(f"[LOAD] HKEYS: {len(keys)} keys")
    if not keys:
        return {}

    all_fields = {}
    batch_size = 50
    total = len(keys)

    for i in range(0, total, batch_size):
        batch = keys[i:i + batch_size]
        bn = i // batch_size + 1
        tb = (total + batch_size - 1) // batch_size

        vals = _exec(["HMGET", "GatekeeperAI"] + batch)
        if vals is None:
            _p(f"[WARN] HMGET batch {bn}/{tb} returned None, skipping")
            time.sleep(1)
            continue

        if isinstance(vals, str):
            try:
                vals = json.loads(vals)
            except:
                pass

        if isinstance(vals, list):
            for k, v in zip(batch, vals):
                if v is not None:
                    all_fields[k] = _safe_json(v)

        if bn % 20 == 0 or bn == tb:
            _p(f"[LOAD] Batch {bn}/{tb} — {len(all_fields)} fields loaded")
        time.sleep(0.2)

    _p(f"[LOAD] Done: {len(all_fields)} fields")
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
    except:
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


def _check_odds_format(payload):
    """Check odds format: 1x2 / current / flat / empty / none."""
    if not isinstance(payload, dict):
        return "none"
    odds = payload.get("odds", {})
    if not isinstance(odds, dict) or not odds:
        return "none"
    if "1x2" in odds:
        s = odds.get("1x2", {})
        if isinstance(s, dict) and s.get("current"):
            return "1x2"
        return "empty"
    if "current" in odds and isinstance(odds["current"], dict) and odds["current"]:
        return "current"
    if any(k in odds for k in ("home", "draw", "away")):
        return "flat"
    return "empty"


def _get_sources_count(payload):
    """Count odds sources."""
    if not isinstance(payload, dict):
        return 0
    odds = payload.get("odds", {})
    if not isinstance(odds, dict):
        return 0
    s = odds.get("1x2", {})
    if not isinstance(s, dict):
        return 0
    sources = s.get("sources", [])
    return len(sources) if isinstance(sources, list) else 0


def _get_verification(payload):
    """Get verification level."""
    if not isinstance(payload, dict):
        return "UNVERIFIED"
    odds = payload.get("odds", {})
    if not isinstance(odds, dict):
        return "UNVERIFIED"
    s = odds.get("1x2", {})
    if not isinstance(s, dict):
        return "UNVERIFIED"
    return s.get("_verification", "UNVERIFIED")


# ---------------------------------------------------------------------------
# MODE: test
# ---------------------------------------------------------------------------
def run_test():
    _p("=== TEST MODE ===")
    _p("\n--- 1. PING ---")
    r = _exec(["PING"])
    _p(f"  Result: {r!r}")

    _p("\n--- 2. is_redis_available ---")
    try:
        avail = redis_hub.is_redis_available()
        _p(f"  Available: {avail}")
    except Exception as e:
        _p(f"  Error: {e}")

    _p("\n--- 3. HLEN ---")
    r = _exec(["HLEN", "GatekeeperAI"])
    _p(f"  HLEN: {r}")

    _p("\n--- 4. HKEYS (first 10) ---")
    r = _exec(["HKEYS", "GatekeeperAI"])
    if r is None:
        _p("  HKEYS returned None!")
        return
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except:
            r = [r]
    _p(f"  Total: {len(r)}")
    _p(f"  First 10: {r[:10]}")

    _p("\n--- 5. Hub functions test ---")
    try:
        from gatekeeper_hub import build_canonical_id, get_match, get_all_odds
        cid = build_canonical_id("Chelsea", "Arsenal", "2026-09-30T19:00:00Z")
        _p(f"  build_canonical_id: {cid}")
        m = get_match(cid)
        _p(f"  get_match({cid}): {'found' if m else 'None (expected for new Redis)'}")
        if m:
            o = get_all_odds(m)
            _p(f"  get_all_odds: sources={len(o.get('sources', []))}, verification={o.get('verification', '?')}")
    except Exception as e:
        _p(f"  Hub test error: {e}")

    _p("\n=== TEST COMPLETE ===")


# ---------------------------------------------------------------------------
# MODE: history
# ---------------------------------------------------------------------------
def run_history():
    _p("=== HISTORY MODE ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return

    hist_keys = sorted([k for k in fields if k.startswith("history:match:")])
    match_keys = sorted([k for k in fields if k.startswith("match:") and not k.startswith("history:")])
    index_keys = sorted([k for k in fields if k.startswith("match:index:") or k.startswith("history:index:") or k.startswith("analysis:index:")])
    match_keys = [k for k in match_keys if not k.startswith("match:index:")]
    other_keys = sorted([k for k in fields if not k.startswith("history:match:") and not k.startswith("match:")])

    _p(f"\n--- Key breakdown ---")
    _p(f"  history:match:* = {len(hist_keys)}")
    _p(f"  match:*         = {len(match_keys)}")
    _p(f" index:*         = {len(index_keys)}")
    _p(f"  other           = {len(other_keys)}")

    # --- Parse history ---
    hist_data = {}
    hist_errors = 0
    hist_odds_formats = {"1x2": 0, "current": 0, "flat": 0, "empty": 0, "none": 0}
    hist_sources = {0: 0, 1: 0, 2: 0, 3: 0}
    hist_verification = {"VERIFIED": 0, "WARNING": 0, "UNVERIFIED": 0}

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
        fmt = _check_odds_format(payload)
        hist_odds_formats[fmt] = hist_odds_formats.get(fmt, 0) + 1
        sc = _get_sources_count(payload)
        bucket = min(sc, 3)
        hist_sources[bucket] = hist_sources.get(bucket, 0) + 1
        v = _get_verification(payload)
        hist_verification[v] = hist_verification.get(v, 0) + 1

    _p(f"\n--- History parse ---")
    _p(f"  Parsed: {len(hist_data)}, Errors: {hist_errors}")

    _p(f"\n--- History odds format ---")
    for k in ("1x2", "current", "flat", "empty", "none"):
        _p(f"  {k}: {hist_odds_formats.get(k, 0)}")

    _p(f"\n--- History sources count ---")
    for k in sorted(hist_sources.keys()):
        label = f"{k}+ sources" if k > 0 else "0 sources"
        _p(f"  {label}: {hist_sources[k]}")

    _p(f"\n--- History verification ---")
    for k in ("VERIFIED", "WARNING", "UNVERIFIED"):
        _p(f"  {k}: {hist_verification.get(k, 0)}")

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
    for lg in sorted(past_leagues.keys(), key=lambda x: -past_leagues[x]):
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

    _p(f"\n{'=' * 60}")
    _p(f"=== VERIFICATION ===")
    _p(f"  history:match:*  = {len(hist_data)}")
    _p(f"  match:* past     = {len(past_match)}")
    _p(f"  match:* future   = {len(future_match)}")
    _p(f"  match:* unknown  = {len(unknown_match)}")
    _p(f"  Duplicates       = {len(past_match) - len(lost)}")
    _p(f"  Lost             = {len(lost)}")
    _p(f"  history + lost   = {len(hist_data) + len(lost)}")
    _p(f"\n=== HISTORY COMPLETE ===")


# ---------------------------------------------------------------------------
# MODE: diagnostics
# ---------------------------------------------------------------------------
def run_diagnostics():
    _p("=== DIAGNOSTICS ===")
    fields = load_all_fields()
    if not fields:
        _p("[FATAL] No fields loaded")
        return
    hist = [k for k in fields if k.startswith("history:match:")]
    match_raw = [k for k in fields if k.startswith("match:") and not k.startswith("history:")]
    index = [k for k in match_raw if k.startswith("match:index:")]
    match = [k for k in match_raw if not k.startswith("match:index:")]
    other = [k for k in fields if not k.startswith("history:match:") and not k.startswith("match:")]

    _p(f"\nTotal: {len(fields)} (history={len(hist)}, match={len(match)}, index={len(index)}, other={len(other)})")
    if other[:10]:
        _p(f"Other keys sample: {other[:10]}")

    # Odds format check for match:* keys
    odds_formats = {"1x2": 0, "current": 0, "flat": 0, "empty": 0, "none": 0}
    sources_count = {0: 0, 1: 0, 2: 0, 3: 0}
    verification = {"VERIFIED": 0, "WARNING": 0, "UNVERIFIED": 0}

    for key in match:
        payload = _unwrap(fields[key])
        fmt = _check_odds_format(payload)
        odds_formats[fmt] += 1
        sc = _get_sources_count(payload)
        bucket = min(sc, 3)
        sources_count[bucket] = sources_count.get(bucket, 0) + 1
        v = _get_verification(payload)
        verification[v] = verification.get(v, 0) + 1

    _p(f"\n--- match:* odds format ---")
    for k in ("1x2", "current", "flat", "empty", "none"):
        _p(f"  {k}: {odds_formats[k]}")

    _p(f"\n--- match:* sources count ---")
    for k in sorted(sources_count.keys()):
        label = f"{k}+ sources" if k > 0 else "0+ sources"
        _p(f"  {label}: {sources_count.get(k, 0)}")

    _p(f"\n--- match:* verification ---")
    for k in ("VERIFIED", "WARNING", "UNVERIFIED"):
        _p(f"  {k}: {verification.get(k, 0)}")

    _p(f"\n=== DIAGNOSTICS COMPLETE ===")


# ---------------------------------------------------------------------------
# MODE: flush (match:* only)
# ---------------------------------------------------------------------------
def run_flush(hard: bool):
    _p(f"=== FLUSH MODE (hard={hard}) ===")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if not keys:
        _p("[FLUSH] No keys found.")
        return
    if isinstance(keys, str):
        try:
            keys = json.loads(keys)
        except:
            keys = [keys]

    if hard:
        to_delete = [k for k in keys if k.startswith("match:")]
        _p(f"[FLUSH] HARD: deleting {len(to_delete)} match:* keys...")
    else:
        to_delete = [k for k in keys if k.startswith("live:")]
        _p(f"[FLUSH] SOFT: deleting {len(to_delete)} live: keys...")

    deleted = 0
    for k in to_delete:
        _exec(["HDEL", "GatekeeperAI", k])
        deleted += 1
        if deleted % 500 == 0:
            _p(f"[FLUSH] Deleted {deleted}/{len(to_delete)}...")
    _p(f"[FLUSH] Done: {deleted} keys deleted.")


# ---------------------------------------------------------------------------
# MODE: purge (ALL keys — complete wipe)
# ---------------------------------------------------------------------------
def run_purge():
    _p("=== PURGE MODE ===")
    _p("[PURGE] This will DELETE ALL keys from GatekeeperAI hash!")
    keys = _exec(["HKEYS", "GatekeeperAI"])
    if not keys:
        _p("[PURGE] No keys found. Redis is already empty.")
        return
    if isinstance(keys, str):
        try:
            keys = json.loads(keys)
        except:
            keys = [keys]

    total = len(keys)
    _p(f"[PURGE] Found {total} keys to delete")

    # Group by prefix for visibility
    prefixes = {}
    for k in keys:
        prefix = k.split(":")[0] + ":" if ":" in k else k
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
    _p(f"[PURGE] Breakdown:")
    for p in sorted(prefixes.keys(), key=lambda x: -prefixes[x]):
        _p(f"  {p} = {prefixes[p]}")

    deleted = 0
    for k in keys:
        _exec(["HDEL", "GatekeeperAI", k])
        deleted += 1
        if deleted % 500 == 0:
            _p(f"[PURGE] Deleted {deleted}/{total}...")
    _p(f"[PURGE] Done: {deleted} keys deleted.")

    # Verify
    remaining = _exec(["HLEN", "GatekeeperAI"])
    _p(f"[PURGE] Verification — HLEN: {remaining}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _p(f"[DIAG] Python {sys.version}")
    _p(f"[DIAG] CWD: {os.getcwd()}")
    _p(f"[DIAG] Args: {sys.argv}")

    mode, flags = _parse_args(sys.argv)
    _p(f"[DIAG] Mode: {mode}")
    if flags:
        _p(f"[DIAG] Flags: {sorted(flags)}")

    try:
        if mode == "test":
            run_test()
        elif mode == "history":
            run_history()
        elif mode == "flush":
            if "yes" in flags:
                run_flush(hard="hard" in flags)
            else:
                _p("Use --flush --yes to confirm (add --hard for full match:* deletion)")
        elif mode == "purge":
            if "yes" in flags:
                run_purge()
            else:
                _p("Use --purge --yes to confirm. This will DELETE EVERYTHING.")
        else:
            run_diagnostics()
    except Exception as e:
        _p(f"[FATAL] Unhandled exception: {e}")
        traceback.print_exc()
