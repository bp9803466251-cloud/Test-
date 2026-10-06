"""
redis_hub.py — Транспортный слой GatekeeperAI.
Реализация на базе встроенной urllib.request (правило 1.3).
Circuit Breaker: 10 ошибок → 60с → авто-восстановление (правило 1.9).
Конверт v700-prod для live-данных (§6).

Интеграция с redis_config.py:
  Все параметры (URL, TOKEN, timeout, circuit breaker, hash name, batch size)
  импортируются из redis_config.py — единой точки конфигурации Redis.
  Локальные env-чтения удалены — устранён дублирующий код (правило 1.3).

v8.11-patched:
  FIX-1: logging вместо print (§1.23)
  FIX-2: Делегирование сериализации в serialize_match/deserialize_match (§20.1)
  FIX-3: Retry с exponential backoff (§9.5)
  FIX-4: is_redis_available() → is_redis_configured() + PING
  FIX-5: __version__, расширенный __all__
  FIX-6: REDIS_MAX_PIPELINE используется в get_all_fields()
  FIX-7: Безопасное логирование — без payload (§1.23)
  FIX-8: PipelineBatch class (§1.20) — batch writes для коллекторов
  FIX-9: HTTP 429 Retry-After handling (§15)
  FIX-10: Request recreation в retry loop (urllib data consumption bug)
  FIX-11: expire_key() / ttl support
  FIX-12: Убран dead code после retry loop
  FIX-13: PipelineBatch.flush() — Upstash pipeline (массив команд одним POST)
  FIX-14: is_redis_available() — case-insensitive PONG check
  FIX-15: _deserialize — fallback для не-dict JSON
"""

import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Конфигурация из redis_config.py (единая точка) ─────────
from redis_config import (
    REDIS_REST_URL,
    REDIS_REST_TOKEN,
    REDIS_TIMEOUT as _CFG_TIMEOUT,
    REDIS_HASH_NAME,
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_TIMEOUT,
    REDIS_MAX_PIPELINE,
)

# ── Константы ──────────────────────────────────────────────
__version__ = "9.3-audited"
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
    if _cb_state == "closed":
        return True
    if _cb_state == "open":
        if time.monotonic() - _cb_opened_at >= CB_RESET_SECONDS:
            _cb_state = "half-open"
            logger.info("Circuit breaker -> half-open")
            return True
        return False
    if _cb_state == "half-open":
        return True
    return True


def _cb_on_success():
    global _cb_state, _cb_failures
    if _cb_state != "closed":
        logger.info("Circuit breaker -> closed (recovered)")
    _cb_failures = 0
    _cb_state = "closed"


def _cb_on_failure():
    global _cb_state, _cb_failures, _cb_opened_at
    _cb_failures += 1
    if _cb_failures >= CB_THRESHOLD and _cb_state != "open":
        _cb_state = "open"
        _cb_opened_at = time.monotonic()
        logger.warning("Circuit breaker -> open after %d failures", _cb_failures)


def get_circuit_breaker_status():
    return {"state": _cb_state, "failures": _cb_failures}


def reset_circuit_breaker():
    global _cb_state, _cb_failures, _cb_opened_at
    _cb_state = "closed"
    _cb_failures = 0
    _cb_opened_at = 0.0
    logger.info("Circuit breaker reset")


# ── Сериализация через хаб (§20.1) ──────────────────────────
def _serialize(obj):
    try:
        from gatekeeper_hub import serialize_match
        return serialize_match(obj)
    except ImportError:
        return json.dumps(obj, ensure_ascii=False, default=str)


def _deserialize(raw):
    """
    Десериализация JSON-строки в Python-объект.
    FIX-15: fallback для не-dict JSON (list, int, str).
    """
    if not raw or not isinstance(raw, str):
        return None
    # Пытаемся через хаб (если доступен)
    try:
        from gatekeeper_hub import deserialize_match
        result = deserialize_match(raw)
        if result is not None:
            return result
    except ImportError:
        pass
    # Fallback — собственный json.loads
    try:
        obj = json.loads(raw)
        # Возвращаем любой валидный JSON, не только dict
        if isinstance(obj, (dict, list, str, int, float, bool)):
            return obj
        return None
    except (json.JSONDecodeError, TypeError):
        return None


# ── Подключение ─────────────────────────────────────────────
def _get_redis_url():
    return REDIS_REST_URL


def _get_redis_token():
    return REDIS_REST_TOKEN


def _execute_upstash_cmd(args, retry=True):
    """
    Единый транспорт для всех команд Upstash REST API.
    args — список аргументов, например ["HGET", "GatekeeperAI", "match:xxx"]
    Возвращает результат (str) или None при ошибке.
    Retry с exponential backoff для transient-ошибок (§9.5).
    """
    if not _cb_can_pass():
        return None

    url = _get_redis_url()
    token = _get_redis_token()
    if not url or not token:
        return None

    payload_bytes = json.dumps(args).encode("utf-8")

    for attempt in range(MAX_RETRIES + 1 if retry else 1):
        # FIX-10: создаём новый Request для каждой попытки
        req = urllib.request.Request(
            url,
            data=payload_bytes,
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
            # FIX-9: HTTP 429 — Retry-After
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                if attempt < MAX_RETRIES and retry:
                    logger.warning("HTTP 429 rate limited, waiting %ds (attempt %d/%d)",
                                   retry_after, attempt + 1, MAX_RETRIES)
                    time.sleep(retry_after)
                    continue
                _cb_on_failure()
                logger.warning("HTTP 429 after %d retries", MAX_RETRIES)
                return None

            # HTTP 4xx (кроме 429) — не retry
            if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                _cb_on_failure()
                logger.warning("HTTP %d: %s", e.code, type(e).__name__)
                return None

            # Transient errors — retry
            if attempt < MAX_RETRIES and retry:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("Retry %d/%d after %.1fs: %s",
                               attempt + 1, MAX_RETRIES, delay, type(e).__name__)
                time.sleep(delay)
                continue

            _cb_on_failure()
            logger.warning("Error after %d retries: %s: %s",
                           MAX_RETRIES, type(e).__name__, e)
            return None

    _cb_on_failure()
    return None


def _execute_pipeline(commands):
    """
    FIX-13: Отправка массива команд одним POST-запросом (Upstash pipeline).
    commands — список списков: [["HSET", ...], ["SET", ...], ...]
    Возвращает список результатов или None.
    """
    if not commands:
        return []

    if not _cb_can_pass():
        return None

    url = _get_redis_url()
    token = _get_redis_token()
    if not url or not token:
        return None

    # Upstash REST API поддерживает массив команд в одном POST
    payload_bytes = json.dumps(commands).encode("utf-8")

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                results = json.loads(body)
                _cb_on_success()
                # Upstash возвращает массив результатов для pipeline
                if isinstance(results, list):
                    return [r.get("result") if isinstance(r, dict) else r for r in results]
                return results.get("result") if isinstance(results, dict) else results
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, OSError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                if attempt < MAX_RETRIES:
                    logger.warning("Pipeline HTTP 429, waiting %ds (attempt %d/%d)",
                                   retry_after, attempt + 1, MAX_RETRIES)
                    time.sleep(retry_after)
                    continue
                _cb_on_failure()
                return None

            if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                _cb_on_failure()
                logger.warning("Pipeline HTTP %d: %s", e.code, type(e).__name__)
                return None

            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("Pipeline retry %d/%d after %.1fs: %s",
                               attempt + 1, MAX_RETRIES, delay, type(e).__name__)
                time.sleep(delay)
                continue

            _cb_on_failure()
            logger.warning("Pipeline error after %d retries: %s", MAX_RETRIES, type(e).__name__)
            return None

    _cb_on_failure()
    return None


# ── Конверт v700-prod ───────────────────────────────────────
def _wrap_envelope(payload, sender_repo="unknown"):
    ts = datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    return {
        "version": ENVELOPE_VERSION,
        "sender_repo": sender_repo,
        "timestamp": ts,
        "payload": payload,
    }


def _unwrap_envelope(data):
    if data is None:
        return None
    if isinstance(data, dict):
        if "payload" in data and "version" in data:
            return data["payload"]
        return data
    if isinstance(data, str):
        obj = _deserialize(data)
        if obj is not None:
            if isinstance(obj, dict):
                if "payload" in obj and "version" in obj:
                    return obj["payload"]
                return obj
        return None
    return data


# ── PipelineBatch (§1.20) ───────────────────────────────────
class PipelineBatch:
    """
    Батч-буфер для массовой записи в Redis.
    Коллекторы используют его для накопления HSET/SET-команд
    и отправки одним POST-запросом (Upstash pipeline).
    FIX-13: flush() использует _execute_pipeline вместо последовательных запросов.
    """
    def __init__(self, max_size=None):
        self._commands = []
        self._max_size = max_size or BATCH_SIZE

    def add_hset(self, field, value):
        """Добавить HSET-команду в буфер."""
        if isinstance(value, (dict, list)):
            serialized = _serialize(value)
        elif isinstance(value, str):
            serialized = value
        else:
            serialized = _serialize(value)
        self._commands.append(["HSET", HASH_NAME, field, serialized])

    def add_set(self, key, value):
        """Добавить SET-команду в буфер."""
        if isinstance(value, (dict, list)):
            serialized = _serialize(value)
        elif isinstance(value, str):
            serialized = value
        else:
            serialized = _serialize(value)
        self._commands.append(["SET", key, serialized])

    def flush(self):
        """
        Выполнить все накопленные команды порциями max_size.
        FIX-13: каждая порция отправляется одним POST (Upstash pipeline),
        а не отдельными запросами на каждую команду.
        """
        total = 0
        for i in range(0, len(self._commands), self._max_size):
            batch = self._commands[i:i + self._max_size]
            results = _execute_pipeline(batch)
            if results is not None:
                total += len(batch)
            else:
                # Fallback: последовательная отправка при неудаче pipeline
                logger.warning("Pipeline failed, falling back to sequential for %d commands", len(batch))
                for cmd in batch:
                    _execute_upstash_cmd(cmd, retry=True)
                    total += 1
        self._commands.clear()
        return total

    def __len__(self):
        return len(self._commands)


# ── Публичный API ───────────────────────────────────────────
def is_redis_available():
    """
    Health-check Redis (PING) с circuit breaker и config check.
    FIX-14: case-insensitive PONG check — устойчив к регистру и whitespace.
    """
    try:
        from redis_config import is_redis_configured
        if not is_redis_configured():
            logger.warning("Redis not configured")
            return False
    except ImportError:
        if not REDIS_REST_URL or not REDIS_REST_TOKEN:
            return False

    result = _execute_upstash_cmd(["PING"])
    if not result:
        return False
    # FIX-14: устойчивая проверка PONG
    return str(result).strip().upper() == "PONG"


def save_to_cache(field_id, value, sender_repo="unknown"):
    """
    Запись поля в хеш GatekeeperAI с конвертом v700-prod.
    value — Python-объект (dict), не строка.
    """
    if isinstance(value, (dict, list)):
        envelope = _wrap_envelope(value, sender_repo)
        serialized = _serialize(envelope)
    elif isinstance(value, str):
        serialized = value
    else:
        serialized = _serialize(value)

    return _execute_upstash_cmd(["HSET", HASH_NAME, field_id, serialized])


def get_from_cache(field_id):
    """Чтение одного поля (с распаковкой конверта)."""
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
    Батчинг через BATCH_SIZE (§9.5).
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
        obj = _deserialize(raw)
        if obj is not None:
            return obj
        return raw
    return raw


def set_key(key, value):
    """
    Запись отдельного ключа (без конверта, raw JSON).
    Для history:match:* и system:* ключей.
    """
    if isinstance(value, (dict, list)):
        serialized = _serialize(value)
    elif isinstance(value, str):
        serialized = value
    else:
        serialized = _serialize(value)
    return _execute_upstash_cmd(["SET", key, serialized])


def delete_key(key):
    """Удаление отдельного ключа."""
    return _execute_upstash_cmd(["DEL", key])


def expire_key(key, seconds):
    """Установить TTL для ключа (FIX-11)."""
    return _execute_upstash_cmd(["EXPIRE", key, str(int(seconds))])


def get_ttl(key):
    """Получить оставшийся TTL ключа в секундах."""
    result = _execute_upstash_cmd(["TTL", key])
    if result is None:
        return -2
    try:
        return int(result)
    except (ValueError, TypeError):
        return -2


def scan_keys(pattern, count=1000):
    """
    SCAN по ключам с pattern (FIX-11).
    Возвращает список ключей. Безопаснее KEYS для больших БД.
    """
    all_keys = []
    cursor = "0"
    while True:
        result = _execute_upstash_cmd(["SCAN", cursor, "MATCH", pattern, "COUNT", str(count)])
        if not result or not isinstance(result, list) or len(result) < 2:
            break
        cursor = result[0]
        keys = result[1]
        if keys:
            all_keys.extend(keys)
        if cursor == "0" or cursor == 0:
            break
    return all_keys


def delete_keys_by_pattern(pattern):
    """
    Удалить все ключи по pattern (FIX-11).
    Использует SCAN + DEL (безопаснее FLUSHDB).
    """
    keys = scan_keys(pattern)
    deleted = 0
    for key in keys:
        _execute_upstash_cmd(["DEL", key])
        deleted += 1
    logger.info("Deleted %d keys matching %s", deleted, pattern)
    return deleted


# ── Экспорт ─────────────────────────────────────────────────
__all__ = [
    "__version__",
    "ENVELOPE_VERSION", "HASH_NAME",
    "MSK_TZ", "BATCH_SIZE", "REDIS_TIMEOUT",
    "is_redis_available",
    "save_to_cache", "get_from_cache", "batch_get_from_cache",
    "delete_from_cache", "get_all_fields", "field_exists",
    "get_key", "set_key", "delete_key",
    "expire_key", "get_ttl", "scan_keys", "delete_keys_by_pattern",
    "get_circuit_breaker_status", "reset_circuit_breaker",
    "PipelineBatch",
    "_execute_upstash_cmd",
    "_execute_pipeline",
]
