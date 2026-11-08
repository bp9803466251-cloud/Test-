#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
source_diagnostics.py — Единый read-only диагностический модуль Gatekeeper-AI.
Версия 10.0.0-merged — результат аудита объединения 7 диагностических единиц.

[10.0.0 MERGE] Вшиты и удалены как отдельные файлы:
    - check_redis.py      -> режимы propline, keys (аудит контракта истории v1.1);
    - redis_diagnostics.py -> статус Redis, Circuit Breaker, DEBUG-трейсинг ключей,
                              indexes/errors/sample (секции 1-7 §18.2/18.3);
    - diagnostic_matches.py -> режим matches (полный дамп, категоризация готовности,
                              детектор дублей);
    - verify_history.py   -> режим history-contracts (контрактный аудит Шага 13);
    - memory precheck §13.4 -> режим memory (флаг для football_data_loader.yml).
Read-only по умолчанию (§18.1). Write-контур Redis — redis_flush.py,
write-телеметрия — metrics.py (контракт system:health не тронут).

[10.0.0 FIX] Транспорт SCAN: redis_command (существует в v710) вместо
    несуществующих scan_keys (redis_diagnostics/match_linker) и
    _execute_upstash_cmd (redis_flush). Прежние вызовы молча возвращали
    пустой список — «fake green» для секции индексов и пула истории.
[10.0.0 FIX] Строгий импорт (13-G1): отсутствие redis_hub/gatekeeper_hub —
    честный exit 1, без ленивых None и fallback-констант HASH_NAME.
[10.0.0 FIX] Канонический читатель истории: Root-скан history:match:* через
    SCAN+GET (из match_linker.fetch_all_history_pool после фикса транспорта),
    дедупликация по canonical_id, Root приоритетнее легаси.
    Легаси-поля history:* в хэше после миграции Шага 13 трактуются как
    незавершённая миграция (вердикт v1.1 «разрыв/двойное хранение»).
[10.0.0 FIX] _resolve_classification/_is_value_signal — единственный источник
    (§1.11: ранее триплет копий в metrics.py / telegram_transport.py;
    при следующем патче эти модули импортируют отсюда).
[10.0.0 PERF] Один HGETALL на прогон (Context.all_fields): все режимы кормятся
    из одной выборки. Propline-аудит больше не делает N+1 HGET (прежде —
    отдельный HGET на каждое поле, риск дожатия bandwidth при DATA STOP).
"""

# [10.0.2 HISTORY-SAMPLE] Новый read-only режим history-sample: SCAN history:match:*,
#     внутренняя консистентность мета (written+deduped+skipped == total_events);
#     stored_matches печатается как информация (счётчик поргонный — прогоны #22/#23),
#     гистограмма дат и топ лиг через MGET-чанки, разбор образцов по контракту v8.13.
# [10.0.5 MEMFIX #85] cmd_memory: fake-empty устранён. redis_command при сбое
#     транспорта возвращает None без исключения — прежний код попадал в ветку
#     "maxmemory не задан" и давал зелёный precheck на мёртвом чтении (#85:
#     used 0.0 MB при живых 3753 записях). Теперь: INFO=None -> NetworkError;
#     used_memory обязателен; DBSIZE кросс-проверка: used=0 при DBSIZE>0 —
#     невозможное состояние, честный NetworkError. Precheck стал fail-closed.
# [10.0.3 SCORE-PROBE] Счёт по канону build_payload v8.13: score={"home","away"},
#     full_time_result (FTR); каскад проб (плоские ключи -> csv_raw FTHG/FTAG);
#     при промахе — дамп ключей записи и честный exit 1 (поверхность: писатель).
#     Косметика: xG-хинт печатается только при None.
import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

__version__ = "10.0.5-memfix"  # Контрактная строка версии — сверить пин в test_contracts.py
# [10.0.0 FIX #76] Диспетчер: всем обработчикам с сигнатурой (ctx, ...) передаётся
#     "ctx": ctx — в full-ветке и в dispatch-таблице (run #74/#75: TypeError).
# [10.0.0 FIX §18.1] cmd_redis НЕ вызывает run_initialization: по поверхности
#     gatekeeper_hub v9.6.9-history тот выполняет WRITE-операции (migrate_schema,
#     cleanup_expired(auto_migrate=True), _flush_health_metrics -> save_to_cache
#     system:health). Read-only диагностика не запускает write-контур; доступность
#     транспорта проверяется is_redis_available() (read-only PING).
# [10.0.0 FIX] CB-статус: реальные ключи поверхности redis_hub —
#     {"open": bool, "error_count": int, "threshold": int} (ранее — state/status,
#     отсюда «unknown» в run #76).
# [10.0.0 FIX] Пустая выборка HGETALL подтверждается HLEN: None-ответ или
#     расхождение — NetworkError (анти-fake-empty; в #76 таймаут HGETALL маскировался
#     «Хэш: 0 полей» при живом bzzoiro:meta).

__all__ = [
    "MODES", "resolve_classification", "is_value_signal", "fetch_history_pool",
    "scan_by_pattern", "main", "__version__",
]

logger = logging.getLogger("source_diagnostics")
logging.basicConfig(level=logging.INFO, format="[DIAG] %(message)s")

MSK_TZ = timezone(timedelta(hours=3))
STALE_THRESHOLD_HOURS = 2        # §16
SCAN_COUNT = "250"               # батч SCAN — экономия bandwidth при DATA STOP
HISTORY_SAMPLE_TRUNCATE = 1200
DEFAULT_LIMIT = 15
SAMPLE_SIZE = 5
# [10.0.1 FIX #79] 100 MB физически недостижим на Upstash-инстансе 64 МБ —
# precheck в full-режиме блокировал загрузчик навсегда. Рабочий порог 32;
# после апгрейда Upstash вернуть 100.
DEFAULT_MIN_FREE_MB = 32         # §13.4

MODES = ("full", "redis", "sources", "matches", "indexes", "errors",
         "sample", "propline", "keys", "history-contracts", "history-sample", "memory")

# [10.0.1 FIX] Структура контура выправлена: 4 коллектора live-данных + 1 загрузчик
# истории. football_data вынесен в LOADERS: это писатель пула истории
# (football_data_to_redis, открытые CSV football-data.co.uk), а не коллектор.
# "main" удалён: виртуальный источник без ключа, meta и писателя давал вечный
# STALE-шум в каждом прогоне (#78/#79).
COLLECTORS = ["sharpapi", "odds_api", "bzzoiro", "propline"]
LOADERS = ["football_data"]

# [10.0.1 FIX] football_data НАМЕРЕННО отсутствует в карте: источник
# football-data.co.uk — открытые CSV, ключа нет по природе сервиса.
# НЕ ПУТАТЬ с одноимённым близнецом football-data.org (REST API с ключом) —
# близость имён уже вызвала ошибку конфигурации; близнеца в контуре нет.
_KEY_ENV = {
    "sharpapi": "SHARP_API_KEY",
    "odds_api": "ODDS_API_KEY",
    "bzzoiro": "BZZOIRO_API_KEY",
    "propline": "PROPLINE_API_KEY",
}

SCAN_PATTERNS = ("history:match:*", "history:*", "system:*", "index:*", "search:results:*")
INDEX_PATTERNS = ("idx:*", "index:*", "search:results:*")

# ---------------------------------------------------------------------------
# Строгий импорт (13-G1): диагностика без ядра не имеет смысла — exit 1.
# ---------------------------------------------------------------------------
try:
    import redis_hub
    from redis_hub import (HASH_NAME, get_all_fields, is_redis_available,
                           redis_command, get_circuit_breaker_status)
except ImportError as e:
    print(f"❌ source_diagnostics: критический сбой импорта redis_hub: {e}")
    sys.exit(1)

try:
    import gatekeeper_hub
    from gatekeeper_hub import get_from_cache, run_initialization
except ImportError as e:
    print(f"❌ source_diagnostics: критический сбой импорта gatekeeper_hub: {e}")
    sys.exit(1)

# Вспомогательные модули: отсутствие честно репортуется, не маскируется.
try:
    import redis_config
except ImportError:
    redis_config = None

try:
    from team_registry import clean_team_name
except ImportError:
    clean_team_name = None


class NetworkError(RuntimeError):
    """Сетевой сбой чтения Redis — осознанный exit 1 без traceback (v1.1-дисциплина)."""


# ===========================================================================
# Общие хелперы (единственные реализации — §1.11)
# ===========================================================================
VERBOSE = True


def _now_msk() -> str:
    return datetime.now(MSK_TZ).isoformat()


def _clean(name):
    if clean_team_name is not None:
        try:
            return clean_team_name(name)
        except Exception:
            pass
    return (name or "").lower().strip().replace(" ", "_")


def _safe_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return default
    return value


def _truncate(text, limit=HISTORY_SAMPLE_TRUNCATE):
    text = str(text)
    if len(text) > limit:
        return text[:limit] + "\n... (обрезано для лога)"
    return text


def _parse_entry(key, value):
    """Парсит запись хэша. Возвращает (dict|None, описание ошибки|None)."""
    if isinstance(value, dict):
        return value, None
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"нечитаемый JSON: {e}"
    if not isinstance(data, dict):
        return None, f"{type(data).__name__} вместо dict"
    return data, None


def _payload(data):
    """Контракт v710: обертка с payload или плоский объект (поля в корне)."""
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else data


def _has_odds_1x2(match):
    """Детекция котировок по контракту v710: вложенный odds, плоский odds_1x2,
    model_inputs.odds_dict (9.6.8 FIX, перенесено дословно)."""
    odds = match.get("odds", {})
    if isinstance(odds, dict) and odds:
        return True
    flat = match.get("odds_1x2")
    if isinstance(flat, dict) and flat:
        return True
    inputs = match.get("model_inputs")
    if isinstance(inputs, dict) and inputs.get("odds_dict"):
        return True
    return False


def resolve_classification(match_data) -> str:
    """Единственный источник правила (§1.11; ранее триплет копий).
    Приоритет core_analysis.classification, fallback verdict."""
    res = match_data.get("core_analysis") if isinstance(match_data, dict) else None
    if not isinstance(res, dict):
        res = match_data if isinstance(match_data, dict) else {}
    return str(res.get("classification") or res.get("verdict") or "")


def is_value_signal(classification: str) -> bool:
    """Единственный источник правила (§1.11): границы определений вместо
    подстрочного матча 'BET' in 'NO BET'."""
    cls = (classification or "").upper()
    return cls.startswith("HOT") or "VALUE_BET" in cls


def _check_stale(last_run, now=None) -> bool:
    """Нечитаемая дата трактуется как STALE (безопасная сторона, 9.6.8 FIX)."""
    if not last_run or last_run == "неизвестно":
        return True
    if now is None:
        now = datetime.now(MSK_TZ)
    try:
        ts = last_run.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt.astimezone(MSK_TZ)).total_seconds() / 3600
        return age > STALE_THRESHOLD_HOURS
    except Exception as e:
        logger.warning("_check_stale(%s): нечитаемая дата -> STALE: %s", last_run, e)
        return True


def _read_meta(key):
    """Читает meta-ключ через get_from_cache (§18.4). Fallback — прямой GET
    через redis_command (ранее get_key из redis_hub: FIX-9.4 — get_key не читал
    ключи, записанные через save_to_cache; после Шага 12-10 meta-ключи плоские,
    GET по Root-ключу покрывает обе формы записи)."""
    try:
        raw = get_from_cache(key)
    except Exception as e:
        logger.warning("_read_meta(%s): get_from_cache: %s", key, e)
        raw = None
    if raw is None:
        try:
            raw = redis_command(["GET", key])
        except Exception as e:
            logger.warning("_read_meta(%s): GET: %s", key, e)
            return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("_read_meta(%s): нечитаемый JSON: %s", key, e)
            return None
    return raw


# ===========================================================================
# Транспорт: SCAN и чтение истории (10.0.0 FIX)
# ===========================================================================
def scan_by_pattern(pattern):
    """SCAN с итерацией по курсору через redis_command.
    [10.0.0 FIX] Прежние вызовы несуществующих scan_keys (_scan_by_pattern
    redis_diagnostics) и _execute_upstash_cmd молча возвращали пустой список.
    Сбой транспорта теперь поднимается как NetworkError — честный exit 1."""
    keys = []
    cursor = "0"
    while True:
        raw = redis_command(["SCAN", cursor, "MATCH", pattern, "COUNT", SCAN_COUNT])
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise NetworkError(f"SCAN {pattern}: неожиданный формат ответа: {raw!r}")
        next_cursor, found = raw[0], raw[1]
        if isinstance(next_cursor, bytes):
            next_cursor = next_cursor.decode("utf-8")
        cursor = str(next_cursor)
        if isinstance(found, list):
            keys.extend(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in found)
        if cursor in ("0", 0):
            break
    return keys


def get_history_record(cid):
    """Читает запись истории по canonical_id (Root-ключ history:match:<cid>)."""
    getter = getattr(redis_hub, "get_history_match", None)
    if callable(getter):
        try:
            rec = getter(cid)
            return rec if isinstance(rec, dict) else None
        except Exception as e:
            logger.warning("history:match:%s: get_history_match: %s", cid, e)
            return None
    try:
        raw = redis_command(["GET", f"history:match:{cid}"])
    except Exception as e:
        logger.warning("history:match:%s: GET: %s", cid, e)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("history:match:%s: нечитаемый JSON: %s", cid, e)
        return None
    return data if isinstance(data, dict) else None


def fetch_history_pool(all_fields):
    """Канонический читатель истории (из match_linker 9.6.8 после фикса
    транспорта): Root-скан history:match:* + GET, дедупликация по canonical_id,
    Root приоритетнее легаси. Возвращает (pool, legacy_count).
    [10.0.0 FIX] Прежний getattr-уровень молча пропускался (scan_keys не
    существует в v710) — после миграции Шага 13 пул всегда был пуст («fake empty»)."""
    try:
        keys = scan_by_pattern("history:match:*")
    except Exception as e:
        raise NetworkError(f"SCAN history:match:*: {e}")
    pool = []
    for key in keys:
        cid = key[len("history:match:"):] if isinstance(key, str) else str(key)
        rec = get_history_record(cid)
        if isinstance(rec, dict):
            rec.setdefault("canonical_id", cid)
            pool.append(rec)
    legacy = []
    for key, value in (all_fields or {}).items():
        if key.lower().startswith("history:match:"):
            data, _err = _parse_entry(key, value)
            if isinstance(data, dict):
                legacy.append(data)
    dedup = {}
    for m in pool + legacy:
        cid = (m.get("canonical_id")
               or f"{m.get('home_clean')}__{m.get('away_clean')}__{m.get('date_utc')}")
        if cid not in dedup:
            dedup[cid] = m
    return sorted(dedup.values(), key=lambda m: m.get("date_utc", "")), len(legacy)


# ===========================================================================
# Контекст прогона: один HGETALL на прогон
# ===========================================================================
# ---------------------------------------------------------------------------
# [10.0.0] Flush-поверхность (§1.11): единые скан и классификаторы для redis_flush.
# Flush импортирует отсюда — таксономия flush и аудита совпадает.
# ---------------------------------------------------------------------------
MATCH_FIELD_PREFIX = "match:"
HISTORY_KEY_PREFIX = "history:match:"


def scan_redis_state():
    """Снимок состояния Redis для flush: поля хэша (пагинированный HSCAN через
    Context.all_fields) + обособленные ключи (SCAN-паттерны SCAN_PATTERNS)."""
    fields = Context().all_fields
    standalone = []
    for pattern in SCAN_PATTERNS:
        standalone.extend(scan_by_pattern(pattern))
    return {"hash_fields": fields, "standalone_keys": sorted(set(standalone))}


def classify_hash_fields(fields):
    """Категории полей хэша. Легаси-матчи без котировок (match_no_odds) —
    отдельная категория: главные кандидаты очистки; раньше выпадали из плана
    purge, и он освобождал 4% вместо 43 МБ."""
    cats = {"match": [], "match_no_odds": [], "history": [], "meta": [],
            "meta_stale": [], "search": [], "system_hash": [], "other": []}
    for field, value in fields.items():
        if field.startswith(MATCH_FIELD_PREFIX):
            data, err = _parse_entry(field, value)
            if err or not isinstance(data, dict):
                cats["match_no_odds"].append(field)
                continue
            if _has_odds_1x2(_payload(data)):
                cats["match"].append(field)
            else:
                cats["match_no_odds"].append(field)
        elif field.startswith("history:"):
            cats["history"].append(field)
        elif field.endswith(":meta"):
            data, err = _parse_entry(field, value)
            raw = data if not err else None
            last_run = None
            if isinstance(raw, dict):
                last_run = raw.get("last_run") or raw.get("updated_at") or raw.get("timestamp")
            if last_run and not _check_stale(str(last_run)):
                cats["meta"].append(field)
            else:
                cats["meta_stale"].append(field)
        elif field.startswith("search:"):
            cats["search"].append(field)
        elif field.startswith("system:"):
            cats["system_hash"].append(field)
        else:
            cats["other"].append(field)
    return cats


def classify_standalone_keys(keys):
    """Категории обособленных Root-ключей."""
    cats = {"history": [], "system": [], "index": [], "search": [], "other": []}
    for key in keys:
        if key.startswith("history:"):
            cats["history"].append(key)
        elif key.startswith("system:"):
            cats["system"].append(key)
        elif key.startswith("index:") or key.startswith("idx:"):
            cats["index"].append(key)
        elif key.startswith("search:"):
            cats["search"].append(key)
        else:
            cats["other"].append(key)
    return cats


class Context:
    def __init__(self):
        self._fields = None

    @property
    def all_fields(self):
        if self._fields is None:
            # [10.0.0 FIX #77] Пагинированный HSCAN вместо единого HGETALL:
            # 43 МБ легаси-хэша (9925 полей) не помещаются в таймаут транспорта,
            # таймаут заглушался до {} и маскировался «0 полей». Размер страницы —
            # GATEKEEPER_DIAG_HSCAN_COUNT (по умолчанию 250). Полнота выборки
            # сверяется с HLEN: расхождение, сбой страницы или нечётные пары —
            # честный NetworkError, никакого частичного плана.
            page = max(50, int(os.environ.get("GATEKEEPER_DIAG_HSCAN_COUNT", "250") or 250))
            fields = {}
            cursor = "0"
            pages = 0
            while True:
                try:
                    raw = redis_command(["HSCAN", HASH_NAME, str(cursor), "COUNT", str(page)])
                except Exception as e:
                    raise NetworkError(f"HSCAN {HASH_NAME}: сбой транспорта: {e}")
                if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                    raise NetworkError(f"HSCAN {HASH_NAME}: неожиданный формат ответа: {raw!r}")
                next_cursor, chunk = raw[0], raw[1]
                if isinstance(next_cursor, bytes):
                    next_cursor = next_cursor.decode("utf-8")
                cursor = str(next_cursor)
                if isinstance(chunk, list):
                    if len(chunk) % 2 != 0:
                        raise NetworkError(
                            f"HSCAN: нечётное число элементов ({len(chunk)}) — нарушение пар поле/значение"
                        )
                    for i in range(0, len(chunk), 2):
                        k = chunk[i].decode("utf-8") if isinstance(chunk[i], bytes) else str(chunk[i])
                        v = chunk[i + 1].decode("utf-8") if isinstance(chunk[i + 1], bytes) else chunk[i + 1]
                        fields[k] = v
                    pages += 1
                    if pages % 10 == 0:
                        print(f"   [DIAG] HSCAN: страниц {pages}, накоплено полей {len(fields)}...")
                if cursor in ("0", 0):
                    break
            try:
                hlen = redis_command(["HLEN", HASH_NAME])
            except Exception as e:
                raise NetworkError(f"HLEN {HASH_NAME}: сбой транспорта: {e}")
            if hlen is None:
                raise NetworkError("HLEN вернул None — сбой транспорта")
            if hlen != len(fields):
                raise NetworkError(
                    f"Полнота выборки нарушена: HSCAN собрал {len(fields)} полей, HLEN={hlen}"
                )
            self._fields = fields
        return self._fields


def _is_real_match(key):
    """Фильтр мусорных ключей (из diagnostic_matches v2 / get_all_matches)."""
    if not key.startswith("match:"):
        return False
    if ":meta" in key or "idem" in key or "canary" in key or key == "match:index":
        return False
    return True


def _extract_odds_1x2(odds_raw):
    if not isinstance(odds_raw, dict):
        return {}
    sec = odds_raw.get("1x2")
    if not isinstance(sec, dict):
        return {}
    return sec.get("opening") or sec.get("open") or sec.get("current") or {}


def _extract_odds_sources(odds_raw, match_sources):
    if not isinstance(odds_raw, dict):
        return match_sources if isinstance(match_sources, list) else []
    sec = odds_raw.get("1x2")
    if isinstance(sec, dict):
        srcs = sec.get("sources")
        if srcs:
            return srcs if isinstance(srcs, list) else [srcs]
    return match_sources if isinstance(match_sources, list) else []


def _detect_duplicates(entries):
    """Детектор дублей (из diagnostic_matches): группировка по дате, попарное
    сравнение имён команд при разных canonical_id."""
    by_date = defaultdict(list)
    for key, data in entries:
        payload = _payload(data)
        d = payload.get("date_utc") or ""
        if d:
            by_date[d].append((key, payload))
    dupes = []
    for d, group in sorted(by_date.items()):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                k1, m1 = group[i]
                k2, m2 = group[j]
                if m1.get("canonical_id") and m1.get("canonical_id") == m2.get("canonical_id"):
                    continue
                n1 = {_clean(m1.get("home_team", "")), _clean(m1.get("away_team", ""))}
                n2 = {_clean(m2.get("home_team", "")), _clean(m2.get("away_team", ""))}
                if n1 and n2 and (n1 & n2):
                    dupes.append({"date": d, "keys": [k1, k2],
                                  "common": sorted(n1 & n2)})
    return dupes


# ===========================================================================
# Режимы
# ===========================================================================
def cmd_redis(ctx):
    """Секция 1 §18.2: статус Redis + Circuit Breaker + DEBUG-трейсинг ключей."""
    info = {"errors": [], "redis_configured": False, "redis_available": False,
            "circuit_breaker": "unknown", "cb_failures": 0}
    if redis_config is not None:
        try:
            info["redis_configured"] = redis_config.is_redis_configured()
            errs = redis_config.get_redis_config_errors()
            if errs:
                info["errors"].extend(errs)
            info["redis_info"] = redis_config.get_redis_info()
        except Exception as e:
            info["errors"].append(f"redis_config: {e}")
    else:
        info["errors"].append("redis_config недоступен — статус конфигурации не проверен")
    # [10.0.0 FIX §18.1] run_initialization() — write-контур хаба (миграция,
    # cleanup_expired(auto_migrate=True), запись system:health). Из read-only
    # диагностики не вызывается. Доступность — честный read-only PING.
    try:
        info["redis_available"] = bool(is_redis_available())
    except Exception as e:
        info["errors"].append(f"redis_available: {e}")
    try:
        cb = get_circuit_breaker_status()
        if isinstance(cb, dict):
            # [10.0.0 FIX] Реальная поверхность redis_hub.get_circuit_breaker_status():
            # {"open": bool, "error_count": int, "threshold": int}
            info["circuit_breaker"] = "open" if cb.get("open") else "closed"
            info["cb_failures"] = cb.get("error_count", 0)
    except Exception as e:
        info["errors"].append(f"circuit_breaker: {e}")

    fields = ctx.all_fields
    match_prefixed = [k for k in fields if k.startswith("match:")]
    canonical = [k for k in fields if "__" in k and not k.startswith("match:")]
    info["hash_fields_total"] = len(fields)
    info["match_keys"] = len(match_prefixed)
    info["canonical_keys"] = len(canonical)
    info["raw_canonical_no_prefix"] = (len(match_prefixed) == 0 and len(canonical) > 0)
    if VERBOSE and fields:
        print(f"[DEBUG REDIS] Первые 10 ключей хэша: {list(fields.keys())[:10]}")

    print("\n=== СТАТУС REDIS И CIRCUIT BREAKER ===")
    print(f"   Redis сконфигурирован: {'✅' if info['redis_configured'] else '❌'}")
    print(f"   Доступен (run_initialization): {'✅' if info['redis_available'] else '❌'}")
    print(f"   Circuit Breaker: {info['circuit_breaker']} (failures: {info['cb_failures']})")
    print(f"   Хэш '{HASH_NAME}': {info['hash_fields_total']} полей "
          f"(match:*: {info['match_keys']}, канонических без префикса: {info['canonical_keys']})")
    if info["raw_canonical_no_prefix"]:
        print("   ⚠️ Коллектор пишет ключи сырым canonical_id без префикса 'match:'")
    for err in info["errors"]:
        print(f"   ❌ {err}")
    ok = bool(info["redis_configured"] and info["redis_available"])
    return info, ok


def cmd_sources(ctx):
    """Режим sources §18.4: мета-мониторинг коллекторов (STALE > 2ч)."""
    sources = {}
    now = datetime.now(MSK_TZ)
    print("\n" + "=" * 60)
    print("📡 ДИАГНОСТИКА ИСТОЧНИКОВ (мета-мониторинг коллекторов §18.4)")
    print("=" * 60)
    # [10.0.1 FIX] Раздельная печать: коллекторы (ключ обязателен) и загрузчик
    # истории (ключ не требуется). STALE-отчёт покрывает обе группы.
    all_sources = COLLECTORS + LOADERS
    for c in all_sources:
        is_loader = c in LOADERS
        meta = _read_meta(f"{c}:meta")
        env_key = _KEY_ENV.get(c)
        if is_loader:
            key_status = "не требуется"
        else:
            key_status = "✅" if (env_key and os.getenv(env_key)) else ("—" if not env_key else "❌")
        entry = {
            "key": key_status,
            "last_run": "нет данных",
            "stale": True,
        }
        if isinstance(meta, dict):
            payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
            last_run = (meta.get("last_run") or meta.get("last_run_at")
                        or payload.get("last_run", "неизвестно"))
            entry["last_run"] = last_run
            entry["stale"] = _check_stale(last_run, now)
            for src_field in ("total_events", "stored_matches", "created", "updated",
                              "error_count", "pages_fetched", "deduped", "enriched",
                              "mode", "status"):
                val = meta.get(src_field, payload.get(src_field))
                if val is not None:
                    entry[src_field] = val
        sources[c] = entry
        stale_mark = " ⚠️ STALE" if entry["stale"] else ""
        extras = {k: v for k, v in entry.items()
                  if k not in ("key", "last_run", "stale")}
        if is_loader:
            print(f"   ⚙ {c} [загрузчик истории]: ключ не требуется "
                  f"(football-data.co.uk, открытые CSV), last_run: {entry['last_run']}{stale_mark}")
        else:
            print(f"   📊 {c}: ключ {entry['key']}, last_run: {entry['last_run']}{stale_mark}")
        if extras and VERBOSE:
            print(f"      {extras}")

    fields = ctx.all_fields
    agg = defaultdict(int)
    for key, value in fields.items():
        if not (key.startswith("match:") or "__" in key) or key.endswith(":meta"):
            continue
        data, _err = _parse_entry(key, value)
        if not data:
            continue
        srcs = data.get("sources")
        if not isinstance(srcs, list):
            srcs = []
        for s in srcs:
            agg[s] += 1
    print("   Источники live-контура: "
          + (", ".join(f"{k}={v}" for k, v in sorted(agg.items())) or "—"))
    stale = [c for c, e in sources.items() if e["stale"]]
    ok = not stale
    if stale:
        print(f"   ⚠️ STALE-коллекторы: {', '.join(stale)}")
    return {"collectors": sources, "live_sources": dict(agg), "stale": stale}, ok


def cmd_matches(ctx, limit=DEFAULT_LIMIT):
    """Режим matches: полный дамп, категоризация готовности, детектор дублей."""
    print("\n=== ПОЛНЫЙ ДАМП МАТЧЕЙ (категоризация + дубли) ===")
    entries = []
    parse_errors = 0
    for key, value in ctx.all_fields.items():
        if not _is_real_match(key):
            continue
        data, err = _parse_entry(key, value)
        if not data:
            parse_errors += 1
            logger.warning("Нечитаемая запись %s: %s", key, err)
            continue
        entries.append((key, data))

    cats = {"value_ready": [], "merged": [], "single_with_pred": [],
            "single_no_pred": [], "no_odds": []}
    for key, data in entries:
        payload = _payload(data)
        sources = _safe_json(payload.get("sources", []), [])
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list):
            sources = []
        odds_raw = _safe_json(payload.get("odds", {}), {})
        nested = _extract_odds_1x2(odds_raw)
        flat = payload.get("odds_1x2")
        one_x_two = flat if isinstance(flat, dict) else nested
        odds_sources = _extract_odds_sources(odds_raw, sources)
        predictions = _safe_json(payload.get("predictions", {}), {})
        entry = {
            "key": key,
            "cid": payload.get("canonical_id", key.replace("match:", "")),
            "home": payload.get("home_team", payload.get("home_clean", "?")),
            "away": payload.get("away_team", payload.get("away_clean", "?")),
            "date": payload.get("date_utc", "?"),
            "league": payload.get("competition", "?"),
            "status": payload.get("status", "?"),
            "sources": sorted(set(list(sources) + list(odds_sources))),
            "oh": one_x_two.get("home", one_x_two.get("1", "?")),
            "od": one_x_two.get("draw", one_x_two.get("X", "?")),
            "oa": one_x_two.get("away", one_x_two.get("2", "?")),
            "has_pred": bool(predictions),
        }
        entry["has_odds"] = bool(one_x_two) or _has_odds_1x2(payload)
        total_sources = len(entry["sources"])
        if total_sources >= 2 and entry["has_odds"] and entry["has_pred"]:
            cats["value_ready"].append(entry)
        if total_sources >= 2:
            cats["merged"].append(entry)
        elif entry["has_pred"]:
            cats["single_with_pred"].append(entry)
        elif entry["has_odds"]:
            cats["single_no_pred"].append(entry)
        else:
            cats["no_odds"].append(entry)

    print(f"   Всего матчей: {len(entries)}"
          + (f", нечитаемых: {parse_errors}" if parse_errors else ""))
    titles = {"value_ready": "🔥 VALUE ENGINE READY (2+ источника + котировки + прогноз)",
              "merged": "✅ СКЛЕЕННЫЕ (2+ источника)",
              "single_with_pred": "🟡 ОДИН ИСТОЧНИК + прогноз",
              "single_no_pred": "🟠 ОДИН ИСТОЧНИК, без прогноза (есть котировки)",
              "no_odds": "⚫ БЕЗ КОТИРОВОК"}
    for cat in ("value_ready", "merged", "single_with_pred", "single_no_pred", "no_odds"):
        group = cats[cat]
        print(f"   {titles[cat]}: {len(group)}")
        if VERBOSE:
            for m in group[:limit]:
                print(f"      {m['home']} vs {m['away']} | {m['date']} | {m['status']}")
                print(f"         CID: {m['cid']} | 1X2: {m['oh']}/{m['od']}/{m['oa']} | "
                      f"источники: {', '.join(m['sources']) or '?'}")

    dupes = _detect_duplicates(entries)
    print(f"   ⚠️ Возможных дублей (разные canonical_id, совпадающие команды): {len(dupes)}")
    if VERBOSE:
        for d in dupes[:limit]:
            print(f"      {d['date']}: {d['keys']} — общие имена: {d['common']}")
    summary = {"total": len(entries), "parse_errors": parse_errors,
               "counts": {c: len(v) for c, v in cats.items()},
               "duplicates": dupes}
    ok = parse_errors == 0 and not dupes
    return summary, ok


def cmd_indexes(ctx, limit=DEFAULT_LIMIT):
    """Режим indexes: SCAN idx:* / index:* / search:results:* (10.0.0 FIX транспорта)."""
    print("\n=== ИНДЕКСЫ И ИНДЕКСОПОДОБНЫЕ КЛЮЧИ ===")
    result = {}
    for pattern in INDEX_PATTERNS:
        keys = scan_by_pattern(pattern)
        result[pattern] = {"count": len(keys), "sample": keys[:limit]}
        print(f"   {pattern}: {len(keys)}")
        if VERBOSE:
            for k in keys[:limit]:
                print(f"     - {k}")
    return result, True


def cmd_errors(ctx, limit=DEFAULT_LIMIT):
    """Режим errors: нечитаемые JSON-записи и паспорта без match_id."""
    print("\n=== ОШИБКИ И АНОМАЛИИ КОНТУРА ===")
    unreadable, no_match_id = [], []
    for key, value in ctx.all_fields.items():
        data, err = _parse_entry(key, value)
        if err is not None:
            unreadable.append((key, str(err)))
            continue
        if _is_real_match(key) and not data.get("match_id"):
            no_match_id.append(key)
    print(f"   Нечитаемых записей: {len(unreadable)}")
    for key, err in unreadable[:limit]:
        print(f"     ❌ {key}: {err}")
    print(f"   Паспортов без match_id: {len(no_match_id)}")
    for key in no_match_id[:limit]:
        print(f"     ❌ {key}")
    ok = not unreadable and not no_match_id
    return {"unreadable": len(unreadable), "no_match_id": len(no_match_id)}, ok


def cmd_sample(ctx, limit=SAMPLE_SIZE):
    """Режим sample: образцы live-матчей."""
    print("\n=== ОБРАЗЦЫ LIVE-МАТЧЕЙ ===")
    shown = 0
    for key, value in ctx.all_fields.items():
        if shown >= limit:
            break
        if not _is_real_match(key):
            continue
        data, _err = _parse_entry(key, value)
        if not data:
            continue
        payload = _payload(data)
        shown += 1
        print(f"\n[{shown}] {key}")
        print(f"    {payload.get('home_team', '?')} vs {payload.get('away_team', '?')} | "
              f"{payload.get('competition', '?')} | {payload.get('date_utc', '?')} | "
              f"{payload.get('status', '?')}")
        flat = payload.get("odds_1x2")
        odds = flat if isinstance(flat, dict) else _extract_odds_1x2(_safe_json(payload.get("odds", {}), {}))
        print(f"    1X2: {odds or '—'}")
        classification = resolve_classification(payload)
        if classification:
            print(f"    classification: {classification} "
                  f"(value-сигнал: {'да' if is_value_signal(classification) else 'нет'})")
    if shown == 0:
        print("   Живой контур пуст.")
    return {"shown": shown}, True


def cmd_propline(ctx, limit=DEFAULT_LIMIT):
    """Режим propline (из check_redis): аудит рынков odds/spreads/totals.
    [10.0.0 PERF] Один HGETALL вместо N+1 HGET — bandwidth при DATA STOP защищён."""
    print("\n=== ДИАГНОСТИКА ДАННЫХ PROPLINE (ODDS, SPREADS, TOTALS) ===")
    found = parse_errors = skipped = 0
    for key, value in ctx.all_fields.items():
        lk = key.lower()
        if "history" in lk or key.endswith(":meta"):
            skipped += 1
            continue
        data, err = _parse_entry(key, value)
        if not data:
            parse_errors += 1
            logger.warning("Нечитаемая запись %s: %s", key, err)
            continue
        payload = _payload(data)
        source = payload.get("source", "")
        source_ids = payload.get("source_ids", {})
        if not isinstance(source_ids, dict):
            source_ids = {}  # [9.6.8 FIX] source_ids: null
        sources = payload.get("sources", [])
        is_propline = (
            source == "propline"
            or "propline" in source_ids
            or "propline" in str(sources).lower()
            or any("propline" in str(v).lower() for v in source_ids.values())
        )
        if not is_propline:
            continue
        found += 1
        if VERBOSE and found <= limit:
            print(f"\n[PROPLINE RECORD #{found}] Ключ Redis: {key}")
            print(f"🔹 Соперники: {payload.get('home_team')} vs {payload.get('away_team')}")
            print(f"🔹 Лига: {payload.get('competition')}")
            print(f"📊 Секции рынков: odds={'odds' in payload}, "
                  f"spreads={'spreads' in payload}, totals={'totals' in payload}")
            print("Полный объект v710:")
            print(_truncate(json.dumps(data, indent=2, ensure_ascii=False), 4000))
        if found == limit + 1 and VERBOSE:
            print(f"\n... (вывод ограничен первыми {limit} записями)")
            break
    print(f"\n   Найдено Propline-записей: {found}; пропущено по фильтру: {skipped}; "
          f"нечитаемых: {parse_errors}")
    return {"found": found, "skipped": skipped, "parse_errors": parse_errors}, True


def cmd_keys(ctx, limit=DEFAULT_LIMIT):
    """Режим keys (из check_redis v1.1 + таксономия redis_flush): аудит контракта
    истории и карта ключей. Вердикт v1.1 с честным exit 1."""
    print("\n=== АУДИТ КОНТРАКТА ИСТОРИИ + КАРТА КЛЮЧЕЙ ===")
    fields = ctx.all_fields
    hash_history = [k for k in fields if k.lower().startswith("history")]
    print(f"   Полей history:* в хэше '{HASH_NAME}': {len(hash_history)}")

    standalone = {}
    for pattern in SCAN_PATTERNS:
        standalone[pattern] = scan_by_pattern(pattern)
    match_keys = standalone.get("history:match:*", [])
    print(f"   Standalone-ключей history:match:*: {len(match_keys)}")

    cats = {"history": [], "system": [], "index": [], "search": [], "other": []}
    for _pattern, keys in standalone.items():
        for k in keys:
            if k.startswith("history:match:"):
                if k not in cats["history"]:
                    cats["history"].append(k)
            elif k.startswith("system:"):
                cats["system"].append(k)
            elif k.startswith("index:") or k.startswith("idx:"):
                cats["index"].append(k)
            elif k.startswith("search:results:"):
                cats["search"].append(k)
            else:
                cats["other"].append(k)
    if VERBOSE:
        for cat, keys in cats.items():
            tail = f" (первые: {keys[:limit]})" if keys else ""
            print(f"   {cat}: {len(keys)}{tail}")

    # Образец записи истории: сначала хэш, иначе standalone (v1.1)
    if hash_history:
        sample_key = hash_history[0]
        data, _err = _parse_entry(sample_key, fields[sample_key])
        print(f"\nОбразец записи истории (поле хэша '{sample_key}'):")
        print(_truncate(json.dumps(data, indent=2, ensure_ascii=False) if data
                        else str(fields[sample_key])))
    elif match_keys:
        cid = match_keys[0][len("history:match:"):]
        rec = get_history_record(cid)
        print(f"\nОбразец записи истории (standalone-ключ '{match_keys[0]}'):")
        print(_truncate(json.dumps(rec, indent=2, ensure_ascii=False) if rec else "<не читается>"))

    # Вердикт о соответствии контрактов писатель/читатель (v1.1, дословно по семантике)
    if hash_history and match_keys:
        verdict, ok = "ДВОЙНОЕ ХРАНЕНИЕ — история лежит и в хэше, и standalone-ключами", False
    elif hash_history:
        verdict, ok = ("КОНТРАКТНЫЙ РАЗРЫВ — история лежит ПОЛЯМИ ХЭША, а читатели "
                       "(match_linker через SCAN 'history:match:*') ищут ОТДЕЛЬНЫЕ ключи"), False
    elif match_keys:
        verdict, ok = "Контракты совпадают — история лежит standalone-ключами", True
    else:
        verdict, ok = "История отсутствует (пустой контур после DATA STOP)", True
    print(f"\n   ВЕРДИКТ: {verdict}")
    if not ok:
        print("   ❌ Требуется действие: выравнивание писателя/читателя или "
              "зачистка через redis_flush (history-only / soft)")
    return {"hash_history_fields": len(hash_history),
            "standalone_history": len(match_keys),
            "categories": {c: len(v) for c, v in cats.items()},
            "verdict": verdict}, ok


def _fail(failures, samples, name, detail):
    failures[name] += 1
    samples[name].append(str(detail))


def cmd_history_contracts(ctx):
    """Режим history-contracts (Шаг 13): контрактный аудит standalone-истории.
    Проверки: сырой JSON без конверта (§13.1), плоский odds_1x2 '1'/'X'/'2',
    отсутствие вложенной секции odds, отсутствие predictions (§13.2),
    снапшоты odds_history с type='closing' и дедупликацией по upstream (12-7),
    csv_raw с opening-колонками, телеметрия football_data:meta (12-10),
    статус degraded (13-G1), сверка числа ключей против stored_matches."""
    print("\n=== КОНТРАКТНЫЙ АУДИТ ИСТОРИИ (ШАГ 13) ===")
    keys = scan_by_pattern("history:match:*")
    failures = defaultdict(int)
    samples = defaultdict(list)
    legacy_hash = [k for k in ctx.all_fields if k.lower().startswith("history")]

    for key in keys:
        cid = key[len("history:match:"):] if isinstance(key, str) else str(key)
        rec = get_history_record(cid)
        if rec is None:
            _fail(failures, samples, "unreadable_record", cid)
            continue
        if isinstance(rec.get("payload"), dict):
            _fail(failures, samples, "envelope_13_1", cid)
        if "odds" in rec:
            _fail(failures, samples, "nested_odds", cid)
        flat = rec.get("odds_1x2")
        if not isinstance(flat, dict) or not flat:
            _fail(failures, samples, "odds_1x2_missing", cid)
        else:
            bad_keys = set(map(str, flat)) - {"1", "X", "2"}
            if bad_keys:
                _fail(failures, samples, f"odds_1x2_bad_keys {sorted(bad_keys)}", cid)
            for v in flat.values():
                try:
                    float(v)
                except (TypeError, ValueError):
                    _fail(failures, samples, "odds_not_numeric", cid)
                    break
        if "predictions" in rec:
            _fail(failures, samples, "predictions_present_13_2", cid)
        snaps = rec.get("odds_history")
        if not isinstance(snaps, list) or not snaps:
            _fail(failures, samples, "odds_history_missing", cid)
        else:
            if not any(isinstance(s, dict) and s.get("type") == "closing" for s in snaps):
                _fail(failures, samples, "closing_snapshot_missing", cid)
            seen = set()
            for s in snaps:
                if not isinstance(s, dict):
                    continue
                stamp = (s.get("upstream") or s.get("ts")
                         or json.dumps(s, sort_keys=True, ensure_ascii=False))
                if stamp in seen:
                    _fail(failures, samples, "snapshot_duplicate_upstream_12_7", cid)
                seen.add(stamp)
        csv_raw = rec.get("csv_raw")
        if csv_raw is not None and "opening" not in str(csv_raw).lower():
            _fail(failures, samples, "csv_raw_missing_opening", cid)

    meta = _read_meta("football_data:meta")
    telemetry = {"present": meta is not None}
    if isinstance(meta, dict):
        stored = meta.get("stored_matches", meta.get("stored"))
        telemetry["stored_matches"] = stored
        telemetry["status"] = meta.get("status")
        if meta.get("status") == "degraded":
            _fail(failures, samples, "degraded_status_13G1", "football_data:meta")
        if isinstance(stored, int) and stored != len(keys):
            _fail(failures, samples, "stored_matches_mismatch",
                  f"meta={stored}, keys={len(keys)}")
    else:
        _fail(failures, samples, "football_data_meta_missing_12_10", "telemetry")
    if legacy_hash:
        _fail(failures, samples, "legacy_hash_history_present", f"{len(legacy_hash)} полей")

    ok = not failures
    print(f"   Проверено записей: {len(keys)}; проблемных проверок: {sum(failures.values())}")
    for name, cnt in sorted(failures.items()):
        print(f"     ❌ {name}: {cnt} (пример: {samples[name][:3]})")
    if ok:
        print("   ✅ Контракт Шага 13 соблюдён")
    return {"checked": len(keys), "failures": dict(failures), "telemetry": telemetry}, ok


def cmd_memory(min_free_mb=DEFAULT_MIN_FREE_MB):
    """Режим memory (§13.4): precheck свободной памяти для football_data_loader.
    Exit 1 при free_mb < min_free_mb; maxmemory=0 (квота не задана) — exit 0 с пометкой.
    [10.0.5 MEMFIX] Fail-closed: мёртвое чтение INFO (None-ответ транспорта) —
    NetworkError, а не ветка "квота не задана" (#85: зелёный свет на fake-empty)."""
    print("\n=== MEMORY PRECHECK (§13.4) ===")
    try:
        info = redis_command(["INFO", "memory"])
    except Exception as e:
        raise NetworkError(f"INFO memory: {e}")
    # [10.0.5 MEMFIX] None-ответ транспорта — НЕ "квота не задана", а мёртвое чтение.
    if info is None:
        raise NetworkError("INFO memory: транспорт вернул None (мёртвое чтение) — precheck fail-closed (#85)")
    text = info.decode("utf-8") if isinstance(info, bytes) else str(info)
    used = maxmem = None
    for line in text.splitlines():
        if line.startswith("used_memory:"):
            used = int(line.split(":", 1)[1])
        elif line.startswith("maxmemory:"):
            maxmem = int(line.split(":", 1)[1])
    # [10.0.5 MEMFIX] used_memory обязателен всегда: живой INFO его содержит.
    if used is None:
        raise NetworkError("INFO memory: used_memory не распознан — ответ усечён или нечитаем")
    result = {"min_free_mb": min_free_mb, "used_memory": used, "maxmemory": maxmem}
    # [10.0.5 MEMFIX] Кросс-проверка с независимым источником: DBSIZE.
    try:
        dbsize = redis_command(["DBSIZE"])
    except Exception as e:
        raise NetworkError(f"DBSIZE: {e}")
    if dbsize is None:
        raise NetworkError("DBSIZE: транспорт вернул None — precheck fail-closed")
    try:
        dbsize = int(dbsize)
    except (ValueError, TypeError):
        raise NetworkError(f"DBSIZE: неожиданный формат ответа: {dbsize!r}")
    result["dbsize"] = dbsize
    # [10.0.5 MEMFIX] Невозможное состояние: used=0 байт при живых ключах —
    # сигнатура мёртвого/усечённого чтения, а не реальной памяти.
    if used == 0 and dbsize > 0:
        raise NetworkError(
            f"INFO memory: used=0 при DBSIZE={dbsize} — невозможное состояние, "
            "чтение недостоверно (анти-fake-empty #85)")
    if maxmem in (None, 0):
        print(f"   ℹ maxmemory не задан (0) — квоту сервер определить нельзя; "
              f"used: {used / 1048576:.1f} MB | dbsize: {dbsize}; "
              "контроль остаётся на стороне Upstash quota")
        result["free_mb"] = None
        return result, True
    free_mb = (maxmem - used) / 1048576
    result["free_mb"] = round(free_mb, 1)
    ok = free_mb >= min_free_mb
    print(f"   used: {used / 1048576:.1f} MB | max: {maxmem / 1048576:.1f} MB | "
          f"free: {free_mb:.1f} MB | порог: {min_free_mb} MB | dbsize: {dbsize}")
    if not ok:
        print("   ❌ Недостаточно памяти — загрузчик должен быть приостановлен (§13.4)")
    return result, ok



# ===========================================================================
# Режим history-sample (read-only, 10.0.2/10.0.3)
# ===========================================================================
HISTORY_SAMPLE_MGET_CHUNK = 50   # MGET-чанк для гистограммы (§13.4: 1 команда на 50 записей)


def _extract_score(rec):
    """Счёт по канону build_payload v8.13: rec['score'] = {"home": int, "away": int}.
    Каскад проб при отсутствии корневой формы (на случай других писателей):
    payload.score -> плоские score_home/away или home_goals/away_goals -> csv_raw FTHG/FTAG.
    Возвращает (score|None, где найдено|None, список выполненных проб)."""
    probes = []
    sc = rec.get("score")
    if isinstance(sc, dict) and sc.get("home") is not None and sc.get("away") is not None:
        return {"home": sc["home"], "away": sc["away"]}, "payload.score", probes
    probes.append("payload.score")
    for h_key, a_key in (("score_home", "score_away"), ("home_goals", "away_goals")):
        h, a = rec.get(h_key), rec.get(a_key)
        if h is not None and a is not None:
            return {"home": h, "away": a}, f"payload.{h_key}/{a_key}", probes
        probes.append(f"payload.{h_key}")
    csv_raw = rec.get("csv_raw")
    if isinstance(csv_raw, dict):
        fh, fa = csv_raw.get("FTHG"), csv_raw.get("FTAG")
        if fh not in (None, "") and fa not in (None, ""):
            try:
                return {"home": int(fh), "away": int(fa)}, "csv_raw.FTHG/FTAG", probes
            except (TypeError, ValueError):
                return {"home": fh, "away": fa}, "csv_raw.FTHG/FTAG(raw)", probes
        probes.append("csv_raw.FTHG/FTAG")
    return None, None, probes


def cmd_history_sample(ctx, limit=DEFAULT_LIMIT, sample_league=None):
    """[10.0.4] history-sample: read-only разбор живых записей history:match:*.
    [10.0.4 FIX] Формула консистентности учитывает плоский skipped (ловушка #83).
    Сверка мета — внутренняя консистентность (§13): written+deduped+skipped == total_events.
    stored_matches печатается как информация: счётчик поргонный (прогон #23: 359 при
    2239 записей в базе), жёсткое сравнение давало бы ложный failure на инкрементах.
    Счёт — контракт v8.13 build_payload (score={"home","away"}, full_time_result);
    при промахе — дамп ключей записи и exit 1: дефект писателя, не читателя."""
    ok = True
    keys = sorted(scan_by_pattern("history:match:*"))
    print("\n=== РАЗБОР ЖИВОЙ ИСТОРИИ (HISTORY-SAMPLE) ===")
    print(f"   history:match:* ключей: {len(keys)}")
    result = {"total_keys": len(keys)}

    meta = _read_meta("football_data:meta")
    if not isinstance(meta, dict):
        if keys:
            print("   ❌ football_data:meta отсутствует при непустой истории (R-1 нарушен)")
            return {"total_keys": len(keys), "meta": "missing"}, False
        print("   ℹ История пуста и мета отсутствует — легитимное состояние до первой записи")
        return {"total_keys": 0, "meta": "missing_before_first_write"}, True

    written = meta.get("written") or 0
    deduped = meta.get("deduped") or 0
    # [10.0.4 FIX] Плоский skipped в формуле: мета v8.15 пишет поле skipped
    # (строки CSV без FTR), в #24 его не было в мете и в проверке — отсюда
    # ложный exit 1 #83 (1790+380+skipped(1)=2171). Отсутствующие поля не считаем.
    skipped_flat = meta.get("skipped") or 0
    skipped = skipped_flat + (meta.get("skipped_past") or 0) + (meta.get("skipped_future") or 0)
    total_events = meta.get("total_events")
    s_sum = written + deduped + skipped
    pending = meta.get("pending_files") or []
    if skipped_flat:
        print(f"   skipped (без FTR/битые): {skipped_flat}")
    if pending:
        print(f"   pending_files (не долиты): {pending}")
        if meta.get("status") == "degraded":
            print("   ⚠ status: degraded — предыдущий прогон прерван, требуется дозагрузка")
    last_run = meta.get("last_run", "неизвестно")
    stale = _check_stale(last_run)
    print(f"   last_run: {last_run} | stale: {'да' if stale else 'нет'}")
    if total_events is not None and s_sum != total_events:
        print(f"   ❌ Мета неконсистентна: written+deduped+skipped = {s_sum} != total_events {total_events}")
        ok = False
    else:
        print(f"   ✅ Мета консистентна: written+deduped+skipped = {s_sum} = total_events {total_events}")
    result["meta_consistent"] = ok
    result["total_events"] = total_events

    sm = meta.get("stored_matches")
    if sm is not None:
        pf = meta.get("processed_files") or []
        print(f"   stored_matches (поргонный): {sm} | обработанных файлов: {len(pf)}")
        if sm != len(keys):
            print(f"   ℹ SCAN {len(keys)} != stored_matches {sm} — норма: счётчик поргонный, "
                  f"а не накопительный (см. прогоны #22/#23)")

    # --- Гистограмма дат и топ лиг: MGET-чанками, 1 команда на 50 записей ---
    date_hist = defaultdict(int)
    league_hist = defaultdict(int)
    readable = unreadable = 0
    recs = []
    for i in range(0, len(keys), HISTORY_SAMPLE_MGET_CHUNK):
        chunk = keys[i:i + HISTORY_SAMPLE_MGET_CHUNK]
        raws = redis_command(["MGET", *chunk])
        if not isinstance(raws, list) or len(raws) != len(chunk):
            raise NetworkError(f"MGET чанк {i}: неожиданный формат ответа: {type(raws).__name__}")
        for key, raw in zip(chunk, raws):
            if raw is None:
                unreadable += 1
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, ValueError):
                unreadable += 1
                continue
            if not isinstance(data, dict):
                unreadable += 1
                continue
            payload = _payload(data)
            readable += 1
            recs.append((key, payload))
            d = str(payload.get("date_utc") or "")[:7]
            date_hist[d if d else "?"] += 1
            league_hist[str(payload.get("league_code") or "?")] += 1
    result["readable"], result["unreadable"] = readable, unreadable
    if unreadable:
        print(f"   ⚠ Нечитаемых записей: {unreadable} (битый JSON / None) — прогон не роняется")
    print(f"   Распределение дат ({readable + unreadable} записей):")
    for d in sorted(date_hist):
        print(f"     {d}: {date_hist[d]}")
    top = sorted(league_hist.items(), key=lambda x: -x[1])[:8]
    print("   Топ лиг по записям: " + ", ".join(f"{k}={v}" for k, v in top))
    result["date_histogram"] = dict(sorted(date_hist.items()))

    # --- Выборка образцов: фильтр по лиге или первые limit отсортированных ключей ---
    if sample_league:
        sl = str(sample_league).strip().lower()
        picked = []
        for key, payload in recs:
            lg = str(payload.get("league_code") or "").lower()
            comp = str(payload.get("competition") or "").lower()
            if sl in (lg, comp):
                picked.append((key, payload))
            if len(picked) >= limit:
                break
        if not picked:
            print(f"   ℹ --sample-league {sample_league}: не найдено за обзор {len(recs)} записей "
                  f"(доп. GET не тратятся: выборка уже в MGET-чанках)")
    else:
        picked = recs[:limit]

    score_ok = score_missing = 0
    key_dump_done = False
    for n, (key, payload) in enumerate(picked, 1):
        print(f"\n   --- Образец {n}: {key.replace('history:match:', '')} ---")
        print(f"     лига: {payload.get('league_code') or '?'} | дата: {payload.get('date_utc') or '?'} | "
              f"статус: {payload.get('status') or '?'} | FTR: {payload.get('full_time_result') or '—'}")
        score, where, probes = _extract_score(payload)
        if score is not None:
            score_ok += 1
            print(f"     счёт: {score['home']}-{score['away']} (источник: {where})")
        elif (payload.get("status") or "") != "completed":
            print(f"     счёт: — (статус {payload.get('status')}, результата по природе нет)")
        else:
            score_missing += 1
            print(f"     ❌ счёт не найден; пробы: {', '.join(probes) or 'нет'}")
            if not key_dump_done:
                print(f"     дамп ключей записи ({len(payload)}): {', '.join(sorted(payload.keys()))}")
                cr0 = payload.get("csv_raw")
                if isinstance(cr0, dict):
                    print(f"     csv_raw FTHG/FTAG: {cr0.get('FTHG')!r} / {cr0.get('FTAG')!r}")
                key_dump_done = True
        stats = payload.get("stats") or {}
        xg_h, xg_a = stats.get("xg_home"), stats.get("xg_away")
        if xg_h is None and xg_a is None:
            print("     xg: None / None (колонок xG нет в CSV этой лиги/сезона — норма)")
        else:
            print(f"     xg: {xg_h} / {xg_a}")
        o1x2 = payload.get("odds_1x2")
        print(f"     odds_1x2: {json.dumps(o1x2, ensure_ascii=False) if o1x2 else '—'}")
        ou = payload.get("odds_over_under") or {}
        ou25 = ou.get("2.5") if isinstance(ou, dict) else None
        closing = ou25.get("closing") if isinstance(ou25, dict) else None
        hist_len = len(ou25.get("odds_history") or []) if isinstance(ou25, dict) else 0
        print(f"     O/U 2.5: {json.dumps(closing, ensure_ascii=False) if closing else '— (нет closing в CSV)'}"
              f" | снапшотов: {hist_len}")
        print(f"     O/U 1.5 / 3.5: {ou.get('1.5')} / {ou.get('3.5')} (None — резерв линий, норма v8.13)")
        ah = payload.get("odds_asian_handicap")
        if isinstance(ah, dict) and ah.get("closing"):
            print(f"     AH: line {ah.get('line')} | closing {json.dumps(ah['closing'], ensure_ascii=False)}"
                  f" | снапшотов: {len(ah.get('odds_history') or [])}")
        else:
            print("     AH: — (нет в CSV)")
        cr = payload.get("csv_raw")
        print(f"     csv_raw: {len(cr) if isinstance(cr, dict) else '—'} полей")

    if picked:
        result["samples"] = len(picked)
        result["score_found"] = score_ok
        result["score_missing_completed"] = score_missing
        if score_missing:
            print(f"\n   ❌ Счёт отсутствует в {score_missing} из {len(picked)} сыгранных образцов — "
                  f"дамп ключей выше; поверхность: писатель (gatekeeper_hub.upsert_match), не читатель")
            ok = False
        else:
            print(f"\n   ✅ История: разбор чист (счёт найден в {score_ok}/{len(picked)} образцов)")
    return result, ok


# ===========================================================================
# CLI
# ===========================================================================
def build_parser():
    parser = argparse.ArgumentParser(
        description=f"Единая диагностика Gatekeeper-AI v{__version__} (read-only)")
    parser.add_argument("mode_pos", nargs="?", default=None, choices=MODES,
                        help="Режим (позиционно или через --mode)")
    parser.add_argument("--mode", dest="mode_opt", choices=MODES)
    for m in MODES:
        if m != "full":
            parser.add_argument(f"--{m}", dest="mode_flag", action="store_const", const=m,
                                help="legacy-совместимость со старыми воркфлоу")
    parser.add_argument("--json", action="store_true", help="Отчёт в JSON")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="Лимит вывода записей (propline/sample/keys/matches/indexes)")
    parser.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB,
                        help="Порог свободной памяти для режима memory (§13.4)")
    parser.add_argument("--sample-league", dest="sample_league", default=None,
                        help="history-sample: фильтр образцов по league_code/competition")
    return parser


def main(argv=None):
    global VERBOSE
    args = build_parser().parse_args(argv)
    mode = args.mode_pos or args.mode_opt or args.mode_flag or "full"

    ctx = Context()
    results = {"mode": mode, "timestamp": _now_msk(), "version": __version__}
    exit_code = 0

    if not is_redis_available():
        print("❌ Redis недоступен (лестница §1.10 пуста или транспорт отверг соединение)")
        sys.exit(1)

    def run(name, fn, kw):
        nonlocal exit_code
        try:
            res, ok = fn(**kw)
            results[name] = res
            if not ok:
                exit_code = 1
        except NetworkError as e:
            results[name] = {"error": str(e)}
            print(f"❌ Сбой секции {name}: Redis недоступен: {e}")
            exit_code = 1

    if mode == "full":
        VERBOSE = False
        run("redis", cmd_redis, {"ctx": ctx})
        run("sources", cmd_sources, {"ctx": ctx})
        run("indexes", cmd_indexes, {"ctx": ctx})
        run("errors", cmd_errors, {"ctx": ctx})
        run("matches", cmd_matches, {"ctx": ctx})
        run("sample", cmd_sample, {"ctx": ctx})
        run("propline", cmd_propline, {"ctx": ctx, "limit": 3})
        run("keys", cmd_keys, {"ctx": ctx})
        run("history-contracts", cmd_history_contracts, {"ctx": ctx})
        run("memory", cmd_memory, {})
    else:
        dispatch = {
            "redis": (cmd_redis, {"ctx": ctx}),
            "sources": (cmd_sources, {"ctx": ctx}),
            "matches": (cmd_matches, {"ctx": ctx, "limit": args.limit}),
            "indexes": (cmd_indexes, {"ctx": ctx, "limit": args.limit}),
            "errors": (cmd_errors, {"ctx": ctx, "limit": args.limit}),
            "sample": (cmd_sample, {"ctx": ctx, "limit": args.limit}),
            "propline": (cmd_propline, {"ctx": ctx, "limit": args.limit}),
            "keys": (cmd_keys, {"ctx": ctx, "limit": args.limit}),
            "history-contracts": (cmd_history_contracts, {"ctx": ctx}),
            "history-sample": (cmd_history_sample, {"ctx": ctx, "limit": args.limit,
                                                     "sample_league": args.sample_league}),
            "memory": (cmd_memory, {"min_free_mb": args.min_free_mb}),
        }
        fn, kw = dispatch[mode]
        run(mode, fn, kw)

    print("\n=== ИТОГОВАЯ СВОДКА ===")
    print(f"   Режим: {mode} | Версия: {__version__} | Метка: {_now_msk()}")
    print(f"   Exit code: {exit_code}")
    if args.json:
        print("\n=== JSON ОТЧЁТ ===")
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()