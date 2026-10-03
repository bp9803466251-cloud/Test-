"""
gatekeeper_hub.py — Единый хаб GatekeeperAI.
Центральный шлюз для создания, обновления и чтения матчей.

Реализует рекомендации:
  §19.2 — INDEX_REGISTRY, odds_priority loading
  §22.8 — Hub function versioning (__version__)
  §23.1 — Cross-Collector Conflict Resolution (should_overwrite)
  §23.2 — Idempotency Keys для patch_match
  §23.3 — Graceful Shutdown (signal handler)
  §24.1 — Match State Machine (set_match_status)
  §24.3 — Configuration Validation при старте
  §24.5 — Metrics Collection (Metrics class, @timed)
  §24.7 — Collector Orchestration (cleanup_owner)
"""

import os
import sys
import json
import time
import signal
import random
from datetime import datetime, timezone, timedelta
from functools import wraps

# ── Конфигурация ───────────────────────────────────────────
from gatekeeper_config import (
    load_config, get_config_errors, is_feature_enabled,
    should_run_cleanup, ns_key, now_msk, now_msk_short,
    get_redis_url, get_redis_token, get_env,
)


# ═══════════════════════════════════════════════════════════
# §22.8: Hub version
# ═══════════════════════════════════════════════════════════

__version__ = "8.9"
HUB_API_VERSION = "8.9"
SCHEMA_VERSION = "v710"


# ═══════════════════════════════════════════════════════════
# §19.2: INDEX_REGISTRY — единый реестр ключей Redis
# ═══════════════════════════════════════════════════════════

INDEX_REGISTRY = {
    # Match data
    "match:{cid}": "Live-матч (hash)",
    "history:match:{cid}": "History-матч (hash)",
    # Daily shards
    "index:shard:{YYYYMMDD}": "Дневной индекс (set of canonical_ids)",
    # Per-source metadata
    "{collector}:meta": "Метаданные коллектора (hash)",
    # Search
    "search:results:latest": "Последние результаты поиска",
    # System keys (§21–§24)
    "system:health": "Health status (hash) — §21.3",
    "system:heartbeat": "Heartbeat timestamps (hash) — §21.3",
    "system:audit_log": "Audit log (sorted set) — §22.1",
    "system:dlq": "Dead Letter Queue (list) — §22.4",
    "system:alerts": "Active alerts (hash) — §23.4",
    "system:state_transitions": "Match state transitions (sorted set) — §24.1",
    "system:metrics": "System metrics (hash) — §24.5",
    # Namespace patterns
    "match:{namespace}:{cid}": "Namespaced live match — §23.7",
    "history:match:{namespace}:{cid}": "Namespaced history — §23.7",
    # Canary
    "match:canary:{cid}": "Canary test match — §23.8",
}

# ═══════════════════════════════════════════════════════════
# §19.2: UPSTREAM_MAP — fallback upstream для коллекторов
# ═══════════════════════════════════════════════════════════

UPSTREAM_MAP = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "bzzoiro": "opta",
    "propline": "pinnacle",
    "football_data": "bet365",
}


# ═══════════════════════════════════════════════════════════
# §24.5: Metrics Collection
# ═══════════════════════════════════════════════════════════

class Metrics:
    """
    In-memory метрики за запуск. Сбрасываются при каждом run_initialization().
    §24.5
    """

    def __init__(self):
        self.counters = {}
        self.timers = {}

    def inc(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n

    def time(self, name, seconds):
        self.timers[name] = self.timers.get(name, 0) + seconds

    def report(self):
        return {"counters": dict(self.counters), "timers": dict(self.timers)}

    def reset(self):
        self.counters = {}
        self.timers = {}


METRICS = Metrics()


def timed(func):
    """Декоратор для автоматического тайминга функций. §24.5"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        result = func(*args, **kwargs)
        elapsed = time.monotonic() - start
        METRICS.time(f"{func.__name__}_total", elapsed)
        return result
    return wrapper


# ═══════════════════════════════════════════════════════════
# §23.3: Graceful Shutdown
# ═══════════════════════════════════════════════════════════

_SHUTDOWN_REQUESTED = False
_SHUTDOWN_INSTALLED = False


def _handle_shutdown(signum, frame):
    """Обработчик SIGTERM/SIGINT. §23.3"""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    log_event("hub", "WARN", "Shutdown requested. Finishing current batch...")


def install_shutdown_handler():
    """
    Устанавливает обработчик SIGTERM/SIGINT.
    Вызывается в run_initialization(). §23.3
    """
    global _SHUTDOWN_INSTALLED
    if _SHUTDOWN_INSTALLED:
        return
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _SHUTDOWN_INSTALLED = True


def is_shutdown_requested():
    """Проверка запроса на завершение. §23.3"""
    return _SHUTDOWN_REQUESTED


# ═══════════════════════════════════════════════════════════
# §24.1: Match State Machine
# ═══════════════════════════════════════════════════════════

MATCH_STATES = {
    "scheduled":   {"transitions": ["live", "cancelled", "postponed"]},
    "live":        {"transitions": ["completed", "cancelled", "interrupted"]},
    "completed":   {"transitions": ["archived"]},
    "archived":    {"transitions": [], "terminal": True},
    "cancelled":   {"transitions": [], "terminal": True},
    "postponed":   {"transitions": ["scheduled"]},
    "interrupted": {"transitions": ["live", "completed", "cancelled"]},
}


def validate_state_transition(current, new_status):
    """Проверяет допустимость перехода. §24.1"""
    state = MATCH_STATES.get(current)
    if not state:
        return new_status == "scheduled"
    if state.get("terminal"):
        return False
    return new_status in state.get("transitions", [])


def set_match_status(canonical_id, new_status, source="system"):
    """
    Единственная функция для смены статуса матча. §24.1
    Запрещает прямой patch_match(..., "status", ...).
    Возвращает True если переход выполнен, False если запрещён.
    """
    match = get_match_any(canonical_id)
    if not match:
        log_event(source, "WARN", "set_match_status: match not found",
                  cid=canonical_id)
        return False

    current = match.get("status", "scheduled")

    if not validate_state_transition(current, new_status):
        log_event(source, "WARN", "Invalid state transition blocked",
                  cid=canonical_id, current=current, attempted=new_status)
        return False

    # Запись через patch_match (section=status)
    patch_match(canonical_id, "status", new_status, source=source)

    # Audit log перехода (§22.1, §24.1)
    _audit_state_transition(canonical_id, current, new_status, source)

    log_event(source, "INFO", "State transition",
              cid=canonical_id, from_state=current, to_state=new_status)
    return True


def _audit_state_transition(cid, from_state, to_state, source):
    """Записывает переход в system:state_transitions. §24.1"""
    try:
        from redis_hub import set_key, get_key
        import json as _json
        key = "system:state_transitions"
        existing = get_key(key)
        log = _json.loads(existing) if existing else []
        log.append({
            "cid": cid,
            "from": from_state,
            "to": to_state,
            "source": source,
            "timestamp": now_msk(),
        })
        # Храним последние 1000 переходов
        if len(log) > 1000:
            log = log[-1000:]
        set_key(key, _json.dumps(log))
    except Exception:
        pass  # Best-effort audit


# ═══════════════════════════════════════════════════════════
# §23.1: Cross-Collector Conflict Resolution
# ═══════════════════════════════════════════════════════════

_source_ranks = {}
_upstream_ranks = {}
_odds_priority_loaded = False


def _load_odds_priority():
    """Загружает ранги из odds_priority.yaml. §19.2, §23.1"""
    global _source_ranks, _upstream_ranks, _odds_priority_loaded
    if _odds_priority_loaded:
        return

    try:
        import yaml
        with open("odds_priority.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _source_ranks = data.get("source_ranks", {})
        _upstream_ranks = data.get("upstream_ranks", {})
    except (FileNotFoundError, ImportError):
        # Fallback — hardcoded ranks (синхронизированы с odds_priority.yaml)
        _source_ranks = {
            "sharpapi": 1, "propline": 1,
            "odds_api": 2, "bzzoiro": 3, "football_data": 4,
        }
        _upstream_ranks = {
            "pinnacle": 1, "betradar": 1,
            "opta": 2, "bet365": 3, "multi_bookmaker": 4,
        }
    _odds_priority_loaded = True


def _get_source_rank(source, upstream=None):
    """
    Возвращает ранг источника (меньше = выше приоритет).
    Источник: odds_priority.yaml (§19.2). §23.1
    """
    _load_odds_priority()
    # Приоритет: source rank, затем upstream rank
    s_rank = _source_ranks.get(source, 99)
    if upstream:
        u_rank = _upstream_ranks.get(upstream, 99)
        return min(s_rank, u_rank)
    return s_rank


def should_overwrite(new_source, new_upstream,
                     existing_source, existing_upstream,
                     section="odds"):
    """
    Решает, должен ли новый источник перезаписать существующее значение.
    Основано на приоритете букмекера, а не на времени записи. §23.1
    """
    new_rank = _get_source_rank(new_source, new_upstream)
    existing_rank = _get_source_rank(existing_source, existing_upstream)

    # Равный ранг — last writer wins
    if new_rank == existing_rank:
        return True

    # Более высокий приоритет (меньший номер) — всегда перебивает
    if new_rank < existing_rank:
        return True

    # Более низкий приоритет — не перебивает, но сохраняется в sources[]
    return False


# ═══════════════════════════════════════════════════════════
# Logging (§20.6)
# ═══════════════════════════════════════════════════════════

def log_event(source, level, message, **kwargs):
    """
    Структурный лог. §20.6
    Выводит в stdout для GitHub Actions. Не выводит payload целиком.
    """
    ts = now_msk_short()
    parts = [f"[{ts}]", f"[{source}]", f"[{level}]", message]
    if kwargs:
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        parts.append(f"({extra})")
    print(" ".join(parts), flush=True)


# ═══════════════════════════════════════════════════════════
# Serialization (§20.1)
# ═══════════════════════════════════════════════════════════

def serialize_match(match_obj):
    """Единый сериализатор. §20.1"""
    return json.dumps(match_obj, ensure_ascii=False, separators=(",", ":"))


def deserialize_match(raw):
    """Единый десериализатор. §20.1"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════
# Canonical ID generation
# ═══════════════════════════════════════════════════════════

def _clean_team_name(name):
    """Нормализация имени команды для canonical_id."""
    if not name:
        return ""
    import re
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "", name)
    # Сокращения
    replacements = {
        "manchesterunited": "man", "manchestercity": "mci",
        "manutd": "man", "mancity": "mci",
    }
    return replacements.get(name, name[:6] if len(name) > 6 else name)


def build_canonical_id(home_team, away_team, date_utc):
    """Строит canonical_id: home__away__YYYYMMDD"""
    home_clean = _clean_team_name(home_team)
    away_clean = _clean_team_name(away_team)
    date_part = ""
    if date_utc:
        # Извлекаем YYYYMMDD из ISO-даты
        date_part = date_utc[:10].replace("-", "")
    return f"{home_clean}__{away_clean}__{date_part}"


# ═══════════════════════════════════════════════════════════
# Date utilities
# ═══════════════════════════════════════════════════════════

def normalize_date(date_str):
    """Нормализует дату в ISO формат YYYY-MM-DDTHH:MM:SSZ."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    if not date_str:
        return ""
    # Уже ISO
    if "T" in date_str:
        return date_str
    # YYYY-MM-DD → добавляем время
    if len(date_str) == 10:
        return date_str + "T00:00:00Z"
    return date_str


def is_future_match(date_utc):
    """Проверяет, что матч в будущем (или без даты). §1.5"""
    if not date_utc:
        return True  # Матчи без даты считаются будущими
    try:
        match_date = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return match_date >= now
    except (ValueError, TypeError):
        return True


# ═══════════════════════════════════════════════════════════
# Redis integration (delagate to redis_hub.py)
# ═══════════════════════════════════════════════════════════

def _get_redis():
    """Ленивый импорт redis_hub."""
    try:
        import redis_hub
        return redis_hub
    except ImportError:
        return None


# ═══════════════════════════════════════════════════════════
# §23.2: Idempotency Keys для patch_match
# ═══════════════════════════════════════════════════════════

def _check_idempotency(canonical_id, idempotency_key):
    """
    Проверяет, была ли операция уже выполнена. §23.2
    Возвращает существующий результат или None.
    """
    if not idempotency_key:
        return None
    rh = _get_redis()
    if not rh:
        return None
    seen_key = f"match:{canonical_id}:idem:{idempotency_key}"
    existing = rh.get_key(seen_key)
    if existing:
        log_event("hub", "DEBUG", "Idempotent skip",
                  key=idempotency_key, cid=canonical_id)
        return existing  # Уже обработано
    return None


def _stamp_idempotency(canonical_id, idempotency_key, result):
    """Регистрирует факт выполнения операции. §23.2"""
    if not idempotency_key:
        return
    rh = _get_redis()
    if not rh:
        return
    seen_key = f"match:{canonical_id}:idem:{idempotency_key}"
    rh.set_key(seen_key, serialize_match({"result": result, "ts": now_msk()}))
    # TTL 1 час — достаточно для ретраев CI
    # Upstash поддерживает EXPIRE
    try:
        rh._execute_upstash_cmd(["EXPIRE", seen_key, "3600"])
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# Core API: upsert_match
# ═══════════════════════════════════════════════════════════

@timed
def upsert_match(home_team="", away_team="", date_utc="",
                 competition="", country="", status="scheduled",
                 source="unknown", mode="live", **extra_fields):
    """
    Создаёт или обновляет матч. Возвращает canonical_id или "".
    §1.4 — единый шлюз, никто не пишет в Redis напрямую.
    §1.5 — прошедшие матчи отсекаются (для mode=live).
    §19.3 — mode="history" для исторических матчей.
    §22.8 — **extra_fields для forward compatibility.
    """
    METRICS.inc("upsert_match")

    # Нормализация
    date_utc = normalize_date(date_utc)
    canonical_id = build_canonical_id(home_team, away_team, date_utc)

    if not canonical_id:
        log_event(source, "ERROR", "upsert_match: empty canonical_id")
        return ""

    # Temporal leakage check (только для live)
    if mode == "live" and not is_future_match(date_utc):
        log_event(source, "DEBUG", "upsert_match: past match skipped",
                  cid=canonical_id)
        METRICS.inc("upsert_match_skipped_past")
        return ""

    # Построение match-объекта
    match_obj = {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": _clean_team_name(home_team),
        "away_clean": _clean_team_name(away_team),
        "competition": competition,
        "country": country,
        "date_utc": date_utc,
        "status": status,
        "score": None,
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_msk(),
        "sources": [source],
        **extra_fields,
    }

    # Нормализация odds в 1x2 (§1.21)
    if "odds" in extra_fields:
        match_obj["odds"] = _normalize_incoming_odds(extra_fields["odds"])

    # Запись в Redis
    rh = _get_redis()
    if rh:
        key = f"match:{canonical_id}" if mode == "live" else f"history:match:{canonical_id}"
        key = ns_key(key, load_config())
        existing = rh.get_from_cache(key)

        if existing and isinstance(existing, dict):
            # Merge: обновляем поля, не перезаписываем целиком
            for k, v in match_obj.items():
                if k not in ("canonical_id", "schema_version", "version"):
                    if v is not None and v != "":
                        if k == "sources":
                            # Добавляем source в список
                            existing_sources = existing.get("sources", [])
                            if source not in existing_sources:
                                existing_sources.append(source)
                            existing["sources"] = existing_sources
                        elif k == "source_ids":
                            # Merge source_ids
                            existing_ids = existing.get("source_ids", {})
                            existing_ids.update(v)
                            existing["source_ids"] = existing_ids
                        elif k == "odds":
                            # Merge odds через _merge_odds
                            existing["odds"] = _merge_odds(
                                existing.get("odds", {}), v, source
                            )
                        else:
                            existing[k] = v
            existing["version"] = existing.get("version", 1) + 1
            existing["updated_at"] = now_msk()
            rh.save_to_cache(key, serialize_match(existing))
        else:
            rh.save_to_cache(key, serialize_match(match_obj))

    log_event(source, "INFO", "upsert_match",
              cid=canonical_id, mode=mode)
    return canonical_id


def _normalize_incoming_odds(odds_data):
    """
    Нормализует odds в формат 1x2. §1.21
    Принимает плоский {"home": "1.85", ...} или {"1x2": {...}}.
    """
    if not odds_data:
        return {}

    # Уже в формате 1x2
    if "1x2" in odds_data:
        return odds_data

    # Плоский формат → 1x2
    if "home" in odds_data or "draw" in odds_data or "away" in odds_data:
        return {
            "1x2": {
                "current": {
                    "home": str(odds_data.get("home", "")),
                    "draw": str(odds_data.get("draw", "")),
                    "away": str(odds_data.get("away", "")),
                }
            }
        }

    # Current-вложенный формат
    if "current" in odds_data:
        return {"1x2": odds_data}

    return odds_data


def _merge_odds(existing_odds, new_odds, source):
    """
    Мержит odds с учётом приоритетов. §23.1
    """
    if not existing_odds:
        return new_odds
    if not new_odds:
        return existing_odds

    result = dict(existing_odds)

    # Мержим по разделам 1x2
    for market in new_odds:
        if market == "1x2":
            new_1x2 = new_odds["1x2"]
            existing_1x2 = result.get("1x2", {})

            # Current — с проверкой приоритета (§23.1)
            if "current" in new_1x2:
                new_current = new_1x2["current"]
                existing_current = existing_1x2.get("current", {})

                if existing_current and is_feature_enabled(
                    "conflict_resolution", load_config()
                ):
                    # Проверяем приоритет
                    existing_source = existing_current.get("_source", "unknown")
                    existing_upstream = existing_current.get("_upstream", "")
                    new_upstream = new_1x2.get("_upstream", UPSTREAM_MAP.get(source, ""))

                    if should_overwrite(source, new_upstream,
                                        existing_source, existing_upstream):
                        new_current["_source"] = source
                        new_current["_upstream"] = new_upstream
                        existing_1x2["current"] = new_current
                    else:
                        log_event(source, "DEBUG", "Lower priority odds kept in sources[]",
                                  cid="odds_merge",
                                  new_rank=_get_source_rank(source, new_upstream),
                                  existing_rank=_get_source_rank(existing_source, existing_upstream))
                else:
                    new_current["_source"] = source
                    existing_1x2["current"] = new_current

            # Opening — first writer wins
            if "opening" in new_1x2 and "opening" not in existing_1x2:
                existing_1x2["opening"] = new_1x2["opening"]

            # Sources — всегда добавляем
            if "sources" not in existing_1x2:
                existing_1x2["sources"] = []
            for src in new_1x2.get("sources", []):
                existing_1x2["sources"].append(src)
            # Ограничиваем длину sources[]
            if len(existing_1x2["sources"]) > 50:
                existing_1x2["sources"] = existing_1x2["sources"][-50:]

            result["1x2"] = existing_1x2
        else:
            # Другие рынки — простая перезапись
            result[market] = new_odds[market]

    return result


# ═══════════════════════════════════════════════════════════
# Core API: patch_match (с §23.2 Idempotency Keys)
# ═══════════════════════════════════════════════════════════

@timed
def patch_match(canonical_id, section, data, source="unknown",
                upstream=None, idempotency_key=None, **kwargs):
    """
    Точечное обновление секции матча (CAS merge-patch).
    §1.4, §1.20 — единый шлюз.
    §23.2 — idempotency_key для защиты от дублей при ретраях CI.
    §22.8 — **kwargs для forward compatibility.

    Возвращает: bool (True если обновлено)
    """
    METRICS.inc("patch_match")

    # §23.2: Idempotency check
    if idempotency_key:
        existing = _check_idempotency(canonical_id, idempotency_key)
        if existing is not None:
            METRICS.inc("patch_match_idempotent_skip")
            return True

    rh = _get_redis()
    if not rh:
        log_event(source, "ERROR", "patch_match: redis_hub not available")
        return False

    key = ns_key(f"match:{canonical_id}", load_config())
    match_obj = rh.get_from_cache(key)

    if not match_obj or not isinstance(match_obj, dict):
        # Попробуем history
        key = ns_key(f"history:match:{canonical_id}", load_config())
        match_obj = rh.get_from_cache(key)

    if not match_obj or not isinstance(match_obj, dict):
        log_event(source, "WARN", "patch_match: match not found",
                  cid=canonical_id)
        METRICS.inc("patch_match_not_found")
        return False

    # Fallback upstream (§1.20)
    if not upstream and section == "odds":
        upstream = UPSTREAM_MAP.get(source, "unknown")

    # Обновление секции
    if section == "odds":
        normalized = _normalize_incoming_odds(data)
        match_obj["odds"] = _merge_odds(
            match_obj.get("odds", {}), normalized, source
        )
    elif section == "status":
        # §24.1: Статус меняется только через set_match_status()
        log_event(source, "WARN",
                  "patch_match: use set_match_status() for status changes. §24.1")
        return False
    else:
        match_obj[section] = data

    match_obj["version"] = match_obj.get("version", 1) + 1
    match_obj["updated_at"] = now_msk()

    # Запись обратно
    rh.save_to_cache(key, serialize_match(match_obj))

    # §23.2: Stamp idempotency
    if idempotency_key:
        _stamp_idempotency(canonical_id, idempotency_key, True)

    log_event(source, "INFO", "patch_match",
              cid=canonical_id, section=section)
    METRICS.inc("patch_match_success")
    return True


# ═══════════════════════════════════════════════════════════
# Core API: get_match_any
# ═══════════════════════════════════════════════════════════

@timed
def get_match_any(canonical_id, namespace="any"):
    """
    Читает матч из live или history. §19.3
    Возвращает dict или None.
    """
    METRICS.inc("get_match_any")
    rh = _get_redis()
    if not rh:
        return None

    config = load_config()

    # Сначала live
    key = ns_key(f"match:{canonical_id}", config)
    match_obj = rh.get_from_cache(key)
    if match_obj and isinstance(match_obj, dict):
        return match_obj

    # Затем history
    key = ns_key(f"history:match:{canonical_id}", config)
    match_obj = rh.get_from_cache(key)
    if match_obj and isinstance(match_obj, dict):
        return match_obj

    return None


# ═══════════════════════════════════════════════════════════
# Core API: save_meta
# ═══════════════════════════════════════════════════════════

def save_meta(collector, **kwargs):
    """
    Сохраняет метаданные коллектора. §1.27
    Автоматически включает метрики (§24.5).
    """
    rh = _get_redis()
    if not rh:
        return

    config = load_config()
    key = ns_key(f"{collector}:meta", config)

    meta = {
        "last_run": now_msk(),
        **kwargs,
        "metrics": METRICS.report(),  # §24.5
    }

    rh.save_to_cache(key, serialize_match(meta))
    log_event(collector, "INFO", "save_meta",
              error_count=kwargs.get("error_count", 0))


# ═══════════════════════════════════════════════════════════
# Core API: cleanup_expired (с §24.7 cleanup_owner)
# ═══════════════════════════════════════════════════════════

def cleanup_expired(dry_run=False, auto_migrate=True):
    """
    Удаляет завершённые матчи. §1.7
    Вызывается только cleanup_owner (§24.7) или любым коллектором,
    если оркестрация не настроена (§1.7a).
    """
    METRICS.inc("cleanup_expired")
    rh = _get_redis()
    if not rh:
        return {"count": 0, "reason": "no_redis"}

    log_event("hub", "INFO", "cleanup_expired starting",
              dry_run=dry_run, auto_migrate=auto_migrate)

    # Реализация зависит от redis_hub API
    # Заглушка — полная реализация в redis_hub.py
    deleted = 0
    migrated = 0

    # ... (делегируется к существующей реализации в redis_hub.py)

    log_event("hub", "INFO", "cleanup_expired done",
              deleted=deleted, migrated=migrated)
    METRICS.inc("cleanup_deleted", deleted)
    return {"count": deleted, "migrated": migrated, "dry_run": dry_run}


# ═══════════════════════════════════════════════════════════
# run_initialization — единая точка входа
# ═══════════════════════════════════════════════════════════

_run_id = None


def run_initialization(collector="unknown"):
    """
    Единая инициализация для всех коллекторов. §1.11
    Выполняет:
    1. Загрузку и валидацию конфигурации (§24.3)
    2. Установку shutdown handler (§23.3)
    3. Генерацию Run ID (§21.2)
    4. Cleanup (с проверкой cleanup_owner, §24.7)
    5. Сброс метрик (§24.5)

    Возвращает dict с метриками инициализации.
    """
    global _run_id

    start = time.monotonic()

    # 1. Конфигурация (§24.3)
    config = load_config()
    errors = get_config_errors()
    if errors:
        for e in errors:
            log_event("hub", "ERROR", f"Config: {e}")

    # 2. Graceful shutdown (§23.3)
    if is_feature_enabled("graceful_shutdown", config):
        install_shutdown_handler()

    # 3. Run ID (§21.2)
    _run_id = f"{collector}_{int(time.time())}_{random.randint(1000, 9999)}"
    log_event("hub", "INFO", "run_initialization",
              collector=collector, run_id=_run_id,
              env=get_env(), hub_version=__version__)

    # 4. Cleanup (§24.7)
    if should_run_cleanup(collector, config):
        cleanup_expired(auto_migrate=is_feature_enabled("auto_migrate", config))
    else:
        log_event(collector, "DEBUG",
                  "Cleanup skipped — not cleanup owner (§24.7)")

    # 5. Reset metrics (§24.5)
    METRICS.reset()
    METRICS.inc("run_initialization")

    elapsed_ms = int((time.monotonic() - start) * 1000)
    METRICS.time("init_latency", elapsed_ms / 1000)

    return {
        "redis_available": _get_redis() is not None,
        "run_id": _run_id,
        "env": get_env(),
        "hub_version": __version__,
        "config_errors": len(errors),
        "init_latency_ms": elapsed_ms,
    }


def get_run_id():
    """Возвращает текущий Run ID. §21.2"""
    return _run_id


# ═══════════════════════════════════════════════════════════
# Module entry point
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Self-test
    init = run_initialization("self_test")
    print(f"\nHub version: {__version__}")
    print(f"Schema version: {SCHEMA_VERSION}")
    print(f"Run ID: {init['run_id']}")
    print(f"Environment: {init['env']}")
    print(f"Config errors: {init['config_errors']}")
    print(f"Init latency: {init['init_latency_ms']}ms")
    print(f"\nMetrics: {json.dumps(METRICS.report(), indent=2)}")
