#!/usr/bin/env python3
"""
Транспортный слой Gatekeeper-AI Ecosystem v710.
Единый инкапсулированный шлюз к Upstash Redis REST API.
Только urllib.request. Никаких сторонних клиентов.

v2.0: + Pipeline, + SCAN, + отдельные ключи, + ZSET, + SET,
      унифицированный breaker (threshold=10, cooldown=60s, timeout=30s)
"""
import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, List, Dict, Tuple

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
REDIS_TIMEOUT = int(os.getenv("GATEKEEPER_REDIS_TIMEOUT", "30"))
EXTERNAL_API_TIMEOUT = int(os.getenv("GATEKEEPER_EXTERNAL_API_TIMEOUT", "15"))
HMGET_CHUNK_SIZE = int(os.getenv("GATEKEEPER_HMGET_CHUNK_SIZE", "50"))

IMMUTABLE_ROOT_ADDRESS = "GatekeeperAI"
MSK_TIMEZONE = timezone(timedelta(hours=3))
ENVELOPE_VERSION = "v710"

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


def _trip_breaker() -> None:
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    _error_count += 1
    if _error_count >= _breaker_threshold:
        if not _circuit_breaker_open:
            _breaker_tripped_at = time.time()
        _circuit_breaker_open = True
        print(f"[REDIS ALERT] Circuit Breaker сработал! Ошибок подряд: {_error_count}")


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
    }


def get_remaining_cooldown() -> int:
    if not _circuit_breaker_open:
        return 0
    elapsed = time.time() - _breaker_tripped_at
    return max(0, int(_breaker_cooldown_seconds - elapsed))


# ---------------------------------------------------------------------------
# Базовый транспорт
# ---------------------------------------------------------------------------

def _execute_upstash_cmd(cmd_parts: List[Any], timeout: int = REDIS_TIMEOUT) -> Optional[Any]:
    if not _check_circuit_breaker():
        return None

    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    redis_url = (redis_url or "").rstrip("/")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")

    if not redis_url or not redis_token:
        print("[CRITICAL ERROR] Конфигурация Upstash Redis не найдена!")
        _trip_breaker()
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
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        target = cmd_parts[2] if len(cmd_parts) > 2 else (cmd_parts[1] if len(cmd_parts) > 1 else "ROOT")
        print(f"[REDIS ERROR] {cmd_name} '{target}': {e}")
        _trip_breaker()
        return None
    except Exception as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        print(f"[REDIS ERROR] Необработанная {cmd_name}: {e}")
        _trip_breaker()
        return None


def _reset_breaker_success() -> None:
    global _error_count
    _error_count = 0


# ---------------------------------------------------------------------------
# Pipeline (для football_data_to_redis.py v6.2)
# ---------------------------------------------------------------------------

def execute_pipeline(commands: List[List[Any]]) -> Optional[Any]:
    """Выполнить pipeline-команды через /pipeline endpoint."""
    if not commands:
        return None
    if not _check_circuit_breaker():
        return None

    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    redis_url = (redis_url or "").rstrip("/")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")

    if not redis_url or not redis_token:
        print("[CRITICAL ERROR] Конфигурация Upstash Redis не найдена!")
        _trip_breaker()
        return None

    url = f"{redis_url}/pipeline"
    payload = json.dumps(commands).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {redis_token}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        req.method = "POST"
        with urllib.request.urlopen(req, timeout=REDIS_TIMEOUT) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            _reset_breaker_success()
            return res_json
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        print(f"[REDIS PIPELINE ERROR] {e}")
        _trip_breaker()
        return None
    except Exception as e:
        print(f"[REDIS PIPELINE ERROR] Необработанная: {e}")
        _trip_breaker()
        return None


class PipelineBatch:
    """
    Батчинг Redis-команд через /pipeline.
    Используется football_data_to_redis.py v6.2 вместо собственного RedisClient.
    """

    def __init__(self, dry_run: bool = False, max_batch: int = 50, batch_delay: float = 0.15):
        self.dry_run = dry_run
        self.max_batch = max_batch
        self.batch_delay = batch_delay
        self._commands: List[List[str]] = []
        self._count = 0
        self._total_sent = 0
        self._total_batches = 0

    def add(self, command: str, *args) -> None:
        self._commands.append([command] + [str(a) for a in args])
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
            self._total_sent += len(commands)
            self._total_batches += 1
            return None

        result = execute_pipeline(commands)
        self._total_sent += len(commands)
        self._total_batches += 1
        if self.batch_delay > 0:
            time.sleep(self.batch_delay)
        return result

    @property
    def total_sent(self) -> int:
        return self._total_sent

    @property
    def total_batches(self) -> int:
        return self._total_batches


# ---------------------------------------------------------------------------
# Hash model (GatekeeperAI) — live-данные
# ---------------------------------------------------------------------------

def get_from_cache(field_id: str) -> Optional[Any]:
    """Точечное извлечение из хэш-кэша с обработкой конверта."""
    raw_result = _execute_upstash_cmd(["HGET", IMMUTABLE_ROOT_ADDRESS, field_id])
    if raw_result is None:
        return None
    try:
        envelope = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if isinstance(envelope, dict) and "payload" in envelope:
        return envelope["payload"]
    return envelope


def save_to_cache(field_id: str, data: Any) -> bool:
    """Атомарная запись в хэш-кэш с конвертом v710."""
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_name = env_repo.split("/")[-1] if "/" in env_repo else (env_repo or "local_dev")
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
                        result[key] = envelope["payload"]
                    else:
                        result[key] = envelope
                except (json.JSONDecodeError, TypeError):
                    result[key] = raw_value

        if iterations % 50 == 0:
            print(f"[REDIS] get_all_fields: {len(result)} полей (итерация {iterations})")

        if str(next_cursor) == "0":
            break
        cursor = next_cursor
        time.sleep(0.1)

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
                    result[key] = envelope["payload"]
                else:
                    result[key] = envelope
            except (json.JSONDecodeError, TypeError):
                result[key] = raw_value
    return result


# ---------------------------------------------------------------------------
# Key model — отдельные ключи (history, analysis)
# ---------------------------------------------------------------------------

def get_key(key: str) -> Optional[Any]:
    """Получить значение отдельного ключа (JSON)."""
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
    return bool(res)


def scan_keys(pattern: str, count: int = 500) -> List[str]:
    """SCAN по паттерну, возвращает список ключей."""
    result = []
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
        time.sleep(0.05)

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

def zadd_key(key: str, score: float, member: str) -> bool:
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


def dbsize() -> Optional[int]:
    res = _execute_upstash_cmd(["DBSIZE"])
    try:
        return int(res) if res else 0
    except (TypeError, ValueError):
        return None
