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
    """Единое чтение live-контура через gatekeeper_hub (§1.4) с легаси-фолбэком."""
    try:
        import gatekeeper_hub
        get_all = getattr(gatekeeper_hub, "get_all_matches", None)
        if get_all:
            data = get_all()
            if isinstance(data, dict) and data:
                return data
            logger.warning("Live-контур пуст (индекс + шарды).")
            return {}
    except ImportError as e:
        logger.warning(f"gatekeeper_hub недоступен, легаси-фолбэк на HGETALL: {e}")
    except Exception as e:
        logger.error(f"Ошибка чтения через gatekeeper_hub: {e}")

    # Легаси-фолбэк: прямой HGETALL (старый хаб до деплоя)
    redis_hub = _get_redis_hub()
    if not redis_hub:
        return {}
    try:
        raw_all = redis_hub.get_all_fields() or {}
        result = {}
        for key, raw in raw_all.items():
            if key.startswith("history:match:") or key.startswith("system:"):
                continue
            try:
                m = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(m, dict):
                    result[key] = m
            except Exception:
                continue
        return result
    except Exception as e:
        logger.error(f"Ошибка чтения Redis: {e}")
        return {}


def process_upcoming_matches():
    """Находит актуальные матчи в Redis, связывает с историей, анализирует и отправляет сводный дашборд."""
    redis_hub = _get_redis_hub()
    if not redis_hub:
        logger.error("redis_hub недоступен")
        return

    logger.info("=== ЗАПУСК ЛИНКОВКИ И АНАЛИЗА АКТУАЛЬНЫХ МАТЧЕЙ ===")

    all_matches = _load_live_matches()
    if not all_matches:
        return

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
            logger.debug(f"Ошибка обработки ключа {key}: {e}")
            continue

    logger.info("═══════════════════════════════════════════════")
    logger.info("         MATCH LINKER & ENGINE SUMMARY         ")
    logger.info("═══════════════════════════════════════════════")
    logger.info(f"  Analyzed matches:   {analyzed_count}")
    logger.info(f"  HOT signals:        {hot_count}")
    logger.info(f"  WARM signals:       {warm_count}")
    logger.info(f"  NO BET:             {no_bet_count}")
    logger.info("═══════════════════════════════════════════════")

    # Формирование и отправка единого сводного дашборда по стандарту v2.0
    stats = {
        "analyzed": analyzed_count,
        "hot": hot_count,
        "warm": warm_count
    }
    
    try:
        import telegram_transport
        logger.info("Отправка сводного дашборда в Telegram...")
        telegram_transport.send_dashboard(analyzed_results, stats) if hasattr(telegram_transport, "send_dashboard") else _send_custom_dashboard(analyzed_results, stats)
    except Exception as e:
        logger.error(f"Не удалось отправить дашборд в Telegram: {e}")


def _send_custom_dashboard(analyzed_results, stats):
    """Fallback-сборка дашборда согласно спецификации v2.0."""
    import telegram_transport

    header = (
        f"<b>🚀 GATEKEEPER-AI VALUE ENGINE DASHBOARD</b>\n"
        f"📅 <i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>\n"
        f"────────────────────────────\n"
        f"📊 <b>Analyzed:</b> <code>{stats.get('analyzed', 0)}</code> | "
        f"🔥 <b>HOT:</b> <code>{stats.get('hot', 0)}</code> | "
        f"⚠️ <b>WARM:</b> <code>{stats.get('warm', 0)}</code>\n"
        f"────────────────────────────\n\n"
    )

    body_chunks = []
    for res in analyzed_results:
        classification = res.get("classification")
        if classification not in ("HOT", "WARM"):
            continue

        match = res.get("_match", {})
        home = match.get("home") or match.get("home_team") or match.get("team_home") or "Home"
        away = match.get("away") or match.get("away_team") or match.get("team_away") or "Away"
        
        emoji = "🔥" if classification == "HOT" else "⚠️"
        edge = res.get("value_pct", 0)
        best_side = res.get("best_side", "N/A")
        u_factor = res.get("u_factor", 0)
        
        odds = res.get("odds", {}) or res.get("_match", {}).get("odds_1x2", {})
        o_home = odds.get("1", odds.get("home", 0))
        p_home = res.get("p_home", 0) * 100
        p_draw = res.get("p_draw", 0) * 100
        p_away = res.get("p_away", 0) * 100

        card = (
            f"{emoji} <b>{classification}: {home} vs {away}</b>\n"
            f"➔ Pick: <b>{best_side}</b> | Edge: <code>{edge:.2%}</code>\n"
            f"➔ O: <code>{o_home}</code> | F: <code>{p_home:.1f}%/{p_draw:.1f}%/{p_away:.1f}%</code>\n"
            f"➔ U-Factor: <code>{u_factor}</code>\n"
            f"────────────────────────────\n"
        )
        body_chunks.append(card)

    if not body_chunks:
        full_dashboard = header + "<i>Актуальных сигналов HOT/WARM на данный момент не обнаружено.</i>"
    else:
        full_dashboard = header + "".join(body_chunks)

    telegram_transport.send_telegram_dashboard(full_dashboard)


if __name__ == "__main__":
    process_upcoming_matches()
        
