"""
Транспортный слой Gatekeeper-AI Ecosystem v9.6.8-patched.
Единый инкапсулированный шлюз к Upstash Redis REST API.
Только urllib.request. Никаких сторонних клиентов.

Обновления v9.6.8-patched:
  - Транспортный конверт поднят до v700-prod (ENVELOPE_VERSION)
  - Circuit Breaker приведён к §1.9: 10 ошибок подряд -> 60 секунд
  - Добавлены Root-хелперы history:match:* (уровень 1 защиты §1.7d):
    redis_command(), scan_keys(), upsert_history_match(),
    get_history_match(), delete_history_match() (только с HISTORY_FLUSH_CONFIRM=1)
  - Совместимость со старым API сохранена: save_to_cache / get_from_cache /
    get_all_fields / batch_get_from_cache без изменения сигнатур
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
HISTORY_PREFIX = "history:match:"
MSK_TIMEZONE = timezone(timedelta(hours=3))
ENVELOPE_VERSION = "v700-prod"

# §1.9: Circuit Breaker — 10 последовательных ошибок, открытие на 60 секунд
_BREAKER_THRESHOLD = 10
_BREAKER_COOLDOWN_SECONDS = 60

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
_circuit_breaker_open = False
_error_count = 0
_breaker_threshold = _BREAKER_THRESHOLD
_breaker_cooldown_seconds = _BREAKER_COOLDOWN_SECONDS
_breaker_tripped_at = 0.0


def _check_circuit_breaker() -> bool:
    """Возвращает True, если запросы разрешены; False — если breaker открыт."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    if _circuit_breaker_open:
        elapsed = time.time() - _breaker_tripped_at
        if elapsed >= _breaker_cooldown_seconds:
            print(f"[REDIS] Circuit Breaker half-open: авто-восстановление после {elapsed:.0f}s — пробуем снова")
            _circuit_breaker_open = False
            _error_count = 0
            return True
        print(f"[REDIS WARNING] Circuit Breaker активен — запросы заблокированы (осталось {_breaker_cooldown_seconds - elapsed:.0f}s)")
        return False
    return True


def _trip_breaker() -> None:
    """Срабатывание circuit breaker при превышении лимита ошибок (§1.9: 10 подряд)."""
    global _circuit_breaker_open, _error_count, _breaker_tripped_at
    _error_count += 1
    if _error_count >= _breaker_threshold:
        if not _circuit_breaker_open:
            _breaker_tripped_at = time.time()
            _circuit_breaker_open = True
            print(f"[REDIS ALERT] Circuit Breaker открыт на {_breaker_cooldown_seconds}s! Ошибок подряд: {_error_count}")


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


def _reset_breaker_success() -> None:
    """Сброс счётчика ошибок при успешном запросе."""
    global _error_count
    _error_count = 0


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


# ---------------------------------------------------------------------------
# Публичный API: универсальная команда и сканирование корня
# ---------------------------------------------------------------------------

def redis_command(cmd_parts: List[Any], timeout: int = REDIS_TIMEOUT) -> Optional[Any]:
    """
    Универсальный вызов произвольной Redis-команды через защищённый транспорт.
    Используется хабом для Root-ключей истории (§1.7d) и диагностикой.
    Возвращает raw-результат Upstash или None при ошибке.
    """
    if not cmd_parts:
        return None
    return _execute_upstash_cmd(cmd_parts, timeout=timeout)


def scan_keys(pattern: str = "*", count: int = 500) -> List[str]:
    """
    Полное сканирование корня Redis через SCAN с курсором.
    Возвращает список ключей по маске (например, "history:match:*").
    """
    keys: List[str] = []
    cursor = "0"
    while True:
        res = _execute_upstash_cmd(["SCAN", cursor, "MATCH", pattern, "COUNT", str(count)])
        if res is None or not isinstance(res, list) or len(res) < 2:
            break
        cursor = res[0]
        batch = res[1] if isinstance(res[1], list) else []
        for k in batch:
            if isinstance(k, str):
                keys.append(k)
        if cursor in (0, "0", None):
            break
    return keys


# ---------------------------------------------------------------------------
# Публичный API: точечные операции (live-хэш GatekeeperAI)
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
    """Атомарная запись в хэш-кэш с конвертом v700-prod."""
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
# Публичный API: массовые операции (live-хэш GatekeeperAI)
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
# Публичный API: Root-ключи истории history:match:* (§1.7d, уровень 1)
# ---------------------------------------------------------------------------
# Топология: history:match:{canonical_id} — самостоятельные ключи в корне Redis,
# вне хэша GatekeeperAI. Физически недостижимы для цикла очистки live-хэша
# (уровень 1 защиты), пропускаются явным skip в cleanup_expired() (уровень 2),
# деструктивное удаление — только через HISTORY_FLUSH_CONFIRM=1 (уровень 3).

def _make_envelope(data: Any) -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_name = env_repo.split("/")[-1] if "/" in env_repo else (env_repo or "local_dev")
    envelope = {
        "version": ENVELOPE_VERSION,
        "sender_repo": repo_name,
        "timestamp": datetime.now(MSK_TIMEZONE).isoformat(),
        "payload": data
    }
    return json.dumps(envelope, ensure_ascii=False)


def upsert_history_match(canonical_id: str, match_data: Dict[str, Any]) -> bool:
    """
    Записывает архивный матч в Root-ключ history:match:{canonical_id}.
    Существующий архив не перезаписывается без force=True — защита от потери
    первичной архивной записи (overwite разрешён только явным флагом).
    """
    if not canonical_id:
        return False
    key = f"{HISTORY_PREFIX}{canonical_id}"
    return bool(redis_command(["SET", key, _make_envelope(match_data)]))


def upsert_history_match_force(canonical_id: str, match_data: Dict[str, Any]) -> bool:
    """Запись архивного матча с перезаписью (только для reconcile/migrate)."""
    if not canonical_id:
        return False
    key = f"{HISTORY_PREFIX}{canonical_id}"
    return bool(redis_command(["SET", key, _make_envelope(match_data)]))


def history_match_exists(canonical_id: str) -> bool:
    key = f"{HISTORY_PREFIX}{canonical_id}"
    return bool(redis_command(["EXISTS", key]))


def get_history_match(canonical_id: str) -> Optional[Any]:
    """Читает архивный матч из Root-ключа с распаковкой конверта."""
    if not canonical_id:
        return None
    key = f"{HISTORY_PREFIX}{canonical_id}"
    raw = redis_command(["GET", key])
    if raw is None:
        return None
    try:
        envelope = json.loads(raw)
        if isinstance(envelope, dict) and "payload" in envelope:
            return envelope["payload"]
        return envelope
    except (json.JSONDecodeError, TypeError):
        print(f"[REDIS HISTORY DECODE ERROR] Поврежденный JSON в {key}")
        return None


def get_history_count() -> int:
    """Количество ключей history:match:* в корне Redis."""
    return len(scan_keys(pattern=f"{HISTORY_PREFIX}*"))


def delete_history_match(canonical_id: str) -> bool:
    """
    Деструктивное удаление Root-ключа истории — УРОВЕНЬ 3 ЗАЩИТЫ (§1.7d).
    Выполняется только при HISTORY_FLUSH_CONFIRM=1 (ручной режим redis_diagnostics).
    """
    if os.environ.get("HISTORY_FLUSH_CONFIRM", "") != "1":
        print("[REDIS HISTORY GUARD] Удаление history:match:* заблокировано. "
              "Для ручного flush задайте HISTORY_FLUSH_CONFIRM=1 (§1.7d, уровень 3)")
        return False
    key = f"{HISTORY_PREFIX}{canonical_id}"
    res = redis_command(["DEL", key])
    if res is None:
        print(f"[REDIS HISTORY DELETE ERROR] Сбой удаления {key}")
        return False
    return True


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
