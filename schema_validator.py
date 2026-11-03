#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: schema_validator.py
Version: 9.6.8-patched (Синхронизировано со схемой v710 и гибридной валидацией gatekeeper_hub)
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §19.1 (jsonschema validation framework)

Мета-схема переведена на ПЛОСКИЙ контракт v710:
  - odds_1x2 на верхнем уровне с ключами "1"/"X"/"2" (как пишет хаб v9.6.8);
  - required ровно как в файле schema_v710.json (без несуществующего case_id);
  - пятизначный enum статусов + нормализация легаси-статусов (синхронно с хабом);
  - непустота clean-полей проверяется вручную (обход number-слепоты minLength в JSON Schema).
Легаси-обход паспорта V6.8 сохранён как контракт CI-тестов, но требует непустых структур.
"""

import logging
from jsonschema import validate, ValidationError
from typing import Dict, Any, Tuple

logging.basicConfig(level=logging.INFO, format="[SCHEMA VALIDATOR] %(message)s")
logger = logging.getLogger("schema_validator")

__version__ = "9.6.8-patched"

# Enum статусов Схемы v710
STATUS_ENUM_V710 = ("scheduled", "live", "completed", "postponed", "cancelled")

# Нормализация легаси-статусов — зеркально гибридной валидации gatekeeper_hub
LEGACY_STATUS_MAP = {
    "awarded": "completed",
    "forfeited": "completed",
    "ended": "completed",
    "finished": "completed",
    "ft": "completed",
    "aet": "completed",
    "notstarted": "scheduled",
    "not_started": "scheduled",
    "ns": "scheduled",
}

# Обязательные поля ровно по файлу schema_v710.json
REQUIRED_V710 = [
    "canonical_id",
    "home_team",
    "away_team",
    "home_clean",
    "away_clean",
    "date_utc",
    "status",
    "competition",
    "schema_version",
]

# Поля, непустота которых не может быть выражена через minLength на number-типе в JSON Schema
NON_EMPTY_CHECK = ("home_clean", "away_clean", "competition")


def _normalize_status(status: Any) -> str:
    """Нормализация статуса под enum v710 (легаси -> канон)."""
    if not isinstance(status, str):
        return status if status in STATUS_ENUM_V710 else "scheduled"
    s = status.strip().lower()
    return LEGACY_STATUS_MAP.get(s, s)


def _check_odds_1x2_flat(passport: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверка плоских котировок: ключи "1"/"X"/"2" — числа > 1.0. Зеркально правилу апсета хаба."""
    odds = passport.get("odds_1x2")
    if odds is None:
        return True, ""  # отсутствие линии допустимо (матч без котировок)
    if not isinstance(odds, dict):
        return False, "odds_1x2 должен быть объектом с ключами 1/X/2"
    for key in ("1", "X", "2"):
        val = odds.get(key)
        if val is None:
            return False, f"odds_1x2: отсутствует обязательный ключ '{key}'"
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return False, f"odds_1x2[{key}]: ожидается число, получено {type(val).__name__}"
        if val <= 1.0:
            return False, f"odds_1x2[{key}]={val}: котировка должна быть > 1.0"
    return True, ""


def _check_clean_fields(passport: Dict[str, Any]) -> Tuple[bool, str]:
    """Ручная проверка непустоты clean-полей (JSON Schema игнорирует minLength на числах)."""
    for field in NON_EMPTY_CHECK:
        val = passport.get(field)
        if val is None:
            return False, f"Поле '{field}' отсутствует"
        if not isinstance(val, str) or not val.strip():
            return False, f"Поле '{field}' пустое или не строка"
    return True, ""


# Строгая мета-схема по спецификации паспорта v710 (Правило §1.14)
GATEKEEPER_SCHEMA_V710 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "canonical_id": {"type": "string", "minLength": 1},
        "schema_version": {"type": "string", "enum": ["v710"]},
        "home_team": {"type": "string", "minLength": 1},
        "away_team": {"type": "string", "minLength": 1},
        "home_clean": {"type": "string", "minLength": 1},
        "away_clean": {"type": "string", "minLength": 1},
        "date_utc": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": list(STATUS_ENUM_V710)},
        "competition": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        # Плоский базис котировок: ключи "1"/"X"/"2" (числа > 1.0 проверяются вручную)
        "odds_1x2": {
            "type": "object",
            "properties": {
                "1": {"type": "number"},
                "X": {"type": "number"},
                "2": {"type": "number"},
                "last_updated": {"type": "string"},
                "provider_priority": {"type": ["string", "integer"]}
            },
            "required": ["1", "X", "2"],
            "additionalProperties": True
        },
        "stats": {
            "type": "object",
            "properties": {
                "xg_home": {"type": "number"},
                "xg_away": {"type": "number"}
            },
            "additionalProperties": True
        },
        "h2h": {"type": "object", "additionalProperties": True},
        "source_map": {"type": "object", "additionalProperties": True},
        "core_analysis": {"type": "object", "additionalProperties": True}
    },
    "required": REQUIRED_V710,
    # additionalProperties: true — синхронно с гибридной валидацией хаба
    # (расширяющиеся секции odds/spreads/totals и future-поля не должны ронять валидацию)
    "additionalProperties": True,
}


def validate_match_passport(passport: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Выполняет строгую проверку входящего пакета данных на соответствие схеме v710.
    Блокирует прохождение поврежденных или урезанных структур в математическое ядро.
    Возвращает (ok, message). Сигнатура и формат совместимы с предыдущей версией.
    """
    # Легаси-обход внутреннего формата V6.8 (контракт CI-тестов),
    # но теперь с проверкой непустоты структур, а не голого наличия двух ключей.
    if "match_metadata" in passport and "model_inputs" in passport:
        mm = passport.get("match_metadata")
        mi = passport.get("model_inputs")
        if isinstance(mm, dict) and mm and isinstance(mi, dict) and mi:
            return True, "PASSED_LEGACY: Обнаружен внутренний формат паспорта V6.8"
        return False, "Критическое нарушение §1.14: легаси-паспорт V6.8 с пустыми match_metadata/model_inputs"

    # Нормализация легаси-статуса ДО валидации: иначе enum-проверка jsonschema
    # отклонит 'awarded'/'finished' раньше, чем сработает нормализация в 'completed'.
    # Работаем на копии — входящий паспорт не мутируем.
    work = dict(passport)
    work["status"] = _normalize_status(work.get("status"))

    try:
        validate(instance=work, schema=GATEKEEPER_SCHEMA_V710)
    except ValidationError as e:
        error_path = " -> ".join([str(p) for p in e.path]) if e.path else "root"
        error_msg = f"Критическое нарушение правила §1.14 в узле [{error_path}]: {e.message}"
        logger.error(f"[REJECTED] {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"Критическое нарушение §1.14: ошибка валидации: {e}"
        logger.error(f"[REJECTED] {error_msg}")
        return False, error_msg

    # Ручная проверка непустоты clean-полей (обход number-слепоты minLength)
    ok, msg = _check_clean_fields(passport)
    if not ok:
        logger.error(f"[REJECTED] {msg}")
        return False, f"Критическое нарушение §1.14: {msg}"

    # Проверка плоских котировок (если линия присутствует)
    ok, msg = _check_odds_1x2_flat(work)
    if not ok:
        logger.error(f"[REJECTED] {msg}")
        return False, f"Критическое нарушение §1.14: {msg}"

    return True, "SUCCESS: Структура полностью валидна по регламенту §19.1"


if __name__ == "__main__":
    # Локальный верификационный тест (4 кейса, имитация отчётов CI)
    print("=== ЗАПУСК ТЕСТИРОВАНИЯ УСТОЙЧИВОСТИ СХЕМЫ v710 ===")

    # 1. Валидный плоский матч v710
    valid_mock = {
        "canonical_id": "arsenal__chelsea__20261105",
        "schema_version": "v710",
        "home_team": "Arsenal FC",
        "away_team": "Chelsea FC",
        "home_clean": "arsenal",
        "away_clean": "chelsea",
        "date_utc": "2026-11-05T20:00:00Z",
        "status": "scheduled",
        "competition": "premier_league",
        "version": 1,
        "odds_1x2": {"1": 2.10, "X": 3.40, "2": 3.30}
    }
    is_ok, msg = validate_match_passport(valid_mock)
    print(f"1. Валидный плоский матч: {'PASS' if is_ok else 'FAIL'} -> {msg}")

    # 2. Поврежденный кейс (имитация атаки Агента Деградации Среды из логов)
    broken_mock = {
        "canonical_id": "CASE_CHAOS_ATTACK",
        "schema_version": "v710",
        # Поля home_team и away_team намеренно удалены хаос-скриптом
        "home_clean": "arsenal",
        "away_clean": "chelsea",
        "date_utc": "2026-11-05T20:00:00Z",
        "status": "scheduled",
        "competition": "premier_league",
    }
    is_ok, msg = validate_match_passport(broken_mock)
    print(f"2. Стресс-тест хаоса: {'FAIL (Успешный перехват)' if not is_ok else 'PASS'} -> {msg}")

    # 3. Легаси-паспорт V6.8 (контракт CI-тестов)
    legacy_mock = {
        "match_metadata": {"fixture_id": 12345, "league": "EPL"},
        "model_inputs": {"xg_home": 1.6, "xg_away": 1.1}
    }
    is_ok, msg = validate_match_passport(legacy_mock)
    print(f"3. Легаси-паспорт V6.8: {'PASS' if is_ok and 'LEGACY' in msg else 'FAIL'} -> {msg}")

    # 3b. Легаси-обход с пустыми структурами — теперь должен быть перехвачен
    empty_legacy = {"match_metadata": {}, "model_inputs": {}}
    is_ok, msg = validate_match_passport(empty_legacy)
    print(f"4. Пустой легаси-обход: {'FAIL (Успешный перехват)' if not is_ok else 'PASS'} -> {msg}")

    # 5. Нормализация легаси-статуса awarded -> completed
    status_mock = dict(valid_mock)
    status_mock["status"] = "awarded"
    is_ok, msg = validate_match_passport(status_mock)
    print(f"5. Легаси-статус 'awarded': {'PASS (нормализован)' if is_ok else 'FAIL'} -> {msg}")
