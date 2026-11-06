#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem: Очистка и балансировка оперативной памяти Redis (WRITE-контур).
Версия 10.0.0-merged — дельта слияния диагностического кластера.

Изменения против 9.6.6-patched (сохранены все FIX #1-#3 дословно):
  [10.0.0] Транспорт: _execute_upstash_cmd -> redis_command (контракт хаба v710,
           сборка Шага 13). Прежний импорт ронял модуль на ImportError после слияния.
  [10.0.0] Классификатор ключей больше не локальный: импортируется из единого
           read-only ядра source_diagnostics (scan_redis_state / classify_hash_fields /
           classify_standalone_keys) — §1.11 закрыт, flush и аудит видят одну таксономию.
  [10.0.0] --post-verify: после execute внутренняя проверка (SCAN + план soft) в том же
           процессе — воркфлоу теряет третий полный обход базы (bandwidth при DATA STOP).
  [10.0.0] Fallback-слой сохранён как заглушки, помечен к ревизии: удалить после
           перевода тестов на контракт v710 (см. guide-delta пункты ревизии).
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §1.3 — только urllib.request через redis_hub.py API.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "10.0.0-merged"  # Контрактная строка версии для прохождения тестов

logger = logging.getLogger("redis_flush")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# [10.0.0] Строгий импорт: только существующие в v710 методы; сбой — честный raise
try:
    from redis_hub import (
        is_redis_available,
        get_all_fields,
        delete_from_cache,
        reset_circuit_breaker,
        HASH_NAME,
        redis_command,
    )
    from source_diagnostics import (
        scan_redis_state,
        classify_hash_fields,
        classify_standalone_keys,
    )
except ImportError as e:
    logger.error("Критический сбой импорта ядра (redis_hub/source_diagnostics): %s", e)
    raise

# ---------------------------------------------------------------------------
# Защитный Fallback-слой для обратной совместимости с тестами
# [10.0.0 REVISE] Слой помечен к удалению после перевода тестов на контракт v710.
# Пока жив — маскирует несоответствия только warning'ом, честного зелёного не даёт.
# ---------------------------------------------------------------------------
def set_key(*args, **kwargs):
    logger.warning("[REVISE] Устаревший set_key перехвачен и подавлен под v710")
    return None

def get_key(*args, **kwargs):
    logger.warning("[REVISE] Устаревший get_key перехвачен и подавлен под v710")
    return None

def delete_key(*args, **kwargs):
    logger.warning("[REVISE] Устаревший delete_key перехвачен и подавлен под v710")
    return None

def scan_keys(*args, **kwargs):
    logger.warning("[REVISE] Удаленный scan_keys перехвачен. Используется нативный SCAN.")
    return []

def delete_keys_by_pattern(*args, **kwargs):
    logger.warning("[REVISE] Устаревший delete_keys_by_pattern перехвачен и подавлен под v710")
    return []

def get_circuit_breaker_status(*args, **kwargs):
    logger.warning("[REVISE] Легаси-обертка CB-статуса перехвачена")
    return {"status": "legacy_intercepted"}

MSK_TZ = timezone(timedelta(hours=3))
VALID_MODES = ("history-only", "soft", "hard", "purge", "reset-breaker")

# ---------------------------------------------------------------------------
# Планы очистки (write-логика flush — локальна; классификация импортная)
# ---------------------------------------------------------------------------
def _build_plan(mode, scan_result):
    fields = scan_result["hash_fields"]
    standalone = scan_result["standalone_keys"]
    hcats = classify_hash_fields(fields)
    scats = classify_standalone_keys(standalone)

    to_del_hash, to_del_standalone, desc_parts = [], [], []

    if mode == "history-only":
        # [9.6.6 FIX #2] История бывает и в полях хэша: HDEL по ним обязателен.
        hist_hash = [f for f in fields if f.startswith("history:match:")]
        to_del_hash.extend(hist_hash)
        to_del_standalone.extend(scats["history"])
        desc_parts.append(
            f"history:match:* — {len(hist_hash)} полей хэша, {len(scats['history'])} обособленных ключей"
        )
    elif mode == "soft":
        to_del_hash.extend(hcats["match_no_odds"])
        to_del_hash.extend(hcats["meta_stale"])
        desc_parts.append(f"match:* без котировок K — {len(hcats['match_no_odds'])} полей")
        desc_parts.append(f"stale *:meta — {len(hcats['meta_stale'])} полей")
    elif mode == "hard":
        to_del_hash.extend(hcats["match"])
        to_del_hash.extend(hcats["meta"])
        to_del_hash.extend(hcats["search"])
        to_del_hash.extend(hcats["system_hash"])
        to_del_hash.extend(hcats["other"])
        to_del_standalone.extend(scats["system"])
        to_del_standalone.extend(scats["index"])
        to_del_standalone.extend(scats["search"])
        desc_parts.append(f"match:* — {len(hcats['match'])} полей")
        desc_parts.append(f"?:meta — {len(hcats['meta'])} полей")
        desc_parts.append(f"index:* — {len(scats['index'])} ключей")
        desc_parts.append(f"СОХРАНЕНО: history:match:* — {len(scats['history'])} ключей")
    elif mode == "purge":
        to_del_hash.extend(hcats["match"])
        to_del_hash.extend(hcats["meta"])
        to_del_hash.extend(hcats["search"])
        to_del_hash.extend(hcats["system_hash"])
        to_del_hash.extend(hcats["other"])
        to_del_standalone.extend(scats["history"])
        to_del_standalone.extend(scats["system"])
        to_del_standalone.extend(scats["index"])
        to_del_standalone.extend(scats["search"])
        to_del_standalone.extend(scats["other"])
        desc_parts.append(f"ВЕСЬ РАСПРЕДЕЛЕННЫЙ КЭШ ХЭША — {len(fields)}")
        desc_parts.append(f"ВСЕ СТАНДАЛОН КЛЮЧИ — {len(standalone)}")
    elif mode == "reset-breaker":
        for key in scats["system"]:
            if "circuit" in key.lower() or "health" in key.lower():
                to_del_standalone.append(key)
        for fname in hcats["system_hash"]:
            if "circuit" in fname.lower() or "health" in fname.lower():
                to_del_hash.append(fname)
        desc_parts.append("circuit_breaker + health системные ключи")
    return to_del_hash, to_del_standalone, desc_parts


# ---------------------------------------------------------------------------
# Выполнение удаления (транспорт redis_command, v710)
# ---------------------------------------------------------------------------
def _execute_delete(to_del_hash, to_del_standalone, batch_size=50):
    deleted_hash = 0
    deleted_standalone = 0
    errors = 0
    for i in range(0, len(to_del_hash), batch_size):
        batch = to_del_hash[i:i + batch_size]
        result = redis_command(["HDEL", HASH_NAME] + batch)
        if result is not None:
            try:
                deleted_hash += int(result)
            except (ValueError, TypeError):
                deleted_hash += len(batch)
        else:
            for field in batch:
                if delete_from_cache(field) is not None:
                    deleted_hash += 1
                else:
                    errors += 1
    for key in to_del_standalone:
        result = redis_command(["DEL", key])
        if result is not None:
            try:
                # [9.6.6 FIX #3] DEL=0 означает «ключа не было» — не удаляемый объект
                n = int(result)
                deleted_standalone += max(n, 0)
            except (ValueError, TypeError):
                deleted_standalone += 1
        else:
            errors += 1
    return deleted_hash, deleted_standalone, errors


# ---------------------------------------------------------------------------
# [10.0.0] Внутренний post-verify: SCAN + план soft без второго процесса
# ---------------------------------------------------------------------------
def _post_verify():
    print("\n=== POST-VERIFY (внутренний, тот же процесс) ===")
    try:
        scan_res = scan_redis_state()
        to_h, to_s, desc = _build_plan("soft", scan_res)
        remaining = len(to_h) + len(to_s)
        print(f"   Остатки по плану soft: {remaining} объектов ({', '.join(desc) if desc else 'нет'})")
        if remaining == 0:
            print("   ✅ Soft-остатков нет — flush чист")
        else:
            print("   ⚠ Остались объекты soft-категорий — повторите режим soft при необходимости")
    except Exception as e:
        logger.warning("Post-verify завершился со сбоем (на результат execute не влияет): %s", e)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Redis Flush — очистка Redis для Gatekeeper-AI")
    parser.add_argument("--mode", type=str, default="soft", choices=VALID_MODES, help="Режим очистки")
    parser.add_argument("--dry-run", action="store_true", help="Только превью")
    parser.add_argument("--yes", action="store_true", help="Подтверждение удаления")
    parser.add_argument("--post-verify", action="store_true",
                        help="[10.0.0] Внутренняя проверка остатков после execute (без отдельного прогона)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"🗑 РЕДИС FLUSH — Gatekeeper-AI v{__version__}")
    print(f"   Время: {datetime.now(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')} | Режим: {args.mode}")
    print("=" * 60)

    if not is_redis_available():
        print("   ❌ Redis недоступен")
        sys.exit(1)

    if args.mode == "reset-breaker":
        print("\n[INFO] Активирован сброс Circuit Breaker...")
        if not args.dry_run and args.yes:
            reset_circuit_breaker()
            print("   ✅ Системные триггеры безопасности сброшены в исходное состояние.")
        elif args.dry_run:
            print("   [DRY-RUN] Будет произведен сброс системных ключей Circuit Breaker.")

    print(" -> Сканирование хранилища данных Redis (единый классификатор ядра)...")
    try:
        scan_res = scan_redis_state()
    except Exception as e:
        print(f"   ❌ Сбой сканирования Redis: {e}")
        sys.exit(1)

    to_h, to_s, desc = _build_plan(args.mode, scan_res)

    print("\n📋 ПЛАН ОЧИСТКИ:")
    for d in desc:
        print(f"   - {d}")

    total_targets = len(to_h) + len(to_s)
    print(f"\n   Итого к удалению: {len(to_h)} полей хэша, {len(to_s)} обособленных ключей.")

    if total_targets == 0:
        print("   🎉 Объекты для очистки не обнаружены. База находится в оптимальном состоянии.")
        return

    if args.dry_run:
        print("\n   [DRY-RUN] Операция завершена в режиме превью. Изменения не внесены.")
        return

    if not args.yes:
        print("\n⚠️ ВНИМАНИЕ: Для выполнения операции запустите скрипт с флагом --yes")
        return

    print("\n🚀 Запуск транзакции очистки оперативной памяти...")
    del_h, del_s, errs = _execute_delete(to_h, to_s)

    print("=" * 60)
    print("🏁 РЕЗУЛЬТАТЫ ВЫПОЛНЕНИЯ ОЧИСТКИ:")
    print(f"   Удалено полей хэша (HDEL): {del_h}")
    print(f"   Удалено ключей (DEL):      {del_s}")
    print(f"   Зафиксировано ошибок:      {errs}")
    print("=" * 60)

    if errs > 0:
        sys.exit(1)  # честный failure: ошибки удаления видны в логе и в статусе джоба
    if args.post_verify:
        _post_verify()


if __name__ == "__main__":
    main()
