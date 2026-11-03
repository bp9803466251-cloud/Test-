#!/usr/bin/env python3
"""
match_linker.py — Модуль связывания реальных/актуальных матчей с историей из Redis
и отправки сводного дашборда в Telegram (Gatekeeper-AI v9.6.8-patched).
"""

import os
import json
import logging
from datetime import datetime, timezone

__version__ = "9.6.8-patched"
__all__ = ["link_match_with_history", "process_upcoming_matches", "__version__"]

logger = logging.getLogger("match_linker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)


def _get_redis_hub():
    try:
        import redis_hub
        return redis_hub
    except ImportError as e:
        logger.error(f"redis_hub не найден: {e}")
        return None


def _get_value_engine():
    try:
        import value_engine
        return value_engine
    except ImportError as e:
        logger.error(f"value_engine не найден: {e}")
        return None


def fetch_all_history_pool():
    """История: Root-ключи history:match:* (v9.6.8 §1.7d) + hash-фолбэк (легаси)."""
    redis_hub = _get_redis_hub()
    if not redis_hub:
        return []

    matches = []

    # Уровень 1: Root-ключи (новая топология v9.6.8)
    scan_keys = getattr(redis_hub, "scan_keys", None)
    get_history_match = getattr(redis_hub, "get_history_match", None)
    if scan_keys and get_history_match:
        try:
            for key in scan_keys("history:match:*"):
                cid = key.replace("history:match:", "") if isinstance(key, str) else key
                m = get_history_match(cid)
                if isinstance(m, dict):
                    matches.append(m)
        except Exception as e:
            logger.warning(f"Root-скан истории недоступен: {e}")

    # Уровень 2: hash-фолбэк (легаси-записи до миграции)
    try:
        all_fields = redis_hub.get_all_fields()
        for key, raw in (all_fields or {}).items():
            if not key.startswith("history:match:"):
                continue
            try:
                match = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(match, dict):
                    matches.append(match)
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Ошибка получения истории из Redis: {e}")

    # Дедупликация по canonical_id: Root-ключи приоритетнее легаси
    dedup = {}
    for m in matches:
        cid = m.get("canonical_id") or f"{m.get('home_clean')}__{m.get('away_clean')}__{m.get('date_utc')}"
        if cid not in dedup:
            dedup[cid] = m
    return sorted(dedup.values(), key=lambda m: m.get("date_utc", ""))


def build_team_history_map(target_match, history_pool, lookback=200):
    target_date = target_match.get("date_utc", "")
    home = target_match.get("home_clean") or target_match.get("home", "").lower().strip()
    away = target_match.get("away_clean") or target_match.get("away", "").lower().strip()

    if not home or not away:
        return None

    history = []
    for m in reversed(history_pool):
        m_date = m.get("date_utc", "")
        if target_date and m_date and m_date >= target_date:
            continue

        m_home = m.get("home_clean") or m.get("home", "").lower().strip()
        m_away = m.get("away_clean") or m.get("away", "").lower().strip()

        if home == m_home or home == m_away or away == m_home or away == m_away:
            history.append(m)
            if len(history) >= lookback:
                break

    return history if history else None


def link_match_with_history(match_data, history_pool=None, lookback=200):
    ve = _get_value_engine()
    if not ve:
        return {"error": "value_engine недоступен"}

    if history_pool is None:
        history_pool = fetch_all_history_pool()

    history_map = build_team_history_map(match_data, history_pool, lookback=lookback)

    try:
        analysis_result = ve.analyze_match(match_data, history_map)
        return analysis_result
    except Exception as e:
        logger.error(f"Ошибка при анализе матча через Value Engine: {e}")
        return None


def _load_live_matches():
    """Единое чтение live-контура через gatekeeper_hub (§1.4). Без обходных путей.

    Возвращает dict матчей, {} если контур пуст, None при ошибке окружения.
    """
    try:
        import gatekeeper_hub
    except ImportError as e:
        logger.error(f"gatekeeper_hub недоступен: {e}")
        return None

    get_all = getattr(gatekeeper_hub, "get_all_matches", None)
    if not get_all:
        logger.error("gatekeeper_hub.get_all_matches недоступен")
        return None

    try:
        data = get_all()
    except Exception as e:
        logger.error(f"Ошибка чтения через gatekeeper_hub: {e}")
        return None

    if isinstance(data, dict) and data:
        return data
    logger.warning("Live-контур пуст (индекс + шарды).")
    return {}


def process_upcoming_matches():
    """Находит актуальные матчи, связывает с историей, анализирует и отправляет дашборд.

    Возвращает True при успешном прогоне (включая пустой контур),
    False при ошибке окружения или отправки.
    """
    redis_hub = _get_redis_hub()
    if not redis_hub:
        logger.error("redis_hub недоступен")
        return False

    logger.info("=== ЗАПУСК ЛИНКОВКИ И АНАЛИЗА АКТУАЛЬНЫХ МАТЧЕЙ ===")

    all_matches = _load_live_matches()
    if all_matches is None:
        return False
    if not all_matches:
        logger.info("Live-контур пуст — нечего линковать.")
        return True

    history_pool = fetch_all_history_pool()
    logger.info(f"Загружен исторический пул: {len(history_pool)} матчей")

    analyzed_count = 0
    hot_count = 0
    warm_count = 0
    no_bet_count = 0

    analyzed_results = []

    for cid, match_data in all_matches.items():
        try:
            if not isinstance(match_data, dict):
                continue

            status = match_data.get("status", "").lower()
            if status in ("completed", "postponed", "cancelled",
                          "ft", "finished", "aet", "ended"):
                continue

            result = link_match_with_history(match_data, history_pool)
            if not result:
                continue

            analyzed_count += 1
            classification = result.get("classification", "NO BET")

            # Сохраняем исходные данные матча внутри результата для дашборда
            result["_match"] = match_data

            if classification == "HOT":
                hot_count += 1
                analyzed_results.append(result)
            elif classification == "WARM":
                warm_count += 1
                analyzed_results.append(result)
            else:
                no_bet_count += 1

        except Exception as e:
            logger.warning(f"Ошибка обработки матча {cid}: {e}")
            continue

    logger.info("═══════════════════════════════════════════════")
    logger.info("         MATCH LINKER & ENGINE SUMMARY         ")
    logger.info("═══════════════════════════════════════════════")
    logger.info(f"  Analyzed matches:   {analyzed_count}")
    logger.info(f"  HOT signals:        {hot_count}")
    logger.info(f"  WARM signals:       {warm_count}")
    logger.info(f"  NO BET:             {no_bet_count}")
    logger.info("═══════════════════════════════════════════════")

    stats = {
        "analyzed": analyzed_count,
        "hot": hot_count,
        "warm": warm_count
    }

    try:
        import telegram_transport
    except ImportError as e:
        logger.error(f"telegram_transport недоступен: {e}")
        return False

    logger.info("Отправка сводного дашборда в Telegram...")
    try:
        if hasattr(telegram_transport, "send_dashboard"):
            telegram_transport.send_dashboard(analyzed_results, stats)
        else:
            _send_custom_dashboard(analyzed_results, stats)
    except Exception as e:
        logger.error(f"Не удалось отправить дашборд в Telegram: {e}")
        return False

    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if process_upcoming_matches() else 1)

