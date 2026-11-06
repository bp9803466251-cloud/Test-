#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem: Главный оркестратор конвейера CORE ENGINE V6.7.
Версия 9.6.8-patched (Схема v710) — Manual Trigger Only (§1.2).
Полная синхронизация с плоским контрактом v710: home_clean/away_clean,
odds_1x2 (ключи "1"/"X"/"2"), enum статусов v710, patch_match для записи.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

# Попытка импорта обязательных компонентов автономной экосистемы
# [MERGE 13-G1] Строгий импорт хаба. Заглушки-фолбэки удалены: при отсутствии
# модуля они возвращали {"redis_available": True} (fake green), и конвейер
# отправлял фальшивый алерт "DATA STOP" вместо честной ошибки. Обоснование
# заглушек (py_compile в CI) ошибочно: py_compile не исполняет импорты.
from gatekeeper_hub import (
    run_initialization,
    get_all_matches,
    get_from_cache,
    now_msk,
    is_shutdown_requested,
    patch_match,
)

# Безопасные импорты: отсутствие модуля не должно ронять конвейер на NameError
try:
    import value_engine
except ImportError:
    value_engine = None

try:
    import match_linker
except ImportError:
    match_linker = None

try:
    import telegram_transport
except ImportError:
    telegram_transport = None

# [MERGE] Единый контракт классификации сигналов (signal_contract — без зависимостей)
from signal_contract import resolve_classification as _resolve_classification

# [MERGE] Телеметрия system:health (§9.1): metrics.py ранее не вызывался никем
# (не было ни шага в sports_pipeline.yml, ни вызова из main) — ключ не обновлялся.
try:
    import metrics as metrics_module
except ImportError:
    metrics_module = None

logger = logging.getLogger("main")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(asctime)s] [main_pipeline] [%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Жесткая фиксация контрактных версий ворот деплоя (синхронизировано с docstring)
__version__ = "10.0.0-merged"
__all__ = ["main", "__version__"]

MSK_TZ = timezone(timedelta(hours=3))
_redis_available = True

# Терминальные статусы: enum v710 + легаси-тройка для непрогнанных записей
TERMINAL_STATUSES = {"completed", "postponed", "cancelled",
                     "ft", "finished", "aet", "ended"}


def _is_terminal(status: str) -> bool:
    return (status or "").lower() in TERMINAL_STATUSES


def _build_strict_dashboard(analyzed_packages: list) -> str:
    """
    Сборка структуры отчета по регламенту строгого вывода TOKEN CONTROL (§4).
    Читает плоский контракт value_engine v9.6.8: classification, odds ("1"/"X"/"2"),
    p_home/p_draw/p_away, edge, target. Легаси-поля (verdict) — fallback.
    Фейковые дефолты (DQS 85, MC 75, U 20) убраны — показываются только реальные поля.
    """
    if not analyzed_packages:
        return "<b>CORE ENGINE V6.7: Срез пре-матч датасетов пуст. Расчеты заморожены по условию DATA STOP.</b>"

    report_blocks = []
    hot_count = 0
    warm_count = 0
    no_bet_count = 0

    for pkg in analyzed_packages:
        res = pkg.get("core_analysis", {}) or {}
        home_team = pkg.get("home_team", "Home Team")
        away_team = pkg.get("away_team", "Away Team")

        classification = _resolve_classification(res)
        if classification == "HOT":
            hot_count += 1
        elif classification == "WARM":
            warm_count += 1
        else:
            no_bet_count += 1

        # Плоский базис котировок v9.6.8 с легаси-fallback
        odds = res.get("odds") or {}
        o1 = odds.get("1", odds.get("home", 0.0))
        oX = odds.get("X", odds.get("draw", 0.0))
        o2 = odds.get("2", odds.get("away", 0.0))

        p_home = res.get("p_home", 0.0)
        p_draw = res.get("p_draw", 0.0)
        p_away = res.get("p_away", 0.0)
        ev_lower = res.get("EV_lower", res.get("ev_lower", 0.0))
        edge = res.get("edge", 0.0)
        target = res.get("target", res.get("TARGET", ""))

        verdict_line = f"🎯 <b>КЛАСС: {classification}</b>"
        if target:
            verdict_line += f" | TARGET: {target}"

        block = (
            f"📊 <b>➔ {home_team} vs {away_team}</b>\n"
            f"• 1X2: {o1} / {oX} / {o2}\n"
            f"• P(H/X/2): {p_home} / {p_draw} / {p_away}\n"
            f"• EV Lower: {ev_lower} | Edge: {edge}\n"
            f"{verdict_line}\n"
        )
        report_blocks.append(block)

    # Сборка подвала системных статусов экосистемы
    now_str = datetime.now(MSK_TZ).strftime("%H:%M")
    redis_status = "+" if _redis_available else "-"
    eco_line = f"Ecosystem+ | Redis{redis_status} | AutonomyMode+"

    status_footer = (
        f"🌐 Redis{redis_status} | 📦 {len(analyzed_packages)} | 🔥 HOT:{hot_count} | "
        f"⚠️ WARM:{warm_count} | ⛔ NO BET:{no_bet_count} | 🕒 {now_str} | ⭐ CORE ENGINE V6.7\n"
        f"{eco_line}"
    )
    report_blocks.append(status_footer)
    return "\n".join(report_blocks)


# ── АВТОНОМНЫЕ МЕТОДЫ ИДЕМПОТЕНТНОГО СОХРАНЕНИЯ РЕЗУЛЬТАТОВ В REDIS ──
def save_analysis(canonical_id: str, analysis_result: dict) -> bool:
    """
    Атомарно сохраняет аналитический паспорт матча обратно в Redis.
    Реализация через patch_match (CAS, секция core_analysis, predictions не затрагиваются).
    """
    logger.info(f"Идемпотентная запись результатов анализа для CID: {canonical_id}")
    try:
        payload = {k: v for k, v in (analysis_result or {}).items() if k != "_match"}
        return bool(patch_match(canonical_id, "core_analysis", payload, source="main"))
    except Exception as e:
        logger.error(f"Ошибка записи анализа для {canonical_id}: {e}")
        return False


def save_search_results(summary_data: dict) -> bool:
    """
    Записывает глобальные результаты текущего поискового среза в Redis.
    Конверт v700-prod, ключ search:results:latest.
    """
    logger.info("Сохранение финальных глобальных метаданных запуска поискового среза.")
    try:
        envelope = {
            "version": "v700-prod",
            "sender_repo": (os.environ.get("GITHUB_REPOSITORY", "local_dev").split("/")[-1]),
            "timestamp": datetime.now(MSK_TZ).isoformat(),
            "payload": summary_data,
        }
        try:
            import redis_hub
            return bool(redis_hub.save_to_cache("search:results:latest", envelope))
        except ImportError:
            pass
        # Fallback: хаб может предоставлять собственный сохранитель
        try:
            import gatekeeper_hub as _gh
            fn = getattr(_gh, "save_search_results", None)
            if callable(fn):
                return bool(fn(summary_data))
        except Exception:
            pass
        logger.error("save_search_results: ни redis_hub, ни хаб-хелпер недоступны.")
        return False
    except Exception as e:
        logger.error(f"Ошибка записи search:results:latest: {e}")
        return False


def save_meta(collector_name: str, last_run: str, total_events: int,
              stored_matches: int, error_count: int) -> bool:
    """
    Обновляет метаданные здоровья подсистемы оркестратора конвейера.
    """
    logger.info(f"Синхронизация метаданных системы для коллектора: {collector_name} ({last_run})")
    meta = {
        "collector": collector_name,
        "last_run": last_run,
        "total_events": total_events,
        "stored_matches": stored_matches,
        "error_count": error_count,
        "version": __version__,
    }
    try:
        import redis_hub
        return bool(redis_hub.save_to_cache(f"system:meta:{collector_name}", meta))
    except Exception as e:
        logger.error(f"Ошибка записи system:meta:{collector_name}: {e}")
        return False


def _send_dashboard_safe(text: str) -> bool:
    """Защищенная отправка дашборда: отсутствие транспорта — ошибка, а не молчание."""
    try:
        if telegram_transport is None:
            raise RuntimeError("telegram_transport недоступен")
        send = getattr(telegram_transport, "send_transport_payload", None)
        if not callable(send):
            raise RuntimeError("telegram_transport.send_transport_payload не найден")
        send(text)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки дашборда в Telegram: {e}")
        return False


# ── ГЛАВНЫЙ ИСПОЛНИТЕЛЬНЫЙ ЦИКЛ ОРКЕСТРАТОРА CONVEYER CORE ──
def main():
    global _redis_available
    logger.info("=== Запуск CORE ENGINE V6.7: Математический стандарт V5.7.12 ===")

    # === ШАГ 1: Инициализация коннектора кэш-слоя ===
    init = run_initialization(collector="main")
    if not init or not init.get("redis_available"):
        _redis_available = False
        logger.error("Критический сбой: База данных Redis недоступна.")
        return

    _redis_available = True

    # === ШАГ 2: Извлечение пред-матч пакетов v710 из хаба ===
    live_matches = get_all_matches()
    if not live_matches:
        logger.warning("Срез пре-матч датасетов (MODEL INPUT) пуст. Расчеты заморожены.")
        _send_dashboard_safe(
            "<b>CORE ENGINE V6.7: Срез пре-матч датасетов пуст. Расчеты заморожены по условию DATA STOP.</b>"
        )
        return

    # === Проверка перехвата Graceful Shutdown (§1.10) ===
    if is_shutdown_requested():
        logger.info("Graceful shutdown перехвачен — досрочное завершение конвейера.")
        return

    analyzed_packages = []
    saved_analysis = 0
    skipped_count = 0

    # === ШАГ 3: Пакетная математическая обработка ===
    for cid, match_data in live_matches.items():
        if not match_data or not isinstance(match_data, dict):
            skipped_count += 1
            continue

        # Фильтрация по enum v710 + легаси-тройке (гейт model_inputs удален —
        # поля model_inputs в схеме v710 не существует)
        status = str(match_data.get("status", "")).lower()
        if _is_terminal(status):
            continue

        try:
            analysis = None

            # Приоритет: полный конвейер линковки с историей
            if match_linker is not None and hasattr(match_linker, "link_match_with_history"):
                try:
                    analysis = match_linker.link_match_with_history(match_data, None)
                except Exception as e:
                    logger.debug(f"Линковка недоступна для {cid}: {e}")

            # Fallback: прямой расчет движком без истории
            if analysis is None and value_engine is not None and hasattr(value_engine, "analyze_match"):
                try:
                    analysis = value_engine.analyze_match(match_data, None)
                except Exception as e:
                    logger.error(f"Ошибка анализа {cid} через value_engine: {e}")

            if not analysis:
                skipped_count += 1
                continue

            if isinstance(analysis, dict) and analysis.get("error"):
                skipped_count += 1
                continue

            analysis_clean = {k: v for k, v in analysis.items() if k != "_match"}
            if save_analysis(cid, analysis_clean):
                saved_analysis += 1

            pkg = {
                "canonical_id": cid,
                "home_team": match_data.get("home_team", "Home Team"),
                "away_team": match_data.get("away_team", "Away Team"),
                "core_analysis": analysis_clean,
            }
            analyzed_packages.append(pkg)

        except Exception as e:
            logger.error(f"Ошибка обработки матча {cid}: {e}")
            skipped_count += 1
            continue

    logger.info(f"Обработано: {len(analyzed_packages)} | Сохранено: {saved_analysis} | Пропущено: {skipped_count}")

    # === ШАГ 4: Динамический подсчет current_errors (§1.2) ===
    # Инкремент при сбоях сохранения среза И отправки дашборда
    current_errors = 0

    summary = {
        "analyzed": len(analyzed_packages),
        "saved": saved_analysis,
        "skipped": skipped_count,
        "total_scanned": len(live_matches),
        "version": __version__,
        "timestamp": datetime.now(MSK_TZ).isoformat(),
    }
    if not save_search_results(summary):
        current_errors += 1

    # === ШАГ 5: Дашборд и отправка ===
    if analyzed_packages:
        dashboard_text = _build_strict_dashboard(analyzed_packages)
        if not _send_dashboard_safe(dashboard_text):
            current_errors += 1
    else:
        logger.warning("Все матчи отфильтрованы или не проанализированы — дашборд не формируется.")

    # === ШАГ 6: Телеметрия system:health (metrics.py) ===
    # [MERGE] Сиротский модуль подключен: ядро диагностики читает system:health
    # через _read_meta — теперь ключ обновляется каждым прогоном конвейера.
    if metrics_module is not None:
        try:
            collected = metrics_module.collect_metrics()
            if not metrics_module.save_metrics(collected):
                current_errors += 1
        except Exception as e:
            logger.error(f"Ошибка телеметрии system:health: {e}")
            current_errors += 1
    else:
        logger.warning("metrics недоступен — system:health не обновляется (честная деградация)")

    # === ШАГ 7: Метаданные здоровья ===
    now_str = now_msk() if callable(now_msk) else datetime.now(MSK_TZ).isoformat()
    save_meta(
        collector_name="main",
        last_run=now_str,
        total_events=len(live_matches),
        stored_matches=saved_analysis,
        error_count=current_errors,
    )

    logger.info("=== Конвейер CORE ENGINE V6.7 завершен ===")


if __name__ == "__main__":
    main()
