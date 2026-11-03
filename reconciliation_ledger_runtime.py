#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gatekeeper-AI Ecosystem
Module: reconciliation_ledger_runtime.py
Version: 9.6.8-patched (Синхронизировано со схемой v710 и CORE ENGINE V6.7)
Topology: Zero-Folder Architecture (Root Execution)
Description: Часть 1 из 2. Автономная fast-MAD фильтрация и валидация фида v710.
Compliance: §1.3 (Strict urllib.request architecture - NO requests library)
            [FIXED 9.6.8] TCP-клиент redis.Redis заменен на Upstash REST API (§6).
            MockRedis - только явный деградационный режим (данные в ОЗУ теряются).
"""

import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error

__version__ = "9.6.8-patched"

logging.basicConfig(level=logging.INFO, format="[LEDGER] %(message)s")
logger = logging.getLogger("ledger")

# Лимит Circuit Breaker пакета (синхронизирован с redis_config / chaos_monkey / YAML: 15)
try:
    from redis_config import CB_FAILURE_THRESHOLD as _PKG_CB_THRESHOLD
except Exception:
    _PKG_CB_THRESHOLD = 15

# Штраф за нарушение схемы v710 — пробрасывается в CB-метрику пакета
_SCHEMA_CORRUPTION_PENALTY = 5


def _load_mad_threshold(config_path: str = "gatekeeper_config.yaml") -> float:
    """mad_threshold из секции value_engine YAML; фолбэк 2.5 (контракт патча)."""
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        raw = (cfg.get("value_engine") or {}).get("mad_threshold")
        if raw is None:
            return 2.5
        val = float(str(raw).strip())
        if val <= 0:
            logger.warning(f"[CONFIG] Невалидный mad_threshold={raw!r} — фолбэк 2.5")
            return 2.5
        return val
    except FileNotFoundError:
        return 2.5
    except Exception as e:
        logger.warning(f"[CONFIG] Ошибка чтения mad_threshold: {e} — фолбэк 2.5")
        return 2.5


class _MockRedisPipeline:
    """Пайплайн деградационного режима (данные только в ОЗУ, теряются при рестарте)."""
    def __init__(self, storage):
        self.storage = storage
        self.commands = []

    def incrby(self, k, v):
        self.commands.append(("INCRBY", k, v))
        return self

    def get(self, k):
        self.commands.append(("GET", k))
        return self

    def set(self, k, v, ex=None):
        self.commands.append(("SET", k, v, ex))
        return self

    def execute(self):
        results = []
        for cmd, *args in self.commands:
            if cmd == "INCRBY":
                k, v = args[0], args[1]
                self.storage[str(k)] = self.storage.get(str(k), 0) + v
                results.append(self.storage[str(k)])
            elif cmd == "GET":
                k = args[0]
                results.append(self.storage.get(str(k), "CLOSED" if "status" in str(k) else "0"))
            elif cmd == "SET":
                k, v = args[0], args[1]
                self.storage[str(k)] = v
                results.append(True)
        self.commands = []
        return results


class _MockRedis:
    """Явный деградационный режим: персистентности НЕТ."""
    def __init__(self):
        self.storage = {}
        self.mode = "mock_offline"

    def pipeline(self):
        return _MockRedisPipeline(self.storage)

    def get(self, k):
        return self.storage.get(str(k))

    def set(self, k, v, ex=None):
        self.storage[str(k)] = v
        return True


class UpstashRestClient:
    """
    REST-клиент Upstash на чистом urllib (§1.3, §6).
    Интерфейс совместим с подмножеством redis-py: pipeline().incrby/get/set + execute().
    """
    def __init__(self):
        from redis_config import REDIS_REST_URL, REDIS_REST_TOKEN
        url = (REDIS_REST_URL or "").strip().rstrip("/")
        token = (REDIS_REST_TOKEN or "").strip()
        if not url or not token:
            raise RuntimeError("Upstash не сконфигурирован (REDIS_REST_URL/REDIS_REST_TOKEN)")
        if not url.startswith("https://"):
            raise RuntimeError(f"Upstash URL должен быть https:// (получено: {url[:12]}...)")
        self.base_url = url
        self.token = token
        self.mode = "upstash_rest"

    def pipeline(self):
        return UpstashPipeline(self)

    def get(self, k):
        res = UpstashPipeline(self).get(k).execute()
        return res[0] if res else None


class UpstashPipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    def incrby(self, k, v):
        self.commands.append(["INCRBY", str(k), str(int(v))])
        return self

    def get(self, k):
        self.commands.append(["GET", str(k)])
        return self

    def set(self, k, v, ex=None):
        if ex is not None:
            self.commands.append(["SET", str(k), str(v), "EX", str(int(ex))])
        else:
            self.commands.append(["SET", str(k), str(v)])
        return self

    def execute(self):
        payload = json.dumps(self.commands).encode("utf-8")
        req = urllib.request.Request(
            f"{self.client.base_url}/pipeline",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.client.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.commands = []
        results = []
        for item in body:
            if isinstance(item, dict) and "result" in item:
                r = item["result"]
            else:
                r = item
            if r is None:
                results.append("0")
            else:
                results.append(r)
        return results


def calculate_fast_mad(metrics, anomaly_threshold: float = 2.5):
    """
    Изолированный расчет Median Absolute Deviation (MAD) на чистом Python.
    Порог отсечения аномалий читается из YAML (value_engine.mad_threshold, фолбэк 2.5).
    """
    if not metrics or len(metrics) < 3:
        return [], 0.0, 0.0

    sorted_metrics = sorted(metrics)
    n = len(sorted_metrics)

    if n % 2 == 1:
        median = sorted_metrics[n // 2]
    else:
        median = (sorted_metrics[(n // 2) - 1] + sorted_metrics[n // 2]) / 2.0

    absolute_deviations = [abs(x - median) for x in metrics]
    sorted_deviations = sorted(absolute_deviations)

    if n % 2 == 1:
        mad = sorted_deviations[n // 2]
    else:
        mad = (sorted_deviations[(n // 2) - 1] + sorted_deviations[n // 2]) / 2.0

    if mad == 0.0:
        mad = 1e-6

    anomalies = []
    for i, x in enumerate(metrics):
        z_score = (0.6745 * (x - median)) / mad
        if abs(z_score) > anomaly_threshold:
            anomalies.append(i)

    return anomalies, median, mad


def validate_flat_feed_v710(payload) -> bool:
    """Строгий структурный валидатор плоского фида v710 / CORE ENGINE V6.7."""
    required_fields = ["gate_id", "timestamp", "metrics", "fallback_active"]
    if not isinstance(payload, dict):
        return False
    if not all(field in payload for field in required_fields):
        return False
    if not isinstance(payload["metrics"], list):
        return False
    return True


def _register_cb_penalty(penalty: int) -> None:
    """Проброс штрафных баллов в глобальную CB-метрику пакета (chaos_monkey_runtime)."""
    try:
        import chaos_monkey_runtime
        chaos_monkey_runtime.register_failure_score(penalty)
    except Exception as e:
        logger.debug(f"[CB] Метрика пакета недоступна, штраф {penalty} не проброшен: {e}")


class GatekeeperReconciliationLedger:
    def __init__(self, redis_client=None):
        if redis_client is not None:
            self.r = redis_client
            self.mode = getattr(redis_client, "mode", "injected")
        else:
            try:
                self.r = UpstashRestClient()
                logger.info(f"[LEDGER INIT] Хранилище: Upstash REST ({self.r.base_url})")
            except Exception as e:
                self.r = _MockRedis()
                logger.warning(
                    "[LEDGER DEGRADED] Upstash не сконфигурирован (%s). "
                    "Включен режим mock_offline: данные хранятся ТОЛЬКО в ОЗУ и будут "
                    "ПОТЕРЯНЫ при рестарте процесса.", e
                )
            self.mode = getattr(self.r, "mode", "unknown")
        self.failure_threshold = _PKG_CB_THRESHOLD
        self.passport_ttl = 86400
        self.mad_threshold = _load_mad_threshold()

    def process_incoming_feed(self, raw_json_feed):
        """Основной конвейер обработки потокового фида."""
        try:
            data = json.loads(raw_json_feed)
        except json.JSONDecodeError as e:
            logger.error(f"Невалидный JSON-пакет в потоке: {e}")
            return "MALFORMED_JSON"

        if not validate_flat_feed_v710(data):
            logger.error("Структура пакета нарушает спецификацию схемы v710")
            _register_cb_penalty(_SCHEMA_CORRUPTION_PENALTY)
            return "SCHEMA_VIOLATION"

        gate_id = data["gate_id"]
        metrics = data["metrics"]

        anomalies, median, mad = calculate_fast_mad(metrics, self.mad_threshold)

        pipe = self.r.pipeline()
        error_key = f"gatekeeper:errors:{gate_id}"
        status_key = f"gatekeeper:status:{gate_id}"
        passport_key = f"gatekeeper:passport:{gate_id}"

        if anomalies:
            logger.warning(f"[ANOMALY ALERT] Выброс котировок на {gate_id}. Индексы: {anomalies} "
                           f"(median={median}, MAD={mad}, порог={self.mad_threshold})")
            pipe.incrby(error_key, 1)
            pipe.get(status_key)
            res = pipe.execute()
            current_errors = int(res[0]) if res else 1
            current_status = str(res[1]) if res and len(res) > 1 else "CLOSED"

            if current_errors >= self.failure_threshold and current_status != "OPEN":
                self._trip_circuit_breaker_isolation(gate_id, current_errors)
                return "CIRCUIT_BREAKER_TRIPPED"

            return "ANOMALY_SUSPECT"
        else:
            pipe.set(passport_key, json.dumps(data), ex=self.passport_ttl)
            pipe.execute()
            logger.info(f"[LEDGER SUCCESS] Паспорт v710 для {gate_id} сохранен ({self.mode}).")
            return "COMMIT_SUCCESS"

    def _trip_circuit_breaker_isolation(self, gate_id, total_errors):
        """Аварийный перевод шлюза в OPEN, каскадный спуск и очистка RAM."""
        pipe = self.r.pipeline()
        pipe.set(f"gatekeeper:status:{gate_id}", "OPEN")
        pipe.set(f"gatekeeper:fallback_target:{gate_id}", "active_core_v6.7_backup")
        pipe.execute()
        logger.warning(f"[CB INTERVENTION] Шлюз {gate_id} изолирован. Ошибки: {total_errors}/{self.failure_threshold}")

        try:
            if os.path.exists("redis_flush.py"):
                import redis_flush
                if hasattr(redis_flush, "balance_memory_ram"):
                    redis_flush.balance_memory_ram()
                    logger.info("[LEDGER + FLUSH] RAM ребалансировка выполнена.")
        except Exception as memory_err:
            logger.warning(f"Балансировщик памяти redis_flush не ответил: {memory_err}")

        # ИБ-нотификация через транспорт пакета. Если он недоступен — честный WARN,
        # уведомление НЕ отправляется (никаких фиктивных «отправок через urllib»).
        try:
            from telegram_transport import send_transport_payload
            alert_text = (f"🚨 <b>CRITICAL DETECTED</b>\nGate <code>{gate_id}</code> dropped to OPEN status.\n"
                          f"Errors accumulated: {total_errors}.")
            send_transport_payload(alert_text)
        except Exception as e:
            logger.warning(f"[NOTIFY FAIL] Нотификация НЕ отправлена (telegram_transport недоступен: {e})")


if __name__ == "__main__":
    print("[INIT] Распределенный леджер сверки v9.6.8-patched запущен в корне репозитория.")
    ledger = GatekeeperReconciliationLedger()
    sample_feed = '{"gate_id": "gate_21_prod", "timestamp": 1792317720, "metrics": [1.85, 1.90, 1.88, 3.50, 1.92], "fallback_active": false}'
    ledger.process_incoming_feed(sample_feed)
