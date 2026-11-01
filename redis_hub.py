# redis_hub.py
"""
Gatekeeper-AI Ecosystem: Транспортный слой Redis Hub.
Версия 9.6.1-audited — Реализация на базе встроенной urllib.request (правило 1.3).
Circuit Breaker: 10 ошибок → 60с → авто-восстановление (правило 1.9).
"""

import os
import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Конфигурация из redis_config.py ────────────────────────
try:
    from redis_config import (
        REDIS_REST_URL,
        REDIS_REST_TOKEN,
        REDIS_TIMEOUT as _CFG_TIMEOUT,
        REDIS_HASH_NAME,
        CB_FAILURE_THRESHOLD,
        CB_RECOVERY_TIMEOUT,
        REDIS_MAX_PIPELINE,
    )
except ImportError:
    # Защищенный fallback для CI-тестов окружения
    REDIS_REST_URL = os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", "")
    REDIS_REST_TOKEN = os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN", "")
    _CFG_TIMEOUT = 10
    REDIS_HASH_NAME = "GatekeeperAI"
    CB_FAILURE_THRESHOLD = 10
    CB_RECOVERY_TIMEOUT = 60
    REDIS_MAX_PIPELINE = 50

# ── Константы ──────────────────────────────────────────────
__version__ = "9.6.1-audited"
ENVELOPE_VERSION = "v700-prod"
HASH_NAME = REDIS_HASH_NAME
REDIS_TIMEOUT = _CFG_TIMEOUT
CB_THRESHOLD = CB_FAILURE_THRESHOLD
CB_RESET_SECONDS = CB_RECOVERY_TIMEOUT
BATCH_SIZE = REDIS_MAX_PIPELINE or 50
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5

MSK_TZ = timezone(timedelta(hours=3))

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# ── Circuit Breaker ─────────────────────────────────────────
_cb_state = "closed"
_cb_failures = 0
_cb_opened_at = 0.0

def _cb_can_pass():
    global _cb_state, _cb_opened_at
    if _cb_state == "closed": return True
    if _cb_state == "open":
        if time.monotonic() - _cb_opened_at >= CB_RESET_SECONDS:
            _cb_state = "half-open"
            return True
        return False
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
    _cb_state, _cb_failures, _cb_opened_at = "closed", 0, 0.0

# ── Сериализация через хаб (§20.1) ──────────────────────────
def _serialize(obj):
    try:
        from gatekeeper_hub import serialize_match
        return serialize_match(obj)
    except ImportError:
        return json.dumps(obj, ensure_ascii=False, default=str)

def _deserialize(raw):
    if not raw or not isinstance(raw, str): return None
    try:
        from gatekeeper_hub import deserialize_match
        res = deserialize_match(raw)
        if res is not None: return res
    except ImportError: pass
    try:
        obj = json.loads(raw)
        if isinstance(obj, (dict, list, str, int, float, bool)): return obj
        return None
    except Exception: return None

# ── Единый transport Upstash REST API ──────────────────────
def _execute_upstash_cmd(args, retry=True):
    if not _cb_can_pass(): return None
    url, token = REDIS_REST_URL, REDIS_REST_TOKEN
    if not url or not token: return None

    payload_bytes = json.dumps(args).encode("utf-8")

    for attempt in range(MAX_RETRIES + 1 if retry else 1):
        req = urllib.request.Request(
            url, data=payload_bytes,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                _cb_on_success()
                return json.loads(body).get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                if attempt < MAX_RETRIES and retry:
                    time.sleep(retry_after)
                    continue
                _cb_on_failure()
                return None
            if e.code < 500:
                _cb_on_failure()
                return None
            if attempt < MAX_RETRIES and retry:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
        except Exception:
            if attempt < MAX_RETRIES and retry:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
    _cb_on_failure()
    return None

def _execute_pipeline(commands):
    if not commands or not _cb_can_pass(): return None
    url, token = REDIS_REST_URL, REDIS_REST_TOKEN
    if not url or not token: return None

    payload_bytes = json.dumps(commands).encode("utf-8")

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url, data=payload_bytes,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                results = json.loads(body)
                _cb_on_success()
                if isinstance(results, list):
                    return [r.get("result") if isinstance(r, dict) else r for r in results]
                return results.get("result") if isinstance(results, dict) else results
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
    _cb_on_failure()
    return None

# ── Конверт v700-prod ───────────────────────────────────────
def _wrap_envelope(payload, sender_repo="unknown"):
    ts = datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    return {"version": ENVELOPE_VERSION, "sender_repo": sender_repo, "timestamp": ts, "payload": payload}

def _unwrap_envelope(data):
    if data is None: return None
    if isinstance(data, dict):
        if "model_inputs" in data or "match_metadata" in data: return data
        if "payload" in data and "version" in data: return data["payload"]
        return data
    if isinstance(data, str):
        obj = _deserialize(data)
        if obj is not None and isinstance(obj, dict):
            if "model_inputs" in obj or "match_metadata" in obj: return obj
            if "payload" in obj and "version" in obj: return obj["payload"]
            return obj
    return data

# ── PipelineBatch ───────────────────────────────────────────
class PipelineBatch:
    def __init__(self, max_size=None):
        self._commands = []
        self._max_size = max_size or BATCH_SIZE

    def add_hset(self, field, value):
        serialized = value if isinstance(value, str) else _serialize(value)
        self._commands.append(["HSET", HASH_NAME, field, serialized])

    def add_set(self, key, value):
        serialized = value if isinstance(value, str) else _serialize(value)
        self._commands.append(["SET", key, serialized])

    def flush(self):
        total = 0
        for i in range(0, len(self._commands), self._max_size):
            batch = self._commands[i:i + self._max_size]
            if _execute_pipeline(batch) is not None: total += len(batch)
            else:
                for cmd in batch:
                    _execute_upstash_cmd(cmd, retry=True)
                    total += 1
        self._commands.clear()
        return total

    def __len__(self): return len(self._commands)

# ── Публичный API ───────────────────────────────────────────
def is_redis_available():
    try:
        from redis_config import is_redis_configured
        if not is_redis_configured(): return False
    except ImportError:
        if not REDIS_REST_URL or not REDIS_REST_TOKEN: return False
    res = _execute_upstash_cmd(["PING"])
    return str(res).strip().upper() == "PONG" if res else False

def save_to_cache(field_id, value, sender_repo="unknown"):
    if isinstance(value, (dict, list)):
        serialized = _serialize(_wrap_envelope(value, sender_repo))
    else:
        serialized = str(value)
    return _execute_upstash_cmd(["HSET", HASH_NAME, field_id, serialized])

def get_from_cache(field_id):
    raw = _execute_upstash_cmd(["HGET", HASH_NAME, field_id])
    if raw is None: return None
    payload = _unwrap_envelope(raw)
    return payload if isinstance(payload, dict) else None

def batch_get_from_cache(field_ids):
    if not field_ids: return {}
    raw = _execute_upstash_cmd(["HMGET", HASH_NAME] + list(field_ids))
    if not raw or not isinstance(raw, list): return {}
    result = {}
    for fid, val in zip(field_ids, raw):
        if val is not None:
            payload = _unwrap_envelope(val)
            if isinstance(payload, dict): result[fid] = payload
    return result

def delete_from_cache(field_id): return _execute_upstash_cmd(["HDEL", HASH_NAME, field_id])

def get_all_fields():
    all_keys = _execute_upstash_cmd(["HKEYS", HASH_NAME])
    if not all_keys or not isinstance(all_keys, list): return {}
    result = {}
    for i in range(0, len(all_keys), BATCH_SIZE):
        batch = all_keys[i:i + BATCH_SIZE]
        values = _execute_upstash_cmd(["HMGET", HASH_NAME] + batch)
        if not values or not isinstance(values, list): continue
        for fid, val in zip(batch, values):
            if val is not None:
                payload = _unwrap_envelope(val)
                if isinstance(payload, dict): result[fid] = payload
    return result

def field_exists(field_id): return bool(_execute_upstash_cmd(["HEXISTS", HASH_NAME, field_id]))

def get_key(key):
    raw = _execute_upstash_cmd(["GET", key])
    return _deserialize(raw) if isinstance(raw, str) else raw

