"""
metrics.py — Сбор и отправка метрик GatekeeperAI.
Использует METRICS из gatekeeper_hub для агрегации.
Отправляет в system:health через gatekeeper_hub (§1.4: hub is the only gateway).

v8.11-patched:
  FIX-1: import save_to_cache из gatekeeper_hub, не из redis_hub (§1.4)
  FIX-2: sender_repo убран — save_to_cache может не принимать этот параметр
  FIX-3: Удалён fallback на redis_hub.save_to_cache — нарушает §1.4 (единый шлюз)
  FIX-4: format_dashboard redis marker "!" при down (синхрон с main.py §12)
  FIX-5: __all__ перенесён в начало (консистентность)
  FIX-6: __version__ 8.10 → 8.11
  FIX-7: NullHandler добавлен (§20.5)
"""

import logging
from datetime import datetime, timezone, timedelta

__version__ = "8.11-patched"

__all__ = ["collect_system_metrics", "send_health_status", "format_dashboard", "__version__"]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

MSK_TZ = timezone(timedelta(hours=3))


def collect_system_metrics(hub_metrics_report: dict) -> dict:
    """
    Агрегация метрик из хаба + системная информация.
    hub_metrics_report — результат METRICS.report() из gatekeeper_hub.
    """
    return {
        "timestamp": datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00"),
        "counters": hub_metrics_report.get("counters", {}),
        "timers": hub_metrics_report.get("timers", {}),
    }


def send_health_status(metrics_data: dict):
    """
    Запись health status в system:health через gatekeeper_hub (§1.4).
    FIX-3: fallback на redis_hub удалён — §1.4 запрещает прямой доступ к Redis
    в обход хаба. При недоступности хаба запись пропускается с логированием.
    """
    try:
        from gatekeeper_hub import save_to_cache
        save_to_cache("system:health", metrics_data)
    except ImportError:
        logger.error("gatekeeper_hub not available, health status not saved")
    except Exception as e:
        logger.error("Failed to send health: %s", e, exc_info=True)


def format_dashboard(stats: dict) -> str:
    """
    Форматирование Telegram-дашборда (§12).
    stats — dict с ключами: total_input, odds_enriched, value_count,
    pred_count, h2h_count, stats_count, redis_available, time, last_module

    FIX-4: redis marker "!" при Redis-down (синхрон с main.py ecosystem marker).
    """
    redis_available = stats.get("redis_available")
    if redis_available is True:
        redis_status = "+"
    elif redis_available is False:
        redis_status = "!"
    else:
        redis_status = "-"
    return (
        f"\U0001F310 Redis{redis_status} | "
        f"\U0001F4E0{stats.get('total_input', 0)} | "
        f"\U0001F4CA Odds:{stats.get('odds_enriched', 0)} | "
        f"\U0001F525 Value:{stats.get('value_count', 0)} | "
        f"Pred:{stats.get('pred_count', 0)} | "
        f"H2H:{stats.get('h2h_count', 0)} | "
        f"Stats:{stats.get('stats_count', 0)} | "
        f"\U0001F552 {stats.get('time', '')} | "
        f"\u2B50 {stats.get('last_module', '')}"
    )
