# gatekeeper_hub.py
"""
Единый хаб Gatekeeper-AI v700-prod.
Все операции с данными матчей проходят только через этот модуль.

Обновления V7.0-prod (TO-BE):
  - TEAM_ALIASES — словарь прозвищ команд
  - detect_upstream() — определение upstream провайдера
  - Новый _merge_odds — накопление в sources[], не перезапись
  - patch_match: параметр upstream, source_map, _snapshots, _verification
  - get_current_odds() — обёртка обратной совместимости
  - get_odds_metadata() — метаданные odds (verification, sources, movement)
  - upsert_history_match() — загрузка history через хаб
  - update_history_indexes() — индексы history:team:*, history:league:*
  - save_analysis() / get_analysis() — метрики движка в analysis:{cid}
  - _betradar_consensus — cross-check SharpAPI vs OddsAPI
  - _mad_outlier — MAD outlier detection для odds

Обновления V5.0 (единый формат 1x2):
  - build_canonical_id() — публичная обёртка для загрузчика и нормализации
  - _normalize_incoming_odds() — нормализация любого входа (1x2, current, плоский) → плоский price
  - _count_independent() — подсчёт уникальных upstream в sources[]
  - _build_1x2() — создание начального 1x2 блока из плоского price
  - get_all_odds() — полные odds-данные одним вызовом для value_engine и main.py

Сохранено из v600:
  - CAS-версионирование (поле version) внутри patch_match
  - Шардированный индекс по датам (match:index:YYYYMMDD)
  - Очистка по завершению: удаление матчей после date_utc + 2 часа или status == completed
  - run_initialization() — health-check + миграция + очистка + reset_circuit_breaker
  - get_matches_by_date_range() — чтение через шарды
  - save_search_results() — сохранение результатов поиска
  - migrate_schema() — миграция старых полей
  - batch_upsert_matches() — передача всех extra-полей
  - find_match_by_source_id() — поиск матча по source_ids
"""
import os
import json
import time
import statistics
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
from search_module import clean_team_name as _base_clean_team_name

MSK_TIMEZONE = timezone(timedelta(hours=3))
MATCH_PREFIX = "match:"
HISTORY_PREFIX = "history:match:"
ANALYSIS_PREFIX = "analysis:"
INDEX_FIELD = "match:index"
MAX_CAS_RETRIES = 3
MAX_HISTORY_ENTRIES = 20
MAX_ODDS_SNAPSHOTS = 10

# Очистка: буфер после начала матча (90 мин + перерыв + овертайм + пенальти)
MATCH_FINISH_BUFFER_HOURS = 2
INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7

# Секции, которые мержатся как dict
MERGE_SECTIONS = ("odds", "stats", "extra", "predictions", "h2h", "source_ids")

# Отложенные метрики
_pending_metrics: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# TEAM_ALIASES — словарь прозвищ команд
# ---------------------------------------------------------------------------
TEAM_ALIASES = {
    # Premier League
    "manchester_united": "man",
    "man_united": "man",
    "man utd": "man",
    "manchester_city": "man_city",
    "man city": "man_city",
    "tottenham_hotspur": "tottenham",
    "tottenham": "tottenham",
    "spurs": "tottenham",
    "wolverhampton": "wolves",
    "wolverhampton_wanderers": "wolves",
    "newcastle_united": "newcastle",
    "newcastle": "newcastle",
    "west_ham_united": "west_ham",
    "west_ham": "west_ham",
    "nottm_forest": "nott_m_forest",
    "nottingham_forest": "nott_m_forest",
    "nott'm_forest": "nott_m_forest",
    "brighton_hove_albion": "brighton",
    "brighton & hove albion": "brighton",
    "leicester_city": "leicester",
    "leicester": "leicester",
    "norwich_city": "norwich",
    "norwich": "norwich",
    # La Liga
    "atletico_madrid": "atletico",
    "atletico": "atletico",
    "athletico_madrid": "atletico",
    "real_betis": "betis",
    "rayo_vallecano": "rayo_vallecano",
    # Serie A
    "internazionale": "inter",
    "inter_milan": "inter",
    "ac_milan": "milan",
    "hellas_verona": "verona",
    # Bundesliga
    "bayern_munich": "bayern",
    "bayern": "bayern",
    "borussia_dortmund": "dortmund",
    "bayer_leverkusen": "leverkusen",
    "borussia_monchengladbach": "monchengladbach",
    # Ligue 1
    "paris_saint_germain": "psg",
    "paris saint-germain": "psg",
    "saint_etienne": "st_etienne",
    # Scottish
    "st_mirren_fc": "st_mirren",
    "st. mirren": "st_mirren",
    "st_mirren": "st_mirren",
    "celtic_fc": "celtic",
    "celtic": "celtic",
    "rangers_fc": "rangers",
    "rangers": "rangers",
    # Другие
    "sporting_cp": "sporting",
    "sporting_lisbon": "sporting",
    "club_brugge": "brugge",
    "fc_bayern": "bayern",
    "real_sociedad_de_futbol": "real_sociedad",
    "athletic_club": "athletic_bilbao",
    "athletic_bilbao": "athletic_bilbao",
    "vfl_wolfsburg": "wolfsburg",
    "sc_freiburg": "freiburg",
    "vfb_stuttgart": "stuttgart",
    "1_fc_union_berlin": "union_berlin",
    "1. fc union berlin": "union_berlin",
    "1_fc_koln": "koln",
    "1. fc koln": "koln",
    "fc_augsburg": "augsburg",
    "vfl_bochum": "bochum",
    "sv_werder_bremen": "bremen",
    "werder_bremen": "bremen",
    "tsg_hoffenheim": "hoffenheim",
    "fc_schalke_04": "schalke",
    "hertha_bsc": "hertha",
    "hamburger_sv": "hamburg",
    # Latin American
    "river_plate": "river_plate",
    "boca_juniors": "boca_juniors",
    "club_atletico_mineiro": "atletico_mineiro",
    "flamengo_rj": "flamengo",
    "cruzeiro_ec": "cruzeiro",
}


def clean_team_name(raw: str) -> str:
    """
    Очистка имени команды + разрешение алиасов.
    Сначала проверяет TEAM_ALIASES, затем базовую очистку из search_module.
    """
    if not raw:
        return ""
    raw_lower = raw.strip().lower()

    # 1. Прямой алиас (по исходной строке)
    if raw_lower in TEAM_ALIASES:
        return TEAM_ALIASES[raw_lower]

    # 2. Базовая очистка
    cleaned = _base_clean_team_name(raw)
    if cleaned is None:
        cleaned = ""
    cleaned_lower = cleaned.strip().lower().replace(" ", "_")

    # 3. Проверка очищенного по алиасам
    if cleaned_lower in TEAM_ALIASES:
        return TEAM_ALIASES[cleaned_lower]

    # 4. Возвращаем очищенный (с заменой пробелов на _)
    return cleaned_lower if cleaned_lower else raw_lower.replace(" ", "_")


# ---------------------------------------------------------------------------
# UPSTREAM_MAP — определение upstream провайдера
# ---------------------------------------------------------------------------
UPSTREAM_MAP = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "propline": "pinnacle",
    "bzzoiro": "opta",
    "football_data": "bet365",
    "unknown": "unknown",
}


def detect_upstream(source: str) -> str:
    """Определить upstream провайдера по имени источника."""
    return UPSTREAM_MAP.get(source, "unknown")


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
    if not date_utc:
        return True
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def _is_numeric(val) -> bool:
    if val is None or val == "-" or val == "":
        return False
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Публичные хелперы для коллекторов
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


# ---------------------------------------------------------------------------
# Odds helpers — накопление, вычисление, верификация
# ---------------------------------------------------------------------------
def _max_per_selection(prices: List[dict]) -> dict:
    """MAX по каждому исходу (home, draw, away) из списка price-диктов."""
    result = {}
    for key in ("home", "draw", "away"):
        values = []
        for p in prices:
            val = p.get(key)
            if val is None or val == "-" or val == "":
                continue
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                continue
        result[key] = str(max(values)) if values else ""
    return result


def _prices_agree(prices: List[dict], threshold: float = 0.05) -> bool:
    """Проверить, согласуются ли цены в пределах порога (по всем исходам)."""
    if len(prices) < 2:
        return False
    for key in ("home", "draw", "away"):
        vals = []
        for p in prices:
            val = p.get(key)
            if val is None or val == "-" or val == "":
                continue
            try:
                vals.append(float(val))
            except (ValueError, TypeError):
                continue
        if vals and (max(vals) - min(vals)) > threshold:
            return False
    return True


def _mad_outlier(values: List[float], new_value: float, threshold: float = 3.0) -> bool:
    """MAD outlier detection: Modified Z-score на основе median absolute deviation."""
    if len(values) < 3:
        return False
    try:
        median = statistics.median(values)
        abs_devs = [abs(v - median) for v in values]
        mad = statistics.median(abs_devs)
        if mad == 0:
            return False
        modified_z = 0.6745 * abs(new_value - median) / mad
        return modified_z > threshold
    except (ValueError, TypeError, statistics.StatisticsError):
        return False


def _calc_movement(opening: Optional[dict], current: Optional[dict]) -> dict:
    """Вычислить movement между opening и current (по home odds)."""
    if not opening or not current:
        return {"direction": "stable", "magnitude": 0.0}
    try:
        open_val = float(opening.get("home", 0))
        cur_val = float(current.get("home", 0))
        diff = cur_val - open_val
        if abs(diff) < 0.01:
            return {"direction": "stable", "magnitude": 0.0}
        return {
            "direction": "up" if diff > 0 else "down",
            "magnitude": round(abs(diff), 2),
        }
    except (ValueError, TypeError):
        return {"direction": "stable", "magnitude": 0.0}


def _merge_odds(existing_odds: dict, new_odds: dict,
                source: str, upstream: str, timestamp: str) -> dict:
    """
    Накопление odds: каждый источник добавляет свой snapshot в sources[].
    Ничего не перезаписывается. Вычисляются current, opening, best, movement,
    _independent_sources, _verification, _betradar_consensus, _outlier_detected.

    Поддерживает:
      - Новый формат: existing_odds = {"1x2": {"current":..., "sources":...}}
      - Старый плоский: existing_odds = {"home": "1.85", "draw": "3.60", "away": "2.10"}
      - Новый вход: new_odds = {"current": {"home":..., "draw":..., "away":...}}
      - Старый вход: new_odds = {"home": "1.85", "draw": "3.60", "away": "2.10"}
    """
    # --- Извлечь существующую секцию 1x2 ---
    section = {}
    if existing_odds:
        if isinstance(existing_odds, dict) and "1x2" in existing_odds:
            section = existing_odds.get("1x2", {})
            if not isinstance(section, dict):
                section = {}
        elif isinstance(existing_odds, dict) and "home" in existing_odds:
            # Старый плоский формат → конвертировать
            section = {
                "current": {
                    "home": str(existing_odds.get("home", "")),
                    "draw": str(existing_odds.get("draw", "")),
                    "away": str(existing_odds.get("away", "")),
                },
                "sources": [],
                "_snapshots": [],
            }
        elif isinstance(existing_odds, dict) and "current" in existing_odds:
            section = existing_odds

    # --- Нормализовать new_odds к виду {current: {home, draw, away}} ---
    if "current" in new_odds and isinstance(new_odds["current"], dict):
        new_price = new_odds["current"]
    elif "home" in new_odds:
        # Старый плоский формат
        new_price = {
            "home": str(new_odds.get("home", "")),
            "draw": str(new_odds.get("draw", "")),
            "away": str(new_odds.get("away", "")),
        }
    else:
        new_price = new_odds

    sources = section.get("sources", [])
    if not isinstance(sources, list):
        sources = []
    snapshots = section.get("_snapshots", [])
    if not isinstance(snapshots, list):
        snapshots = []

    # 1. Добавить новый source (без перезаписи)
    sources.append({
        "source": source,
        "upstream": upstream,
        "price": new_price,
        "timestamp": timestamp,
    })

    # 2. Snapshot
    snapshots.append({
        "version": len(snapshots) + 1,
        "odds": new_price,
        "source": source,
        "timestamp": timestamp,
    })
    if len(snapshots) > MAX_ODDS_SNAPSHOTS:
        snapshots = snapshots[-MAX_ODDS_SNAPSHOTS:]

    # 3. Opening (первый snapshot — не перезаписываем)
    open_snapshot = section.get("_open_snapshot") or section.get("opening")
    if not open_snapshot and snapshots:
        open_snapshot = snapshots[0]["odds"]
    if not isinstance(open_snapshot, dict):
        open_snapshot = None

    # 4. Current = MAX по каждому исходу
    all_prices = [s["price"] for s in sources if isinstance(s.get("price"), dict)]
    current = _max_per_selection(all_prices)

    # 5. Independent sources (уникальные upstream)
    unique_upstreams = list(set(s.get("upstream", "unknown") for s in sources))
    independent = len(unique_upstreams)

    # 6. BetRadar consensus (если 2+ источника с upstream=betradar)
    betradar_prices = [s["price"] for s in sources
                       if s.get("upstream") == "betradar" and isinstance(s.get("price"), dict)]
    betradar_consensus = _prices_agree(betradar_prices) if len(betradar_prices) >= 2 else False

    # 7. MAD outlier detection
    all_home = [float(p["home"]) for p in all_prices
                if p.get("home") and _is_numeric(p["home"])]
    new_home_val = float(new_price.get("home", 0)) if _is_numeric(new_price.get("home")) else 0.0
    outlier = _mad_outlier(all_home, new_home_val)

    # 8. Verification
    if independent >= 2:
        verification = "VERIFIED"
    elif independent == 1 and len(sources) >= 1:
        verification = "WARNING"
    else:
        verification = "UNVERIFIED"

    # 9. Movement
    movement = _calc_movement(open_snapshot, current)

    return {
        "1x2": {
            "current": current,
            "opening": open_snapshot,
            "best": _max_per_selection(all_prices),
            "sources": sources,
            "_snapshots": snapshots,
            "_open_snapshot": open_snapshot,
            "_independent_sources": independent,
            "_betradar_consensus": betradar_consensus,
            "_outlier_detected": outlier,
            "_verification": verification,
            "_upstream": unique_upstreams,
            "movement": movement,
        }
    }


def _merge_source_ids(existing: dict, new: dict) -> dict:
    for key, val in new.items():
        if val is not None and val != "":
            existing[key] = val
    return existing


# ---------------------------------------------------------------------------
# get_current_odds / get_odds_metadata — обёртки обратной совместимости
# ---------------------------------------------------------------------------
def get_current_odds(match_payload: dict) -> dict:
    """
    Возвращает odds в плоском формате {home, draw, away}.
    Работает со старым и новым форматом.
    """
    odds = match_payload.get("odds", {})
    if not isinstance(odds, dict):
        return {}

    # Новый формат: odds.1x2.current
    if "1x2" in odds:
        section = odds.get("1x2", {})
        if isinstance(section, dict):
            current = section.get("current", {})
            if isinstance(current, dict):
                return current

    # Промежуточный формат: odds.current
    if "current" in odds and isinstance(odds["current"], dict):
        return odds["current"]

    # Старый формат: odds = {home, draw, away}
    if "home" in odds:
        return {"home": odds.get("home", ""), "draw": odds.get("draw", ""), "away": odds.get("away", "")}

    return {}


def get_odds_metadata(match_payload: dict) -> dict:
    """
    Возвращает метаданные odds: verification, sources, movement.
    Только для нового формата. Для старого — заглушка.
    """
    odds = match_payload.get("odds", {})
    if not isinstance(odds, dict):
        return _empty_odds_metadata()

    if "1x2" in odds:
        section = odds.get("1x2", {})
        if isinstance(section, dict):
            return {
                "verification": section.get("_verification", "UNVERIFIED"),
                "independent_sources": section.get("_independent_sources", 0),
                "upstream": section.get("_upstream", []),
                "movement": section.get("movement", {}),
                "betradar_consensus": section.get("_betradar_consensus", False),
                "outlier_detected": section.get("_outlier_detected", False),
                "sources_count": len(section.get("sources", [])),
                "opening": section.get("opening") or section.get("_open_snapshot") or {},
            }

    return _empty_odds_metadata()


def _empty_odds_metadata() -> dict:
    return {
        "verification": "UNVERIFIED",
        "independent_sources": 0,
        "upstream": [],
        "movement": {},
        "betradar_consensus": False,
        "outlier_detected": False,
        "sources_count": 0,
        "opening": {},
    }


# ---------------------------------------------------------------------------
# Валидация секций
# ---------------------------------------------------------------------------
def _validate_section(section: str, data: Any) -> bool:
    if not isinstance(data, dict):
        print(f"[HUB VALIDATE] Секция '{section}': данные не являются dict")
        return False

    if section == "odds":
        # Новый формат: data = {"current": {"home":..., "draw":..., "away":...}}
        if "current" in data and isinstance(data["current"], dict):
            price = data["current"]
            for key in ("home", "draw", "away"):
                val = price.get(key, "-")
                if val == "-" or val is None or val == "":
                    continue
                try:
                    float(val)
                except (ValueError, TypeError):
                    print(f"[HUB VALIDATE] Секция 'odds.current': поле '{key}'='{val}' не число")
                    return False
            return True
        # Старый формат: data = {"home":..., "draw":..., "away":...}
        if "home" in data or "draw" in data or "away" in data:
            for key in ("home", "draw", "away"):
                val = data.get(key, "-")
                if val == "-" or val is None or val == "":
                    continue
                try:
                    float(val)
                except (ValueError, TypeError):
                    print(f"[HUB VALIDATE] Секция 'odds': поле '{key}'='{val}' не число")
                    return False
            return True
        # Пустой odds — допустимо (первое создание)
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


# ---------------------------------------------------------------------------
# Инициализация объекта матча
# ---------------------------------------------------------------------------
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
        "source_map": {},
        "sources": [source],
        "section_history": [],
        "version": 0,
        "schema_version": "v700",
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
    today = datetime.now(MSK_TIMEZONE).date()
    keys = []
    for i in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        d = today + timedelta(days=i)
        keys.append(f"match:index:{d.strftime('%Y%m%d')}")
    return keys


def _update_index(canonical_id: str, date_utc: str = "") -> None:
    index = get_from_cache(INDEX_FIELD)
    if index is None:
        index = []
    if not isinstance(index, list):
        index = []
    if canonical_id not in index:
        index.append(canonical_id)
        save_to_cache(INDEX_FIELD, index)

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
    if not ids_to_remove:
        return 0

    index = all_data.get(INDEX_FIELD)
    if isinstance(index, list):
        changed = False
        for cid in list(ids_to_remove):
            if cid in index:
                index.remove(cid)
                changed = True
        if changed:
            save_to_cache(INDEX_FIELD, index)

    removed_total = 0
    for shard_key in _get_all_shard_keys():
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
            existing["schema_version"] = "v700"
        if "source_map" not in existing:
            existing["source_map"] = {}
        if source not in existing.get("sources", []):
            existing.setdefault("sources", []).append(source)

        _merge_extra_fields(existing, extra_fields, source)
        save_to_cache(field_name, existing)
    else:
        match_obj = _init_match_object(home_team, away_team, date_utc, competition, country, status, source)
        _merge_extra_fields(match_obj, extra_fields, source)
        save_to_cache(field_name, match_obj)
        _update_index(canonical_id, date_utc)

    return canonical_id


def _merge_extra_fields(match_obj: dict, extra_fields: dict, source: str = "unknown") -> None:
    upstream = detect_upstream(source)
    ts = _now_msk()

    for k, v in extra_fields.items():
        if v is None:
            continue

        if k == "odds" and isinstance(v, dict):
            existing_odds = match_obj.get("odds", {})
            if not isinstance(existing_odds, dict):
                existing_odds = {}
            match_obj["odds"] = _merge_odds(existing_odds, v, source, upstream, ts)

        elif k == "source_ids" and isinstance(v, dict):
            existing_ids = match_obj.get("source_ids", {})
            if not isinstance(existing_ids, dict):
                existing_ids = {}
            match_obj["source_ids"] = _merge_source_ids(existing_ids, v)

        elif k == "predictions" and isinstance(v, dict):
            v["_updated_at"] = ts
            match_obj["predictions"] = v

        elif k == "h2h" and isinstance(v, dict):
            v["_updated_at"] = ts
            match_obj["h2h"] = v

        elif k == "stats" and isinstance(v, dict):
            existing_stats = match_obj.get("stats", {})
            if not isinstance(existing_stats, dict):
                existing_stats = {}
            existing_stats.update(v)
            existing_stats["_updated_at"] = ts
            match_obj["stats"] = existing_stats

        else:
            match_obj[k] = v

        # Обновить source_map для этой секции
        if k in ("odds", "predictions", "h2h", "stats"):
            match_obj.setdefault("source_map", {})[k] = {
                "source": source,
                "upstream": upstream,
                "timestamp": ts,
            }


def patch_match(canonical_id: str, section: str, data: Any, source: str = "", upstream: str = None) -> bool:
    """
    Обновление секции матча. CAS + накопление.
    Для odds: каждый источник добавляет свой snapshot в sources[].
    Для predictions/h2h: перезапись с _previous + timestamp.
    Для stats: merge с provider attribution.
    """
    field_name = _make_field_name(canonical_id)

    if upstream is None:
        upstream = detect_upstream(source) if source else "unknown"

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
            match["odds"] = _merge_odds(existing_odds, data, source, upstream, now)

        elif section == "source_ids" and isinstance(data, dict):
            existing_ids = match.get("source_ids", {})
            if not isinstance(existing_ids, dict):
                existing_ids = {}
            match["source_ids"] = _merge_source_ids(existing_ids, data)

        elif section == "predictions" and isinstance(data, dict):
            prev = match.get("predictions", {})
            if isinstance(prev, dict) and prev:
                # Сохранить предыдущий (без служебных полей)
                prev_clean = {k: v for k, v in prev.items() if not k.startswith("_")}
                if prev_clean:
                    data["_previous"] = prev_clean
            data["_updated_at"] = now
            match["predictions"] = data

        elif section == "h2h" and isinstance(data, dict):
            prev = match.get("h2h", {})
            if isinstance(prev, dict) and prev:
                prev_clean = {k: v for k, v in prev.items() if not k.startswith("_")}
                if prev_clean:
                    data["_previous"] = prev_clean
            data["_updated_at"] = now
            match["h2h"] = data

        elif section == "stats" and isinstance(data, dict):
            existing_stats = match.get("stats", {})
            if not isinstance(existing_stats, dict):
                existing_stats = {}
            existing_stats.update(data)
            existing_stats["_updated_at"] = now
            existing_stats["_provider"] = source
            match["stats"] = existing_stats

        elif section == "score":
            match["score"] = data

        elif section == "status":
            match["status"] = data

        else:
            match[section] = data

        # --- source_map ---
        if section not in ("score", "status"):
            match.setdefault("source_map", {})[section] = {
                "source": source,
                "upstream": upstream,
                "timestamp": now,
            }

        # --- metadata ---
        match["updated_at"] = now
        match["version"] = expected_version + 1
        if "schema_version" not in match:
            match["schema_version"] = "v700"

        if source and source not in match.get("sources", []):
            match.setdefault("sources", []).append(source)

        history = match.get("section_history", [])
        if not isinstance(history, list):
            history = []
        history.append({
            "section": section,
            "source": source or "unknown",
            "upstream": upstream,
            "timestamp": now,
        })
        if len(history) > MAX_HISTORY_ENTRIES:
            history = history[-MAX_HISTORY_ENTRIES:]
        match["section_history"] = history

        if save_to_cache(field_name, match):
            return True

        time.sleep(0.3)

    print(f"[HUB ERROR] CAS: исчерпаны {MAX_CAS_RETRIES} попытки для {canonical_id}")
    _increment_metric("cas_conflicts_total")
    return False


def patch_match_by_teams(home_team, away_team, date_utc, section, data, source="", upstream=None) -> bool:
    canonical_id = _make_canonical_id(home_team, away_team, date_utc)
    return patch_match(canonical_id, section, data, source, upstream)


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
                        _merge_extra_fields(existing, extra_fields, source)
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
# History: загрузка через хаб
# ---------------------------------------------------------------------------
def upsert_history_match(canonical_id: str, payload: dict) -> bool:
    """
    Загрузка исторического матча через хаб.
    Не фильтрует прошедшие матчи (все уже завершены).
    Не использует CAS (version=1 навсегда).
    Пишет в history:match:* вместо match:*.
    """
    if not payload.get("home_team") or not payload.get("away_team"):
        print("[HUB HISTORY] Нет home_team или away_team")
        return False

    # TEAM_ALIASES через clean_team_name из хаба
    payload["home_clean"] = clean_team_name(payload["home_team"])
    payload["away_clean"] = clean_team_name(payload["away_team"])

    if not canonical_id:
        date_str = ""
        try:
            dt = datetime.fromisoformat(payload.get("date_utc", "").replace("Z", "+00:00"))
            date_str = dt.strftime("%Y%m%d")
        except Exception:
            date_str = "unknown"
        canonical_id = f"{payload['home_clean']}__{payload['away_clean']}__{date_str}"
    payload["canonical_id"] = canonical_id

    # Конверт v700
    payload.setdefault("schema_version", "v700")
    payload.setdefault("version", 1)
    payload.setdefault("sources", ["football_data"])
    payload.setdefault("section_history", [])
    payload.setdefault("source_map", {})
    payload.setdefault("source_ids", {})

    history_key = f"{HISTORY_PREFIX}{canonical_id}"
    save_to_cache(history_key, payload)

    # Индексы
    update_history_indexes(
        canonical_id,
        payload.get("date_utc", ""),
        payload["home_clean"],
        payload["away_clean"],
        payload.get("league_code", ""),
    )

    return True


def update_history_indexes(cid: str, date_utc: str, home_clean: str, away_clean: str, league_code: str = "") -> None:
    """3 индекса: по дате, по командам, по лиге."""
    date_str = ""
    if date_utc:
        try:
            dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y%m%d")
        except Exception:
            pass

    if date_str:
        idx_key = f"history:index:{date_str}"
        existing = get_from_cache(idx_key) or []
        if not isinstance(existing, list):
            existing = []
        if cid not in existing:
            existing.append(cid)
            save_to_cache(idx_key, existing)

    for team in [home_clean, away_clean]:
        if team:
            tkey = f"history:team:{team}"
            existing = get_from_cache(tkey) or []
            if not isinstance(existing, list):
                existing = []
            if cid not in existing:
                existing.append(cid)
                save_to_cache(tkey, existing)

    if league_code:
        lkey = f"history:league:{league_code}"
        existing = get_from_cache(lkey) or []
        if not isinstance(existing, list):
            existing = []
        if cid not in existing:
            existing.append(cid)
            save_to_cache(lkey, existing)


def get_history(canonical_id: str) -> Optional[dict]:
    """Чтение исторического матча."""
    return get_from_cache(f"{HISTORY_PREFIX}{canonical_id}")


def get_history_by_team(team_clean: str) -> List[str]:
    """Список canonical_id матчей команды из history."""
    key = f"history:team:{team_clean}"
    result = get_from_cache(key)
    if isinstance(result, list):
        return result
    return []


# ---------------------------------------------------------------------------
# Analysis: метрики движка
# ---------------------------------------------------------------------------
def save_analysis(canonical_id: str, analysis_data: dict) -> bool:
    """Запись метрик движка в analysis:{cid}."""
    if not canonical_id:
        return False

    key = f"{ANALYSIS_PREFIX}{canonical_id}"
    now = _now_msk()

    # analysis_version++
    existing = get_from_cache(key)
    analysis_version = 1
    if existing and isinstance(existing, dict):
        prev = existing.get("analysis_version", 0)
        analysis_version = prev + 1

    payload = {
        "canonical_id": canonical_id,
        "analysis_version": analysis_version,
        "analyzed_at": now,
    }
    payload.update(analysis_data)

    wrapper = {
        "version": "v700-prod",
        "sender_repo": "value_engine",
        "timestamp": now,
        "payload": payload,
    }

    save_to_cache(key, wrapper)

    # Индекс по дате
    date_str = now[:10].replace("-", "")
    idx_key = f"analysis:index:{date_str}"
    idx = get_from_cache(idx_key) or []
    if not isinstance(idx, list):
        idx = []
    if canonical_id not in idx:
        idx.append(canonical_id)
        save_to_cache(idx_key, idx)

    return True


def get_analysis(canonical_id: str) -> Optional[dict]:
    """Чтение метрик матча."""
    return get_from_cache(f"{ANALYSIS_PREFIX}{canonical_id}")


def get_analysis_by_date(date_str: str) -> Dict[str, Any]:
    """Список метрик за день (date_str = YYYYMMDD)."""
    idx = get_from_cache(f"analysis:index:{date_str}") or []
    if not isinstance(idx, list):
        return {}
    results = {}
    for cid in idx:
        a = get_from_cache(f"{ANALYSIS_PREFIX}{cid}")
        if a and isinstance(a, dict):
            results[cid] = a.get("payload", a)
    return results


# ---------------------------------------------------------------------------
# Миграция схемы
# ---------------------------------------------------------------------------
def migrate_schema(all_data: Optional[Dict[str, Any]] = None) -> int:
    """
    Миграция старых матчей к v700: добавляет version, schema_version,
    section_history, source_map. Переносит старые поля.
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
            payload["schema_version"] = "v700"
            changed = True
        if "section_history" not in payload:
            payload["section_history"] = []
            changed = True
        if "source_map" not in payload:
            payload["source_map"] = {}
            changed = True

        # Миграция старых odds в новый формат (если плоский)
        odds = payload.get("odds", {})
        if isinstance(odds, dict) and "home" in odds and "1x2" not in odds:
            # Старый плоский → новый
            payload["odds"] = {
                "1x2": {
                    "current": {
                        "home": str(odds.get("home", "")),
                        "draw": str(odds.get("draw", "")),
                        "away": str(odds.get("away", "")),
                    },
                    "sources": [],
                    "_snapshots": [],
                    "_independent_sources": 0,
                    "_verification": "UNVERIFIED",
                }
            }
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

        # Убрать лишние поля Alice-AI
        if "extra" in payload and not payload.get("extra"):
            del payload["extra"]
            changed = True

        if changed:
            save_to_cache(key, payload)
            count += 1

    _update_health_metric("last_migration_at", _now_msk())
    return count


# ---------------------------------------------------------------------------
# Миграция завершённых матчей в history
# ---------------------------------------------------------------------------
def migrate_to_history(match_key: str, match_data: dict) -> bool:
    """
    Перенос завершённого матча из match:* в history:match:*.
    Вызывается из cleanup_expired ПЕРЕД удалением match:*.
    """
    if not isinstance(match_data, dict):
        return False

    payload = match_data.get("payload", match_data)

    if payload.get("status") != "completed" or not payload.get("score"):
        return False

    cid = payload.get("canonical_id", match_key.replace("match:", ""))

    # Проверить, есть ли уже в history
    existing = get_from_cache(f"{HISTORY_PREFIX}{cid}")

    if existing:
        # MERGE: дополнить enrichment из live
        if isinstance(existing, dict):
            if payload.get("predictions") and not existing.get("predictions"):
                existing["predictions"] = payload["predictions"]
            if payload.get("stats"):
                existing.setdefault("stats", {})
                if isinstance(existing["stats"], dict):
                    existing["stats"].update(payload["stats"])
            if payload.get("h2h") and not existing.get("h2h"):
                existing["h2h"] = payload["h2h"]
            save_to_cache(f"{HISTORY_PREFIX}{cid}", existing)
    else:
        # Нет в history (лига Alice-AI) — создать
        payload.setdefault("season", "")
        payload.setdefault("league_code", "")
        # Убрать live-поля, не нужные в history
        for fld in ("extra", "timestamp"):
            payload.pop(fld, None)
        upsert_history_match(cid, payload)

    return True


# ---------------------------------------------------------------------------
# Очистка: завершённые матчи + 2 часа
# ---------------------------------------------------------------------------
def cleanup_expired(all_data: Optional[Dict[str, Any]] = None) -> dict:
    """
    Очистка Redis: удаляет завершённые матчи.
    Перед удалением — миграция в history (если матч завершён).
    """
    if all_data is None:
        all_data = get_all_fields()

    now = datetime.now(MSK_TIMEZONE)
    finished_cutoff = now - timedelta(hours=MATCH_FINISH_BUFFER_HOURS)
    deleted_count = 0
    checked = 0
    finished_count = 0
    expired_count = 0
    migrated_count = 0

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
            # Миграция в history перед удалением
            try:
                if migrate_to_history(key, payload):
                    migrated_count += 1
            except Exception as e:
                print(f"[HUB MIGRATE] Ошибка миграции {cid}: {e}")
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

    _batch_remove_from_index(ids_to_remove, all_data)

    for key in keys_to_delete:
        delete_from_cache(key)
        deleted_count += 1

    _update_health_metric("last_cleanup_count", deleted_count)
    _update_health_metric("last_cleanup_at", now.isoformat())
    _update_health_metric("last_cleanup_finished", finished_count)
    _update_health_metric("last_cleanup_expired", expired_count)
    _update_health_metric("last_cleanup_migrated", migrated_count)

    stats = {
        "checked": checked,
        "deleted": deleted_count,
        "finished": finished_count,
        "expired": expired_count,
        "migrated": migrated_count,
        "remaining": checked - finished_count if checked > 0 else 0,
    }
    print(f"[HUB] Cleanup: deleted {deleted_count} keys "
          f"(finished={finished_count}, expired={expired_count}, "
          f"migrated={migrated_count}, buffer={MATCH_FINISH_BUFFER_HOURS}h)")
    return stats


# ---------------------------------------------------------------------------
# Сохранение результатов поиска
# ---------------------------------------------------------------------------
def save_search_results(results: Dict[str, Any]) -> bool:
    if not results:
        return False
    payload = {
        "version": "v700",
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
    Один get_all_fields() для миграции и очистки.
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

    print("[HUB] run_initialization: миграция схемы v700...")
    metrics["migration_count"] = migrate_schema(all_data)

    print("[HUB] run_initialization: очистка завершённых матчей...")
    cleanup_result = cleanup_expired(all_data)
    metrics["cleanup_count"] = cleanup_result.get("deleted", 0)
    metrics["cleanup_finished"] = cleanup_result.get("finished", 0)
    metrics["cleanup_expired"] = cleanup_result.get("expired", 0)
    metrics["cleanup_migrated"] = cleanup_result.get("migrated", 0)

    _flush_health_metrics()

    latency_ms = int((time.time() - start) * 1000)
    metrics["init_latency_ms"] = latency_ms
    _update_health_metric("init_latency_ms", latency_ms)
    _flush_health_metrics()

    print(f"[HUB] Init: Redis OK, CB reset={metrics['circuit_breaker_reset']}, "
          f"миграция {metrics['migration_count']}, "
          f"очистка {metrics['cleanup_count']}, "
          f"мигрировано {metrics.get('cleanup_migrated', 0)}, "
          f"latency {latency_ms}ms")
    return metrics


# ---------------------------------------------------------------------------
# V5.0: Единый формат 1x2 — публичные функции для всех модулей
# ---------------------------------------------------------------------------

def build_canonical_id(home_team: str, away_team: str, date_utc: str) -> str:
    """
    Публичная обёртка над _make_canonical_id.
    Используется football_data_loader и normalize_history_ids
    вместо собственной реализации — гарантирует единый canonical_id.
    """
    return _make_canonical_id(home_team, away_team, date_utc)


def _normalize_incoming_odds(data: Any) -> Optional[dict]:
    """
    Нормализует любой входной формат odds к плоскому price dict.
    Принимает:
      - 1x2-формат: {"1x2": {"current": {"home": "1.85", ...}}}
      - current-вложенный: {"current": {"home": "1.85", ...}}
      - плоский: {"home": "1.85", "draw": "3.60", "away": "2.10"}
    Возвращает:
      - {"home": "1.85", "draw": "3.60", "away": "2.10"} или None
    """
    if not isinstance(data, dict):
        return None

    # 1x2-формат
    if "1x2" in data:
        block = data.get("1x2", {})
        if isinstance(block, dict):
            cur = block.get("current", {})
            if isinstance(cur, dict) and "home" in cur:
                return {
                    "home": str(cur.get("home", "")),
                    "draw": str(cur.get("draw", "")),
                    "away": str(cur.get("away", "")),
                }

    # current-вложенный
    if "current" in data and isinstance(data["current"], dict):
        cur = data["current"]
        if "home" in cur:
            return {
                "home": str(cur.get("home", "")),
                "draw": str(cur.get("draw", "")),
                "away": str(cur.get("away", "")),
            }

    # плоский
    if "home" in data:
        return {
            "home": str(data.get("home", "")),
            "draw": str(data.get("draw", "")),
            "away": str(data.get("away", "")),
        }

    return None


def _count_independent(sources: list) -> int:
    """
    Подсчёт уникальных upstream в sources[].
    Исключает 'unknown' — только реальные провайдеры.
    Используется в get_all_odds и доступна для diagnostics.
    """
    if not isinstance(sources, list):
        return 0
    upstreams = set()
    for s in sources:
        if not isinstance(s, dict):
            continue
        up = s.get("upstream", "unknown")
        if up and up != "unknown":
            upstreams.add(up)
    return len(upstreams)


def _build_1x2(price: dict, source: str, upstream: str,
               timestamp: str = None, odds_type: str = "closing") -> dict:
    """
    Создание начального 1x2 блока из плоского price.
    Используется football_data_loader для записи history
    с правильной структурой 1x2 (а не плоским форматом).
    """
    if timestamp is None:
        timestamp = _now_msk()

    if not isinstance(price, dict):
        price = {}

    flat = {
        "home": str(price.get("home", "")),
        "draw": str(price.get("draw", "")),
        "away": str(price.get("away", "")),
    }

    return {
        "1x2": {
            "current": dict(flat),
            "opening": dict(flat),
            "best": dict(flat),
            "sources": [{
                "source": source,
                "upstream": upstream,
                "price": dict(flat),
                "timestamp": timestamp,
                "type": odds_type,
            }],
        }
    }


def get_all_odds(match_payload: dict) -> dict:
    """
    Полные odds-данные одним вызовом.
    Возвращает current, opening, best, sources, independent_sources,
    verification, movement — всё что нужно value_engine и main.py.

    Для старого формата (плоский или current-вложенный) —
    возвращает что есть, independent_sources=0.
    """
    odds = match_payload.get("odds", {})
    if not isinstance(odds, dict):
        return _empty_all_odds()

    # Новый формат: 1x2
    if "1x2" in odds:
        block = odds.get("1x2", {})
        if not isinstance(block, dict):
            return _empty_all_odds()

        sources = block.get("sources", [])
        if not isinstance(sources, list):
            sources = []

        # Opening: prefer explicit opening, fallback to first source
        opening = block.get("opening", {})
        if not isinstance(opening, dict) or not opening:
            if sources and isinstance(sources[0].get("price"), dict):
                opening = sources[0]["price"]
            else:
                opening = {}

        # Best
        best = block.get("best", {})
        if not isinstance(best, dict):
            best = {}

        # Current
        current = block.get("current", {})
        if not isinstance(current, dict):
            current = {}

        # Upstreams
        upstreams = list(set(
            s.get("upstream", "unknown") for s in sources
            if isinstance(s, dict) and s.get("upstream", "unknown") != "unknown"
        ))

        # Metadata from block (if computed by _merge_odds)
        verification = block.get("_verification", "UNVERIFIED")
        if not isinstance(verification, str):
            verification = "UNVERIFIED"

        independent = _count_independent(sources)
        if independent >= 2:
            verification = "VERIFIED"
        elif independent == 1:
            verification = "WARNING" if verification == "UNVERIFIED" else verification

        betradar_consensus = block.get("_betradar_consensus", False)
        outlier_detected = block.get("_outlier_detected", False)
        movement = block.get("movement", {})
        if not isinstance(movement, dict):
            movement = {}

        return {
            "current": current,
            "opening": opening,
            "best": best,
            "sources": sources,
            "independent_sources": independent,
            "verification": verification,
            "betradar_consensus": bool(betradar_consensus),
            "outlier_detected": bool(outlier_detected),
            "movement": movement,
            "upstreams": upstreams,
        }

    # Промежуточный формат: current-вложенный
    if "current" in odds and isinstance(odds["current"], dict):
        cur = odds["current"]
        return {
            "current": cur,
            "opening": {},
            "best": cur,
            "sources": [],
            "independent_sources": 0,
            "verification": "UNVERIFIED",
            "betradar_consensus": False,
            "outlier_detected": False,
            "movement": {},
            "upstreams": [],
        }

    # Старый формат: плоский
    if "home" in odds:
        flat = {
            "home": str(odds.get("home", "")),
            "draw": str(odds.get("draw", "")),
            "away": str(odds.get("away", "")),
        }
        return {
            "current": flat,
            "opening": {},
            "best": flat,
            "sources": [],
            "independent_sources": 0,
            "verification": "UNVERIFIED",
            "betradar_consensus": False,
            "outlier_detected": False,
            "movement": {},
            "upstreams": [],
        }

    return _empty_all_odds()


def _empty_all_odds() -> dict:
    """Заглушка для отсутствующих odds."""
    return {
        "current": {},
        "opening": {},
        "best": {},
        "sources": [],
        "independent_sources": 0,
        "verification": "UNVERIFIED",
        "betradar_consensus": False,
        "outlier_detected": False,
        "movement": {},
        "upstreams": [],
    }
