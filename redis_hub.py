"""
redis_hub.py — Транспортный слой GatekeeperAI.
Реализация на базе встроенной urllib.request (правило 1.3).
Circuit Breaker: 10 ошибок → 60с → авто-восстановление (правило 1.9).
Конверт v700-prod для live-данных (§6).
"""

import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Константы ──────────────────────────────────────────────
ENVELOPE_VERSION = "v700-prod"
HASH_NAME = "GatekeeperAI"
REDIS_TIMEOUT = 30  # секунд
CB_THRESHOLD = 10
CB_RESET_SECONDS = 60
BATCH_SIZE = 50  # для HKEYS + HMGET

MSK_TZ = timezone(timedelta(hours=3))

# ── Circuit Breaker ─────────────────────────────────────────
_cb_state = "closed"  # closed | open | half-open
_cb_failures = 0
_cb_opened_at = 0.0


def _cb_can_pass():
    global _cb_state, _cb_opened_at
    if _cb_state == "closed":
        return True
    if _cb_state == "open":
        if time.monotonic() - _cb_opened_at >= CB_RESET_SECONDS:
            _cb_state = "half-open"
            return True
        return False
    if _cb_state == "half-open":
        return True
    return True


def _cb_on_success():
    global _cb_state, _cb_failures
    _cb_failures = 0
    _cb_state = "closed"


def _cb_on_failure():
    global _cb_state, _cb_failures, _cb_opened_at
    _cb_failures += 1
    if _cb_failures >= CB_THRESHOLD and _cb_state != "open":
        _cb_state = "open"
        _cb_opened_at = time.monotonic()


def get_circuit_breaker_status():
    return {"state": _cb_state, "failures": _cb_failures}


def reset_circuit_breaker():
    global _cb_state, _cb_failures, _cb_opened_at
    _cb_state = "closed"
    _cb_failures = 0
    _cb_opened_at = 0.0


# ── Подключение ─────────────────────────────────────────────
def _get_redis_url():
    return os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", 
                          os.environ.get("UPSTASH_REDIS_REST_URL", ""))


def _get_redis_token():
    return os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN",
                          os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""))


def _execute_upstash_cmd(args):
    """
    Единый транспорт для всех команд Upstash REST API.
    args — список аргументов команды, например ["HGET", "GatekeeperAI", "match:xxx"]
    Возвращает результат (str) или None при ошибке.
    """
    if not _cb_can_pass():
        return None

    url = _get_redis_url()
    token = _get_redis_token()
    if not url or not token:
        return None

    payload = json.dumps(args).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            _cb_on_success()
            return result.get("result")
    except (urllib.error.URLError, urllib.error.HTTPError, 
            json.JSONDecodeError, OSError, TimeoutError) as e:
        _cb_on_failure()
        print(f"[REDIS_HUB] Error: {type(e).__name__}: {e}")
        return None


# ── Конверт v700-prod ───────────────────────────────────────
def _wrap_envelope(payload, sender_repo="unknown"):
    """Оборачивает payload в конверт v700-prod."""
    ts = datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    return {
        "version": ENVELOPE_VERSION,
        "sender_repo": sender_repo,
        "timestamp": ts,
        "payload": payload,
    }


def _unwrap_envelope(data):
    """
    Распаковывает конверт. Возвращает payload (правило 1.16).
    Если data — не конверт, возвращает как есть (backward compat).
    """
    if data is None:
        return None
    if isinstance(data, dict):
        if "payload" in data and "version" in data:
            return data["payload"]
        return data
    # Строка — может быть JSON
    if isinstance(data, str):
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                if "payload" in obj and "version" in obj:
                    return obj["payload"]
                return obj
            return obj
        except (json.JSONDecodeError, TypeError):
            return None
    return data


# ── Публичный API ───────────────────────────────────────────
def is_redis_available():
    """Health-check Redis (PING) с circuit breaker."""
    result = _execute_upstash_cmd(["PING"])
    return result == "PONG" if result else False


def save_to_cache(field_id, value, sender_repo="unknown"):
    """
    Запись поля в хеш GatekeeperAI с конвертом v700-prod.
    value — Python-объект (dict), не строка (FIX-3 в hub).
    """
    if isinstance(value, (dict, list)):
        envelope = _wrap_envelope(value, sender_repo)
        serialized = json.dumps(envelope, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        serialized = value
    else:
        serialized = json.dumps(value, default=str)

    return _execute_upstash_cmd(["HSET", HASH_NAME, field_id, serialized])


def get_from_cache(field_id):
    """
    Чтение одного поля (с распаковкой конверта).
    Возвращает payload (dict) или None (правило 1.16, 1.17).
    """
    raw = _execute_upstash_cmd(["HGET", HASH_NAME, field_id])
    if raw is None:
        return None
    payload = _unwrap_envelope(raw)
    if not isinstance(payload, dict):
        return None
    return payload


def batch_get_from_cache(field_ids):
    """Пакетное чтение нескольких полей."""
    if not field_ids:
        return {}
    args = ["HMGET", HASH_NAME] + list(field_ids)
    raw = _execute_upstash_cmd(args)
    if not raw or not isinstance(raw, list):
        return {}
    result = {}
    for fid, val in zip(field_ids, raw):
        if val is not None:
            payload = _unwrap_envelope(val)
            if isinstance(payload, dict):
                result[fid] = payload
    return result


def delete_from_cache(field_id):
    """Удаление поля (HDEL)."""
    return _execute_upstash_cmd(["HDEL", HASH_NAME, field_id])


def get_all_fields():
    """
    Возвращает все поля хэш-таблицы (правило 1.15: HKEYS + HMGET).
    При >20 000 полей HGETALL нестабилен.
    """
    keys_raw = _execute_upstash_cmd(["HKEYS", HASH_NAME])
    if not keys_raw or not isinstance(keys_raw, list):
        return {}

    all_keys = keys_raw
    result = {}
    for i in range(0, len(all_keys), BATCH_SIZE):
        batch = all_keys[i:i + BATCH_SIZE]
        args = ["HMGET", HASH_NAME] + batch
        values = _execute_upstash_cmd(args)
        if not values or not isinstance(values, list):
            continue
        for fid, val in zip(batch, values):
            if val is not None:
                payload = _unwrap_envelope(val)
                if isinstance(payload, dict):
                    result[fid] = payload
    return result


def field_exists(field_id):
    """Проверка существования поля."""
    result = _execute_upstash_cmd(["HEXISTS", HASH_NAME, field_id])
    return bool(result)


def get_key(key):
    """
    Чтение отдельного ключа (без конверта, raw JSON).
    Для history:match:* и system:* ключей.
    """
    raw = _execute_upstash_cmd(["GET", key])
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def set_key(key, value):
    """
    Запись отдельного ключа (без конверта, raw JSON).
    Для history:match:* и system:* ключей.
    """
    if isinstance(value, (dict, list)):
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        serialized = value
    else:
        serialized = json.dumps(value, default=str)
    return _execute_upstash_cmd(["SET", key, serialized])


def delete_key(key):
    """Удаление отдельного ключа."""
    return _execute_upstash_cmd(["DEL", key])


# ── Экспорт ─────────────────────────────────────────────────
__all__ = [
    "ENVELOPE_VERSION", "HASH_NAME",
    "is_redis_available",
    "save_to_cache", "get_from_cache", "batch_get_from_cache",
    "delete_from_cache", "get_all_fields", "field_exists",
    "get_key", "set_key", "delete_key",
    "get_circuit_breaker_status", "reset_circuit_breaker",
    "_execute_upstash_cmd",
]
