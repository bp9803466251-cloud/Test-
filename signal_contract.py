#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem: signal_contract.py
Version: 10.0.0-merged (создан циклом объединения диагностики v10)

Назначение
==========
Единственный источник истины для распознавания сигнального класса матча
(resolve_classification / is_value_signal). До объединения эти две функции
существовали четырьмя несинхронизированными копиями:

  - telegram_transport.py: дефолт "NO BET", без маппинга легаси-verdict
  - metrics.py:            дефолт "", без маппинга легаси-verdict
  - main.py:               дефолт "NO BET", + маппинг легаси-verdict (VALUE_BET->HOT, WARM->WARM)
  - source_diagnostics.py: дефолт "", без маппинга легаси-verdict

Аннотация metrics.py ("Синхронизировано с telegram_transport") не соответствовала
действительности: дефолты различались, маппинг существовал только в main.

Каноническая семантика объединяет все три использования:
  1) приоритет core_analysis.classification (контракт v710), fallback verdict (легаси);
  2) легаси-verdict маппится в классы v710 (VALUE_BET -> HOT, WARM -> WARM);
  3) дефолт "NO BET" (безопасная сторона: отсутствие разбора считается отсутствием сигнала).

Architecture
============
Модуль НЕ имеет зависимостей вообще: ни redis_hub, ни gatekeeper_hub, ни
сторонних пакетов. Это осознанное решение: telegram_transport сознательно
автономен (чистый urllib, §1.3) — вешать контракт на модуль, тянущий хабы,
значит сломать его изоляцию. Поэтому контрактом владеет отдельный файл.

Потребители: telegram_transport, metrics, main, source_diagnostics (ядро).
[9.6.8] Проверка по границам определений (HOT*, VALUE_BET) вместо подстрочного
матча: 'BET' in 'NO BET' ошибочно считало отбракованный матч value-сигналом.
"""

import logging

logger = logging.getLogger("signal_contract")
logger.addHandler(logging.NullHandler())

__version__ = "10.0.0-merged"
__all__ = ["resolve_classification", "is_value_signal", "is_warm_signal", "__version__"]

# Дефолт: без разбора — нет сигнала (безопасная сторона для всех читателей)
DEFAULT_CLASSIFICATION = "NO BET"


def resolve_classification(match_data) -> str:
    """Единая точка чтения сигнального класса матча.

    Приоритет: core_analysis.classification (контракт v710) -> core_analysis.verdict /
    легаси-верdict верхнего уровня (с маппингом VALUE_BET -> HOT, WARM -> WARM).

    Безопасный возврат "NO BET" на входе любого типа (None, str, list) —
    согласовано с [9.6.8 FIX] metrics: сбой одного матча не глушит цикл.
    """
    if not isinstance(match_data, dict):
        return DEFAULT_CLASSIFICATION
    res = match_data.get("core_analysis") or match_data
    if not isinstance(res, dict):
        return DEFAULT_CLASSIFICATION

    classification = res.get("classification")
    if classification:
        return str(classification).upper()

    verdict = res.get("verdict", "")
    verdict_str = str(verdict)
    if "VALUE_BET" in verdict_str:
        return "HOT"
    if "WARM" in verdict_str.upper():
        return "WARM"
    if verdict_str:
        # Непустой нераспознанный verdict — не легаси-класс: отдаём как есть, upper
        return verdict_str.upper()
    return DEFAULT_CLASSIFICATION


def is_value_signal(classification: str) -> bool:
    """Верифицированный value-сигнал по границам определений: HOT* или *VALUE_BET*.

    WARM (слабый сигнал без верификации) НЕ считается — в соответствии с
    различением заголовков в транспорте 9.6.8.
    """
    cls = (classification or "").upper()
    return cls.startswith("HOT") or "VALUE_BET" in cls


def is_warm_signal(classification: str) -> bool:
    """Слабый сигнал WARM — заголовок транспорта, не входит в value-счётчик."""
    return (classification or "").upper().startswith("WARM")
