#!/usr/bin/env python3
"""
metrics.py — Сбор метрик GatekeeperAI v8.11-patched.
Использует set_key (raw JSON) для system:health (§9.1).
"""
import logging
from datetime import datetime, timezone, timedelta

__version__ = "8.11-patched"
__all__ = ["collect_metrics", "save_metrics", "__version__"]

logger = logging.getLogger("metrics")
logger.addHandler(logging.NullHandler())

MSK_TZ = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def collect_metrics(hub=None):
    """Собирает метрики системы."""
    metrics = {
        "timestamp": now_msk(),
        "matches_total": 0,
        "matches_with_odds": 0,
        "collectors_active": 0,
        "errors_total": 0,
    }
    
    if hub is None:
        try:
            import gatekeeper_hub as hub
        except ImportError:
            logger.error("gatekeeper_hub not found")
            return metrics
    
    try:
        matches = hub.get_all_matches()
        metrics["matches_total"] = len(matches) if matches else 0
        
        with_odds = 0
        for cid, data in (matches or {}).items():
            if isinstance(data, dict) and data.get("odds"):
                with_odds += 1
        metrics["matches_with_odds"] = with_odds
    except Exception as e:
        logger.error("collect_metrics error: %s", e)
    
    try:
        registry = getattr(hub, "MODULE_REGISTRY", {})
        metrics["collectors_active"] = len(registry)
    except Exception:
        pass
    
    return metrics


def save_metrics(metrics=None):
    """Сохраняет метрики в Redis через set_key (§9.1 — raw JSON)."""
    if metrics is None:
        metrics = collect_metrics()
    
    try:
        import gatekeeper_hub as hub
        hub.set_key("system:health", __import__("json").dumps(metrics))
        logger.info("Metrics saved to system:health")
    except Exception as e:
        logger.error("save_metrics error: %s", e)
    
    return metrics


if __name__ == "__main__":
    m = collect_metrics()
    print(__import__("json").dumps(m, indent=2))
