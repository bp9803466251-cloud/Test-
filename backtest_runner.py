#!/usr/bin/env python3
"""
backtest_runner.py — Раннер бектеста GatekeeperAI (§25.4).

Симуляция на history:match:* с temporal cutoff:
  1. Читает все history:match:* из Redis
  2. Сортирует по date_utc (хронологически)
  3. Для каждого матча строит history_map из предшествующих матчей тех же команд
  4. Вызывает analyze_match() из value_engine
  5. Сравнивает прогноз с фактическим результатом
  6. Считает Brier score, RPS, drift detection (sliding window)
  7. Сохраняет отчёт в backtest_report.json + Redis (analysis:backtest:latest)

§25.4: Drift detection — Brier worsened >=10% → флаг E005
§23.5: PAVA — если OOS < 100 → Calibration Status = LIMITED
§27: analyze_match никогда не падает — graceful degradation

Запуск:
  python backtest_runner.py                          # все матчи, window=50
  python backtest_runner.py --seasons 2425           # только сезон 2425
  python backtest_runner.py --leagues E0,SP1         # только Premier League + La Liga
  python backtest_runner.py --window 50              # размер скользящего окна
  python backtest_runner.py --limit 500              # лимит матчей
  python backtest_runner.py --dry-run                # без записи в Redis
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta

__version__ = "9.3-audited"
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
    """Ленивый импорт gatekeeper_hub."""
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError as e:
        logger.error(f"gatekeeper_hub не найден: {e}")
        return None


def _get_redis_hub():
    """Ленивый импорт redis_hub."""
    try:
        import redis_hub
        return redis_hub
    except ImportError as e:
        logger.error(f"redis_hub не найден: {e}")
        return None


def _get_value_engine():
    """Ленивый импорт value_engine."""
    try:
        import value_engine
        return value_engine
    except ImportError as e:
        logger.error(f"value_engine не найден: {e}")
        return None


def _fetch_history_matches(hub, seasons=None, leagues=None, limit=0):
    """
    Читает все history:match:* из Redis.
    Использует get_key() для чтения history-ключей.
    """
    redis_hub = _get_redis_hub()
    if redis_hub is None:
        logger.error("redis_hub недоступен — нет доступа к Redis")
        return []

    # Получаем все ключи history:match:*
    try:
        # Используем get_all_fields или сканирование
        if hasattr(redis_hub, "scan_keys"):
            keys = redis_hub.scan_keys("history:match:*")
        elif hasattr(redis_hub, "get_keys"):
            keys = redis_hub.get_keys("history:match:*")
        else:
            # Fallback: читаем через хаб, если есть метод
            logger.warning("scan_keys/get_keys недоступны — пробую через хаб")
            keys = []
            if hasattr(hub, "get_history_keys"):
                keys = hub.get_history_keys()

        if not keys:
            logger.warning("history:match:* ключи не найдены. Загрузите через football_data_loader.yml")
            return []
    except Exception as e:
        logger.error(f"Ошибка сканирования history:match:* — {e}")
        return []

    matches = []
    for key in keys:
        try:
            # Извлекаем canonical_id из ключа
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            cid = key.replace("history:match:", "")

            # Читаем матч
            if hasattr(redis_hub, "get_key"):
                raw = redis_hub.get_key(f"history:match:{cid}")
            elif hasattr(hub, "get_history_match"):
                raw = hub.get_history_match(cid)
            else:
                raw = redis_hub.get_key(f"history:match:{cid}")

            if raw is None:
                continue

            if isinstance(raw, str):
                match = json.loads(raw)
            elif isinstance(raw, dict):
                match = raw
            else:
                continue

            # Фильтр по сезонам
            if seasons:
                match_season = match.get("season", "")
                if match_season and match_season not in seasons:
                    continue

            # Фильтр по лигам
            if leagues:
                match_league = match.get("league_code", "")
                if match_league and match_league not in leagues:
                    continue

            matches.append(match)

        except Exception as e:
            logger.debug(f"Ошибка чтения матча {key}: {e}")
            continue

    # Сортировка по date_utc (хронологически — temporal cutoff)
    matches.sort(key=lambda m: m.get("date_utc", ""))

    # Лимит
    if limit > 0:
        matches = matches[:limit]

    logger.info(f"Загружено {len(matches)} history-матчей")
    return matches


def _build_history_map(matches, current_idx, lookback=200):
    """
    Строит history_map для матча из предшествующих матчей тех же команд.
    temporal cutoff: только матчи до current_idx.
    """
    current = matches[current_idx]
    home = current.get("home_clean", "")
    away = current.get("away_clean", "")

    history = []
    # Идём назад от current_idx
    for i in range(current_idx - 1, max(-1, current_idx - lookback - 1), -1):
        m = matches[i]
        m_home = m.get("home_clean", "")
        m_away = m.get("away_clean", "")

        # Матчи с участием тех же команд
        if home in (m_home, m_away) or away in (m_home, m_away):
            history.append(m)

    return history if history else None


def _actual_result(match):
    """Извлекает фактический результат матча из score."""
    score = match.get("score")
    if not score or not isinstance(score, dict):
        return None

    home = score.get("home")
    away = score.get("away")

    if home is None or away is None:
        return None

    if home > away:
        return "HOME"
    elif home < away:
        return "AWAY"
    else:
        return "DRAW"


def _brier_score(predicted_probs, actual_outcome):
    """
    Brier score для 1X2.
    predicted_probs: dict {"home": p, "draw": p, "away": p}
    actual_outcome: "HOME" / "DRAW" / "AWAY"
    """
    actual = {"home": 0, "draw": 0, "away": 0}
    key = actual_outcome.lower() if actual_outcome else "home"
    if key in actual:
        actual[key] = 1

    brier = 0
    for outcome in ["home", "draw", "away"]:
        p = predicted_probs.get(outcome, 0)
        brier += (p - actual[outcome]) ** 2

    return brier / 2  # Нормализованный Brier для 3 исходов


def _rps_score(predicted_probs, actual_outcome):
    """
    Ranked Probability Score для ординальных рынков (1X2).
    RPS измеряет качество вероятностных прогнозов для ранжированных исходов.
    Меньше = лучше.
    """
    outcomes = ["home", "draw", "away"]
    actual = [0, 0, 0]
    key = actual_outcome.lower() if actual_outcome else "home"
    if key in outcomes:
        actual[outcomes.index(key)] = 1

    # Кумулятивные суммы
    cum_pred = []
    cum_actual = []
    s_pred = 0
    s_actual = 0
    for i in range(len(outcomes)):
        s_pred += predicted_probs.get(outcomes[i], 0)
        s_actual += actual[i]
        cum_pred.append(s_pred)
        cum_actual.append(s_actual)

    rps = sum((cum_pred[i] - cum_actual[i]) ** 2 for i in range(len(outcomes)))
    return rps / (len(outcomes) - 1)


def _drift_detection(brier_history, window_size=50):
    """
    Drift detection (§25.4): Brier worsened >=10% → флаг E005.
    Скользящее окно: сравниваем средний Brier текущего окна с предыдущим.
    """
    if len(brier_history) < window_size * 2:
        return {"drift_detected": False, "drift_flags": 0, "avg_brier_recent": 0, "avg_brier_prev": 0}

    drift_flags = 0
    drift_points = []

    for i in range(window_size * 2, len(brier_history) + 1):
        recent = brier_history[i - window_size:i]
        prev = brier_history[i - window_size * 2:i - window_size]

        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)

        if avg_prev > 0:
            worsening = (avg_recent - avg_prev) / avg_prev
            if worsening >= 0.10:  # >=10% — флаг E005
                drift_flags += 1
                drift_points.append({
                    "index": i,
                    "avg_brier_prev": round(avg_prev, 4),
                    "avg_brier_recent": round(avg_recent, 4),
                    "worsening_pct": round(worsening * 100, 1)
                })

    avg_brier_recent = sum(brier_history[-window_size:]) / min(len(brier_history), window_size)
    avg_brier_prev = sum(brier_history[-window_size*2:-window_size]) / min(max(len(brier_history) - window_size, 0), window_size) if len(brier_history) >= window_size * 2 else 0

    return {
        "drift_detected": drift_flags > 0,
        "drift_flags": drift_flags,
        "drift_points": drift_points[:20],  # первые 20 точек
        "avg_brier_recent": round(avg_brier_recent, 4),
        "avg_brier_prev": round(avg_brier_prev, 4)
    }


def run_backtest(seasons=None, leagues=None, window_size=50, limit=0, dry_run=False):
    """
    Основная функция бектеста (§25.4).

    Returns:
        dict: отчёт бектеста
    """
    hub = _get_hub()
    ve = _get_value_engine()
    redis_hub = _get_redis_hub()

    if hub is None:
        return {"error": "gatekeeper_hub недоступен"}
    if ve is None:
        return {"error": "value_engine недоступен"}
    if redis_hub is None:
        return {"error": "redis_hub недоступен"}

    # Проверка Redis
    try:
        if hasattr(redis_hub, "is_redis_configured"):
            if not redis_hub.is_redis_configured():
                return {"error": "Redis не сконфигурирован. Проверьте SHARED_UPSTASH_REDIS_REST_URL"}
    except Exception:
        pass

    # Шаг 1: Загрузка history-матчей
    logger.info("=== ШАГ 1: Загрузка history-матчей ===")
    matches = _fetch_history_matches(hub, seasons, leagues, limit)

    if not matches:
        logger.error("Нет history-матчей. Загрузите через football_data_loader.yml")
        return {
            "error": "no_history_matches",
            "hint": "Запустите football_data_loader.yml для загрузки CSV"
        }

    # Шаг 2: Temporal cutoff — матчи уже отсортированы хронологически
    logger.info(f"=== ШАГ 2: Temporal cutoff — {len(matches)} матчей ===")
    logger.info(f"Диапазон: {matches[0].get('date_utc', '?')} → {matches[-1].get('date_utc', '?')}")

    # Шаг 3: Прогон analyze_match для каждого матча
    logger.info(f"=== ШАГ 3: Прогон analyze_match (window={window_size}) ===")

    brier_history = []
    rps_history = []
    hot_count = 0
    warm_count = 0
    no_bet_count = 0
    skipped_count = 0
    analyzed_count = 0
    correct_predictions = 0
    total_predictions = 0

    # Для калибровки
    calibration_bins = {  # 10 бинов по 0.1
        "0.0-0.1": {"predicted": [], "actual": []},
        "0.1-0.2": {"predicted": [], "actual": []},
        "0.2-0.3": {"predicted": [], "actual": []},
        "0.3-0.4": {"predicted": [], "actual": []},
        "0.4-0.5": {"predicted": [], "actual": []},
        "0.5-0.6": {"predicted": [], "actual": []},
        "0.6-0.7": {"predicted": [], "actual": []},
        "0.7-0.8": {"predicted": [], "actual": []},
        "0.8-0.9": {"predicted": [], "actual": []},
        "0.9-1.0": {"predicted": [], "actual": []},
    }

    for i, match in enumerate(matches):
        # Прогресс
        if (i + 1) % 500 == 0:
            logger.info(f"  Обработано {i+1}/{len(matches)} матчей...")

        # Строим history_map из предшествующих матчей
        history_map = _build_history_map(matches, i, lookback=200)

        # Получаем фактический результат
        actual = _actual_result(match)
        if actual is None:
            skipped_count += 1
            continue

        # Вызов analyze_match (§21.5 — чистая функция, не падает)
        try:
            result = ve.analyze_match(match, history_map)
        except Exception as e:
            logger.debug(f"analyze_match упал на матче {match.get('canonical_id', '?')}: {e}")
            skipped_count += 1
            continue

        if result is None:
            # §27: матч отброшен — нет данных
            skipped_count += 1
            continue

        analyzed_count += 1

        # Извлекаем прогноз
        value_analysis = result if isinstance(result, dict) else {}
        best_side = value_analysis.get("best_side", "")
        classification = value_analysis.get("classification", "NO BET")

        # Классификация
        if classification == "HOT":
            hot_count += 1
        elif classification == "WARM":
            warm_count += 1
        else:
            no_bet_count += 1

        # Проверка точности прогноза
        if best_side and actual:
            total_predictions += 1
            if best_side.upper() == actual:
                correct_predictions += 1

        # Brier score
        # Извлекаем вероятности из result
        # analyze_match возвращает value_analysis с p_home, p_draw, p_away
        # или внутри model_breakdown
        p_home = value_analysis.get("p_home", 0)
        p_draw = value_analysis.get("p_draw", 0)
        p_away = value_analysis.get("p_away", 0)

        # Если p_* нет на верхнем уровне — ищем в scenario_realistic
        if not p_home and not p_away:
            sr = value_analysis.get("scenario_realistic", {})
            p_home = sr.get("p_home", 0)
            p_away = sr.get("p_away", 0)
            p_draw = sr.get("p_draw", 1 - p_home - p_away)

        # Если всё ещё нет — пропускаем Brier
        if p_home or p_away:
            predicted_probs = {"home": p_home, "draw": p_draw, "away": p_away}
            brier = _brier_score(predicted_probs, actual)
            rps = _rps_score(predicted_probs, actual)
            brier_history.append(brier)
            rps_history.append(rps)

            # Калибровочные бины (для best_side)
            if best_side:
                p_best = predicted_probs.get(best_side.lower(), 0)
                bin_key = f"{int(p_best * 10) / 10:.1f}-{int(p_best * 10 + 1) / 10:.1f}"
                if bin_key in calibration_bins:
                    calibration_bins[bin_key]["predicted"].append(p_best)
                    calibration_bins[bin_key]["actual"].append(1 if best_side.upper() == actual else 0)

    # Шаг 4: Агрегация метрик
    logger.info("=== ШАГ 4: Агрегация метрик ===")

    avg_brier = sum(brier_history) / len(brier_history) if brier_history else 0
    avg_rps = sum(rps_history) / len(rps_history) if rps_history else 0
    accuracy = (correct_predictions / total_predictions * 100) if total_predictions > 0 else 0

    # Drift detection
    drift = _drift_detection(brier_history, window_size)

    # Calibration status (§23.5)
    oos_count = len(brier_history)
    if oos_count < 100:
        calibration_status = "LIMITED"
    else:
        calibration_status = "OK"

    # Калибровочные бины — summary
    calibration_table = []
    for bin_key, data in calibration_bins.items():
        if data["predicted"]:
            avg_pred = sum(data["predicted"]) / len(data["predicted"])
            avg_actual = sum(data["actual"]) / len(data["actual"])
            calibration_table.append({
                "bin": bin_key,
                "n": len(data["predicted"]),
                "avg_predicted": round(avg_pred, 3),
                "avg_actual": round(avg_actual, 3),
                "gap": round(abs(avg_pred - avg_actual), 3)
            })

    report = {
        "version": __version__,
        "timestamp": _now_msk(),
        "total_matches": len(matches),
        "analyzed_matches": analyzed_count,
        "skipped_matches": skipped_count,
        "hot_count": hot_count,
        "warm_count": warm_count,
        "no_bet_count": no_bet_count,
        "correct_predictions": correct_predictions,
        "total_predictions": total_predictions,
        "accuracy_pct": round(accuracy, 2),
        "avg_brier": round(avg_brier, 4),
        "avg_rps": round(avg_rps, 4),
        "oos_count": oos_count,
        "calibration_status": calibration_status,
        "window_size": window_size,
        "drift": drift,
        "calibration_table": calibration_table,
        "seasons_filter": seasons or "all",
        "leagues_filter": leagues or "all",
        "dry_run": dry_run
    }

    # Шаг 5: Сохранение отчёта
    logger.info("=== ШАГ 5: Сохранение отчёта ===")

    # Локальный файл
    try:
        with open("backtest_report.json", "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info("Отчёт сохранён: backtest_report.json")
    except Exception as e:
        logger.error(f"Не удалось сохранить backtest_report.json: {e}")

    # Redis (если не dry_run)
    if not dry_run:
        try:
            if hasattr(redis_hub, "set_key"):
                redis_hub.set_key("analysis:backtest:latest", json.dumps(report))
                logger.info("Отчёт сохранён в Redis: analysis:backtest:latest")
        except Exception as e:
            logger.warning(f"Не удалось сохранить в Redis: {e}")

    # Вывод сводки
    logger.info("")
    logger.info("═══════════════════════════════════════════════")
    logger.info("         BACKTEST SUMMARY")
    logger.info("═══════════════════════════════════════════════")
    logger.info(f"  Matches total:     {report['total_matches']}")
    logger.info(f"  Analyzed:           {report['analyzed_matches']}")
    logger.info(f"  Skipped:            {report['skipped_matches']}")
    logger.info(f"  HOT:                {report['hot_count']}")
    logger.info(f"  WARM:               {report['warm_count']}")
    logger.info(f"  NO BET:             {report['no_bet_count']}")
    logger.info(f"  Accuracy:           {report['accuracy_pct']}%")
    logger.info(f"  Avg Brier:          {report['avg_brier']}")
    logger.info(f"  Avg RPS:            {report['avg_rps']}")
    logger.info(f"  OOS count:          {report['oos_count']}")
    logger.info(f"  Calibration:        {report['calibration_status']}")
    logger.info(f"  Drift flags:        {report['drift']['drift_flags']}")
    logger.info(f"  Dry run:            {report['dry_run']}")
    logger.info("═══════════════════════════════════════════════")

    return report


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="GatekeeperAI Backtest Runner (§25.4)")
    parser.add_argument("--seasons", type=str, default="", help="Сезоны через запятую (пусто = все)")
    parser.add_argument("--leagues", type=str, default="", help="Лиги через запятую (пусто = все)")
    parser.add_argument("--window", type=int, default=50, help="Размер скользящего окна (default: 50)")
    parser.add_argument("--limit", type=int, default=0, help="Лимит матчей (0 = все)")
    parser.add_argument("--dry-run", action="store_true", help="Без записи в Redis")
    parser.add_argument("--version", action="version", version=f"backtest_runner {__version__}")

    args = parser.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",")] if args.seasons else None
    leagues = [l.strip() for l in args.leagues.split(",")] if args.leagues else None

    report = run_backtest(
        seasons=seasons,
        leagues=leagues,
        window_size=args.window,
        limit=args.limit,
        dry_run=args.dry_run
    )

    if "error" in report:
        logger.error(f"Backtest failed: {report['error']}")
        if "hint" in report:
            logger.error(f"Hint: {report['hint']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
