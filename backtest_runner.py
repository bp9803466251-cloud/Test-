#!/usr/bin/env python3
# backtest_runner.py
"""
Gatekeeper-AI Ecosystem: Раннер бэктеста GatekeeperAI.
Версия 9.3.2-chunk — синхронизировано с вероятностями CORE ENGINE V6.7 (Стандарт V5.7.12).

Обновления (патч контракта истории v9.3.1):
  - _fetch_history_matches выровнен по контракту писатель/читатель (§1.7d).
  - Удалены вызовы несуществующих redis_hub.get_key/set_key.
  - Голые except: continue заменены на честные счётчики.

Обновления (патч транспорта v9.3.2):
  - Fallback-чтение легаси-хэша переписано с HGETALL (один запрос на весь хэш:
    Backtest #5 получил 'The read operation timed out' на 9925 полях) на чанковую
    схему: HSCAN-курсор по именам полей + HMGET пачками HMGET_CHUNK_SIZE.
    Каждый запрос переносит малый объём — таймаут 6с и лимит Upstash 10MB не
    задеваются даже на больших архивах.
  - Неудачные чанки и нечитаемые записи считаются честно — без молчаливого
    проглатывания.
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

__version__ = "9.3.2-chunk"
__all__ = ["run_backtest", "main", "__version__"]

logger = logging.getLogger("backtest_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

MSK_TZ = timezone(timedelta(hours=3))
REPORT_PATH = "backtest_report.json"

def _now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")

def _write_report(report):
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Не удалось записать %s: %s", REPORT_PATH, e)

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

# ── [v9.3.2] Чанковое чтение легаси-хэша ──
def _root_addr(redis_hub):
    return getattr(redis_hub, "IMMUTABLE_ROOT_ADDRESS", "GatekeeperAI")

def _scan_hash_fields(redis_hub, pattern):
    """HSCAN-курсор по хэшу: возвращает только имена полей, значения не переносятся."""
    fields, cursor = [], "0"
    root = _root_addr(redis_hub)
    while True:
        res = redis_hub.redis_command(["HSCAN", root, cursor, "MATCH", pattern, "COUNT", "200"])
        if res is None:
            raise RuntimeError("HSCAN вернул None (сбой транспорта/breaker)")
        if not isinstance(res, (list, tuple)) or len(res) < 2:
            raise RuntimeError(f"HSCAN вернул неожиданный формат: {type(res).__name__}")
        cursor, batch = res[0], res[1]
        if isinstance(batch, list):
            fields.extend(k for k in batch if isinstance(k, str))
        if cursor in ("0", 0, None):
            break
    return fields

def _unwrap_envelope(raw):
    """Распаковка конверта v700-prod: payload в приоритете, плоский объект как фолбэк."""
    obj = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(obj, dict) and isinstance(obj.get("payload"), dict):
        return obj["payload"]
    return obj

def _fetch_hash_chunked(redis_hub, stats):
    """
    Читает легаси-историю из хэша пачками HMGET. Возвращает True, если путь применён
    (даже при частичных ошибках), None — если путь недоступен (нет redis_command).
    """
    if not hasattr(redis_hub, "redis_command"):
        return None
    try:
        fields = _scan_hash_fields(redis_hub, "history:match:*")
    except Exception as e:
        logger.warning("HSCAN хэша истории провалился: %s", e)
        return True  # путь доступен, но провалился — не маскируем get_all_fields

    if not fields:
        return True
    logger.info("Легаси-хэш: %d полей history:match:* — чанковое чтение", len(fields))

    root = _root_addr(redis_hub)
    chunk_size = int(getattr(redis_hub, "HMGET_CHUNK_SIZE", 50) or 50)
    for i in range(0, len(fields), chunk_size):
        chunk = fields[i:i + chunk_size]
        try:
            raws = redis_hub.redis_command(["HMGET", root] + chunk)
        except Exception as e:
            logger.warning("HMGET чанка [%d:%d] упал: %s", i, i + chunk_size, e)
            stats["fetch_errors"] += len(chunk)
            continue
        if raws is None or not isinstance(raws, list):
            logger.warning("HMGET чанка [%d:%d] вернул None — чанк пропущен", i, i + chunk_size)
            stats["fetch_errors"] += len(chunk)
            continue
        for field, raw in zip(chunk, raws):
            if not str(field).startswith("history:match:"):
                continue
            if raw is None:
                stats["parse_errors"] += 1
                logger.warning("Поле истории '%s' вернуло nil", field)
                continue
            try:
                value = _unwrap_envelope(raw)
            except (json.JSONDecodeError, TypeError) as e:
                stats["parse_errors"] += 1
                logger.warning("Нечитаемая запись истории в хэше '%s': %s", field, e)
                continue
            if not isinstance(value, dict):
                stats["parse_errors"] += 1
                logger.warning("Запись истории '%s': %s вместо dict", field, type(value).__name__)
                continue
            if stats["accept"](value):
                stats["n_hash"] += 1
    return True

def _fetch_history_matches(hub, seasons=None, leagues=None, limit=0):
    """
    Приоритет §1.7d: standalone-ключи history:match:* (scan_keys + get_history_match).
    Fallback: легаси-хранение истории полями хэша GatekeeperAI — чанково (v9.3.2).
    """
    redis_hub = _get_redis_hub()
    if redis_hub is None:
        return []

    matches = []
    seen_cids = set()
    stats = {"parse_errors": 0, "fetch_errors": 0, "n_standalone": 0, "n_hash": 0}

    def _accept(match):
        if not isinstance(match, dict):
            return False
        if seasons and match.get("season") not in seasons:
            return False
        if leagues and match.get("league_code") not in leagues:
            return False
        cid = match.get("canonical_id", "")
        if not cid or cid in seen_cids:
            return False
        seen_cids.add(cid)
        matches.append(match)
        return True
    stats["accept"] = _accept

    # 1) Канонический путь §1.7d: Root-ключи history:match:*
    try:
        keys = redis_hub.scan_keys("history:match:*") if hasattr(redis_hub, "scan_keys") else []
    except Exception as e:
        logger.warning("SCAN history:match:* провалился: %s — продолжаю через hash-фолбэк", e)
        keys = []

    for key in keys:
        if isinstance(key, bytes):
            key = key.decode("utf-8", errors="replace")
        cid = key.replace("history:match:", "")
        try:
            match = redis_hub.get_history_match(cid) if hasattr(redis_hub, "get_history_match") else None
        except Exception as e:
            logger.warning("Ошибка чтения standalone-ключа %s: %s", key, e)
            continue
        if match is None:
            stats["parse_errors"] += 1
            logger.warning("Standalone-ключ %s вернул None (нечитаем/отсутствует)", key)
            continue
        if _accept(match):
            stats["n_standalone"] += 1

    # 2) Fallback: легаси-хэш — чанково (HSCAN + HMGET), HGETALL только как последний резерв
    if stats["n_standalone"] == 0:
        chunked = _fetch_hash_chunked(redis_hub, stats)
        if chunked is None and hasattr(redis_hub, "get_all_fields"):
            # Резерв для окружений без redis_command: только малые хэши (риск таймаута на больших)
            logger.warning("redis_command недоступен — резервный HGETALL (риск таймаута на больших хэшах)")
            try:
                all_fields = redis_hub.get_all_fields()
            except Exception as e:
                logger.warning("HGETALL хэша истории провалился: %s", e)
                all_fields = {}
            for field, value in all_fields.items():
                if not str(field).startswith("history:match:"):
                    continue
                if not isinstance(value, dict):
                    stats["parse_errors"] += 1
                    continue
                if _accept(value):
                    stats["n_hash"] += 1

    if stats["parse_errors"]:
        logger.warning("Итого нечитаемых записей истории: %d", stats["parse_errors"])
    if stats["fetch_errors"]:
        logger.warning("Итого полей, потерянных на транспортных сбоях: %d", stats["fetch_errors"])
    logger.info("История загружена: standalone=%d, hash=%d, уникальных=%d, нечитаемых=%d, транспортных_сбоев=%d",
                stats["n_standalone"], stats["n_hash"], len(matches), stats["parse_errors"], stats["fetch_errors"])

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
        home = int(score.get("home")); away = int(score.get("away"))
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
    if not hub or not ve or not redis_hub:
        report = {"version": __version__, "timestamp": _now_msk(), "error": "components_missing",
                  "metrics_note": "not computed (components unavailable)"}
        _write_report(report)
        return report

    matches = _fetch_history_matches(hub, seasons, leagues, limit)
    if not matches:
        report = {"version": __version__, "timestamp": _now_msk(), "error": "no_history_matches",
                  "total_matches": 0, "analyzed_matches": 0, "hot_count": 0, "warm_count": 0,
                  "avg_brier": None, "avg_rps": None, "metrics_note": "not computed (no history matches)",
                  "calibration_status": "unknown", "drift": {"drift_detected": False, "drift_flags": 0},
                  "dry_run": dry_run}
        _write_report(report)
        return report

    brier_history, rps_history = [], []
    hot_count, warm_count, analyzed_count = 0, 0, 0
    correct_predictions, total_predictions = 0, 0
    engine_errors = 0

    for i, match in enumerate(matches):
        history_map = _build_history_map(matches, i, lookback=200)
        actual = _actual_result(match)
        if actual is None: continue
        try:
            result = ve.process_full_analysis(match)  # Вызов полной сигнатуры CORE ENGINE V6.7
        except Exception as e:
            engine_errors += 1
            logger.warning("value_engine не обработал матч %s: %s", match.get("canonical_id", i), e)
            continue
        if not result:
            engine_errors += 1
            logger.warning("value_engine вернул пустой результат для %s", match.get("canonical_id", i))
            continue

        analyzed_count += 1
        fair_probs = result.get("fair_probabilities", {})
        p_h = fair_probs.get("home", 33.3) / 100.0
        p_d = fair_probs.get("draw", 33.4) / 100.0
        p_a = fair_probs.get("away", 33.3) / 100.0

        predicted_probs = {"home": p_h, "draw": p_d, "away": p_a}
        brier_history.append(_brier_score(predicted_probs, actual))
        rps_history.append(_rps_score(predicted_probs, actual))

        pred = max(predicted_probs, key=predicted_probs.get)
        if pred == actual.lower(): correct_predictions += 1
        total_predictions += 1

        verdict = result.get("verdict", "")
        if "MAIN BET" in verdict: hot_count += 1
        elif "VALIDATED VALUE" in verdict: warm_count += 1

    avg_brier = sum(brier_history) / len(brier_history) if brier_history else None
    avg_rps = sum(rps_history) / len(rps_history) if rps_history else None
    drift = _drift_detection(brier_history, window_size)
    calibration_status = "unknown" if not brier_history else ("OK" if len(brier_history) >= 100 else "LIMITED")

    report = {
        "version": __version__, "timestamp": _now_msk(), "total_matches": len(matches),
        "analyzed_matches": analyzed_count, "engine_errors": engine_errors,
        "hot_count": hot_count, "warm_count": warm_count,
        "accuracy": round(correct_predictions / total_predictions, 4) if total_predictions else None,
        "avg_brier": round(avg_brier, 4) if avg_brier is not None else None,
        "avg_rps": round(avg_rps, 4) if avg_rps is not None else None,
        "metrics_note": "not computed" if avg_brier is None else "computed",
        "calibration_status": calibration_status, "drift": drift, "dry_run": dry_run
    }

    if not dry_run:
        # [v9.3.1 FIX] redis_hub.set_key не существует — штатный API хэша: save_to_cache
        redis_hub.save_to_cache("analysis:backtest:latest", report)
    _write_report(report)
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
  
