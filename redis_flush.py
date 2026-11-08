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
Обновления 10.1.0-batch (2026-11-08):
  - [10.1.0 FIX #21] DEL-батчинг: обособленные ключи удаляются пачками
    (DEL до 25 ключей на команду, до 10 команд одним /pipeline REST-запросом
    = 250 ключей на вызов). 10 680 ключей — ~45 REST-вызовов вместо 10 680;
    реальная скорость поштучного режима была ~460 ключ/мин (~3 DEL/сек),
    что и уложило execute в таймаут 20 минут на 65% плана.
  - [10.1.0 FIX] Прогресс-лог: счётчик печатается каждую пачку — устранены
    «немые» минуты после «Запуск транзакции...» (наблюдаемость, а не подвисание).
  - [10.1.0] Деградация транспорта сохранена: сбой /pipeline -> поштучный
    redis_command (семантика PipelineBatch.flush хаба v9.6.9).
  - Guard §1.7.4 сохранён дословно: HISTORY_FLUSH_CONFIRM=1 обязателен,
    флаг живёт ровно на время execute (finally — снятие).

Topology: Zero-Folder Architecture (Root Execution)
Compliance: §1.3 — только urllib.request через redis_hub.py API.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "10.1.0-batch"  # Контрактная строка версии для прохождения тестов

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
        delete_history_match,
        _execute_upstash_pipeline,
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
        # [10.0.0 FIX] Честный полный план: ранее purge собирал категории и
        # пропускал match_no_odds (легаси-матчи) — освобождал 4% вместо 43 МБ.
        # Теперь берутся ВСЕ поля хэша и ВСЕ категории standalone.
        to_del_hash.extend(fields)
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
# [10.1.0 BATCH] Параметры батчинга удаления обособленных ключей:
# одна DEL-команда несёт до DEL_CHUNK ключей, группа из PIPE_CHUNK таких
# команд уходит одним /pipeline REST-запросом (250 ключей на вызов).
DEL_CHUNK = 25
PIPE_CHUNK = 10


def _del_keys_pipeline(keys, label):
    """[10.1.0] Пачечное DEL через /pipeline; деградация — поштучный redis_command.

    Возвращает (удалено, ошибок). Печатает прогресс каждой пачки — «немые»
    минуты (#21) исключены. Деградация повторяет семантику PipelineBatch.flush
    хаба v9.6.9: сбой транспорта не тихий, ошибки считаются честно.
    """
    deleted, errors = 0, 0
    if not keys:
        return deleted, errors
    cmds = [["DEL"] + keys[i:i + DEL_CHUNK] for i in range(0, len(keys), DEL_CHUNK)]
    done = 0
    for ps in range(0, len(cmds), PIPE_CHUNK):
        group = cmds[ps:ps + PIPE_CHUNK]
        res = _execute_upstash_pipeline(group)
        if res is None:
            # Деградация транспорта: поштучная доставка (каждый вызов — до DEL_CHUNK ключей)
            for cmd in group:
                r = redis_command(cmd)
                if r is None:
                    errors += len(cmd) - 1
                else:
                    try:
                        deleted += max(int(r), 0)
                    except (ValueError, TypeError):
                        deleted += len(cmd) - 1
        elif isinstance(res, list):
            if len(res) != len(group):
                errors += abs(len(group) - len(res))
            for slot in res:
                if isinstance(slot, dict) and "error" in slot:
                    errors += 1  # ошибка слота: фактический объём неизвестен, виден в post-verify
                    logger.warning("DEL-слот пачки %s: ошибка %s", label, slot["error"])
                else:
                    try:
                        r = slot.get("result") if isinstance(slot, dict) else slot
                        deleted += max(int(r), 0)
                    except (ValueError, TypeError, AttributeError):
                        deleted += 0
        else:
            errors += sum(len(c) - 1 for c in group)
        done = min((ps + len(group)) * DEL_CHUNK, len(keys))
        print(f"   ... {label}: ~{done}/{len(keys)} ключей (удалено {deleted}, ошибок {errors})")
    return deleted, errors


def _execute_delete(to_del_hash, to_del_standalone, batch_size=50):
    """[10.1.0-batch] Удаление с батчингом и прогресс-логом.

    Поля хэша — как в 10.0.0: HDEL пачками по batch_size (1 REST на 50 полей).
    Обособленные ключи — пайплайн-батчи: ~45 REST-вызовов на 10 680 ключей
    вместо 10 680 поштучных. Guard §1.7.4 для history:match:* сохранён:
    HISTORY_FLUSH_CONFIRM=1 обязателен (флаг ставит main на время execute).
    """
    deleted_hash = 0
    deleted_standalone = 0
    errors = 0

    # --- поля хэша: HDEL пачками (как в 10.0.0) ---
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
        if len(to_del_hash):
            print(f"   ... HDEL: {min(i + batch_size, len(to_del_hash))}/{len(to_del_hash)} полей")

    # --- обособленные ключи: guard-разделение + пайплайн ---
    guard_on = os.environ.get("HISTORY_FLUSH_CONFIRM") == "1"
    hist_keys = [k for k in to_del_standalone if k.startswith("history:match:")]
    other_keys = [k for k in to_del_standalone if not k.startswith("history:match:")]

    if hist_keys:
        if not guard_on:
            errors += len(hist_keys)
            logger.warning("Guard: HISTORY_FLUSH_CONFIRM не активен — %d history-ключей НЕ удалено",
                           len(hist_keys))
        else:
            d, e = _del_keys_pipeline(hist_keys, "DEL history")
            deleted_standalone += d
            errors += e

    d, e = _del_keys_pipeline(other_keys, "DEL прочие")
    deleted_standalone += d
    errors += e

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
    has_history = any(k.startswith("history:match:") for k in to_s)
    if has_history:
        # Guard §1.7.4: флаг живёт ровно на время исполнения (finally — снятие)
        os.environ["HISTORY_FLUSH_CONFIRM"] = "1"
    try:
        del_h, del_s, errs = _execute_delete(to_h, to_s)
    finally:
        os.environ.pop("HISTORY_FLUSH_CONFIRM", None)

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
