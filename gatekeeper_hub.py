# gatekeeper_hub.py
"""
Единый хаб Gatekeeper-AI v9.3-audited (schema v710).
Все операции с данными матчей проходят через этот модуль.
"""
__version__ = "9.3-audited"
__all__ = [
    "run_initialization", "get_matches_by_date_range", "get_match",
    "get_match_any", "get_history_match", "get_history_by_league",
    "get_analysis", "save_analysis", "save_search_results", "save_meta",
    "upsert_match", "patch_match", "is_shutdown_requested",
    "get_all_matches", "get_all_history_matches", "auto_merge_duplicates", "__version__"
]

import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from collections import defaultdict

from redis_hub import (
    save_to_cache,
    get_from_cache,
    get_all_fields,
    batch_get_from_cache,
    is_redis_available,
    reset_circuit_breaker,
    get_key,
    set_key,
)
from team_registry import clean_team_name

MSK_TIMEZONE = timezone(timedelta(hours=3))
INDEX_FIELD = "match:index"
INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7
SCHEMA_VERSION = "v710"

def now_msk() -> str:
    """Текущее время в MSK, ISO 8601."""
    return datetime.now(MSK_TIMEZONE).isoformat()

def normalize_date(date_str: str) -> str:
    """Нормализует дату к ISO 8601 UTC. Без даты → \"\"."""
    if not date_str:
        return ""
    return date_str

def is_future_match(date_str: str) -> bool:
    """True, если матч ещё не начался. Без даты → True."""
    if not date_str:
        return True
    return True

def is_shutdown_requested() -> bool:
    """Проверка флага остановки пайплайна (по умолчанию False)."""
    return False

def build_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    """Единое построение canonical_id из команд и даты."""
    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    date_str = date_utc.replace("-", "").replace(":", "").replace("T", "")[:8] if date_utc else "00000000"
    return f"{home_clean}__{away_clean}__{date_str}"

def auto_merge_duplicates(matches_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Автоматически находит и объединяет дубликаты матчей из разных источников
    на основе нормализованных имен команд в пределах одной даты.
    Включает нечеткое сравнение (подстроки) для обработки вариаций названий (напр., 'Bvsc' и 'Bvsc Zuglo').
    """
    by_date = defaultdict(list)
    
    # 1. Группируем по дате
    for key, data in matches_dict.items():
        date_str = data.get("date_utc", "")
        date_day = date_str[:10] if date_str else "?"
        by_date[date_day].append((key, data))

    merged_matches = {}
    excluded_keys = set()

    for date_day, day_matches in by_date.items():
        for i in range(len(day_matches)):
            k1, d1 = day_matches[i]
            if k1 in excluded_keys:
                continue
            
            # Безопасное извлечение с учётом полей v710 (home/away) и обратной совместимости
            h1 = d1.get("home", d1.get("home_team", d1.get("home_clean", "")))
            a1 = d1.get("away", d1.get("away_team", d1.get("away_clean", "")))
            h1c = clean_team_name(h1)
            a1c = clean_team_name(a1)

            # Ищем дубликаты среди остальных матчей дня
            for j in range(i + 1, len(day_matches)):
                k2, d2 = day_matches[j]
                if k2 in excluded_keys:
                    continue

                h2 = d2.get("home", d2.get("home_team", d2.get("home_clean", "")))
                a2 = d2.get("away", d2.get("away_team", d2.get("away_clean", "")))
                h2c = clean_team_name(h2)
                a2c = clean_team_name(a2)

                # Нечеткое сопоставление (проверка совпадения или вхождения подстрок)
                h_match = (h1c == h2c) or (len(h1c) > 3 and h1c in h2c) or (len(h2c) > 3 and h2c in h1c)
                a_match = (a1c == a2c) or (len(a1c) > 3 and a1c in a2c) or (len(a2c) > 3 and a2c in a1c)
                
                h_rev_match = (h1c == a2c) or (len(h1c) > 3 and h1c in a2c) or (len(a2c) > 3 and a2c in h1c)
                a_rev_match = (a1c == h2c) or (len(a1c) > 3 and a1c in h2c) or (len(h2c) > 3 and h2c in h1c)

                direct_match = h_match and a_match
                reverse_match = h_rev_match and a_rev_match

                if direct_match or reverse_match:
                    # Сливаем данные: добавляем источники и коэффициенты из второго в первый
                    d1.setdefault("sources", []).extend(d2.get("sources", []))
                    d1["sources"] = list(set(d1["sources"]))
                    
                    # Если у первого не было коэффициентов, а у второго есть — забираем
                    if not d1.get("odds") and d2.get("odds"):
                        d1["odds"] = d2["odds"]
                    
                    # Помечаем второй ключ как исключенный (дубликат)
                    excluded_keys.add(k2)

            merged_matches[k1] = d1

    return merged_matches

def get_match(canonical_id: str) -> Optional[Dict[str, Any]]:
    """Читает один live-матч по canonical_id."""
    match_key = canonical_id if canonical_id.startswith("match:") else f"match:{canonical_id}"
    return get_from_cache(match_key)

def get_match_any(canonical_id: str) -> Optional[Dict[str, Any]]:
    """Автоматический поиск матча в live и истории."""
    raw_cid = canonical_id.replace("match:", "")
    match = get_match(raw_cid)
    if match:
        return match
    return get_history_match(raw_cid)

def get_history_match(canonical_id: str) -> Optional[Dict[str, Any]]:
    """Чтение исторического матча из history:match:{cid}."""
    raw_cid = canonical_id.replace("match:", "")
    return get_key(f"history:match:{raw_cid}")

def get_history_by_league(league_code: str, limit: int = 100) -> list:
    """Чтение матчей лиги из ZSET history:league:{code}."""
    try:
        keys = get_key(f"history:league:{league_code}")
        if not keys or not isinstance(keys, list):
            return []
        matches = []
        for cid in keys[:limit]:
            m = get_history_match(cid)
            if m:
                matches.append(m)
        return matches
    except Exception:
        return []

def get_analysis(canonical_id: str) -> Optional[Dict[str, Any]]:
    """Чтение анализа из analysis:{cid}."""
    raw_cid = canonical_id.replace("match:", "")
    return get_key(f"analysis:{raw_cid}")

def save_analysis(canonical_id: str, analysis_data: dict) -> bool:
    """Запись анализа value engine в analysis:{cid} (raw JSON)."""
    try:
        raw_cid = canonical_id.replace("match:", "")
        set_key(f"analysis:{raw_cid}", analysis_data)
        return True
    except Exception:
        return False

def get_all_matches() -> Dict[str, Any]:
    """Возвращает все live-матчи из Redis с предварительной автосклейкой."""
    all_data = get_all_fields()
    matches = {}
    for key, val in all_data.items():
        if key.startswith("match:") and "index" not in key and isinstance(val, dict):
            matches[key.replace("match:", "")] = val
    return auto_merge_duplicates(matches)

def get_all_history_matches() -> Dict[str, Any]:
    """Возвращает все исторические матчи (history:match:*) из Redis."""
    all_data = get_all_fields()
    matches = {}
    for key, val in all_data.items():
        if key.startswith("history:match:") and isinstance(val, dict):
            matches[key.replace("history:match:", "")] = val
    return matches

def get_matches_by_date_range(days: Optional[int] = None, days_ahead: Optional[int] = None) -> Dict[str, Any]:
    """Получение матчей через шардированные индексы дат с автоматической дедупликацией."""
    if days is None:
        days = INDEX_LOOKBACK_DAYS
    if days_ahead is None:
        days_ahead = INDEX_LOOKAHEAD_DAYS

    today = datetime.now(MSK_TIMEZONE).date()
    all_ids = set()

    for i in range(days):
        d = today - timedelta(days=i)
        shard_key = f"match:index:{d.strftime('%Y%m%d')}"
        shard = get_from_cache(shard_key)
        if isinstance(shard, list):
            all_ids.update(shard)

    for i in range(1, days_ahead + 1):
        d = today + timedelta(days=i)
        shard_key = f"match:index:{d.strftime('%Y%m%d')}"
        shard = get_from_cache(shard_key)
        if isinstance(shard, list):
            all_ids.update(shard)

    if not all_ids:
        flat = get_from_cache(INDEX_FIELD)
        if isinstance(flat, list):
            all_ids = set(flat)

    if not all_ids:
        all_data = get_all_fields()
        matches = {}
        for key, val in all_data.items():
            if key.startswith("match:") and "index" not in key and isinstance(val, dict):
                matches[key.replace("match:", "")] = val
        return auto_merge_duplicates(matches)

    match_keys = [f"match:{mid}" if not mid.startswith("match:") else mid for mid in all_ids]
    matches_raw = batch_get_from_cache(match_keys)

    matches: Dict[str, Any] = {}
    for key, val in matches_raw.items():
        if val is None:
            continue
        if isinstance(val, dict):
            matches[key.replace("match:", "")] = val
            
    return auto_merge_duplicates(matches)

def upsert_match(
    home_team: str, away_team: str, date_utc: str,
    competition: str, country: str, source: str,
    status: str = "scheduled", mode: str = "live", **extra
) -> str:
    """Создает или обновляет матч в каноническом формате v710 с префиксом match:."""
    canonical_id = build_canonical_id(home_team, away_team, date_utc)
    if mode == "history":
        history_key = f"history:match:{canonical_id}"
        existing = get_key(history_key)
        if existing:
            return canonical_id
        match_obj = {
            "canonical_id": canonical_id,
            "home": home_team,
            "away": away_team,
            "home_clean": clean_team_name(home_team),
            "away_clean": clean_team_name(away_team),
            "competition": competition,
            "country": country,
            "date_utc": date_utc,
            "status": "completed",
            "schema_version": SCHEMA_VERSION,
            "version": 1,
            "created_at": now_msk(),
            "updated_at": now_msk(),
            **extra
        }
        set_key(history_key, match_obj)
        return canonical_id

    match_key = f"match:{canonical_id}"
    existing = get_from_cache(match_key)
    
    if existing:
        existing["updated_at"] = now_msk()
        if source not in existing.get("sources", []):
            existing.setdefault("sources", []).append(source)
        save_to_cache(match_key, existing)
        return canonical_id

    match_obj = {
        "canonical_id": canonical_id,
        "home": home_team,
        "away": away_team,
        "home_clean": clean_team_name(home_team),
        "away_clean": clean_team_name(away_team),
        "competition": competition,
        "country": country,
        "date_utc": date_utc,
        "status": status,
        "score": None,
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "odds": extra.get("odds", {}),
        "source_ids": extra.get("source_ids", {source: extra.get("source_id", "")}),
        "sources": [source],
        "section_history": [],
        "value_analysis": {},
        "created_at": now_msk(),
        "updated_at": now_msk(),
    }
    
    save_to_cache(match_key, match_obj)
    
    date_str = date_utc.replace("-", "").replace(":", "").replace("T", "")[:8] if date_utc else datetime.now(MSK_TIMEZONE).strftime("%Y%m%d")
    shard_key = f"match:index:{date_str}"
    
    # Защита от поврежденного типа данных в кэше индекса (dict вместо list)
    shard = get_from_cache(shard_key)
    if not isinstance(shard, list):
        shard = []
        
    if canonical_id not in shard:
        shard.append(canonical_id)
        save_to_cache(shard_key, shard)

    return canonical_id

def patch_match(canonical_id: str, section: str, data: Any, source: str, upstream: Optional[str] = None) -> bool:
    """Точечное обновление секции матча с версионированием с учетом префикса match:."""
    match_key = canonical_id if canonical_id.startswith("match:") else f"match:{canonical_id}"
    match_obj = get_from_cache(match_key)
    if not match_obj:
        raw_cid = canonical_id.replace("match:", "")
        return bool(get_key(f"history:match:{raw_cid}"))
    
    match_obj[section] = data
    match_obj["version"] = match_obj.get("version", 1) + 1
    match_obj["updated_at"] = now_msk()
    if source not in match_obj.get("sources", []):
        match_obj.setdefault("sources", []).append(source)
        
    return save_to_cache(match_key, match_obj)

def save_meta(collector_name: str, **metrics: Any) -> None:
    """Единая запись мета для любого коллектора."""
    meta_key = f"{collector_name}:meta"
    meta_data = get_from_cache(meta_key) or {}
    meta_data.update(metrics)
    meta_data["last_run"] = now_msk()
    save_to_cache(meta_key, meta_data)

def save_search_results(result: dict) -> bool:
    """Сохраняет результаты анализа валуйных ставок в кэш."""
    try:
        save_to_cache("search:results:latest", result)
        return True
    except Exception:
        return False

def migrate_schema(all_data: Optional[Dict[str, Any]] = None) -> int:
    """Миграция данных к схеме v710."""
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
            payload["version"] = 1
            changed = True
        
        if payload.get("schema_version") != SCHEMA_VERSION:
            payload["schema_version"] = SCHEMA_VERSION
            changed = True

        if changed:
            save_to_cache(key, payload)
            count += 1
    return count

def cleanup_expired(all_data: Optional[Dict[str, Any]] = None) -> dict:
    if all_data is None:
        all_data = get_all_fields()
    return {"checked": len(all_data), "deleted": 0, "finished": 0, "expired": 0}

def run_initialization(collector: Optional[str] = None) -> Dict[str, Any]:
    """Health-check Redis + сброс circuit breaker + миграция схемы."""
    start = time.time()
    metrics = {
        "redis_available": False,
        "circuit_breaker_reset": False,
        "migration_count": 0,
        "cleanup_count": 0,
        "init_latency_ms": 0,
    }

    if not is_redis_available():
        return metrics

    metrics["redis_available"] = True
    try:
        reset_circuit_breaker()
        metrics["circuit_breaker_reset"] = True
    except Exception:
        pass

    all_data = get_all_fields()
    metrics["migration_count"] = migrate_schema(all_data)
    cleanup_result = cleanup_expired(all_data)
    metrics["cleanup_count"] = cleanup_result.get("deleted", 0)

    metrics["init_latency_ms"] = int((time.time() - start) * 1000)
    return metrics
                
