#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: schema_validator.py
Version: 9.6.5-audited (Синхронизировано с регламентами v9.4-bulk и v9.5-source)
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §19.1 (jsonschema validation framework)
"""

import logging
from jsonschema import validate, ValidationError
from typing import Dict, Any, Tuple

logging.basicConfig(level=logging.INFO, format="[SCHEMA VALIDATOR] %(message)s")
logger = logging.getLogger("schema_validator")

# Жестко зафиксированная мета-схема по спецификации паспорта v710 (Правило §1.14)
GATEKEEPER_SCHEMA_V710 = {
    "type": "object",
    "properties": {
        "case_id": {"type": "string"},
        "schema_version": {"type": "string", "enum": ["v710"]},
        "home_team": {"type": "string", "minLength": 2},
        "away_team": {"type": "string", "minLength": 2},
        "odds": {
            "type": "object",
            "properties": {
                "1x2": {
                    "type": "object",
                    "properties": {
                        "current": {
                            "type": "object",
                            "properties": {
                                "home_clean": {"type": "number"},
                                "draw": {"type": "number"},
                                "away_clean": {"type": "number"}
                            },
                            "required": ["home_clean", "draw", "away_clean"]
                        }
                    },
                    "required": ["current"]
                }
            },
            "required": ["1x2"]
        },
        "predictions": {"type": "object"},
        "stats": {
            "type": "object",
            "properties": {
                "xg_home": {"type": "number"},
                "xg_away": {"type": "number"}
            }
        }
    },
    # Защита от деградации: поля, без которых value_engine гарантированно упадет
    "required": ["case_id", "schema_version", "home_team", "away_team", "odds"]
}
# ==============================================================================
# ЧАСТЬ 2: ОПЕРАЦИОННЫЙ ИНТЕРФЕЙС ВАЛИДАЦИИ И ТЕСТОВЫЙ ПЕРЕХВАТЧИК
# ==============================================================================

def validate_match_passport(passport: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Выполняет строгую проверку входящего пакета данных на соответствие схеме v710.
    Блокирует прохождение поврежденных или урезанных структур в математическое ядро.
    """
    # Внутренний лог-мост старого формата V6.8 пропускается без валидации JSON-схемы верхнего уровня
    if "match_metadata" in passport and "model_inputs" in passport:
        return True, "PASSED_LEGACY: Обнаружен внутренний формат паспорта V6.8"

    try:
        validate(instance=passport, schema=GATEKEEPER_SCHEMA_V710)
        return True, "SUCCESS: Структура полностью валидна по регламенту §19.1"
    except ValidationError as e:
        error_path = " -> ".join([str(p) for p in e.path]) if e.path else "root"
        error_msg = f"Критическое нарушение правила §1.14 в узле [{error_path}]: {e.message}"
        logger.error(f"[REJECTED] {error_msg}")
        return False, error_msg


if __name__ == "__main__":
    # Локальный верификационный тест (Имитация отчетов CI)
    print("=== ЗАПУСК ТЕСТИРОВАНИЯ УСТОЙЧИВОСТИ СХЕМЫ v710 ===")
    
    # 1. Валидный кейс
    valid_mock = {
        "case_id": "CASE_01_VALID",
        "schema_version": "v710",
        "home_team": "Leicester City", "away_team": "Chelsea FC",
        "odds": {"1x2": {"current": {"home_clean": 4.20, "draw": 3.80, "away_clean": 1.75}}}
    }
    is_ok, msg = validate_match_passport(valid_mock)
    print(f"Результат тестирования валидной схемы: {'PASS' if is_ok else 'FAIL'} -> {msg}")

    # 2. Поврежденный кейс (Имитация атаки Агента Деградации Среды из логов)
    broken_mock = {
        "case_id": "CASE_CHAOS_ATTACK",
        "schema_version": "v710",
        # Поля home_team и away_team намеренно удалены хаос-скриптом
        "odds": {"1x2": {"current": {"home_clean": 4.20, "draw": 3.80, "away_clean": 1.75}}}
    }
    is_ok, msg = validate_match_passport(broken_mock)
    print(f"Результат стресс-теста под воздействием хаоса: {'FAIL (Успешный перехват)' if not is_ok else 'PASS'} -> {msg}")
                                 
