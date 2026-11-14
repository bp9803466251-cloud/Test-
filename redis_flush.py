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

  [10.3.0 REGISTRY] Новый режим registry: точечная чистка реестра registry:*
  (поля легаси-хэша registry:league:*/registry:map:country_code/
  registry:alias:league + обособленные ключи registry:bridge:*,
  registry:system:health, registry:upload:checkpoint). История и живой
  контур сохраняются — сценарий «перезаливка P0 без полного wipe»:
  после чистки загрузчик стартует без --resume. Поверхность ключей
  верхнего уровня добирается полным SCAN * (SCAN_PATTERNS ядра их
  не видят — подтверждено картой ключей #103/#104).
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "10.3.1-league-flush"  # Гейт 19 не затронут: _classify_hash_fields не менялся

# [10.1.0 BATCH #21] Дефект #21: _execute_delete для обособленных ключей делал
# ОДИН REST-вызов на ключ (redis_command DEL / delete_history_match) — 10680
# вызовов x ~300мс ~ час, при лимите шага 20 минут прогон убит на 65%.
# Пайплайн-транспорт redis_hub (§13.4/§27.3) существовал и не использовался.
# Теперь: DEL-команды по 25 ключей, пачки по 10 команд одним /pipeline-запросом
# (250 ключей за REST-вызов), прогресс-лог каждой пачки, деградация до
# поштучной доставки при сбое пайплайна. Guard §1.7.4 сохранён дословно:
# флаг HISTORY_FLUSH_CONFIRM живёт только на время execute.
# [10.2.0 PURGE-TOTAL] Семантика purge усилена до тотальной: полный SCAN * по
# курсору (независим от SCAN_PATTERNS read-only ядра — 559 ключей analysis:*,
# невидимых пяти паттернам, теперь охвачены), DBSIZE-сверка в dry-run,
# поимённый батчевый DEL (§13.4), хэш — последним (HDEL всех полей; пустой
# хэш исчезает сам), пост-verify с проверкой DBSIZE == 0. Остальные режимы
# не тронуты.

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
# [10.3.0 REGISTRY] Добавлен режим registry: точечная чистка реестра §14.
VALID_MODES = ("history-only", "soft", "hard", "purge", "reset-breaker", "registry", "league-flush")


def _full_key_scan():
    """[10.2.0 PURGE-TOTAL] Полный SCAN * по курсору через redis_command.
    Не переиспользует scan_redis_state: тот привязан к SCAN_PATTERNS ядра
    (пять паттернов) — ключи вне их (analysis:*, иные префиксы) ему невидимы,
    что подтверждено прогонами #91-#93 (DBSIZE 561 при нулях по категориям).
    Возвращает (keys, dbsize). Сбой транспорта — честный RuntimeError."""
    keys = []
    cursor = "0"
    while True:
        raw = redis_command(["SCAN", cursor, "MATCH", "*", "COUNT", "250"])
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise RuntimeError(f"SCAN *: неожиданный формат ответа: {raw!r}")
        next_cursor, found = raw[0], raw[1]
        if isinstance(next_cursor, bytes):
            next_cursor = next_cursor.decode("utf-8")
        cursor = str(next_cursor)
        if isinstance(found, list):
            keys.extend(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in found)
        if cursor in ("0", 0):
            break
    raw_size = redis_command(["DBSIZE"])
    try:
        dbsize = int(raw_size)
    except (TypeError, ValueError):
        dbsize = -1
    return keys, dbsize

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
    elif mode == "registry":
        # [10.3.0 REGISTRY] Точечная чистка реестра registry:* (§14): поля
        # легаси-хэша (registry:league:*, registry:map:country_code,
        # registry:alias:league) + обособленные ключи (registry:bridge:*,
        # registry:system:health, registry:upload:checkpoint). История
        # (history:match:*) и живой контур (match:*) не затрагиваются.
        # Фильтрация по префиксу, а не по категориям классификатора —
        # намеренно: classify_standalone_keys не знает про новые ключи
        # (registry:system:health, registry:upload:checkpoint) и разложит
        # их по категориям непредсказуемо; прямой префикс надёжнее и не
        # требует правки read-only ядра. Guard §1.7.4 не затрагивается:
        # среди registry-ключей нет history:match:*.
        reg_hash = [f for f in fields if f.startswith("registry:")]
        reg_standalone = sorted(set(k for k in standalone if k.startswith("registry:")))
        to_del_hash.extend(reg_hash)
        to_del_standalone.extend(reg_standalone)
        desc_parts.append(f"registry:* поля хэша — {len(reg_hash)} (лиги, карта стран, alias:league)")
        desc_parts.append(f"registry:* обособленные ключи — {len(reg_standalone)} (bridge, health, checkpoint)")
    elif mode == "league-flush":
        # [10.3.1 LEAGUE-FLUSH] Точечная чистка ТОЛЬКО лиг: поля хэша
        # registry:league:* (тела лиг) + агрегированная карта
        # registry:alias:league ({slug: канон}). Сценарий: перезаливка P0
        # с честными canonical_name без потери заселённого P1/P2.
        # ПОД ЗАЩИТОЙ (в план не попадают никогда): registry:team:*,
        # registry:bridge:*, registry:alias:team*, registry:map:*,
        # registry:system:health, registry:upload:checkpoint,
        # match:*, history:match:* — фильтр только по префиксам лиг.
        league_hash = [f for f in fields
                       if f.startswith("registry:league:")
                       or f == "registry:alias:league"]
        # обособленные ключи лиг (теоретический кейс) — той же защитой
        league_standalone = sorted(k for k in standalone
                                   if k.startswith("registry:league:")
                                   or k == "registry:alias:league")
        to_del_hash.extend(league_hash)
        to_del_standalone.extend(league_standalone)
        kept_teams = sum(1 for f in fields if f.startswith("registry:team:"))
        desc_parts.append(f"registry:league:* — {len(league_hash)} полей хэша (тела лиг + alias:league)")
        if league_standalone:
            desc_parts.append(f"registry:league:* — {len(league_standalone)} обособленных ключей")
        desc_parts.append(f"СОХРАНЕНО: registry:team:* — {kept_teams} команд и весь прочий реестр")
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
DEL_KEYS_PER_CMD = 25      # ключей в одной DEL-команде (§13.4: безопасный батч)
PIPE_CMDS_PER_REQ = 10     # DEL-команд в одном /pipeline-запросе


def _execute_delete(to_del_hash, to_del_standalone, batch_size=50):
    """[10.1.0 BATCH] Удаление: HDEL полей хэша (как в 10.0.0), обособленные
    ключи — пайплайн-батчами (250 ключей за REST-вызов) вместо поштучного DEL.
    Возвращает (deleted_hash, deleted_standalone, errors)."""
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

    # Guard §1.7.4: история удаляется только при HISTORY_FLUSH_CONFIRM=1,
    # флаг ставится программно и только на время execute (см. main).
    guard_on = os.environ.get("HISTORY_FLUSH_CONFIRM") == "1"
    hist_keys = [k for k in to_del_standalone if k.startswith("history:match:")]
    other_keys = [k for k in to_del_standalone if not k.startswith("history:match:")]
    if not guard_on and hist_keys:
        errors += len(hist_keys)
        logger.warning("Guard: HISTORY_FLUSH_CONFIRM не активен — %d ключей истории НЕ удалены",
                       len(hist_keys))

    def _flush_group(label, keys):
        nonlocal deleted_standalone, errors
        total = len(keys)
        if total == 0:
            return
        done = 0
        step = DEL_KEYS_PER_CMD * PIPE_CMDS_PER_REQ
        for i in range(0, total, DEL_KEYS_PER_CMD):
            chunk = keys[i:i + DEL_KEYS_PER_CMD]
            # Команда DEL принимает список ключей: ["DEL", k1, k2, ...]
            pipeline_cmds.append(["DEL"] + chunk)
            if len(pipeline_cmds) >= PIPE_CMDS_PER_REQ or i + DEL_KEYS_PER_CMD >= total:
                cmds, pipeline_cmds[:] = list(pipeline_cmds), []
                res = _execute_upstash_pipeline(cmds)
                if res is None:
                    # Деградация: пайплайн недоступен — поштучная доставка
                    logger.warning("Пайплайн недоступен (%s) — деградация до поштучной доставки", label)
                    for cmd in cmds:
                        for key in cmd[1:]:
                            r = redis_command(cmd)
                            if r is not None:
                                try:
                                    deleted_standalone += max(int(r), 0)
                                except (ValueError, TypeError):
                                    deleted_standalone += 1
                            else:
                                errors += 1
                    continue
                for cmd, slot in zip(cmds, res):
                    if isinstance(slot, dict) and slot.get("error"):
                        errors += len(cmd) - 1
                        logger.warning("%s: слот пайплайна вернул ошибку: %s", label, slot["error"])
                        continue
                    n = slot.get("result") if isinstance(slot, dict) else slot
                    try:
                        deleted_standalone += max(int(n), 0)
                    except (ValueError, TypeError):
                        deleted_standalone += len(cmd) - 1
                    done += len(cmd) - 1
                if done % step == 0 or done >= total:
                    print(f"   ... DEL {label}: ~{done}/{total} ключей (удалено {deleted_standalone}, ошибок {errors})")

    pipeline_cmds = []
    _flush_group("history", hist_keys if guard_on else [])
    _flush_group("прочие", other_keys)
    return deleted_hash, deleted_standalone, errors


# ---------------------------------------------------------------------------
# [10.0.0] Внутренний post-verify: SCAN + план soft без второго процесса
# ---------------------------------------------------------------------------
def _post_verify(purge_total=False, registry_mode=False, league_mode=False):
    print("\n=== POST-VERIFY (внутренний, тот же процесс) ===")
    if purge_total:
        raw = redis_command(["DBSIZE"])
        try:
            dbsize = int(raw)
        except (TypeError, ValueError):
            dbsize = -1
        if dbsize == 0:
            print("   ✅ DBSIZE = 0 — база полностью пуста (purge-total подтверждён)")
        else:
            print(f"   ❌ DBSIZE = {dbsize} — после purge-total остались ключи (полный SCAN * ниже покажет какие)")
            return
    try:
        scan_res = scan_redis_state()
        to_h, to_s, desc = _build_plan("soft", scan_res)
        remaining = len(to_h) + len(to_s)
        print(f"   Остатки по плану soft: {remaining} объектов ({', '.join(desc) if desc else 'нет'})")
        if remaining == 0:
            print("   ✅ Soft-остатков нет — flush чист")
        else:
            print("   ⚠ Остались объекты soft-категорий — повторите режим soft при необходимости")
        if registry_mode:
            # [10.3.0 REGISTRY] Проверка реестра: после чистого прогона
            # registry:* не должно остаться ни на верхнем уровне, ни в хэше.
            try:
                keys, dbsize = _full_key_scan()
                reg_left = sorted(k for k in keys if k.startswith("registry:"))
                hash_left = sorted(f for f in scan_res["hash_fields"]
                                   if f.startswith("registry:"))
                if not reg_left and not hash_left:
                    print("   ✅ Реестр чист: registry:* ключей и полей нет")
                else:
                    print(f"   ⚠ Остатки реестра: {len(reg_left)} ключей, "
                          f"{len(hash_left)} полей хэша — повторите режим registry")
            except Exception as e:
                logger.warning("Registry post-verify: %s", e)
        if league_mode:
            # [10.3.1 LEAGUE-FLUSH] Лиги должны быть чисты; команды — целы.
            try:
                keys, _dbsize = _full_key_scan()
                lg_left = sorted(k for k in keys
                                 if k.startswith("registry:league:")
                                 or k == "registry:alias:league")
                hash_left = sorted(f for f in scan_res["hash_fields"]
                                   if f.startswith("registry:league:")
                                   or f == "registry:alias:league")
                teams_left = sum(1 for f in scan_res["hash_fields"]
                                 if f.startswith("registry:team:"))
                if not lg_left and not hash_left:
                    print(f"   ✅ Лиги чисты: registry:league:* и registry:alias:league удалены")
                    print(f"   ✅ Команд осталось: {teams_left} (registry:team:* не тронуты)")
                else:
                    print(f"   ⚠ Остатки лиг: {len(lg_left)} ключей, "
                          f"{len(hash_left)} полей хэша — повторите режим league-flush")
            except Exception as e:
                logger.warning("League post-verify: %s", e)
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

    if args.mode == "purge":
        # [10.2.0 PURGE-TOTAL] Полная поверхность: SCAN * вместо SCAN_PATTERNS.
        try:
            full_keys, dbsize_before = _full_key_scan()
        except Exception as e:
            print(f"   ❌ Сбой полного SCAN *: {e}")
            sys.exit(1)
        to_h = list(scan_res["hash_fields"].keys())
        # Хэш удаляется через HDEL всех полей (пустой хэш исчезает сам) —
        # DEL по имени хэша не нужен и не ведётся; исключаем из списка.
        to_s = sorted(k for k in full_keys if k != HASH_NAME)
        if HASH_NAME in full_keys and not to_h:
            # Теоретический кейс: хэш-ключ без полей — добираем DEL'ом
            to_s.append(HASH_NAME)
        prefixes = {}
        for k in to_s:
            pfx = k.split(":", 1)[0]
            prefixes[pfx] = prefixes.get(pfx, 0) + 1
        desc = ["TOTAL WIPE: purge теперь тотальный — удаляется ВСЁ, включая мета-ключи и health"]
        desc.append("DBSIZE до: " + str(dbsize_before) + " | план: " + str(len(to_h))
                    + " полей хэша + " + str(len(to_s)) + " обособленных ключей")
        desc.append("префиксы: " + (", ".join(f"{p}={n}" for p, n in sorted(prefixes.items())) or "пусто"))
        if to_s and dbsize_before >= 0 and len(to_s) + (1 if to_h else 0) != dbsize_before:
            desc.append("⚠️ план + хэш не сходится с DBSIZE (" + str(len(to_s) + (1 if to_h else 0))
                        + " vs " + str(dbsize_before) + ") — сверка плана перед execute обязательна")
    elif args.mode == "league-flush":
        # [10.3.1 LEAGUE-FLUSH] Поверхность добирается полным SCAN * — как в
        # registry — но в план попадают только лиговые префиксы. scan_redis_state
        # привязан к SCAN_PATTERNS ядра и лиговые ключи верхнего уровня (если
        # когда-нибудь появятся) может не видеть. Поля хэша полны и так (HGETALL).
        try:
            full_keys, dbsize_before = _full_key_scan()
        except Exception as e:
            print(f"   ❌ Сбой полного SCAN *: {e}")
            sys.exit(1)
        league_extra = sorted(k for k in full_keys
                              if (k.startswith("registry:league:")
                                  or k == "registry:alias:league")
                              and k not in scan_res["standalone_keys"])
        scan_res["standalone_keys"] = sorted(
            set(scan_res["standalone_keys"]) | set(league_extra))
        print(f"   [LEAGUE-FLUSH] Полный SCAN *: {len(full_keys)} ключей всего | "
              f"добрано лиговых ключей вне SCAN_PATTERNS: {len(league_extra)}")
        to_h, to_s, desc = _build_plan(args.mode, scan_res)
    elif args.mode == "registry":
        # [10.3.0 REGISTRY] Дополнение поверхности ключей: scan_redis_state
        # привязан к SCAN_PATTERNS ядра и registry:* ключи верхнего уровня
        # (bridge/health/checkpoint) в standalone-список не кладёт — это
        # подтверждено картой ключей прогонов #103/#104 (SCAN * вне
        # SCAN_PATTERNS видел registry:bridge:reep). Полный SCAN * — как в
        # purge — добирает их; поля хэша полны и так (HGETALL целиком).
        try:
            full_keys, dbsize_before = _full_key_scan()
        except Exception as e:
            print(f"   ❌ Сбой полного SCAN *: {e}")
            sys.exit(1)
        reg_extra = sorted(k for k in full_keys
                           if k.startswith("registry:")
                           and k not in scan_res["standalone_keys"])
        scan_res["standalone_keys"] = sorted(
            set(scan_res["standalone_keys"]) | set(reg_extra))
        print(f"   [REGISTRY] Полный SCAN *: {len(full_keys)} ключей всего | "
              f"добрано registry:* ключей вне SCAN_PATTERNS: {len(reg_extra)}")
        to_h, to_s, desc = _build_plan(args.mode, scan_res)
    else:
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
        _post_verify(purge_total=(args.mode == "purge"),
                     registry_mode=(args.mode == "registry"),
                     league_mode=(args.mode == "league-flush"))


if __name__ == "__main__":
    main()
