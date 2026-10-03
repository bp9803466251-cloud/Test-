#!/usr/bin/env python3
"""
Транспортный слой Gatekeeper-AI Ecosystem v710.
Единый инкапсулированный шлюз к Upstash Redis REST API.
Только urllib.request. Никаких сторонних клиентов.

v2.2: + __all__, + _get_redis_config helper, + save_to_cache type guard,
      + get_from_cache double-serialization guard, + PipelineBatch type safety,
      + _trip_breaker error classification, + docstrings for key vs cache,
      - дублирование URL/Token чтения (extracted to _get_redis_config),
      - get_all_fields sleep 0.1 → 0.02
v2.1: + zadd_key dual-API, + zrange_key_withscores, + type_key,
      + execute_pipeline retry, + cooldown_remaining
v2.0: + Pipeline, + SCAN, + отдельные ключи, + ZSET, + SET,
      унифицированный breaker (threshold=10, cooldown=60s, timeout=30s)
"""
import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict, Tuple

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
REDIS_TIMEOUT = int(os.getenv("GATEKEEPER_REDIS_TIMEOUT", "30"))
HMGET_CHUNK_SIZE = int(os.getenv("GATEKEEPER_HMGET_CHUNK_SIZE", "50"))

IMMUTABLE_ROOT_ADDRESS = "GatekeeperAI"
ENVELOPE_VERSION = "v710"

__all__ = [
    # Circuit Breaker
    "reset_circuit_breaker", "get_circuit_breaker_status", "get_remaining_cooldown",
    # Pipeline
    "execute_pipeline", "PipelineBatch",
    # Hash-кэш (конверт v710) — HSET/HGET в GatekeeperAI
    "get_from_cache", "save_to_cache", "delete_from_cache", "field_exists",
    "get_all_fields", "batch_get_from_cache",
    # Индивидуальные ключи (без конверта) — GET/SET
    "get_key", "set_key", "delete_key", "delete_keys", "key_exists",
    "type_key", "scan_keys",
    # Hash на произвольных ключах
    "hset_key", "hget_key", "hgetall_key", "hdel_key", "hlen_key",
    # ZSET
    "zadd_key", "zcard_key", "zrange_key", "zrange_key_withscores", "zrem_key",
    # SET
    "sadd_key", "smembers_key", "srem_key",
    # Системные
    "is_redis_available", "dbsize",
]


# ---------------------------------------------------------------------------
# Circuit Breaker — унифицированный (threshold=10, cooldown=60s)
# ---------------------------------------------------------------------------
_circuit_breaker_open = False
_error_count = 0
_breaker_threshold = 10
_breaker_cooldown_seconds = 60
_breaker_tripped_at = 0.0


def _check_circuit_breaker() -> bool:
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    if _circuit_breaker_open:
        elapsed = time.time() - _breaker_tripped_at
        if elapsed >= _breaker_cooldown_seconds:
            print(f"[REDIS] Circuit Breaker авто-восстановление после {elapsed:.0f}s")
            _circuit_breaker_open = False
            _error_count = 0
            return True
        print(f"[REDIS WARNING] Circuit Breaker активен — осталось {_breaker_cooldown_seconds - elapsed:.0f}s")
        return False
    return True


def _trip_breaker(error_type: str = "transient") -> None:
    """Increment error count. error_type: 'transient' | 'auth' | 'config'."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    _error_count += 1
    if _error_count >= _breaker_threshold:
        if not _circuit_breaker_open:
            _breaker_tripped_at = time.time()
        _circuit_breaker_open = True
        print(f"[REDIS ALERT] Circuit Breaker сработал! Ошибок подряд: {_error_count} (последняя: {error_type})")


def reset_circuit_breaker() -> None:
    """Публичный сброс circuit breaker."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    was_open = _circuit_breaker_open
    _circuit_breaker_open = False
    _error_count = 0
    _breaker_tripped_at = 0.0
    if was_open:
        print("[REDIS] Circuit Breaker сброшен вручную")


def get_circuit_breaker_status() -> dict:
    return {
        "open": _circuit_breaker_open,
        "error_count": _error_count,
        "threshold": _breaker_threshold,
        "cooldown_remaining": get_remaining_cooldown(),
    }


def get_remaining_cooldown() -> int:
    if not _circuit_breaker_open:
        return 0
    elapsed = time.time() - _breaker_tripped_at
    return max(0, int(_breaker_cooldown_seconds - elapsed))


# ---------------------------------------------------------------------------
# Redis конфигурация — единая точка
# ---------------------------------------------------------------------------
def _get_redis_config() -> Tuple[Optional[str], Optional[str]]:
    """Возвращает (url, token) из ENV. Пробует UPSTASH_* затем SHARED_*."""
    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    redis_url = (redis_url or "").rstrip("/")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")
    return (redis_url or None), (redis_token or None)


# ---------------------------------------------------------------------------
# Базовый транспорт
# ---------------------------------------------------------------------------

def _execute_upstash_cmd(cmd_parts: List[Any], timeout: int = REDIS_TIMEOUT) -> Optional[Any]:
    if not _check_circuit_breaker():
        return None

    redis_url, redis_token = _get_redis_config()

    if not redis_url or not redis_token:
        print("[CRITICAL ERROR] Конфигурация Upstash Redis не найдена!")
        _trip_breaker("config")
        return None

    url = f"{redis_url}/"
    payload = json.dumps(cmd_parts).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {redis_token}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        req.method = "POST"
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            _reset_breaker_success()
            return res_json.get("result")
    except urllib.error.HTTPError as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        target = cmd_parts[2] if len(cmd_parts) > 2 else (cmd_parts[1] if len(cmd_parts) > 1 else "ROOT")
        # 401/403 — auth ошибки, не решатся retry
        error_type = "auth" if e.code in (401, 403) else "transient"
        print(f"[REDIS ERROR] {cmd_name} \'{target}\': HTTP {e.code} {e.reason}")
        _trip_breaker(error_type)
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        target = cmd_parts[2] if len(cmd_parts) > 2 else (cmd_parts[1] if len(cmd_parts) > 1 else "ROOT")
        print(f"[REDIS ERROR] {cmd_name} \'{target}\': {e}")
        _trip_breaker("transient")
        return None
    except Exception as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        print(f"[REDIS ERROR] Необработанная {cmd_name}: {e}")
        _trip_breaker("transient")
        return None


def _reset_breaker_success() -> None:
    global _error_count
    _error_count = 0


# ---------------------------------------------------------------------------
# Pipeline — с retry
# ---------------------------------------------------------------------------

def execute_pipeline(commands: List[List[Any]]) -> Optional[Any]:
    """Выполнить pipeline-команды через /pipeline endpoint. 2 retry с backoff."""
    if not commands:
        return None
    if not _check_circuit_breaker():
        return None

    redis_url, redis_token = _get_redis_config()

    if not redis_url or not redis_token:
        print("[CRITICAL ERROR] Конфигурация Upstash Redis не найдена!")
        _trip_breaker("config")
        return None

    url = f"{redis_url}/pipeline"
    payload = json.dumps(commands).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {redis_token}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            req.method = "POST"
            with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as response:
                res_json = json.loads(response.read().decode("utf-8"))
                _reset_breaker_success()
                return res_json
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(f"[REDIS PIPELINE ERROR] Auth: HTTP {e.code}")
                _trip_breaker("auth")
                return None
            if attempt < max_retries - 1:
                wait_sec = 2 * (attempt + 1)
                print(f"[REDIS PIPELINE] retry {attempt + 1}/{max_retries} через {wait_sec}s: HTTP {e.code}")
                time.sleep(wait_sec)
                continue
            print(f"[REDIS PIPELINE ERROR] HTTP {e.code}: {e.reason}")
            _trip_breaker("transient")
            return None
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            if attempt < max_retries - 1:
                wait_sec = 2 * (attempt + 1)
                print(f"[REDIS PIPELINE] retry {attempt + 1}/{max_retries} через {wait_sec}s: {e}")
                time.sleep(wait_sec)
                continue
            print(f"[REDIS PIPELINE ERROR] {e}")
            _trip_breaker("transient")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                wait_sec = 2 * (attempt + 1)
                print(f"[REDIS PIPELINE] retry {attempt + 1}/{max_retries} через {wait_sec}s: {e}")
                time.sleep(wait_sec)
                continue
            print(f"[REDIS PIPELINE ERROR] Необработанная: {e}")
            _trip_breaker("transient")
            return None

    return None


class PipelineBatch:
    """
    Батчинг Redis-команд через /pipeline.
    Используется football_data_to_redis.py вместо собственного RedisClient.
    """

    def __init__(self, dry_run: bool = False, max_batch: int = 50, batch_delay: float = 0.15):
        self.dry_run = dry_run
        self.max_batch = max_batch
        self.batch_delay = batch_delay
        self._commands: List[List[str]] = []
        self._count = 0
        self._total_sent_count = 0
        self._total_batch_count = 0

    def add(self, command: str, *args) -> None:
        """Добавить команду. Все args приводятся к str (требование Upstash REST API)."""
        # Filter None args — Redis не принимает None как значение
        safe_args = [str(a) for a in args if a is not None]
        self._commands.append([command] + safe_args)
        self._count += 1
        if self._count >= self.max_batch:
            self.flush()

    def flush(self) -> Optional[Any]:
        if not self._commands:
            return None
        commands = self._commands
        self._commands = []
        self._count = 0

        if self.dry_run:
            self._total_sent_count += len(commands)
            self._total_batch_count += 1
            return None

        result = execute_pipeline(commands)
        self._total_sent_count += len(commands)
        self._total_batch_count += 1
        if self.batch_delay > 0:
            time.sleep(self.batch_delay)
        return result

    @property
    def total_sent(self) -> int:
        return self._total_sent_count

    @property
    def total_batches(self) -> int:
        return self._total_batch_count


# ---------------------------------------------------------------------------
# Hash-кэш (конверт v710) — HSET/HGET в IMMUTABLE_ROOT_ADDRESS
# ---------------------------------------------------------------------------

def get_from_cache(field_id: str) -> Optional[Any]:
    """
    Точечное извлечение из хэш-кэша с обработкой конверта.
    Возвращает payload (Python-объект) или None.
    Защита от double-serialization: если payload — строка JSON, парсит её.
    """
    raw_result = _execute_upstash_cmd(["HGET", IMMUTABLE_ROOT_ADDRESS, field_id])
    if raw_result is None:
        return None
    try:
        envelope = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(envelope, dict) and "payload" in envelope:
        payload = envelope["payload"]
        # Double-serialization guard: если caller передал json.dumps() в save_to_cache,
        # payload будет строкой JSON. Пытаемся распарсить.
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                return payload
        return payload
    return envelope


def save_to_cache(field_id: str, data: Any) -> bool:
    """
    Атомарная запись в хэш-кэш с конвертом v710.
    data должен быть Python-объектом (dict, list, str, number).
    Если data — строка JSON, она будет сохранена как строка (double-serialization).
    Предупреждение выводится в stderr.
    """
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_name = env_repo.split("/")[-1] if "/" in env_repo else (env_repo or "local_dev")
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Type guard: если data — строка, которая выглядит как JSON, предупреждаем
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            import sys
            print(f"[REDIS WARNING] save_to_cache('{field_id}'): data — JSON-строка. "
                  f"Передавайте Python-объект, не json.dumps().", file=sys.stderr)

    envelope = {
        "version": ENVELOPE_VERSION,
        "sender_repo": repo_name,
        "timestamp": current_time,
        "payload": data,
    }

    res = _execute_upstash_cmd(["HSET", IMMUTABLE_ROOT_ADDRESS, field_id, json.dumps(envelope)])
    return res is not None


def delete_from_cache(field_id: str) -> bool:
    res = _execute_upstash_cmd(["HDEL", IMMUTABLE_ROOT_ADDRESS, field_id])
    return res is not None


def field_exists(field_id: str) -> bool:
    res = _execute_upstash_cmd(["HEXISTS", IMMUTABLE_ROOT_ADDRESS, field_id])
    return bool(res)


def get_all_fields() -> Dict[str, Any]:
    """Возвращает все поля хэш-кэша как dict {field_id: value}."""
    hlen_res = _execute_upstash_cmd(["HLEN", IMMUTABLE_ROOT_ADDRESS])
    total_expected = 0
    if hlen_res is not None:
        try:
            total_expected = int(hlen_res)
        except (TypeError, ValueError):
            pass
    print(f"[REDIS] get_all_fields: HLEN={total_expected}")

    result = {}
    cursor = "0"
    iterations = 0
    max_iterations = 5000
    consecutive_failures = 0
    max_consecutive_failures = 5

    while iterations < max_iterations:
        iterations += 1
        res = None
        for retry_attempt in range(3):
            res = _execute_upstash_cmd(
                ["HSCAN", IMMUTABLE_ROOT_ADDRESS, str(cursor), "COUNT", "50"],
                timeout=30,
            )
            if res is not None:
                break
            if retry_attempt < 2:
                wait_sec = 3 * (retry_attempt + 1)
                print(f"[REDIS] get_all_fields: retry {retry_attempt + 1}/3 через {wait_sec}s")
                time.sleep(wait_sec)

        if res is None or not isinstance(res, list) or len(res) < 2:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                print(f"[REDIS WARNING] get_all_fields: {consecutive_failures} неудач подряд, остановка")
                break
            wait_sec = 5 * consecutive_failures
            print(f"[REDIS] get_all_fields: ждём {wait_sec}s")
            time.sleep(wait_sec)
            continue

        consecutive_failures = 0
        next_cursor = res[0]
        fields = res[1]

        if not isinstance(fields, list):
            break

        for i in range(0, len(fields), 2):
            if i + 1 < len(fields):
                key = fields[i]
                raw_value = fields[i + 1]
                if raw_value is None:
                    continue
                try:
                    envelope = json.loads(raw_value)
                    if isinstance(envelope, dict) and "payload" in envelope:
                        payload = envelope["payload"]
                        # Double-serialization guard
                        if isinstance(payload, str):
                            try:
                                result[key] = json.loads(payload)
                            except (json.JSONDecodeError, TypeError):
                                result[key] = payload
                        else:
                            result[key] = payload
                    else:
                        result[key] = envelope
                except (json.JSONDecodeError, TypeError):
                    result[key] = raw_value

        if iterations % 50 == 0:
            print(f"[REDIS] get_all_fields: {len(result)} полей (итерация {iterations})")

        if str(next_cursor) == "0":
            break
        cursor = next_cursor
        time.sleep(0.02)

    if total_expected > 0 and len(result) != total_expected:
        diff = total_expected - len(result)
        if diff > 0:
            print(f"[REDIS WARNING] get_all_fields: HLEN={total_expected}, загружено={len(result)}, пропущено {diff}")
    else:
        print(f"[REDIS] get_all_fields: HLEN={total_expected}, загружено={len(result)}")

    return result


def batch_get_from_cache(keys: List[str], chunk_size: int = HMGET_CHUNK_SIZE) -> Dict[str, Any]:
    """Пакетное чтение полей через HMGET."""
    if not keys:
        return {}
    result = {}
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        cmd = ["HMGET", IMMUTABLE_ROOT_ADDRESS] + chunk
        res = _execute_upstash_cmd(cmd)
        if res is None:
            break
        for key, raw_value in zip(chunk, res):
            if raw_value is None:
                continue
            try:
                envelope = json.loads(raw_value)
                if isinstance(envelope, dict) and "payload" in envelope:
                    payload = envelope["payload"]
                    if isinstance(payload, str):
                        try:
                            result[key] = json.loads(payload)
                        except (json.JSONDecodeError, TypeError):
                            result[key] = payload
                    else:
                        result[key] = payload
                else:
                    result[key] = envelope
            except (json.JSONDecodeError, TypeError):
                result[key] = raw_value
    return result


# ---------------------------------------------------------------------------
# Индивидуальные ключи (без конверта) — GET/SET
# ---------------------------------------------------------------------------

def get_key(key: str) -> Optional[Any]:
    """Получить значение отдельного ключа (JSON, без конверта)."""
    raw = _execute_upstash_cmd(["GET", key])
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def set_key(key: str, value: Any) -> bool:
    """Записать отдельный ключ (JSON, без конверта)."""
    data = json.dumps(value) if not isinstance(value, str) else value
    res = _execute_upstash_cmd(["SET", key, data])
    return res is not None


def delete_key(key: str) -> bool:
    res = _execute_upstash_cmd(["DEL", key])
    return res is not None


def delete_keys(keys: List[str]) -> int:
    """Удалить несколько ключей. Возвращает количество удалённых."""
    if not keys:
        return 0
    res = _execute_upstash_cmd(["DEL"] + keys)
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


def key_exists(key: str) -> bool:
    res = _execute_upstash_cmd(["EXISTS", key])
    try:
        return bool(int(res)) if res else False
    except (TypeError, ValueError):
        return False


def type_key(key: str) -> str:
    """Возвращает тип ключа: string, hash, zset, set, list, none."""
    res = _execute_upstash_cmd(["TYPE", key])
    if res is None:
        return "none"
    if isinstance(res, str):
        return res
    return str(res) if res else "none"


def scan_keys(pattern: str, count: int = 500) -> List[str]:
    """SCAN по паттерну (например, 'history:match:*')."""
    result: List[str] = []
    cursor = "0"
    iterations = 0
    max_iterations = 1000

    while iterations < max_iterations:
        iterations += 1
        res = _execute_upstash_cmd(["SCAN", str(cursor), "MATCH", pattern, "COUNT", str(count)])
        if res is None or not isinstance(res, list) or len(res) < 2:
            break

        next_cursor = res[0]
        keys = res[1]

        if isinstance(keys, list):
            result.extend(keys)

        if str(next_cursor) == "0":
            break
        cursor = next_cursor
        time.sleep(0.01)

    return result


# ---------------------------------------------------------------------------
# Hash на произвольных ключах
# ---------------------------------------------------------------------------

def hset_key(key: str, field: str, value: str) -> bool:
    res = _execute_upstash_cmd(["HSET", key, field, value])
    return res is not None


def hget_key(key: str, field: str) -> Optional[str]:
    return _execute_upstash_cmd(["HGET", key, field])


def hgetall_key(key: str) -> Dict[str, str]:
    res = _execute_upstash_cmd(["HGETALL", key])
    if not res or not isinstance(res, list):
        return {}
    result = {}
    for i in range(0, len(res), 2):
        if i + 1 < len(res):
            result[res[i]] = res[i + 1]
    return result


def hdel_key(key: str, *fields: str) -> int:
    if not fields:
        return 0
    res = _execute_upstash_cmd(["HDEL", key] + list(fields))
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


def hlen_key(key: str) -> int:
    res = _execute_upstash_cmd(["HLEN", key])
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# ZSET (для history:league:*)
# ---------------------------------------------------------------------------

def zadd_key(key: str, score=None, member=None, mapping=None) -> bool:
    """
    Добавить элемент в ZSET. Dual-API:
      zadd_key("history:league:E0", score=1.5, member="cid_123")
      zadd_key("history:league:E0", mapping={"cid_1": 1.5, "cid_2": 2.0})
    """
    if mapping is not None:
        if not isinstance(mapping, dict) or not mapping:
            return False
        cmd = ["ZADD", key]
        for m, s in mapping.items():
            cmd.extend([str(s), m])
        res = _execute_upstash_cmd(cmd)
        return res is not None
    else:
        if score is None or member is None:
            return False
        res = _execute_upstash_cmd(["ZADD", key, str(score), member])
        return res is not None


def zcard_key(key: str) -> int:
    res = _execute_upstash_cmd(["ZCARD", key])
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


def zrange_key(key: str, start: int = 0, end: int = -1) -> List[str]:
    res = _execute_upstash_cmd(["ZRANGE", key, str(start), str(end)])
    return res if isinstance(res, list) else []


def zrange_key_withscores(key: str, start: int = 0, end: int = -1) -> List[tuple]:
    """ZRANGE ... WITHSCORES. Возвращает [(member, score), ...]."""
    res = _execute_upstash_cmd(["ZRANGE", key, str(start), str(end), "WITHSCORES"])
    if not isinstance(res, list) or not res:
        return []
    pairs = []
    for i in range(0, len(res), 2):
        if i + 1 < len(res):
            member = res[i]
            try:
                score = float(res[i + 1])
            except (TypeError, ValueError):
                score = 0.0
            pairs.append((member, score))
    return pairs


def zrem_key(key: str, *members: str) -> int:
    if not members:
        return 0
    res = _execute_upstash_cmd(["ZREM", key] + list(members))
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# SET (для history:team:*)
# ---------------------------------------------------------------------------

def sadd_key(key: str, *members: str) -> bool:
    if not members:
        return False
    res = _execute_upstash_cmd(["SADD", key] + list(members))
    return res is not None


def smembers_key(key: str) -> List[str]:
    res = _execute_upstash_cmd(["SMEMBERS", key])
    return res if isinstance(res, list) else []


def srem_key(key: str, *members: str) -> int:
    if not members:
        return 0
    res = _execute_upstash_cmd(["SREM", key] + list(members))
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Системные функции
# ---------------------------------------------------------------------------

def is_redis_available() -> bool:
    if not _check_circuit_breaker():
        return False
    res = _execute_upstash_cmd(["PING"])
    return res is not None


def dbsize() -> int:
    res = _execute_upstash_cmd(["DBSIZE"])
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return 0
