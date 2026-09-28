# gatekeeper_hub.py
"""
Единый хаб Gatekeeper-AI v600-prod.
Все операции с данными матчей проходят только через этот модуль.

Обновления V6.0-prod:
  - CAS-версионирование (поле version) внутри patch_match
  - Шардированный индекс по датам (match:index:YYYYMMDD)
  - Очистка по завершению: удаление матчей после date_utc + 2 часа или status == completed
  - TTL исключён: нет поля expires_at, нет CLEANUP_TTL_DAYS
  - run_initialization() — health-check + миграция + очистка + reset_circuit_breaker
  - get_matches_by_date_range() — чтение через шарды
  - save_search_results() — сохранение результатов поиска
  - migrate_schema() — добавление version/schema_version + миграция старых полей
  - batch_upsert_matches() — передача всех extra-полей (включая source_ids)
  - Единые секции: odds, predictions, stats, h2h, source_ids
  - patch_match: умный мерж odds (не перезаписывает число на "-")
  - find_match_by_source_id() — поиск матча по source_ids
  - _remove_from_index: очистка шардов по завершению матчей
  - Фильтр будущих матчей: upsert_match отклоняет прошедшие матчи
  - Совместимость: patch_match(canonical_id, section, data, source)

v600-prod-hotfix:
  - run_initialization: один get_all_fields() для миграции + очистки
  - cleanup_expired: батч-удаление из шардов (1 read + 1 write на шард, не 24 на матч)
  - _remove_from_index: фикс пропуска сегодняшнего шарда (i == 0)
  - _update_health_metric_batch: одна запись вместо 4
  - _increment_metric: отложенный счётчик (не дёргает Redis на каждый матч)
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Set

from redis_hub import (
    save_to_cache,
    get_from_cache,
    delete_from_cache,
    get_all_fields,
    batch_get_from_cache,
    is_redis_available,
    reset_circuit_breaker,
)
from search_module import clean_team_name

MSK_TIMEZONE = timezone(timedelta(hours=3))
MATCH_PREFIX = "match:"
INDEX_FIELD = "match:index"
MAX_CAS_RETRIES = 3
MAX_HISTORY_ENTRIES = 20

# Очистка: буфер после начала матча (90 мин + перерыв + овертайм + пенальти)
MATCH_FINISH_BUFFER_HOURS = 2
INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7  # Смотреть вперёд на 7 дней (SharpAPI грузит на неделю)

# Секции, которые мержатся как dict (а не перезаписываются целиком)
MERGE_SECTIONS = ("odds", "stats", "extra", "predictions", "h2h", "source_ids")

# Отложенные метрики (записываются один раз в конце)
_pending_metrics: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Системные метрики (system:health)
# ---------------------------------------------------------------------------
def _update_health_metric(key: str, value: Any) -> None:
    global _pending_metrics
    if isinstance(value, (int, float)):
        _pending_metrics[key] = value
    elif isinstance(value, dict):
        _pending_metrics.update(value)
    else:
        _pending_metrics[key] = str(value)


def _flush_health_metrics() -> None:
    """Записывает накопленные метрики одним вызовом."""
    global _pending_metrics
    if not _pending_metrics:
        return
    health = get_from_cache("system:health") or {}
    if not isinstance(health, dict):
        health = {}
    health.update(_pending_metrics)
    save_to_cache("system:health", health)
    _pending_metrics = {}


def _increment_metric(key: str) -> None:
    global _pending_metrics
    _pending_metrics[key] = _pending_metrics.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Внутренние утилиты
# ---------------------------------------------------------------------------
def _now_msk() -> str:
    return datetime.now(MSK_TIMEZONE).isoformat()


def _make_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    home_c = clean_team_name(home_team).replace(" ", "_")
    away_c = clean_team_name(away_team).replace(" ", "_")
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y%m%d")
    except Exception:
        date_str = "unknown"
    return f"{home_c}__{away_c}__{date_str}"


def _make_field_name(canonical_id: str) -> str:
    return f"{MATCH_PREFIX}{canonical_id}"


def _is_future_match(date_utc: str) -> bool:
    """Возвращает True, если матч ещё не начался (date_utc >= now_utc). Если даты нет — считаем будущим."""
    if not date_utc:
        return True
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True  # если не можем распарсить — пропускаем


# ---------------------------------------------------------------------------
# Публичные хелперы для коллекторов (правило 1.11, 12.9)
# ---------------------------------------------------------------------------
def now_msk() -> str:
    return _now_msk()


def is_future_match(date_utc: str) -> bool:
    return _is_future_match(date_utc)


def normalize_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        if isinstance(date_str, (int, float)):
            dt = datetime.fromtimestamp(float(date_str), tz=timezone.utc)
        elif isinstance(date_str, str):
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return ""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""


def save_meta(collector: str, **kwargs) -> None:
    if not collector:
        return
    field_id = f"{collector}:meta"
    meta = get_from_cache(field_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["last_run"] = _now_msk()
    meta.update(kwargs)
    save_to_cache(field_id, meta)


def _is_numeric(val) -> bool:
    if val is None or val == "-" or val == "":
        return False
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def _merge_odds(existing: dict, new: dict) -> dict:
    for key in ("home", "draw", "away"):
        new_val = new.get(key, "-")
        cur_val = existing.get(key, "-")
        if new_val == "-" or new_val is None:
            continue
        if cur_val == "-" or cur_val is None:
            existing[key] = new_val
        elif _is_numeric(new_val) and _is_numeric(cur_val):
            if float(new_val) > float(cur_val):
                existing[key] = new_val
        else:
            existing[key] = new_val
    if "source" in new:
        existing["source"] = new["source"]
    if "updated_at" in new:
        existing["updated_at"] = new["updated_at"]
    return existing


def _merge_source_ids(existing: dict, new: dict) -> dict:
    for key, val in new.items():
        if val is not None and val != "":
            existing[key] = val
    return existing


def _validate_section(section: str, data: Any) -> bool:
    if not isinstance(data, dict):
        print(f"[HUB VALIDATE] Секция '{section}': данные не являются dict")
        return False

    if section == "odds":
        for key in ("home", "draw", "away"):
            val = data.get(key, "-")
            if val == "-" or val is None:
                continue
            try:
                float(val)
            except (ValueError, TypeError):
                print(f"[HUB VALIDATE] Секция 'odds': поле '{key}'='{val}' не число")
                return False
        return True

    if section == "score":
        if data.get("home") is None and data.get("away") is None:
            return True
        for key in ("home", "away"):
            val = data.get(key)
            if val is not None:
                try:
                    int(val)
                except (ValueError, TypeError):
                    print(f"[HUB VALIDATE] Секция 'score': поле '{key}'='{val}' не int")
                    return False
        return True

    if section == "stats":
        for key, val in data.items():
            if val is None:
                continue
            if isinstance(val, (int, float)):
                continue
            try:
                float(val)
            except (ValueError, TypeError):
                if isinstance(val, str):
                    continue
                print(f"[HUB VALIDATE] Секция 'stats': поле '{key}' тип {type(val).__name__}")
                return False
        return True

    return True


def _init_match_object(home_team, away_team, date_utc, competition="", country="", status="scheduled", source="unknown") -> dict:
    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    canonical_id = _make_canonical_id(home_team, away_team, date_utc)
    now = _now_msk()

    return {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_clean,
        "away_clean": away_clean,
        "competition": competition,
        "country": country,
        "date_utc": date_utc,
        "status": status,
        "score": None,
        "odds": {},
        "predictions": {},
        "stats": {},
        "h2h": {},
        "source_ids": {},
        "extra": {},
        "sources": [source],
        "section_history": [],
        "version": 0,
        "schema_version": "v600",
        "timestamp": now,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Шардированный индекс
# ---------------------------------------------------------------------------
def _get_shard_key(date_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        date_fmt = dt.strftime("%Y%m%d")
        return f"match:index:{date_fmt}"
    except Exception:
        return "match:index:unknown"


def _get_all_shard_keys() -> List[str]:
    """Возвращает все ключи шардов от -LOOKBACK до +LOOKAHEAD дней (включая сегодня)."""
    today = datetime.now(MSK_TIMEZONE).date()
    keys = []
    for i in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        d = today + timedelta(days=i)
        keys.append(f"match:index:{d.strftime('%Y%m%d')}")
    return keys


def _update_index(canonical_id: str, date_utc: str = "") -> None:
    # Legacy плоский индекс
    index = get_from_cache(INDEX_FIELD)
    if index is None:
        index = []
    if not isinstance(index, list):
        index = []
    if canonical_id not in index:
        index.append(canonical_id)
        save_to_cache(INDEX_FIELD, index)

    # Шардированный индекс
    if date_utc:
        shard_key = _get_shard_key(date_utc)
        shard = get_from_cache(shard_key)
        if shard is None:
            shard = []
        if not isinstance(shard, list):
            shard = []
        if canonical_id not in shard:
            shard.append(canonical_id)
            save_to_cache(shard_key, shard)


def _remove_from_index(canonical_id: str, date_utc: str = "") -> None:
    """Удаляет canonical_id из плоского индекса и всех шардов."""
    index = get_from_cache(INDEX_FIELD)
    if index and isinstance(index, list) and canonical_id in index:
        index.remove(canonical_id)
        save_to_cache(INDEX_FIELD, index)

    for shard_key in _get_all_shard_keys():
        shard = get_from_cache(shard_key)
        if shard and isinstance(shard, list) and canonical_id in shard:
            shard.remove(canonical_id)
            save_to_cache(shard_key, shard)

    if date_utc:
        shard_key = _get_shard_key(date_utc)
        shard = get_from_cache(shard_key)
        if shard and isinstance(shard, list) and canonical_id in shard:
            shard.remove(canonical_id)
            save_to_cache(shard_key, shard)


def _batch_remove_from_index(ids_to_remove: Set[str], all_data: Dict[str, Any]) -> int:
    """
    Батч-удаление: читает каждый шард ОДИН раз, удаляет все ID, пишет ОДИН раз.
    Вместо 24 вызовов на матч — 2 вызова на шард (всего ~20 вызовов).
    """
    if not ids_to_remove:
        return 0

    # Плоский индекс
    index = all_data.get(INDEX_FIELD)
    if isinstance(index, list):
        changed = False
        for cid in list(ids_to_remove):
            if cid in index:
                index.remove(cid)
                changed = True
        if changed:
            save_to_cache(INDEX_FIELD, index)

    # Все шарды — один read + один write на шард
    removed_total = 0
    for shard_key in _get_all_shard_keys():
        # Если шард уже в all_data — берём оттуда, иначе читаем
        shard = all_data.get(shard_key)
        if shard is None:
            shard = get_from_cache(shard_key)
        if not isinstance(shard, list):
            continue

        changed = False
        for cid in list(ids_to_remove):
            if cid in shard:
                shard.remove(cid)
                changed = True
                removed_total += 1

        if changed:
            save_to_cache(shard_key, shard)

    return removed_total


# ---------------------------------------------------------------------------
# Публичный API: создание и обновление матчей
# ---------------------------------------------------------------------------
def upsert_match(home_team, away_team, date_utc, competition="", country="", status="scheduled", source="unknown", **extra_fields) -> str:
    if not _is_future_match(date_utc):
        _increment_metric("skipped_past_matches")
        return ""

    canonical_id = _make_canonical_id(home_team, away_team, date_utc)
    field_name = _make_field_name(canonical_id)
    existing = get_from_cache(field_name)

    if existing:
        existing["competition"] = competition or existing.get("competition", "")
        existing["country"] = country or existing.get("country", "")
        existing["status"] = status or existing.get("status", "scheduled")
        existing["updated_at"] = _now_msk()
        existing["version"] = existing.get("version", 0) + 1
        if "schema_version" not in existing:
            existing["schema_version"] = "v600"
        if source not in existing.get("sources", []):
            existing.setdefault("sources", []).append(source)

        _merge_extra_fields(existing, extra_fields)
        save_to_cache(field_name, existing)
    else:
        match_obj = _init_match_object(home_team, away_team, date_utc, competition, country, status, source)
        _merge_extra_fields(match_obj, extra_fields)
        save_to_cache(field_name, match_obj)
        _update_index(canonical_id, date_utc)

    return canonical_id


def _merge_extra_fields(match_obj: dict, extra_fields: dict) -> None:
    for k, v in extra_fields.items():
        if v is None:
            continue

        if k == "odds" and isinstance(v, dict):
            existing_odds = match_obj.get("odds", {})
            if not isinstance(existing_odds, dict):
                existing_odds = {}
            match_obj["odds"] = _merge_odds(existing_odds, v)

        elif k == "source_ids" and isinstance(v, dict):
            existing_ids = match_obj.get("source_ids", {})
            if not isinstance(existing_ids, dict):
                existing_ids = {}
            match_obj["source_ids"] = _merge_source_ids(existing_ids, v)

        elif k == "predictions" and isinstance(v, dict):
            match_obj["predictions"] = v

        elif k == "h2h" and isinstance(v, dict):
            match_obj["h2h"] = v

        elif k == "stats" and isinstance(v, dict):
            existing_stats = match_obj.get("stats", {})
            if not isinstance(existing_stats, dict):
                existing_stats = {}
            existing_stats.update(v)
            match_obj["stats"] = existing_stats

        else:
            match_obj[k] = v


def patch_match(canonical_id: str, section: str, data: Any, source: str = "") -> bool:
    field_name = _make_field_name(canonical_id)

    for attempt in range(MAX_CAS_RETRIES):
        match = get_from_cache(field_name)
        if not match:
            print(f"[HUB] Матч {canonical_id} не найден для patch")
            return False

        if not _validate_section(section, data):
            print(f"[HUB] Валидация отклонена: {canonical_id}, секция '{section}', источник '{source}'")
            return False

        expected_version = match.get("version", 0)
        now = _now_msk()

        if section == "odds" and isinstance(data, dict):
            existing_odds = match.get("odds", {})
            if not isinstance(existing_odds, dict):
                existing_odds = {}
            match["odds"] = _merge_odds(existing_odds, data)

        elif section == "source_ids" and isinstance(data, dict):
            existing_ids = match.get("source_ids", {})
            if not isinstance(existing_ids, dict):
                existing_ids = {}
            match["source_ids"] = _merge_source_ids(existing_ids, data)

        elif section in ("predictions", "h2h") and isinstance(data, dict):
            match[section] = data

        elif section in ("stats", "extra") and isinstance(data, dict):
            existing_section = match.get(section, {})
            if not isinstance(existing_section, dict):
                existing_section = {}
            existing_section.update(data)
            match[section] = existing_section

        elif section == "score":
            match["score"] = data

        elif section == "status":
            match["status"] = data

        else:
            match[section] = data

        match["updated_at"] = now
        match["version"] = expected_version + 1
        if "schema_version" not in match:
            match["schema_version"] = "v600"

        if source and source not in match.get("sources", []):
            match.setdefault("sources", []).append(source)

        history = match.get("section_history", [])
        if not isinstance(history, list):
            history = []
        history.append({"section": section, "source": source or "unknown", "updated_at": now})
        if len(history) > MAX_HISTORY_ENTRIES:
            history = history[-MAX_HISTORY_ENTRIES:]
        match["section_history"] = history

        if save_to_cache(field_name, match):
            return True

        time.sleep(0.3)

    print(f"[HUB ERROR] CAS: исчерпаны {MAX_CAS_RETRIES} попытки для {canonical_id}")
    _increment_metric("cas_conflicts_total")
    return False


def patch_match_by_teams(home_team, away_team, date_utc, section, data, source="") -> bool:
    canonical_id = _make_canonical_id(home_team, away_team, date_utc)
    return patch_match(canonical_id, section, data, source)


def batch_upsert_matches(matches: List[dict], source: str = "producer") -> dict:
    stats = {"total": len(matches), "created": 0, "updated": 0, "skipped_past": 0, "deduped": 0}

    all_data = get_all_fields()
    source_id_index = {}
    for key, payload in all_data.items():
        if not key.startswith("match:") or key.startswith("match:index"):
            continue
        if not isinstance(payload, dict):
            continue
        sids = payload.get("source_ids", {})
        if not isinstance(sids, dict):
            continue
        cid = payload.get("canonical_id", key.replace("match:", ""))
        for src_name, src_id in sids.items():
            if src_id:
                source_id_index.setdefault(src_name, {})[src_id] = cid

    for m in matches:
        home = m.get("home_team", "")
        away = m.get("away_team", "")
        date = m.get("date_utc", m.get("date", ""))
        if not home or not away or not date:
            continue

        if not _is_future_match(date):
            stats["skipped_past"] += 1
            continue

        new_source_ids = m.get("source_ids", {})
        if isinstance(new_source_ids, dict):
            deduped = False
            for src_name, src_id in new_source_ids.items():
                if src_id and src_name in source_id_index and src_id in source_id_index[src_name]:
                    existing_cid = source_id_index[src_name][src_id]
                    existing_field = _make_field_name(existing_cid)
                    existing = get_from_cache(existing_field)
                    if existing:
                        existing["updated_at"] = _now_msk()
                        existing["version"] = existing.get("version", 0) + 1
                        if source not in existing.get("sources", []):
                            existing.setdefault("sources", []).append(source)
                        standard_keys = {
                            "home_team", "away_team", "date_utc", "date",
                            "competition", "league", "country", "status", "score",
                        }
                        extra_fields = {}
                        for k, v in m.items():
                            if k not in standard_keys and v is not None:
                                extra_fields[k] = v
                        _merge_extra_fields(existing, extra_fields)
                        save_to_cache(existing_field, existing)
                        stats["deduped"] += 1
                        deduped = True
                    break
            if deduped:
                continue

        field_name = _make_field_name(_make_canonical_id(home, away, date))
        existing = get_from_cache(field_name)
        if existing:
            stats["updated"] += 1
        else:
            stats["created"] += 1

        standard_keys = {
            "home_team", "away_team", "date_utc", "date",
            "competition", "league", "country", "status", "score",
        }
        extra_fields = {}
        for k, v in m.items():
            if k not in standard_keys and v is not None:
                extra_fields[k] = v

        upsert_match(
            home_team=home, away_team=away, date_utc=date,
            competition=m.get("competition", m.get("league", "")),
            country=m.get("country", ""),
            status=m.get("status", "scheduled"),
            source=source,
            **extra_fields,
        )

        if isinstance(new_source_ids, dict):
            new_cid = _make_canonical_id(home, away, date)
            for src_name, src_id in new_source_ids.items():
                if src_id:
                    source_id_index.setdefault(src_name, {})[src_id] = new_cid

    return stats


# ---------------------------------------------------------------------------
# Публичный API: чтение матчей
# ---------------------------------------------------------------------------
def get_match(canonical_id: str) -> Optional[dict]:
    return get_from_cache(_make_field_name(canonical_id))


def get_match_by_teams(home_team, away_team, date_utc) -> Optional[dict]:
    return get_match(_make_canonical_id(home_team, away_team, date_utc))


def get_all_matches() -> List[dict]:
    index = get_from_cache(INDEX_FIELD)
    if not index or not isinstance(index, list):
        return []
    match_keys = [_make_field_name(mid) for mid in index]
    matches_raw = batch_get_from_cache(match_keys)
    matches = []
    for key, val in matches_raw.items():
        if val is not None and isinstance(val, dict):
            matches.append(val)
    return matches


def get_matches_by_date_range(days: Optional[int] = None, days_ahead: Optional[int] = None) -> Dict[str, Any]:
    if days is None:
        days = INDEX_LOOKBACK_DAYS
    if days_ahead is None:
        days_ahead = INDEX_LOOKAHEAD_DAYS

    today = datetime.now(MSK_TIMEZONE).date()
    all_ids = set()

    for i in range(days):
        d = today - timedelta(days=i)
        date_fmt = d.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = get_from_cache(shard_key)
        if isinstance(shard, list):
            all_ids.update(shard)

    for i in range(1, days_ahead + 1):
        d = today + timedelta(days=i)
        date_fmt = d.strftime("%Y%m%d")
        shard_key = f"match:index:{date_fmt}"
        shard = get_from_cache(shard_key)
        if isinstance(shard, list):
            all_ids.update(shard)

    if not all_ids:
        flat = get_from_cache(INDEX_FIELD)
        if isinstance(flat, list):
            all_ids = set(flat)

    if not all_ids:
        return {}

    match_keys = [_make_field_name(mid) for mid in all_ids]
    matches_raw = batch_get_from_cache(match_keys)

    matches: Dict[str, Any] = {}
    for key, val in matches_raw.items():
        if val is None:
            continue
        if isinstance(val, dict):
            matches[key.replace("match:", "")] = val
    return matches


def get_matches_count() -> int:
    index = get_from_cache(INDEX_FIELD)
    if not index or not isinstance(index, list):
        return 0
    return len(index)


def find_match_fuzzy(home_team, away_team) -> Optional[dict]:
    home_clean = clean_team_name(home_team).lower()
    away_clean = clean_team_name(away_team).lower()
    for m in get_all_matches():
        mh = m.get("home_clean", "").lower()
        ma = m.get("away_clean", "").lower()
        if mh == home_clean and ma == away_clean:
            return m
        if home_clean in mh and away_clean in ma:
            return m
        if mh in home_clean and ma in away_clean:
            return m
    return None


def find_match_by_source_id(source_name: str, source_id: str) -> Optional[dict]:
    if not source_name or not source_id:
        return None
    all_data = get_all_fields()
    for key, payload in all_data.items():
        if not key.startswith("match:") or key.startswith("match:index"):
            continue
        if not isinstance(payload, dict):
            continue
        sids = payload.get("source_ids", {})
        if isinstance(sids, dict) and sids.get(source_name) == source_id:
            return payload
    return None


# ---------------------------------------------------------------------------
# Миграция схемы
# ---------------------------------------------------------------------------
def migrate_schema(all_data: Optional[Dict[str, Any]] = None) -> int:
    """
    Добавляет version, schema_version, section_history к старым матчам.
    Принимает all_data от run_initialization, чтобы не делать второй HGETALL.
    """
    if all_data is None:
        all_data = get_all_fields()

    count = 0
    for key, payload in all_data.items():
        if not key.startswith("match:") or key.startswith("match:index"):
            continue
        if not isinstance(payload, dict):
            continue
        changed = False

        if "version" not in payload:
            payload["version"] = 0
            changed = True
        if "schema_version" not in payload:
            payload["schema_version"] = "v600"
            changed = True
        if "section_history" not in payload:
            payload["section_history"] = []
            changed = True

        old_pred = payload.pop("bzzoiro_prediction", None)
        if old_pred and isinstance(old_pred, dict):
            if not payload.get("predictions"):
                payload["predictions"] = old_pred
            changed = True

        old_h2h = payload.pop("bzzoiro_h2h", None)
        if old_h2h and isinstance(old_h2h, dict):
            if not payload.get("h2h"):
                payload["h2h"] = old_h2h
            changed = True

        source_id_keys = {
            "sharpapi_event_id": "sharpapi",
            "odds_api_event_id": "odds_api",
            "bzzoiro_event_id": "bzzoiro",
        }
        for old_key, source_name in source_id_keys.items():
            old_val = payload.pop(old_key, None)
            if old_val is not None:
                payload.setdefault("source_ids", {})[source_name] = old_val
                changed = True

        if "expires_at" in payload:
            del payload["expires_at"]
            changed = True

        if changed:
            save_to_cache(key, payload)
            count += 1

    _update_health_metric("last_migration_at", _now_msk())
    return count


# ---------------------------------------------------------------------------
# Очистка: завершённые матчи + 2 часа (без TTL)
# ---------------------------------------------------------------------------
def cleanup_expired(all_data: Optional[Dict[str, Any]] = None) -> dict:
    """
    Очистка Redis: удаляет завершённые матчи.
    Принимает all_data от run_initialization, чтобы не делать второй HGETALL.
    Использует _batch_remove_from_index для батч-удаления из шардов.
    """
    if all_data is None:
        all_data = get_all_fields()

    now = datetime.now(MSK_TIMEZONE)
    finished_cutoff = now - timedelta(hours=MATCH_FINISH_BUFFER_HOURS)
    deleted_count = 0
    checked = 0
    finished_count = 0
    expired_count = 0

    ids_to_remove: Set[str] = set()
    keys_to_delete: List[str] = []

    for key, payload in all_data.items():
        if not key.startswith("match:") or key.startswith("match:index"):
            continue
        if not isinstance(payload, dict):
            continue
        checked += 1

        status = payload.get("status", "")
        if status == "completed":
            cid = payload.get("canonical_id", key.replace("match:", ""))
            ids_to_remove.add(cid)
            keys_to_delete.append(key)
            finished_count += 1
            continue

        date_str = payload.get("date_utc", "")
        if date_str:
            try:
                match_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if match_dt.tzinfo is None:
                    match_dt = match_dt.replace(tzinfo=timezone.utc)
                match_dt_msk = match_dt.astimezone(MSK_TIMEZONE)

                if match_dt_msk < finished_cutoff:
                    cid = payload.get("canonical_id", key.replace("match:", ""))
                    ids_to_remove.add(cid)
                    keys_to_delete.append(key)
                    expired_count += 1
                continue
            except (ValueError, TypeError):
                pass
        # Нет даты — пропускаем

    # Батч-удаление из индексов (один read + один write на шард)
    _batch_remove_from_index(ids_to_remove, all_data)

    # Удаление самих матчей
    for key in keys_to_delete:
        delete_from_cache(key)
        deleted_count += 1

    _update_health_metric("last_cleanup_count", deleted_count)
    _update_health_metric("last_cleanup_at", now.isoformat())
    _update_health_metric("last_cleanup_finished", finished_count)
    _update_health_metric("last_cleanup_expired", expired_count)

    stats = {
        "checked": checked,
        "deleted": deleted_count,
        "finished": finished_count,
        "expired": expired_count,
        "remaining": checked - finished_count if checked > 0 else 0,
    }
    print(f"[HUB] Cleanup: deleted {deleted_count} keys "
          f"(finished={finished_count}, expired={expired_count}, "
          f"buffer={MATCH_FINISH_BUFFER_HOURS}h)")
    return stats


# ---------------------------------------------------------------------------
# Сохранение результатов поиска
# ---------------------------------------------------------------------------
def save_search_results(results: Dict[str, Any]) -> bool:
    if not results:
        return False
    payload = {
        "version": "v600",
        "timestamp": _now_msk(),
        "sender_repo": os.getenv("GITHUB_REPOSITORY", "unknown"),
        "data": results,
    }
    saved = save_to_cache("search:results:latest", payload)
    ts_key = f"search:results:{datetime.now(MSK_TIMEZONE).strftime('%Y%m%d%H%M%S')}"
    save_to_cache(ts_key, payload)
    return saved


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------
def run_initialization() -> Dict[str, Any]:
    """
    Health-check Redis + reset circuit breaker + миграция + очистка.
    Один get_all_fields() для миграции и очистки — не два.
    """
    start = time.time()
    metrics = {
        "redis_available": False,
        "circuit_breaker_reset": False,
        "migration_count": 0,
        "cleanup_count": 0,
        "init_latency_ms": 0,
    }

    print("[HUB] run_initialization: проверка Redis...")
    if not is_redis_available():
        print("[HUB WARNING] Redis недоступен. Инициализация прервана.")
        return metrics

    metrics["redis_available"] = True
    print("[HUB] run_initialization: Redis OK")

    try:
        reset_circuit_breaker()
        metrics["circuit_breaker_reset"] = True
    except Exception as e:
        print(f"[HUB WARNING] reset_circuit_breaker() не удался: {e}")

    print("[HUB] run_initialization: get_all_fields()...")
    all_data = get_all_fields()
    print(f"[HUB] run_initialization: получено {len(all_data)} полей")

    print("[HUB] run_initialization: миграция схемы...")
    metrics["migration_count"] = migrate_schema(all_data)

    print("[HUB] run_initialization: очистка завершённых матчей...")
    cleanup_result = cleanup_expired(all_data)
    metrics["cleanup_count"] = cleanup_result.get("deleted", 0)
    metrics["cleanup_finished"] = cleanup_result.get("finished", 0)
    metrics["cleanup_expired"] = cleanup_result.get("expired", 0)

    _flush_health_metrics()

    latency_ms = int((time.time() - start) * 1000)
    metrics["init_latency_ms"] = latency_ms
    _update_health_metric("init_latency_ms", latency_ms)
    _flush_health_metrics()

    print(f"[HUB] Init: Redis OK, CB reset={metrics['circuit_breaker_reset']}, "
          f"миграция {metrics['migration_count']}, "
          f"очистка {metrics['cleanup_count']}, latency {latency_ms}ms")
    return metrics

