"""
metrics.py — Сбор и отправка метрик GatekeeperAI.
Использует METRICS из gatekeeper_hub для агрегации.
Отправляет в system:health через redis_hub.
"""

import json
from datetime import datetime, timezone, timedelta

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
    Запись health status в system:health.
    """
    try:
        from redis_hub import save_to_cache
        save_to_cache("system:health", metrics_data, sender_repo="metrics")
    except Exception as e:
        print(f"[METRICS] Failed to send health: {e}")


def format_dashboard(stats: dict) -> str:
    """
    Форматирование Telegram-дашборда (§12).
    stats — dict с ключами: total_input, odds_enriched, value_count,
    pred_count, h2h_count, stats_count, redis_status, time, last_module
    """
    redis_status = "+" if stats.get("redis_available") else "-"
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


__all__ = ["collect_system_metrics", "send_health_status", "format_dashboard"]
