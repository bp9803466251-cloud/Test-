# metrics.py
"""
Gatekeeper-AI Ecosystem: Сбор и агрегация системных метрик.
Версия 10.0.0-merged (MERGE: контракт сигналов — signal_contract; meta-скан dual-pattern).

[9.6.8 FIX] Подстрочный матч "BET" in "NO BET" исключен: value-сигналы определяются
            по границам определений (HOT*, VALUE_BET) - синхронно с
            telegram_transport._resolve_classification (9.6.8).
[9.6.8 FIX] Приоритет classification, fallback verdict (контракт v710): ранее читался
            только verdict - все счётчики молча обнулялись, если ядро пишет classification.
[9.6.8 FIX] Сбой одного матча больше не обнуляет всю матрицу: обработка изолирована,
            сбои инкрементируют errors_total (поле задействовано по назначению).
[9.6.8 FIX] Молчаливые except: pass заменены на логирование (стандарт 9.6.8).
[9.6.8 FIX] Нестандартный возврат hub (list/None вместо dict) обрабатывается
            с явным warning, а не падением внутри общего try.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "10.0.0-merged"
__all__ = ["collect_metrics", "save_metrics", "__version__"]

logger = logging.getLogger("metrics")
logger.addHandler(logging.NullHandler())

MSK_TZ = timezone(timedelta(hours=3))

from signal_contract import (
    resolve_classification as _resolve_classification,
    is_value_signal as _is_value_signal,
)  # [MERGE] единый контракт сигналов (было: две локальные копии с расхождением дефолта)

# Коллекторы, чья активность проверяется по мета-маркерам в кэше
_COLLECTORS = ["sharpapi", "odds_api", "bzzoiro", "propline", "football_data", "main"]


def now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def collect_metrics(hub=None):
    """Собирает метрики здоровья и наполненности распределенного кэша."""
    metrics = {
        "timestamp": now_msk(),
        "matches_total": 0,
        "matches_with_odds": 0,
        "matches_with_predictions": 0,
        "matches_with_h2h": 0,
        "matches_with_stats": 0,
        "matches_with_value": 0,
        "collectors_active": 0,
        "errors_total": 0,
    }

    if hub is None:
        try:
            import gatekeeper_hub as hub
        except ImportError:
            logger.error("gatekeeper_hub не найден в корне репозитория")
            return metrics

    try:
        matches = hub.get_all_matches()
        if not isinstance(matches, dict):
            # [9.6.8 FIX] Нестандартный возврат: раньше .items() падал внутри
            # общего try и глотался - все счётчики молча оставались нулями.
            logger.warning(
                "hub.get_all_matches вернул %s вместо dict - матрица обнулена",
                type(matches).__name__,
            )
            matches = {}
        metrics["matches_total"] = len(matches)

        with_odds = with_pred = with_h2h = with_stats = with_value = 0
        for cid, data in matches.items():
            if not isinstance(data, dict):
                logger.warning(
                    "Матч %s: данные %s вместо dict - пропущен", cid, type(data).__name__
                )
                metrics["errors_total"] += 1
                continue
            try:
                # [9.6.8 FIX] Сбой одного матча изолирован: раньше любое None-поле
                # (например sources: null -> 'football_data' in None) роняло TypeError
                # и глушило ВЕСЬ цикл - все счётчики оставались нулями при полном Redis.
                inputs = data.get("model_inputs")
                if not isinstance(inputs, dict):
                    inputs = {}
                if inputs.get("odds_dict"):
                    with_odds += 1
                if inputs.get("p_model_dict"):
                    with_pred += 1

                sources = data.get("sources")
                if not isinstance(sources, list):
                    sources = []
                if data.get("h2h") or "football_data" in sources:
                    with_h2h += 1

                if data.get("stats") or inputs.get("expected_goals"):
                    with_stats += 1

                classification = _resolve_classification(data)
                if classification and _is_value_signal(classification):
                    with_value += 1
            except Exception as e:
                metrics["errors_total"] += 1
                logger.error("Ошибка при разборе матча %s: %s", cid, e)

        metrics["matches_with_odds"] = with_odds
        metrics["matches_with_predictions"] = with_pred
        metrics["matches_with_h2h"] = with_h2h
        metrics["matches_with_stats"] = with_stats
        metrics["matches_with_value"] = with_value
    except Exception as e:
        logger.error("Ошибка при итерации по матчам в collect_metrics: %s", e)

    # Сканирование мета-маркеров активности Serverless-коллекторов
    active = 0
    for c in _COLLECTORS:
        meta = None
        try:
            meta = hub.get_from_cache(f"system:meta:{c}") or hub.get_from_cache(f"{c}:meta")  # [MERGE] main пишет в system:meta:*
        except Exception as e:
            # [9.6.8 FIX] Прежнее молчаливое except: pass заменено на логирование
            logger.warning("get_from_cache(%s:meta) завершился сбоем: %s", c, e)
        if not meta:
            try:
                raw = hub.get_key(f"{c}:meta")
                if raw and isinstance(raw, str):
                    meta = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Нечитаемый JSON мета-маркера %s:meta: %s", c, e)
            except Exception as e:
                logger.warning("get_key(%s:meta) завершился сбоем: %s", c, e)
        if isinstance(meta, dict):
            payload = meta.get("payload")
            if not isinstance(payload, dict):
                # [9.6.8 FIX] payload-строка раньше роняла .get("last_run")
                payload = {}
            if meta.get("last_run") or payload.get("last_run"):
                active += 1
    metrics["collectors_active"] = active

    return metrics


def save_metrics(metrics=None):
    """Идемпотентно фиксирует raw JSON метрик в ключ system:health (§9.1)."""
    if metrics is None:
        metrics = collect_metrics()
    try:
        import gatekeeper_hub as hub
        hub.set_key("system:health", json.dumps(metrics, ensure_ascii=False))
        logger.info("Системные метрики успешно зафиксированы в system:health")
    except Exception as e:
        logger.error("save_metrics error: %s", e)
    return metrics


if __name__ == "__main__":
    m = collect_metrics()
    print(json.dumps(m, indent=2, ensure_ascii=False))
                
