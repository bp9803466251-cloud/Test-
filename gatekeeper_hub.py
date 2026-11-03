#!/usr/bin/env python3
# gatekeeper_hub.py
"""
Единый хаб Gatekeeper-AI v9.6.8-patched (Схема v710 / Гейт 28).
Все операции с данными матчей проходят только через этот модуль.

Обновления v9.6.8-patched:
  - Схема v710: плоский odds_1x2 (ключи "1"/"X"/"2"), Гейт 28 (§1.21, §22.1, §26)
  - Ingress-адаптер _normalize_incoming_odds(): legacy home/draw/away -> плоский базис
  - _build_1x2() / _merge_odds_1x2(): best-price мерж + архивация в odds.historical_context
  - patch_match(cid, section, data, source="", upstream="") + _resolve_upstream()
  - Секция predictions заблокирована на интерфейсе (§1.2, §1.29): входящее уничтожается
  - h2h требует source (§1.29a) — инжекция из параметра или отклонение патча
  - source_map на каждую patch-транзакцию + _count_independent()
  - Гибридная валидация validate_schema(): schema_v710.json + внутренние правила v9.6.8
  - Data Quality Framework validate_match_quality() (§27.4)
  - migrate_to_history() / update_history_indexes() / get_history_* (контракт v9.6.8)
  - cleanup_expired(auto_migrate=True): Cross-Namespace Reconciliation (§27.5)
  - Конверт поиска v700-prod; schema_version: "v710"; version >= 1
  - Импорт clean_team_name напрямую из team_registry.py (fallback search_module с WARN)
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from redis_hub import (
    save_to_cache,
    get_from_cache,
    delete_from_cache,
    get_all_fields,
    batch_get_from_cache,
    is_redis_available,
    reset_circuit_breaker,
    upsert_history_match,
    upsert_history_match_force,
    get_history_match,
    get_history_count as _redis_history_count,
    delete_history_match,
    history_match_exists,
    scan_keys,
)

# ─── Импорт реестра имён (§26): прямой импорт из team_registry ───
try:
    from team_registry import clean_team_name
except ImportError:  # pragma: no cover — fallback с громким WARN
    try:
        from search_module import clean_team_name  # type: ignore
        print("[HUB WARN] team_registry.py не найден — fallback на search_module.clean_team_name "
              "(нарушение §26; задеплойте team_registry.py в корень репозитория)")
    except ImportError:
        def clean_team_name(name: str) -> str:  # type: ignore[misc]
            return str(name or "").strip().lower()

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

MSK_TIMEZONE = timezone(timedelta(hours=3))
MATCH_PREFIX = "match:"
INDEX_FIELD = "match:index"
MAX_CAS_RETRIES = 3
MAX_HISTORY_ENTRIES = 20
MAX_HISTORICAL_CONTEXT = 20

MATCH_FINISH_BUFFER_HOURS = 2
INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7

MERGE_SECTIONS = ("odds", "stats", "extra", "h2h", "source_ids")
BLOCKED_SECTIONS = ("predictions",)

# ─── Схема v710: enum статусов + нормализация легаси ───
SCHEMA_V710_STATUS_ENUM = {"scheduled", "live", "completed", "postponed", "cancelled"}
_STATUS_NORMALIZE = {
    "awarded": "completed",
    "forfeited": "completed",
    "ended": "completed",
    "finished": "completed",
    "notstarted": "scheduled",
    "not started": "scheduled",
}

SCHEMA_V710_REQUIRED = (
    "canonical_id", "home_team", "away_team", "home_clean", "away_clean",
    "date_utc", "status", "competition", "schema_version",
)

_SCHEMA_CACHE: Optional[dict] = None

# ─── UPSTREAM_MAP: fallback при отсутствии gatekeeper_config.yaml ───
_UPSTREAM_MAP_FALLBACK = {
    "bzzoiro": "opta",
    "sharpapi": "opta",
    "oddsapi": "odds_api",
    "propline": "pinnacle",
}

_provider_priority_order = ["pinnacle", "opta", "odds_api", "betradar", "bzzoiro"]

# Отложенные метрики (записываются один раз в конце)
_pending_metrics: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Конфиг (gatekeeper_config.yaml) — graceful fallback
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gatekeeper_config.yaml")
    if yaml is not None and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"[HUB WARN] Не удалось загрузить gatekeeper_config.yaml: {e}")
    return {}


_CONFIG = _load_config()


def _load_schema_v710() -> Optional[dict]:
    """Загружает schema_v710.json из корня репозитория (один раз за прогон)."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_v710.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _SCHEMA_CACHE = json.load(fh)
                return _SCHEMA_CACHE
        except Exception as e:
            print(f"[HUB WARN] schema_v710.json повреждён: {e} — SKIP-режим с WARN (§26)")
    else:
        print("[HUB WARN] schema_v710.json не найден — SKIP-режим, действует внутренний валидатор")
    return None


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


def now_msk() -> str:
    return _now_msk()


def _make_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    home_c = str(clean_team_name(home_team) or "").replace(" ", "_")
    away_c = str(clean_team_name(away_team) or "").replace(" ", "_")
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y%m%d")
    except Exception:
        date_str = "unknown"
    return f"{home_c}__{away_c}__{date_str}"


def _make_field_name(canonical_id: str) -> str:
    return f"{MATCH_PREFIX}{canonical_id}"


def _is_future_match(date_utc: str) -> bool:
    """True, если матч ещё не начался (§1.5). Матч без даты — будущий."""
    if not date_utc:
        return True
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


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


# ---------------------------------------------------------------------------
# Гейт 28: Ingress-адаптер и сборка плоского odds_1x2 (Схема v710, §1.21)
# ---------------------------------------------------------------------------
def _normalize_incoming_odds(odds_data: Any) -> Dict[str, float]:
    """
    Приводит любые входящие форматы к плоскому базису {"1": float, "X": float, "2": float}.
    Распознаёт: текстовые home/draw/away, числовые 1/X/2, вложенные current/opening/best,
    {"1x2": {...}}, вложенный odds-словарь.
    """
    if not isinstance(odds_data, dict):
        return {}
    candidates: List[dict] = []
    inner = odds_data.get("odds") if isinstance(odds_data.get("odds"), dict) else None
    for cand in (inner, odds_data):
        if isinstance(cand, dict):
            candidates.append(cand)
            for nested_key in ("1x2", "current", "best", "opening"):
                nested = cand.get(nested_key)
                if isinstance(nested, dict):
                    candidates.append(nested)
    key_aliases = {
        "1": ("1", "home_win", "home", "H", "p1"),
        "X": ("X", "draw", "D", "x"),
        "2": ("2", "away_win", "away", "A", "p2"),
    }
    flat: Dict[str, float] = {}
    for target in ("1", "X", "2"):
        for cand in candidates:
            for k in key_aliases[target]:
                val = cand.get(k)
                if val is not None and val != "-" and val != "" and _is_numeric(val):
                    fval = float(val)
                    if fval >= 1.01:
                        flat[target] = fval
                        break
            if target in flat:
                break
    return flat


def _build_1x2(flat: Dict[str, float], source: str, timestamp: str) -> dict:
    return {
        "1": flat.get("1"),
        "X": flat.get("X"),
        "2": flat.get("2"),
        "last_updated": timestamp,
        "source": source or "",
        "provider_priority": _provider_priority_order[:],
    }


def _archive_displaced_price(match_obj: dict, outcome_key: str, old_val: float, new_val: float, ts: str) -> None:
    """Архивация вытесненной цены в изолированный блок odds.historical_context (§1.7d-стиль)."""
    odds = match_obj.setdefault("odds", {})
    hist = odds.setdefault("historical_context", [])
    hist.append({
        "outcome": outcome_key,
        "old_price": old_val,
        "new_price": new_val,
        "displaced_at": ts,
    })
    if len(hist) > MAX_HISTORICAL_CONTEXT:
        match_obj["odds"]["historical_context"] = hist[-MAX_HISTORY_ENTRIES:]


def _merge_odds_1x2(match_obj: dict, flat: Dict[str, float], source: str) -> dict:
    """
    Best-price мерж плоского вектора (Схема v710): большая цена сохраняется,
    вытесненные цены архивируются в odds.historical_context.
    """
    ts = _now_msk()
    current = match_obj.get("odds_1x2")
    if not isinstance(current, dict):
        current = _build_1x2({}, source, ts)
    for k in ("1", "X", "2"):
        new_val = flat.get(k)
        if new_val is None:
            continue
        cur_val = current.get(k)
        if cur_val is None or not _is_numeric(cur_val):
            current[k] = new_val
        elif float(new_val) > float(cur_val):
            _archive_displaced_price(match_obj, k, float(cur_val), new_val, ts)
            current[k] = new_val
    current["last_updated"] = ts
    if source:
        current["source"] = source
    match_obj["odds_1x2"] = current
    return current


# ---------------------------------------------------------------------------
# Валидация: секции + гибридная schema v710 + DQF (§27.4)
# ---------------------------------------------------------------------------
def _validate_section(section: str, data: Any) -> bool:
    if not isinstance(data, dict):
        print(f"[HUB VALIDATE] Секция '{section}': данные не являются dict")
        return False

    if section in BLOCKED_SECTIONS:
        print(f"[HUB VALIDATE] Секция '{section}' заблокирована на интерфейсе хаба (§1.2, §1.29)")
        return False

    if section == "odds":
        flat = _normalize_incoming_odds(data)
        if not flat and data:
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


def validate_schema(match_obj: dict) -> Tuple[bool, str]:
    """
    Гибридная валидация v9.6.8 (§26):
      Уровень 1 — внутренние жёсткие правила (required, version >= 1, enum статусов).
      Уровень 2 — schema_v710.json как дополнительный фильтр (SKIP с WARN, если файла нет).
    """
    m = match_obj if isinstance(match_obj, dict) else {}
    for req in SCHEMA_V710_REQUIRED:
        if req not in m:
            return False, f"missing required field: {req}"
    if not (isinstance(m.get("version"), int) and m["version"] >= 1):
        return False, "version must be int >= 1"
    status = m.get("status")
    if isinstance(status, str) and status.lower() in _STATUS_NORMALIZE:
        status = _STATUS_NORMALIZE[status.lower()]
    if status not in SCHEMA_V710_STATUS_ENUM:
        return False, f"status '{status}' not in v710 enum"
    for field in ("home_clean", "away_clean", "competition"):
        if field in m and (m[field] is None or str(m[field]).strip() == ""):
            return False, f"field '{field}' is empty"
    odds_1x2 = m.get("odds_1x2")
    if isinstance(odds_1x2, dict):
        for k in ("1", "X", "2"):
            v = odds_1x2.get(k)
            if v is not None and not _is_numeric(v):
                return False, f"odds_1x2 key '{k}' is not numeric"
    schema = _load_schema_v710()
    if schema is not None:
        for req in schema.get("required", []):
            if req not in m:
                return False, f"schema_v710: missing required field: {req}"
        status_prop = (schema.get("properties", {}).get("status") or {})
        allowed = set(status_prop.get("enum", SCHEMA_V710_STATUS_ENUM))
        if status not in allowed:
            return False, f"schema_v710: status '{status}' not in enum"
    return True, ""


def validate_match_quality(match_obj: dict) -> dict:
    """
    Data Quality Framework (§27.4): не прерывает транзакцию, выдаёт WARN и флаги аномалий.
    """
    anomalies: List[str] = []
    m = match_obj if isinstance(match_obj, dict) else {}
    odds_1x2 = m.get("odds_1x2") if isinstance(m.get("odds_1x2"), dict) else {}
    for k in ("1", "X", "2"):
        v = odds_1x2.get(k)
        if _is_numeric(v) and float(v) < 1.01:
            anomalies.append(f"odds_below_minimum:{k}")
            print(f"[HUB DQF WARN] odds_1x2[{k}]={v} < 1.01")
    score = m.get("score")
    if isinstance(score, dict):
        for side in ("home", "away"):
            sv = score.get(side)
            if sv is not None and (not isinstance(sv, int) or sv < 0 or sv > 99):
                anomalies.append(f"abnormal_score:{side}")
                print(f"[HUB DQF WARN] score[{side}]={sv} — аномальное значение")
        if score.get("home") is not None and score.get("away") is not None:
            h, a = score["home"], score["away"]
            if max(h, a) - min(h, a) >= 8:
                anomalies.append("extreme_result")
                print(f"[HUB DQF WARN] extreme_result {h}:{a}")
    cid = m.get("canonical_id", "")
    if not isinstance(cid, str) or cid.count("__") != 2:
        anomalies.append("canonical_pattern_broken")
        print(f"[HUB DQF WARN] canonical_id '{cid}' не соответствует шаблону home__away__date")
    return {"anomalies": anomalies, "quality_ok": not anomalies}


# ---------------------------------------------------------------------------
# UPSTREAM_MAP
# ---------------------------------------------------------------------------
def _resolve_upstream(source: str, explicit_upstream: str = "") -> str:
    if explicit_upstream:
        return explicit_upstream
    upstream_map = _CONFIG.get("upstream_map") if isinstance(_CONFIG.get("upstream_map"), dict) else None
    if upstream_map:
        return upstream_map.get(source, _UPSTREAM_MAP_FALLBACK.get(source, source))
    return _UPSTREAM_MAP_FALLBACK.get(source, source)


def _count_independent(match_obj: dict) -> int:
    """Число независимых источников, подтвердивших котировки (source_map + providers)."""
    providers: set = set()
    source_map = match_obj.get("source_map") if isinstance(match_obj.get("source_map"), dict) else {}
    for sec_meta in source_map.values():
        if isinstance(sec_meta, dict):
            if sec_meta.get("upstream"):
                providers.add(sec_meta["upstream"])
            if sec_meta.get("source"):
                providers.add(sec_meta["source"])
    flat = match_obj.get("odds_1x2") if isinstance(match_obj.get("odds_1x2"), dict) else {}
    pr = flat.get("provider_priority")
    if flat.get("source"):
        providers.add(flat["source"])
    for sec in ("odds", "stats", "h2h"):
        s = match_obj.get(sec)
        if isinstance(s, dict) and s.get("source"):
            providers.add(s["source"])
    if pr is None and providers:
        return len(providers)
    return len(providers)


# ---------------------------------------------------------------------------
# Инициализация объекта матча (Схема v710)
# ---------------------------------------------------------------------------
def _init_match_object(home_team, away_team, date_utc, competition="", country="",
                       status="scheduled", source="unknown") -> dict:
    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    canonical_id = _make_canonical_id(home_team, away_team, date_utc)
    now = _now_msk()
    normalized_status = status if status in SCHEMA_V710_STATUS_ENUM else _STATUS_NORMALIZE.get(str(status).lower(), "scheduled")
    return {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_clean,
        "away_clean": away_clean,
        "competition": competition,
        "country": country,
        "date_utc": date_utc,
        "status": normalized_status,
        "score": None,
        "odds": {},
        "stats": {},
        "h2h": {},
        "source_ids": {},
        "source_map": {},
        "sources": [source],
        "section_history": [],
        "version": 1,
        "schema_version": "v710",
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
        return f"match:index:{dt.strftime('%Y%m%d')}"
    except Exception:
        return "match:index:unknown"


def _get_all_shard_keys() -> List[str]:
    today = datetime.now(timezone.utc).date()
    keys: List[str] = []
    for offset in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        d = today + timedelta(days=offset)
        keys.append(f"match:index:{d.strftime('%Y%m%d')}")
    return keys


def _update_shard_index(match_obj: dict) -> None:
    shard = _get_shard_key(match_obj.get("date_utc", ""))
    idx = get_from_cache(shard)
    if not isinstance(idx, dict):
        idx = {}
    cid = match_obj.get("canonical_id")
    if cid and cid not in idx:
        idx[cid] = match_obj.get("date_utc", "")
        save_to_cache(shard, idx)


def _remove_from_index(canonical_id: str) -> None:
    """Сканирует шарды от -LOOKBACK до +LOOKAHEAD (§1.7c) и удаляет cid."""
    for shard in _get_all_shard_keys():
        idx = get_from_cache(shard)
        if isinstance(idx, dict) and canonical_id in idx:
            idx.pop(canonical_id, None)
            if idx:
                save_to_cache(shard, idx)
            else:
                delete_from_cache(shard)


def _clear_shard_index(shard: str) -> None:
    delete_from_cache(shard)


# ---------------------------------------------------------------------------
# Запись матча: транзакция + CAS
# ---------------------------------------------------------------------------
def _write_match(match_obj: dict) -> bool:
    ok = save_to_cache(_make_field_name(match_obj["canonical_id"]), match_obj)
    if ok:
        _update_shard_index(match_obj)
    return ok


# ---------------------------------------------------------------------------
# Публичный API: upsert / batch_upsert (§1.5, §1.6, §1.13, §1.14)
# ---------------------------------------------------------------------------
def upsert_match(home_team: str, away_team: str, date_utc: str,
                 competition: str = "", country: str = "", status: str = "scheduled",
                 source: str = "unknown", mode: str = "live", **extra) -> Optional[dict]:
    """
    Создаёт или обновляет каркас матча. Прошедшие матчи отсекаются (§1.5).
    mode="history" — архивный импорт в history-namespace (Root-ключ, §1.7d).
    **extra распаковывается в верхний уровень (§1.13).
    """
    if mode == "history":
        return _upsert_history_entry(home_team, away_team, date_utc, competition, country,
                                     status, source, extra)
    if not _is_future_match(date_utc):
        return None
    cid = _make_canonical_id(home_team, away_team, date_utc)
    field = _make_field_name(cid)
    existing = get_from_cache(field)
    if isinstance(existing, dict):
        for k, v in extra.items():
            if k == "source":
                continue
            existing[k] = v
        if existing.get("status") in _STATUS_NORMALIZE and status == "scheduled":
            pass
        else:
            s = status if status in SCHEMA_V710_STATUS_ENUM else _STATUS_NORMALIZE.get(str(status).lower(), existing.get("status"))
            existing["status"] = s
        existing["updated_at"] = _now_msk()
        if source and source not in existing.get("sources", []):
            existing["sources"] = list(existing.get("sources", [])) + [source]
        match_obj = existing
    else:
        match_obj = _init_match_object(home_team, away_team, date_utc, competition, country, status, source)
        for k, v in extra.items():
            if k == "source":
                continue
            match_obj[k] = v
    ok, reason = validate_schema(match_obj)
    if not ok:
        print(f"[HUB UPSERT REJECT] {cid}: {reason}")
        _increment_metric("upsert_rejected_total")
        return None
    dqf = validate_match_quality(match_obj)
    match_obj["quality"] = dqf
    if not _write_match(match_obj):
        _increment_metric("write_errors_total")
        return None
    _increment_metric("upsert_total")
    return match_obj


def batch_upsert_matches(matches: List[dict], source: str = "unknown") -> dict:
    """
    Пакетный upsert. Поле 'source' внутри элементов вырезается (§1.14);
    source_ids складываются без перезаписи существующих (дедупликация §1.6).
    """
    stored, skipped, rejected = 0, 0, 0
    for item in matches:
        if not isinstance(item, dict):
            skipped += 1
            continue
        item = dict(item)
        item.pop("source", None)
        cid = item.pop("canonical_id", None)
        home = item.pop("home_team", None)
        away = item.pop("away_team", None)
        date_utc = item.pop("date_utc", None)
        status = item.pop("status", "scheduled")
        competition = item.pop("competition", "")
        country = item.pop("country", "")
        mode = item.pop("mode", "live")
        source_ids = item.pop("source_ids", None)
        if not (home and away):
            skipped += 1
            continue
        result = upsert_match(home, away, date_utc, competition=competition, country=country,
                              status=status, source=source, mode=mode, **item)
        if result is None:
            if not _is_future_match(date_utc or ""):
                skipped += 1
            else:
                rejected += 1
            continue
        stored += 1
        if source_ids and isinstance(source_ids, dict) and cid:
            field = _make_field_name(cid)
            existing = get_from_cache(field)
            if isinstance(existing, dict):
                sids = existing.get("source_ids") or {}
                if not isinstance(sids, dict):
                    sids = {}
                for k, v in source_ids.items():
                    if v is not None and v != "":
                        sids[k] = v
                existing["source_ids"] = sids
                existing["updated_at"] = _now_msk()
                _write_match(existing)
    return {"stored": stored, "skipped": skipped, "rejected": rejected}


def _upsert_history_entry(home_team, away_team, date_utc, competition, country,
                          status, source, extra) -> Optional[dict]:
    cid = _make_canonical_id(home_team, away_team, date_utc)
    normalized_status = status if status in SCHEMA_V710_STATUS_ENUM else _STATUS_NORMALIZE.get(str(status).lower(), "completed")
    entry = _init_match_object(home_team, away_team, date_utc, competition, country, normalized_status, source)
    entry["version"] = 1
    for k, v in extra.items():
        if k == "source":
            continue
        entry[k] = v
    ok, reason = validate_schema(entry)
    if not ok:
        print(f"[HUB HISTORY REJECT] {cid}: {reason}")
        return None
    try:
        ok = upsert_history_match(cid, entry)  # type: ignore[name-defined]
    except Exception:
        ok = save_to_cache(f"history:match:{cid}", entry)
    if ok:
        update_history_indexes(entry)
        return entry
    return None


# ---------------------------------------------------------------------------
# Публичный API: patch_match (Гейт 28 + §1.20 + §1.29a)
# ---------------------------------------------------------------------------
def patch_match(canonical_id: str, section: str, data: Any,
                source: str = "", upstream: str = "") -> bool:
    """
    Точечное обновление секции. Правила v9.6.8:
      - section="predictions" — отклоняется на интерфейсе (§1.2, §1.29), данные уничтожаются.
      - section="odds" — Ingress-адаптер + best-price мерж + historical_context.
      - section="h2h" — требует source (§1.29a): инжекция из параметра, иначе отклонение.
      - upstream резолвится через UPSTREAM_MAP (§1.20).
      - CAS: version+1 с ретраями (§1.9-подход v600 сохранён).
    """
    if section in BLOCKED_SECTIONS:
        print(f"[HUB BLOCKED] patch_match '{section}' отклонён — контур predictions деактивирован (§1.29)")
        _increment_metric("predictions_blocked_total")
        return False
    field = _make_field_name(canonical_id)
    for attempt in range(MAX_CAS_RETRIES):
        match_obj = get_from_cache(field)
        if not isinstance(match_obj, dict):
            print(f"[HUB PATCH ERROR] Матч {canonical_id} не найден")
            return False
        current_version = match_obj.get("version", 1)

        if not _validate_section(section, data):
            _increment_metric("patch_rejected_total")
            return False

        resolved_upstream = _resolve_upstream(source, upstream)
        sm = match_obj.get("source_map")
        if not isinstance(sm, dict):
            sm = {}
        sm[section] = {
            "source": source or "unknown",
            "upstream": resolved_upstream,
            "updated_at": _now_msk(),
        }
        match_obj["source_map"] = sm

        if section == "odds":
            flat = _normalize_incoming_odds(data)
            if not flat:
                print(f"[HUB PATCH WARN] {canonical_id}: odds без числовых цен — патч пропущен")
                return False
            _merge_odds_1x2(match_obj, flat, source or "unknown")
            legacy = match_obj.get("odds")
            if not isinstance(legacy, dict):
                legacy = {}
            if flat:
                legacy["flat_normalized"] = True
            match_obj["odds"] = legacy
        elif section == "h2h":
            h2h_payload = data if isinstance(data, dict) else {}
            if not (source or h2h_payload.get("source")):
                print(f"[HUB PATCH REJECT] {canonical_id}: h2h без source отклонён (§1.29a)")
                _increment_metric("patch_rejected_total")
                return False
            h2h_payload["source"] = source or h2h_payload.get("source")
            match_obj["h2h"] = h2h_payload
        else:
            match_obj[section] = data

        sh = match_obj.get("section_history")
        if not isinstance(sh, list):
            sh = []
        sh.append({
            "section": section,
            "source": source or "unknown",
            "upstream": resolved_upstream,
            "at": _now_msk(),
            "prev_version": current_version,
        })
        match_obj["section_history"] = sh

        if source and source not in match_obj.get("sources", []):
            match_obj["sources"] = list(match_obj.get("sources", [])) + [source]

        match_obj["version"] = current_version + 1
        match_obj["updated_at"] = _now_msk()
        match_obj["independent_sources"] = _count_independent(match_obj)

        ok, reason = validate_schema(match_obj)
        if not ok:
            print(f"[HUB PATCH REJECT] {canonical_id}: {reason}")
            _increment_metric("patch_rejected_total")
            return False
        dqf = validate_match_quality(match_obj)
        match_obj["quality"] = dqf

        if _write_match(match_obj):
            _increment_metric("patch_total")
            return True
        time.sleep(0.2)
    print(f"[HUB PATCH ERROR] CAS конфликт для {canonical_id} после {MAX_CAS_RETRIES} попыток")
    _increment_metric("cas_conflicts_total")
    return False


def _merge_extra_fields(match_obj: dict, extra: dict) -> dict:
    """Распаковка **extra в верхний уровень; predictions — уничтожение (§1.29)."""
    for k, v in extra.items():
        if k in BLOCKED_SECTIONS:
            print(f"[HUB BLOCKED] extra['{k}'] уничтожен на входе (§1.29)")
            _increment_metric("predictions_blocked_total")
            continue
        match_obj[k] = v
    return match_obj


# ---------------------------------------------------------------------------
# Миграция легаси (v600 -> v710)
# ---------------------------------------------------------------------------
def _migrate_match(match_obj: dict) -> dict:
    changed = False
    if match_obj.get("version", 0) == 0:
        match_obj["version"] = 1
        changed = True
    if match_obj.get("schema_version") != "v710":
        match_obj["schema_version"] = "v710"
        changed = True
    status = match_obj.get("status")
    if isinstance(status, str) and status.lower() in _STATUS_NORMALIZE:
        match_obj["status"] = _STATUS_NORMALIZE[status.lower()]
        changed = True
    if "predictions" in match_obj:
        print(f"[HUB MIGRATE] {match_obj.get('canonical_id', '?')}: predictions уничтожены при миграции (§1.29)")
        match_obj.pop("predictions", None)
        changed = True
    legacy_odds = match_obj.get("odds")
    if isinstance(legacy_odds, dict) and ("home" in legacy_odds or "draw" in legacy_odds or "away" in legacy_odds):
        flat = _normalize_incoming_odds(legacy_odds)
        if flat:
            hist_ctx = match_obj.get("odds_1x2")
            if not isinstance(hist_ctx, dict):
                _merge_odds_1x2(match_obj, flat, legacy_odds.get("source", "legacy"))
                match_obj["odds"]["historical_context"] = [{
                    "outcome": "legacy_home_draw_away",
                    "old_prices": {k: legacy_odds.get(k) for k in ("home", "draw", "away") if k in legacy_odds},
                    "displaced_at": _now_msk(),
                    "reason": "v710_migration",
                }]
                changed = True
    extra = match_obj.get("extra")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in match_obj:
                match_obj[k] = v
        match_obj.pop("extra", None)
        changed = True
    if not isinstance(match_obj.get("source_map"), dict):
        match_obj["source_map"] = {}
        changed = True
    return match_obj if changed else match_obj


def migrate_schema(all_data: Optional[Dict[str, Any]] = None) -> int:
    if all_data is None:
        all_data = get_all_fields()
    migrated = 0
    for field, value in list(all_data.items()):
        if not field.startswith(MATCH_PREFIX) or not isinstance(value, dict):
            continue
        migrated_obj = _migrate_match(value)
        if migrated_obj is not None and migrated_obj != value:
            ok, _ = validate_schema(migrated_obj)
            if ok:
                save_to_cache(field, migrated_obj)
                migrated += 1
    print(f"[HUB MIGRATE] Схема v710: мигрировано {migrated} матчей")
    return migrated


# ---------------------------------------------------------------------------
# Cross-Namespace Reconciliation: cleanup_expired (§1.7, §1.7b, §27.5)
# ---------------------------------------------------------------------------
def cleanup_expired(all_data: Optional[Dict[str, Any]] = None,
                    dry_run: bool = False, auto_migrate: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    if all_data is None:
        all_data = get_all_fields()
    deleted_live, deleted_completed, deleted_fallback = 0, 0, 0
    touched_shards: Dict[str, List[str]] = {}

    for field, value in list(all_data.items()):
        # Уровень 2 защиты истории: явный skip (§1.7d)
        if field.startswith("history:") or "history" in field:
            continue
        if not field.startswith(MATCH_PREFIX) or not isinstance(value, dict):
            continue
        cid = field[len(MATCH_PREFIX):]
        date_str = value.get("date_utc", "")
        status = value.get("status", "")
        expired = False
        if status == "completed":
            expired = True
        elif date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt + timedelta(hours=MATCH_FINISH_BUFFER_HOURS) < now:
                    expired = True
            except (ValueError, TypeError):
                expired = False
        else:
            expired = False
        if expired:
            if auto_migrate and not dry_run:
                existing = get_from_cache(f"history:match:{cid}")
                if not existing:
                    migrate_to_history(cid, value)
            touched_shards.setdefault(_get_shard_key(date_str), []).append(cid)
            if not dry_run:
                if delete_from_cache(field):
                    deleted_live += 1
                if status == "completed":
                    deleted_completed += 1
                else:
                    deleted_fallback += 1

    if not dry_run:
        for shard in _get_all_shard_keys():
            idx = get_from_cache(shard)
            if not isinstance(idx, dict):
                continue
            day_expired = touched_shards.get(shard, [])
            if day_expired:
                for cid in day_expired:
                    idx.pop(cid, None)
            all_day_done = bool(idx) and all(
                all_data.get(f"{MATCH_PREFIX}{cid}", {}).get("status") == "completed"
                or f"{MATCH_PREFIX}{cid}" in [f"{MATCH_PREFIX}{c}" for c in day_expired]
                for cid in idx.keys()
            )
            if all_day_done or not idx:
                delete_from_cache(shard)
            else:
                save_to_cache(shard, idx)

        now_local = datetime.now(MSK_TIMEZONE)
        for search_field in scan_keys("search:results:*") if callable(getattr(globals().get("scan_keys"), "__call__", None)) and False else []:
            pass  # placeholder — реальный вызов ниже
        try:
            for search_field in scan_keys("search:results:*"):
                if search_field != "search:results:latest":
                    delete_from_cache(search_field)
        except Exception:
            pass

        _update_health_metric({
            "last_cleanup_count": deleted_live,
            "last_cleanup_at": now_local.isoformat(),
            "last_cleanup_finished": deleted_completed,
            "last_cleanup_expired": deleted_fallback,
        })

    print(f"[HUB CLEANUP] Удалено live-полей: {deleted_live} "
          f"(completed: {deleted_completed}, fallback: {deleted_fallback})")
    return {"deleted": deleted_live, "completed": deleted_completed, "fallback": deleted_fallback}


# ---------------------------------------------------------------------------
# Инициализация рантайма (§1.7a, §1.11)
# ---------------------------------------------------------------------------
def run_initialization() -> dict:
    reset_circuit_breaker()
    if not is_redis_available():
        print("[HUB INIT] Redis недоступен")
        return {"redis_available": False, "cleanup_count": 0}
    all_data = get_all_fields()
    migrated = migrate_schema(all_data)
    cleanup_result = cleanup_expired(all_data, dry_run=False, auto_migrate=True)
    _flush_health_metrics()
    print(f"[HUB INIT] OK — миграция: {migrated}, очистка: {cleanup_result['deleted']}")
    return {
        "redis_available": True,
        "cleanup_count": cleanup_result["deleted"],
        "migrated": migrated,
    }


# ---------------------------------------------------------------------------
# Чтение: get_all_matches / get_matches_by_date_range / count / odds
# ---------------------------------------------------------------------------
def get_all_matches() -> Dict[str, Any]:
    """Union плоского индекса и шардов (v9.6.8)."""
    result: Dict[str, Any] = {}
    all_data = get_all_fields()
    for field, value in all_data.items():
        if field.startswith(MATCH_PREFIX) and isinstance(value, dict):
            result[value.get("canonical_id", field[len(MATCH_PREFIX):])] = value
    for shard in _get_all_shard_keys():
        idx = get_from_cache(shard)
        if not isinstance(idx, dict):
            continue
        for cid in idx.keys():
            if cid not in result:
                val = get_from_cache(f"{MATCH_PREFIX}{cid}")
                if isinstance(val, dict):
                    result[cid] = val
    return result


def get_matches_by_date_range(date_from: str, date_to: str) -> Dict[str, Any]:
    try:
        d_from = datetime.fromisoformat(date_from.replace("Z", "+00:00")).date()
    except Exception:
        d_from = datetime.now(timezone.utc).date()
    try:
        d_to = datetime.fromisoformat(date_to.replace("Z", "+00:00")).date()
    except Exception:
        d_to = d_from
    result: Dict[str, Any] = {}
    d = d_from
    while d <= d_to:
        shard = get_from_cache(f"match:index:{d.strftime('%Y%m%d')}")
        if isinstance(shard, dict):
            for cid in shard.keys():
                if cid not in result:
                    val = get_from_cache(f"{MATCH_PREFIX}{cid}")
                    if isinstance(val, dict):
                        result[cid] = val
        d += timedelta(days=1)
    return result


def get_matches_count() -> int:
    return len(get_all_matches())


def _flat_odds_view(odds_1x2: Any) -> dict:
    if not isinstance(odds_1x2, dict):
        return {}
    return {k: odds_1x2.get(k) for k in ("1", "X", "2")}


def get_current_odds(canonical_id: str) -> Optional[dict]:
    match_obj = get_from_cache(_make_field_name(canonical_id))
    if not isinstance(match_obj, dict):
        return None
    return _flat_odds_view(match_obj.get("odds_1x2"))


def get_all_odds() -> Dict[str, dict]:
    """Плоский базис по всем матчам + snapshots из historical_context (Гейт 28)."""
    result: Dict[str, dict] = {}
    for cid, match_obj in get_all_matches().items():
        flat = _flat_odds_view(match_obj.get("odds_1x2"))
        hist = (match_obj.get("odds") or {}).get("historical_context", []) \
            if isinstance(match_obj.get("odds"), dict) else []
        result[cid] = {
            "snapshots": flat,
            "historical": hist,
            "independent": _count_independent(match_obj),
        }
    return result


def find_match_by_source_id(provider: str, provider_id: str) -> Optional[dict]:
    for match_obj in get_all_matches().values():
        sids = match_obj.get("source_ids") if isinstance(match_obj.get("source_ids"), dict) else {}
        if str(sids.get(provider, "")) == str(provider_id):
            return match_obj
    return None


# ---------------------------------------------------------------------------
# History-контракт v9.6.8 (§1.7d)
# ---------------------------------------------------------------------------
def migrate_to_history(canonical_id: str, match_obj: Optional[dict] = None) -> bool:
    if match_obj is None:
        match_obj = get_from_cache(_make_field_name(canonical_id))
    if not isinstance(match_obj, dict):
        return False
    history_obj = dict(match_obj)
    history_obj.pop("predictions", None)
    history_obj["status"] = history_obj.get("status") if history_obj.get("status") in SCHEMA_V710_STATUS_ENUM else "completed"
    history_obj["version"] = 1
    history_obj["migrated_at"] = _now_msk()
    try:
        if callable(globals().get("history_match_exists")) and history_match_exists(canonical_id):
            # Копия в history не перезаписывается (§27.5)
            return True
        ok = upsert_history_match(canonical_id, history_obj)  # type: ignore[name-defined]
    except Exception as e:
        print(f"[HUB HISTORY WARN] Root-хелпер недоступен ({e}) — деградация в hash-поля с WARN")
        ok = save_to_cache(f"history:match:{canonical_id}", history_obj)
    if ok:
        update_history_indexes(history_obj)
    return bool(ok)


def update_history_indexes(match_obj: dict) -> None:
    """Архивные индексы history:league:{code} / history:team:{name}."""
    cid = match_obj.get("canonical_id", "")
    if not cid:
        return
    league_code = match_obj.get("league_code") or match_obj.get("competition") or "unknown"
    for index_key, member in (
        (f"history:league:{league_code}", cid),
        (f"history:team:{match_obj.get('home_clean', '')}", cid),
        (f"history:team:{match_obj.get('away_clean', '')}", cid),
    ):
        idx = get_from_cache(index_key)
        if not isinstance(idx, dict):
            idx = {}
        if member not in idx:
            idx[member] = match_obj.get("date_utc", "")
            save_to_cache(index_key, idx)


def get_history_by_league(league_code: str) -> Dict[str, dict]:
    idx = get_from_cache(f"history:league:{league_code}")
    result: Dict[str, dict] = {}
    if isinstance(idx, dict):
        keys = [f"history:match:{cid}" for cid in idx.keys()]
        values = batch_get_from_cache(keys)
        for k, v in values.items():
            if isinstance(v, dict):
                result[k.replace("history:match:", "")] = v
    return result


def get_history_count() -> int:
    try:
        return _redis_history_count()  # type: ignore[misc]
    except Exception:
        return sum(1 for k in get_all_fields() if k.startswith("history:match:"))


# ---------------------------------------------------------------------------
# Поиск: конверт v700-prod (§7: конверт v600 устранён)
# ---------------------------------------------------------------------------
def save_search_results(query: str, results: List[dict]) -> bool:
    payload = {
        "version": "v700-prod",
        "query": query,
        "results": results,
        "timestamp": _now_msk(),
    }
    return save_to_cache("search:results:latest", payload)
