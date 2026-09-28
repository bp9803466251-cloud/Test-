"""
Транспортный слой Gatekeeper-AI Ecosystem v600-prod.
Единый инкапсулированный шлюз к Upstash Redis REST API.
Только urllib.request. Никаких сторонних клиентов.
"""
import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, List, Dict

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
REDIS_TIMEOUT = int(os.getenv("GATEKEEPER_REDIS_TIMEOUT", "6"))
EXTERNAL_API_TIMEOUT = int(os.getenv("GATEKEEPER_EXTERNAL_API_TIMEOUT", "15"))
HMGET_CHUNK_SIZE = int(os.getenv("GATEKEEPER_HMGET_CHUNK_SIZE", "50"))

IMMUTABLE_ROOT_ADDRESS = "GatekeeperAI"
MSK_TIMEZONE = timezone(timedelta(hours=3))
ENVELOPE_VERSION = "v600-prod"

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
_circuit_breaker_open = False
_error_count = 0
_breaker_threshold = 5
_breaker_cooldown_seconds = 30
_breaker_tripped_at = 0.0


def _check_circuit_breaker() -> bool:
    """Возвращает True, если запросы разрешены; False — если breaker открыт."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    if _circuit_breaker_open:
        elapsed = time.time() - _breaker_tripped_at
        if elapsed >= _breaker_cooldown_seconds:
            print(f"[REDIS] Circuit Breaker авто-восстановление после {elapsed:.0f}s — пробуем снова")
            _circuit_breaker_open = False
            _error_count = 0
            return True
        print(f"[REDIS WARNING] Circuit Breaker активен — запросы заблокированы (осталось {_breaker_cooldown_seconds - elapsed:.0f}s)")
        return False
    return True


def _trip_breaker() -> None:
    """Срабатывание circuit breaker при превышении лимита ошибок."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    _error_count += 1
    if _error_count >= _breaker_threshold:
        if not _circuit_breaker_open:
            _breaker_tripped_at = time.time()
        _circuit_breaker_open = True
        print(f"[REDIS ALERT] Circuit Breaker сработал! Ошибок подряд: {_error_count}")


def reset_circuit_breaker() -> None:
    """
    Сброс circuit breaker — публичная функция.
    Вызывается из gatekeeper_hub.run_initialization() при успешном подключении.
    """
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    was_open = _circuit_breaker_open
    _circuit_breaker_open = False
    _error_count = 0
    _breaker_tripped_at = 0.0
    if was_open:
        print("[REDIS] Circuit Breaker сброшен вручную — запросы разрешены")


# ---------------------------------------------------------------------------
# Базовый транспорт
# ---------------------------------------------------------------------------

def _execute_upstash_cmd(cmd_parts: List[Any], timeout: int = REDIS_TIMEOUT) -> Optional[Any]:
    """
    Единый инкапсулированный транспорт для всех команд Upstash Redis REST API.
    timeout: по умолчанию REDIS_TIMEOUT (6 сек). Для внешних API
    использовать EXTERNAL_API_TIMEOUT (15 сек) — но не в этом модуле.
    """
    if not _check_circuit_breaker():
        return None

    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    redis_url = (redis_url or "").rstrip("/")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")

    if not redis_url or not redis_token:
        print("[CRITICAL ERROR] Конфигурация Upstash Redis не найдена в окружении!")
        _trip_breaker()
        return None

    url = f"{redis_url}/"
    payload = json.dumps(cmd_parts).encode('utf-8')
    headers = {
        "Authorization": f"Bearer {redis_token}",
        "Content-Type": "application/json"
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
        target_field = cmd_parts[2] if len(cmd_parts) > 2 else "ROOT"
        print(f"[REDIS SYSTEM ERROR] Ошибка команды {cmd_name} для поля '{target_field}': {e}")
        _trip_breaker()
        return None
    except Exception as e:
        cmd_name = cmd_parts[0].upper() if cmd_parts else "UNKNOWN"
        target_field = cmd_parts[2] if len(cmd_parts) > 2 else "ROOT"
        print(f"[REDIS SYSTEM ERROR] Необработанная ошибка {cmd_name} для поля '{target_field}': {e}")
        _trip_breaker()
        return None


def _reset_breaker_success() -> None:
    """Сброс счётчика ошибок при успешном запросе."""
    global _error_count
    _error_count = 0


# ---------------------------------------------------------------------------
# Публичный API: точечные операции
# ---------------------------------------------------------------------------

def get_from_cache(field_id: str) -> Optional[Any]:
    """Точечное извлечение из хэш-кэша с обработкой конверта."""
    raw_result = _execute_upstash_cmd(["HGET", IMMUTABLE_ROOT_ADDRESS, field_id])
    if raw_result is None:
        return None

    try:
        envelope = json.loads(raw_result)
    except json.JSONDecodeError:
        print(f"[ECO CACHE DECODE ERROR] Поле {field_id} содержит поврежденный JSON!")
        return None

    if isinstance(envelope, dict) and "payload" in envelope:
        return envelope["payload"]
    return envelope


def save_to_cache(field_id: str, data: Any) -> bool:
    """Атомарная запись в хэш-кэш с конвертом v600-prod."""
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_name = env_repo.split("/")[-1] if "/" in env_repo else (env_repo or "local_dev")
    current_time = datetime.now(MSK_TIMEZONE).isoformat()

    envelope = {
        "version": ENVELOPE_VERSION,
        "sender_repo": repo_name,
        "timestamp": current_time,
        "payload": data
    }

    res = _execute_upstash_cmd(["HSET", IMMUTABLE_ROOT_ADDRESS, field_id, json.dumps(envelope)])
    if res is None:
        print(f"[ECO CACHE WRITE ERROR] Сбой сети при записи поля {field_id}")
        return False
    return True


def delete_from_cache(field_id: str) -> bool:
    """Удаление поля из хэш-кэша."""
    res = _execute_upstash_cmd(["HDEL", IMMUTABLE_ROOT_ADDRESS, field_id])
    if res is None:
        print(f"[ECO CACHE DELETE ERROR] Сбой при удалении поля {field_id}")
        return False
    return True


def field_exists(field_id: str) -> bool:
    """Проверка существования поля в хэш-кэше."""
    res = _execute_upstash_cmd(["HEXISTS", IMMUTABLE_ROOT_ADDRESS, field_id])
    return bool(res)


# ---------------------------------------------------------------------------
# Публичный API: массовые операции
# ---------------------------------------------------------------------------

def get_all_fields() -> Dict[str, Any]:
    """
    Возвращает все поля хэш-кэша как dict {field_id: value}.
    Используется для миграций и инициализации.
    """
    res = _execute_upstash_cmd(["HGETALL", IMMUTABLE_ROOT_ADDRESS])
    if res is None or not isinstance(res, list):
        return {}

    result = {}
    for i in range(0, len(res), 2):
        if i + 1 < len(res):
            key = res[i]
            raw_value = res[i + 1]
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


def batch_get_from_cache(keys: List[str], chunk_size: int = HMGET_CHUNK_SIZE) -> Dict[str, Any]:
    """
    Пакетное чтение нескольких полей через HMGET с чанкованием.
    Разбивает keys на батчи по chunk_size элементов.
    Возвращает dict {field_id: value}.
    """
    if not keys:
        return {}

    result = {}
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        cmd = ["HMGET", IMMUTABLE_ROOT_ADDRESS] + chunk
        res = _execute_upstash_cmd(cmd)

        if res is None:
            print(f"[REDIS WARNING] Батч HMGET ({i}–{i + len(chunk)}) провален, остальные батчи пропускаются")
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
# Системные функции
# ---------------------------------------------------------------------------

def get_circuit_breaker_status() -> dict:
    """Возвращает статус circuit breaker для health-check."""
    return {
        "open": _circuit_breaker_open,
        "error_count": _error_count,
        "threshold": _breaker_threshold,
    }


def is_redis_available() -> bool:
    """Быстрая проверка доступности Redis (PING)."""
    # FIX: идём через _check_circuit_breaker() для авто-восстановления после cooldown
    if not _check_circuit_breaker():
        return False
    res = _execute_upstash_cmd(["PING"])
    return res is not None
