#!/usr/bin/env python3
"""
match_linker.py — Модуль связывания реальных/актуальных матчей с историей из Redis
и отправки сводного дашборда в Telegram (Gatekeeper-AI, слияние 10.0.0).

[10.0.0 FIX] fetch_all_history_pool: Устранён dead-code Root-скана. Прежняя версия
             ждала redis_hub.scan_keys + redis_hub.get_history_match — функций,
             НЕ СУЩЕСТВУЮЩИХ в хабе v710 (сборка Шага 13). getattr возвращал None,
             Уровень 1 молча пропускался, и после миграции Шага 13 пул истории
             всегда собирался из легаси-полей хэша (fake empty конвейера).
             Теперь: SCAN через redis_command (контракт v710) + GET по каждому
             ключу с пакетированием COUNT=250.
[10.0.0 FIX] Транспорт приведён к реальному контракту v710: send_transport_payload.
             Прежняя версия в главной ветке проверяла hasattr(send_dashboard) —
             функции, которой в telegram_transport нет и не было (мёртвая ветка),
             а в fallback искала send_message / send_telegram_message, которых
             в транспорте тоже нет. Теперь: прямой вызов без getattr на
             критичном пути (README-правило 9); сборка текста — локальная.
[10.0.0 FIX] Классификация — через signal_contract (единая семантика квадруплета,
             устранённого слиянием): is_value_signal -> HOT-маркер 🔥,
             is_warm_signal -> WARM-маркер ⚠️ (прежде терялся), иначе ℹ️ NO BET.
             Error-пакет value_engine ({"error": ...}) больше не попадает в
             счётчик NO BET — пропускается с warning (паритет с main.py).
[10.0.0 DECISION] Дашборд — за пайплайном (core engine), который его шлёт сам.
             Standalone-прогон (match_linker.yml) стартует с --no-dashboard.
             Дефолт кода сохранён (send_dashboard=True): ручные запуски без
             флага ведут себя как раньше; main.py не затронут — он вызывает
             только link_match_with_history.
[10.0.0]     Легаси-Уровень 2 (hash-поля) сохранён как детектор незавершённой
             миграции: его срабатывание после Шага 13 = сигнал в warning.
[10.0.0]     Примечание §1.11: в source_diagnostics есть зеркальный читатель
             fetch_history_pool (для аудита). Унификация в один метод хаба —
             кандидат на следующий цикл; сейчас читатели семантически совпадают
             (Root-first, дедуп по canonical_id), расхождение контролируется
             режимом keys/history-contracts ядра.
[9.6.8 FIX]  Сохранены: изоляция сбоев цикла, честный exit-код (0/1), статус-фильтр.
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

# Строгий импорт: контракт сигналов обязателен (README-правило 7), сбой — честный ImportError
from signal_contract import is_value_signal, is_warm_signal

__version__ = "10.0.0-merged"
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


def _scan_history_keys(redis_hub):
    """[10.0.0] SCAN history:match:* через redis_command с итерацией по курсору."""
    keys, cursor = [], "0"
    while True:
        raw = redis_hub.redis_command(["SCAN", cursor, "MATCH", "history:match:*", "COUNT", "250"])
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise RuntimeError(f"SCAN вернул неожиданный формат: {type(raw).__name__}")
        cursor, found = raw[0], raw[1]
        if isinstance(cursor, bytes):
            cursor = cursor.decode("utf-8")
        cursor = str(cursor)
        if found and isinstance(found, list):
            keys.extend(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in found)
        if cursor == "0":
            break
    return keys


def fetch_all_history_pool():
    """История: Root-ключи history:match:* (SCAN+GET, контракт v710) + hash-фолбэк.

    Возвращает отсортированный по date_utc список без дубликатов canonical_id.
    Пустой пул при живой базе = симптом: проверить source_diagnostics --mode keys.
    """
    redis_hub = _get_redis_hub()
    if not redis_hub:
        return []

    matches = []

    # Уровень 1: Root-ключи (новая топология Шага 13) — теперь реальный, а не dead-code
    root_hit = 0
    try:
        for key in _scan_history_keys(redis_hub):
            cid = key.replace("history:match:", "") if isinstance(key, str) else key
            raw = redis_hub.redis_command(["GET", key])
            if raw is None:
                continue
            m = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(m, dict):
                m.setdefault("canonical_id", cid)
                matches.append(m)
                root_hit += 1
    except Exception as e:
        logger.warning(f"Root-скан истории недоступен: {e}")

    # Уровень 2: hash-фолбэк (легаси-записи до миграции Шага 13).
    # После миграции любое срабатывание — признак незавершённой миграции.
    legacy = 0
    try:
        all_fields = redis_hub.get_all_fields()
        for key, raw in (all_fields or {}).items():
            if not key.startswith("history:match:"):
                continue
            try:
                match = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(match, dict):
                    matches.append(match)
                    legacy += 1
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Ошибка получения истории из Redis: {e}")

    if legacy and root_hit:
        logger.warning(
            f"Незавершённая миграция Шага 13: {legacy} легаси-полей хэша при "
            f"{root_hit} Root-ключах — выполните source_diagnostics --mode keys"
        )

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


def _build_dashboard_text(results, stats):
    """[10.0.0] Сборка текстовой сводки. Маркеры по signal_contract:
    🔥 HOT (value-сигнал), ⚠️ WARM, ℹ️ NO BET. Warm-маркер в прежней
    fallback-версии терялся (всё, кроме HOT, печаталось как ℹ️)."""
    lines = [
        "📊 MATCH LINKER DASHBOARD",
        f"Analyzed: {stats.get('analyzed', 0)} | "
        f"HOT: {stats.get('hot', 0)} | WARM: {stats.get('warm', 0)}",
        "",
    ]
    for r in results[:20]:
        m = r.get("_match", {})
        cls = str(r.get("classification", "NO BET"))
        if is_value_signal(cls):
            mark = "🔥"
        elif is_warm_signal(cls):
            mark = "⚠️"
        else:
            mark = "ℹ️"
        lines.append(f"{mark} {cls}: {m.get('home_team', '?')} vs "
                     f"{m.get('away_team', '?')} ({m.get('date_utc', '?')})")
    return "\n".join(lines)


def process_upcoming_matches(send_dashboard=True):
    """Находит актуальные матчи, связывает с историей, анализирует и отправляет дашборд.

    Возвращает True при успешном прогоне (включая пустой контур),
    False при ошибке окружения или отправки.
    send_dashboard=False — standalone-режим без дашборда [DECISION 10.0.0]:
    сводку шлёт пайплайн, standalone-прогон оставляет только линковку/анализ.
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

            # [10.0.0 FIX] Error-пакет value_engine — не сигнал, не NO BET.
            # Прежде {"error": ...} попадал в счётчик NO BET и тишины в дашборде.
            if isinstance(result, dict) and result.get("error"):
                logger.warning(f"Ошибка анализа матча {cid}: {result.get('error')}")
                continue

            analyzed_count += 1
            classification = str(result.get("classification", "NO BET"))

            # Сохраняем исходные данные матча внутри результата для дашборда
            result["_match"] = match_data

            if is_value_signal(classification):
                hot_count += 1
                analyzed_results.append(result)
            elif is_warm_signal(classification):
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

    if not send_dashboard:
        logger.info("Дашборд отключен (--no-dashboard): сводку шлёт пайплайн. Прогон завершён.")
        return True

    logger.info("Отправка сводного дашборда в Telegram...")
    try:
        import telegram_transport
    except ImportError as e:
        logger.error(f"telegram_transport недоступен: {e}")
        return False

    try:
        # [10.0.0 FIX] Прямой вызов реального контракта транспорта (README-правило 9).
        # Прежние hasattr(send_dashboard) и поиск send_message/send_telegram_message
        # были мёртвыми именами: главная ветка не срабатывала никогда, fallback
        # заканчивался NameError, глотавшимся except'ом.
        payload = _build_dashboard_text(analyzed_results, stats)
        telegram_transport.send_transport_payload(payload)
    except Exception as e:
        logger.error(f"Не удалось отправить дашборд в Telegram: {e}")
        return False

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match Linker & Analyzer")
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Не отправлять сводный дашборд (standalone-прогон; дашборд шлёт пайплайн)",
    )
    args = parser.parse_args()
    sys.exit(0 if process_upcoming_matches(send_dashboard=not args.no_dashboard) else 1)
