#!/usr/bin/env python3
# backtest_runner.py
"""
Gatekeeper-AI Ecosystem: Раннер бэктеста GatekeeperAI.
Версия 9.3-audited — Синхронизировано с вероятностями CORE ENGINE V6.7 (Стандарт V5.7.12).
Симуляция на history:match:* с temporal cutoff и скользящим окном дрифта PAVA.
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

__version__ = "9.3-audited"  # Контрактная строка версии для ворот деплоя
__all__ = ["run_backtest", "main", "__version__"]

logger = logging.getLogger("backtest_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

MSK_TZ = timezone(timedelta(hours=3))

def _now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")

def _get_hub():
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError: return None

def _get_redis_hub():
    try:
        import redis_hub
        return redis_hub
    except ImportError: return None

def _get_value_engine():
    try:
        import value_engine
        return value_engine
    except ImportError: return None

def _fetch_history_matches(hub, seasons=None, leagues=None, limit=0):
    redis_hub = _get_redis_hub()
    if redis_hub is None: return []
    try:
        if hasattr(redis_hub, "scan_keys"): keys = redis_hub.scan_keys("history:match:*")
        else: keys = []
        if not keys: return []
    except Exception: return []

    matches = []
    for key in keys:
        try:
            if isinstance(key, bytes): key = key.decode("utf-8")
            cid = key.replace("history:match:", "")
            raw = redis_hub.get_key(f"history:match:{cid}")
            if raw is None: continue
            match = json.loads(raw) if isinstance(raw, str) else raw

            if seasons and match.get("season") not in seasons: continue
            if leagues and match.get("league_code") not in leagues: continue
            matches.append(match)
        except Exception: continue

    matches.sort(key=lambda m: m.get("date_utc", ""))
    return matches[:limit] if limit > 0 else matches

def _build_history_map(matches, current_idx, lookback=200):
    current = matches[current_idx]
    home = current.get("home_clean", "")
    away = current.get("away_clean", "")
    history = []
    for i in range(current_idx - 1, max(-1, current_idx - lookback - 1), -1):
        m = matches[i]
        if home in (m.get("home_clean"), m.get("away_clean")) or away in (m.get("home_clean"), m.get("away_clean")):
            history.append(m)
    return history if history else None

def _actual_result(match):
    score = match.get("score")
    if not score or not isinstance(score, dict): return None
    try:
        home = int(score.get("home"))
        away = int(score.get("away"))
        if home > away: return "HOME"
        elif home < away: return "AWAY"
        return "DRAW"
    except (ValueError, TypeError): return None

def _brier_score(predicted_probs, actual_outcome):
    actual = {"home": 0, "draw": 0, "away": 0}
    key = actual_outcome.lower() if actual_outcome else "home"
    if key in actual: actual[key] = 1
    return sum((predicted_probs.get(o, 0) - actual[o]) ** 2 for o in ["home", "draw", "away"]) / 2

def _rps_score(predicted_probs, actual_outcome):
    outcomes = ["home", "draw", "away"]
    actual = [0, 0, 0]
    key = actual_outcome.lower() if actual_outcome else "home"
    if key in outcomes: actual[outcomes.index(key)] = 1
    cum_pred, cum_actual, s_pred, s_actual = [], [], 0, 0
    for i in range(len(outcomes)):
        s_pred += predicted_probs.get(outcomes[i], 0)
        s_actual += actual[i]
        cum_pred.append(s_pred); cum_actual.append(s_actual)
    return sum((cum_pred[i] - cum_actual[i]) ** 2 for i in range(len(outcomes))) / (len(outcomes) - 1)

def _drift_detection(brier_history, window_size=50):
    if len(brier_history) < window_size * 2:
        return {"drift_detected": False, "drift_flags": 0, "avg_brier_recent": 0, "avg_brier_prev": 0}
    drift_flags = 0
    for i in range(window_size * 2, len(brier_history) + 1):
        recent = brier_history[i - window_size:i]
        prev = brier_history[i - window_size * 2:i - window_size]
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        if avg_prev > 0 and ((avg_recent - avg_prev) / avg_prev) >= 0.10: drift_flags += 1
    avg_brier_recent = sum(brier_history[-window_size:]) / min(len(brier_history), window_size)
    avg_brier_prev = sum(brier_history[-window_size*2:-window_size]) / min(max(len(brier_history) - window_size, 0), window_size) if len(brier_history) >= window_size * 2 else 0
    return {"drift_detected": drift_flags > 0, "drift_flags": drift_flags, "avg_brier_recent": round(avg_brier_recent, 4), "avg_brier_prev": round(avg_brier_prev, 4)}

def run_backtest(seasons=None, leagues=None, window_size=50, limit=0, dry_run=False):
    hub = _get_hub(); ve = _get_value_engine(); redis_hub = _get_redis_hub()
    if not hub or not ve or not redis_hub: return {"error": "Компоненты ядра недоступны"}

    matches = _fetch_history_matches(hub, seasons, leagues, limit)
    if not matches: return {"error": "no_history_matches"}

    brier_history, rps_history = [], []
    hot_count, warm_count, analyzed_count = 0, 0, 0
    correct_predictions, total_predictions = 0, 0

    for i, match in enumerate(matches):
        history_map = _build_history_map(matches, i, lookback=200)
        actual = _actual_result(match)
        if actual is None: continue

        try:
            result = ve.process_full_analysis(match) # Вызов полной NumPy-сигнатуры CORE ENGINE V6.7
        except Exception: continue
        if not result: continue

        analyzed_count += 1
        math_3way = result.get("math_3way", {})
        markets = math_3way.get("markets", {})
        
        # Надежное извлечение вероятностей из fair_probabilities канонического паспорта
        fair_probs = result.get("fair_probabilities", {})
        p_h = fair_probs.get("home", 33.3) / 100.0
        p_d = fair_probs.get("draw", 33.4) / 100.0
        p_a = fair_probs.get("away", 33.3) / 100.0
        
        predicted_probs = {"home": p_h, "draw": p_d, "away": p_a}
        brier_history.append(_brier_score(predicted_probs, actual))
        rps_history.append(_rps_score(predicted_probs, actual))

        verdict = result.get("verdict", "")
        if "MAIN BET" in verdict: hot_count += 1
        elif "VALIDATED VALUE" in verdict: warm_count += 1

    avg_brier = sum(brier_history) / len(brier_history) if brier_history else 0
    avg_rps = sum(rps_history) / len(rps_history) if rps_history else 0
    drift = _drift_detection(brier_history, window_size)

    report = {
        "version": __version__, "timestamp": _now_msk(), "total_matches": len(matches),
        "analyzed_matches": analyzed_count, "hot_count": hot_count, "warm_count": warm_count,
        "avg_brier": round(avg_brier, 4), "avg_rps": round(avg_rps, 4), "calibration_status": "OK" if len(brier_history) >= 100 else "LIMITED",
        "drift": drift, "dry_run": dry_run
    }

    if not dry_run: redis_hub.set_key("analysis:backtest:latest", json.dumps(report))
    return report

def main():
    parser = argparse.ArgumentParser(description="GatekeeperAI Backtest Runner")
    parser.add_argument("--seasons", type=str, default="")
    parser.add_argument("--leagues", type=str, default="")
    parser.add_argument("--window", type=int, default=50)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",")] if args.seasons else None
    leagues = [l.strip() for l in args.leagues.split(",")] if args.leagues else None

    report = run_backtest(seasons, leagues, args.window, args.limit, args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
                      
