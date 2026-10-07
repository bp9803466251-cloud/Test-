#!/usr/bin/env python3
"""
redis_flush.py — Очистка Redis для Gatekeeper-AI v9.3-audited.
5 режимов: history-only, soft, hard, purge, reset-breaker.
Использует redis_hub.py API (§6, правило 1.4 — через транспортный слой).

Запуск:
  python redis_flush.py --dry-run --mode history-only
  python redis_flush.py --dry-run --mode soft
  python redis_flush.py --dry-run --mode hard
  python redis_flush.py --dry-run --mode purge
  python redis_flush.py --mode hard          (реальное удаление)
  python redis_flush.py --mode reset-breaker (реальное удаление)
  python redis_flush.py                      (dry-run, mode=soft по умолчанию)
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

__version__ = "9.3-audited"

logger = logging.getLogger("redis_flush")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from redis_hub import (
    is_redis_available,
    get_all_fields,
    get_key,
    set_key,
    delete_key,
    delete_from_cache,
    scan_keys,
    delete_keys_by_pattern,
    get_circuit_breaker_status,
    reset_circuit_breaker,
    HASH_NAME,
    _execute_upstash_cmd,
)

MSK_TZ = timezone(timedelta(hours=3))

VALID_MODES = ("history-only", "soft", "hard", "purge", "reset-breaker")


# ---------------------------------------------------------------------------
# Скан Redis — классификация ключей
# ---------------------------------------------------------------------------
def _scan_redis():
    """Сканирует Redis и возвращает классифицированные ключи."""
    result = {
        "hash_fields": {},      # field_name -> value (из HKEYS+HMGET)
        "standalone_keys": [],   # history:*, system:* и др.
    }

    # 1. Hash fields (match:*, *:meta, search:results:*)
    try:
        all_fields = get_all_fields()
        if isinstance(all_fields, dict):
            result["hash_fields"] = all_fields
    except Exception as e:
        logger.warning("get_all_fields() error: %s", e)

    # 2. Standalone keys (history:match:*, system:*)
    for pattern in ("history:match:*", "system:*", "index:*", "search:results:*"):
        try:
            keys = scan_keys(pattern)
            if keys:
                result["standalone_keys"].extend(keys)
        except Exception as e:
            logger.warning("scan_keys(%s) error: %s", pattern, e)

    return result


def _classify_hash_fields(fields):
    """Классифицирует hash fields по категориям."""
    cats = {
        "match": [],          # match:*
        "match_no_odds": [],  # match:* без odds
        "meta": [],           # *:meta
        "meta_stale": [],     # *:meta stale
        "search": [],         # search:results:*
        "system_hash": [],    # system:* в hash
        "other": [],          # прочее
    }

    now = datetime.now(MSK_TZ)

    for fname, fval in fields.items():
        if fname.startswith("match:"):
            cats["match"].append(fname)
            # Проверка наличия odds
            if isinstance(fval, dict):
                all_odds = fval.get("all_odds", fval.get("odds", {}))
                if not all_odds:
                    cats["match_no_odds"].append(fname)
            else:
                cats["match_no_odds"].append(fname)
        elif fname.endswith(":meta"):
            cats["meta"].append(fname)
            # Проверка stale
            if isinstance(fval, dict):
                last_run = fval.get("last_run", fval.get("last_run_at", ""))
                if not last_run or last_run == "неизвестно":
                    cats["meta_stale"].append(fname)
                else:
                    try:
                        ts = last_run.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age_h = (now - dt.astimezone(MSK_TZ)).total_seconds() / 3600
                        if age_h > 2:
                            cats["meta_stale"].append(fname)
                    except Exception:
                        cats["meta_stale"].append(fname)
            else:
                cats["meta_stale"].append(fname)
        elif fname.startswith("search:results:"):
            cats["search"].append(fname)
        elif fname.startswith("system:"):
            cats["system_hash"].append(fname)
        else:
            cats["other"].append(fname)

    return cats


def _classify_standalone(keys):
    """Классифицирует standalone keys."""
    cats = {
        "history": [],   # history:match:*
        "system": [],    # system:*
        "index": [],     # index:*
        "search": [],    # search:results:*
        "other": [],
    }
    for key in keys:
        if key.startswith("history:match:"):
            cats["history"].append(key)
        elif key.startswith("system:"):
            cats["system"].append(key)
        elif key.startswith("index:"):
            cats["index"].append(key)
        elif key.startswith("search:results:"):
            cats["search"].append(key)
        else:
            cats["other"].append(key)
    return cats


# ---------------------------------------------------------------------------
# Планы очистки
# ---------------------------------------------------------------------------
def _build_plan(mode, scan_result):
    """Строит план удаления для заданного режима.
    Возвращает: (hash_fields_to_delete, standalone_keys_to_delete, description)
    """
    fields = scan_result["hash_fields"]
    standalone = scan_result["standalone_keys"]

    hcats = _classify_hash_fields(fields)
    scats = _classify_standalone(standalone)

    to_del_hash = []
    to_del_standalone = []
    desc_parts = []

    if mode == "history-only":
        to_del_standalone.extend(scats["history"])
        desc_parts.append(f"history:match:* — {len(scats['history'])} ключей")

    elif mode == "soft":
        to_del_hash.extend(hcats["match_no_odds"])
        to_del_hash.extend(hcats["meta_stale"])
        desc_parts.append(f"match:* без odds — {len(hcats['match_no_odds'])} полей")
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
        desc_parts.append(f"*:meta — {len(hcats['meta'])} полей")
        desc_parts.append(f"search:results:* — {len(hcats['search']) + len(scats['search'])} ")
        desc_parts.append(f"system:* — {len(hcats['system_hash']) + len(scats['system'])} ")
        desc_parts.append(f"index:* — {len(scats['index'])} ключей")
        desc_parts.append(f"other — {len(hcats['other'])} полей")
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
        total_h = len(fields)
        total_s = len(standalone)
        desc_parts.append(f"ВСЕ hash fields — {total_h}")
        desc_parts.append(f"ВСЕ standalone keys — {total_s}")

    elif mode == "reset-breaker":
        # Только system:circuit_breaker + system:health
        for key in scats["system"]:
            if "circuit" in key.lower() or "health" in key.lower():
                to_del_standalone.append(key)
        for fname in hcats["system_hash"]:
            if "circuit" in fname.lower() or "health" in fname.lower():
                to_del_hash.append(fname)
        desc_parts.append(f"circuit_breaker + health keys")

    return to_del_hash, to_del_standalone, desc_parts


# ---------------------------------------------------------------------------
# Выполнение удаления
# ---------------------------------------------------------------------------
def _execute_delete(to_del_hash, to_del_standalone, batch_size=50):
    """Удаляет ключи батчами. Возвращает (deleted_hash, deleted_standalone, errors)."""
    deleted_hash = 0
    deleted_standalone = 0
    errors = 0

    # Hash fields — батчами через HDEL
    for i in range(0, len(to_del_hash), batch_size):
        batch = to_del_hash[i:i + batch_size]
        args = ["HDEL", HASH_NAME] + batch
        result = _execute_upstash_cmd(args)
        if result is not None:
            try:
                deleted_hash += int(result)
            except (ValueError, TypeError):
                deleted_hash += len(batch)
        else:
            # Fallback — по одному
            for field in batch:
                r = delete_from_cache(field)
                if r is not None:
                    deleted_hash += 1
                else:
                    errors += 1

    # Standalone keys — по одному через DEL
    for key in to_del_standalone:
        result = _execute_upstash_cmd(["DEL", key])
        if result is not None:
            try:
                n = int(result)
                deleted_standalone += n if n > 0 else 1
            except (ValueError, TypeError):
                deleted_standalone += 1
        else:
            errors += 1

    return deleted_hash, deleted_standalone, errors


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Redis Flush — очистка Redis для Gatekeeper-AI"
    )
    parser.add_argument(
        "--mode", type=str, default="soft",
        choices=VALID_MODES,
        help="Режим очистки (default: soft)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только превью, без удаления"
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Подтверждение (без --yes удаление не выполняется)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print(f"🗑 РЕДИС FLUSH — Gatekeeper-AI v{__version__}")
    print(f"   Время: {datetime.now(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    print(f"   Режим: {args.mode}")
    print(f"   Dry-run: {'ДА' if args.dry_run else 'НЕТ'}")
    print(f"   Подтверждение: {'ДА' if args.yes else 'НЕТ'}")
    print("=" * 60)

    # 1. Проверка Redis
    print("\n[1/4] Проверка Redis...")
    if not is_redis_available():
        print("   ❌ Redis недоступен")
        sys.exit(1)
    print("   ✅ Redis доступен")

    # 2. Сброс circuit breaker (для reset-breaker)
    if args.mode == "reset-breaker":
        print("\n[2/4] Сброс circuit breaker...")
        cb_before = get_circuit_breaker_status()
        print(f"   До: state={cb_before.get('state')}, failures={cb_before.get('failures')}")
        if not args.dry_run and args.yes:
            reset_circuit_breaker()
            cb_after = get_circuit_breaker_status()
            print(f"   После: state={cb_after.get('state')}, failures={cb_after.get('failures')}")
        else:
            print("   (dry-run — сброс не выполнен)")

    # 3. Сканирование
    print(f"\n[{'3/4' if args.mode != 'reset-breaker' else '3/4'}] Сканирование Redis...")
    scan_result = _scan_redis()

    hcats = _classify_hash_fields(scan_result["hash_fields"])
    scats = _classify_standalone(scan_result["standalone_keys"])

    total_hash = len(scan_result["hash_fields"])
    total_standalone = len(scan_result["standalone_keys"])

    print(f"   Hash fields: {total_hash}")
    print(f"     match:* — {len(hcats['match'])} (без odds: {len(hcats['match_no_odds'])})")
    print(f"     *:meta — {len(hcats['meta'])} (stale: {len(hcats['meta_stale'])})")
    print(f"     search:results:* — {len(hcats['search'])}")
    print(f"     system:* — {len(hcats['system_hash'])}")
    print(f"     other — {len(hcats['other'])}")
    print(f"   Standalone keys: {total_standalone}")
    print(f"     history:match:* — {len(scats['history'])}")
    print(f"     system:* — {len(scats['system'])}")
    print(f"     index:* — {len(scats['index'])}")
    print(f"     search:results:* — {len(scats['search'])}")
    print(f"     other — {len(scats['other'])}")

    # 4. План удаления
    if args.mode == "reset-breaker":
        to_del_hash, to_del_standalone, desc_parts = _build_plan(args.mode, scan_result)
        print(f"\n[4/4] План сброса:")
        for part in desc_parts:
            print(f"   • {part}")
        if to_del_hash:
            print(f"   Hash fields к удалению: {to_del_hash}")
        if to_del_standalone:
            print(f"   Standalone keys к удалению: {to_del_standalone}")
        print("\n✅ Circuit breaker сброс завершён (dry-run)" if args.dry_run else
              "\n✅ Circuit breaker сброшен" if args.yes else
              "\n⚠ Требуется --yes для выполнения")
        return

    to_del_hash, to_del_standalone, desc_parts = _build_plan(args.mode, scan_result)

    print(f"\n[4/4] План удаления ({args.mode}):")
    for part in desc_parts:
        print(f"   • {part}")
    print(f"\n   Итого к удалению:")
    print(f"     Hash fields: {len(to_del_hash)}")
    print(f"     Standalone keys: {len(to_del_standalone)}")

    if not to_del_hash and not to_del_standalone:
        print("\n   ✅ Нечего удалять — Redis чист")
        return

    # Выполнение
    if args.dry_run:
        print("\n   📋 DRY-RUN — ключи не удалены")
        print("   Для выполнения: python redis_flush.py --mode {} --yes".format(args.mode))
        return

    if not args.yes:
        print("\n   ⚠ Требуется --yes для подтверждения удаления")
        return

    print("\n   Выполняю удаление...")
    deleted_h, deleted_s, errors = _execute_delete(to_del_hash, to_del_standalone)

    print(f"\n   ✅ Удалено:")
    print(f"     Hash fields: {deleted_h}")
    print(f"     Standalone keys: {deleted_s}")
    print(f"     Ошибок: {errors}")

    # Verify
    print("\n   Проверка после очистки...")
    scan_after = _scan_redis()
    hcats_after = _classify_hash_fields(scan_after["hash_fields"])
    scats_after = _classify_standalone(scan_after["standalone_keys"])

    print(f"     Hash fields: {len(scan_after['hash_fields'])} (было {total_hash})")
    print(f"     Standalone keys: {len(scan_after['standalone_keys'])} (было {total_standalone})")
    print(f"     match:* — {len(hcats_after['match'])}")
    print(f"     *:meta — {len(hcats_after['meta'])}")
    print(f"     history:match:* — {len(scats_after['history'])}")

    print("\n" + "=" * 60)
    print(f"🗑 Redis Flush завершён — режим: {args.mode}")
    print("=" * 60)


if __name__ == "__main__":
    main()
