# metrics.py
"""
Gatekeeper-AI Ecosystem: Сбор и агрегация системных метрик.
Версия 9.3-audited — Полная синхронизация с паспортами CORE ENGINE V6.7.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "9.3-audited"
__all__ = ["collect_metrics", "save_metrics", "__version__"]

logger = logging.getLogger("metrics")
logger.addHandler(logging.NullHandler())

MSK_TZ = timezone(timedelta(hours=3))

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
        metrics["matches_total"] = len(matches) if matches else 0
        
        with_odds = with_pred = with_h2h = with_stats = with_value = 0
        for cid, data in (matches or {}).items():
            if isinstance(data, dict):
                # Проверка вложенных структур модели по спецификации v5.7.12
                inputs = data.get("model_inputs", {})
                if inputs.get("odds_dict"): with_odds += 1
                if inputs.get("p_model_dict"): with_pred += 1
                
                # Проверка исторических архивов
                if data.get("h2h") or "football_data" in data.get("sources", []): with_h2h += 1
                if data.get("stats") or inputs.get("expected_goals"): with_stats += 1
                
                # Синхронизация с вердиктами VALIDATED VALUE ядра CORE ENGINE V6.7
                core_an = data.get("core_analysis", {})
                if core_an and "BET" in core_an.get("verdict", ""):
                    with_value += 1
                    
        metrics["matches_with_odds"] = with_odds
        metrics["matches_with_predictions"] = with_pred
        metrics["matches_with_h2h"] = with_h2h
        metrics["matches_with_stats"] = with_stats
        metrics["matches_with_value"] = with_value
    except Exception as e:
        logger.error("Ошибка при итерации по матчам в collect_metrics: %s", e)
    
    # Сканирование мета-маркеров активности Serverless-коллекторов
    try:
        collectors = ["sharpapi", "odds_api", "bzzoiro", "propline", "football_data", "main"]
        active = 0
        for c in collectors:
            meta = None
            try: meta = hub.get_from_cache(f"{c}:meta")
            except Exception: pass
            if not meta:
                try:
                    raw = hub.get_key(f"{c}:meta")
                    if raw and isinstance(raw, str): meta = json.loads(raw)
                except Exception: pass
            if meta and isinstance(meta, dict) and (meta.get("last_run") or meta.get("payload", {}).get("last_run")):
                active += 1
        metrics["collectors_active"] = active
    except Exception:
        pass
    
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
    
