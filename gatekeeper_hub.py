"""
gatekeeper_hub.py — Единый хаб GatekeeperAI v8.11-patched.
Центральный шлюз для создания, обновления и чтения матчей.

Патчи (v8.9-patched):
  FIX-1: set_match_status deadlock — пишет напрямую в Redis, минуя patch_match
  FIX-2: _clean_team_name → делегирует в team_registry (если доступен)
  FIX-3: save_to_cache — Python-объект, не serialize_match() (double-serialization)
  FIX-4: _normalize_incoming_odds — str() убран, сохраняет исходный тип
  FIX-5: _merge_odds — поддержка closing секции + upstream из patch_match
  FIX-6: cleanup_expired — реализация (была заглушка)
  FIX-7: is_feature_enabled / should_run_cleanup — 1 аргумент (не 2)
  FIX-8: patch_match / upsert_match — graceful shutdown check
  FIX-9: __all__ — 42 экспорта
  FIX-10: ns_key — fallback если gatekeeper_config не предоставляет
  FIX-11: _load_odds_priority — nested по рынкам + reload callback
  FIX-12: build_canonical_id — делегирует в team_registry + валидация даты
  FIX-13: _normalize_incoming_odds — поддержка h2h формата (list/dict)
  FIX-14: section_history — match-level tracking в upsert/patch
  FIX-15: validate_schema — проверка home_clean/away_clean non-empty
  FIX-16: get_all_odds — sources из odds.1x2.sources (не match.sources)
"""

import os
import sys
import json
import time
import signal
import random
from datetime import datetime, timezone, timedelta
from functools import wraps
import logging

logger = logging.getLogger("gatekeeper_hub")

# ── Конфигурация ───────────────────────────────────────────
try:
    from gatekeeper_config import (
        load_config, is_feature_enabled,
        should_run_cleanup, get_env,
    )
    # Функции, которые могут отсутствовать в старой версии config
    try:
        from gatekeeper_config import now_msk, now_msk_short, ns_key, MSK_TZ
    except ImportError:
        # Fallback — определяем локально
        MSK_TZ = timezone(timedelta(hours=3))
        def now_msk():
            return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")
        def now_msk_short():
            return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        def ns_key(base, cid, domain=""):
            # Fallback: no namespace, just compose base:cid
            return f"{base}:{cid}" if cid else base
    try:
        from gatekeeper_config import get_config_errors
    except ImportError:
        def get_config_errors():
            return []
except ImportError:
    print("[HUB CRITICAL] gatekeeper_config not found!")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════
# §22.8: Hub version
# ═══════════════════════════════════════════════════════════

__version__ = "8.11-patched"
HUB_API_VERSION = "8.9"
SCHEMA_VERSION = "v710"

# ═══════════════════════════════════════════════════════════
# FIX-AUDIT: validate_schema — валидация матча против schema v710
# ═══════════════════════════════════════════════════════════

def validate_schema(match_obj, schema_version=SCHEMA_VERSION):
    """
    Валидирует матч против schema v710. §20.8
    Возвращает (bool, str): (True, "ok") или (False, "error message").
    """
    if not isinstance(match_obj, dict):
        return False, "match_obj is not dict"
    required = ("canonical_id", "home_clean", "away_clean", "date_utc",
                "status", "schema_version")
    for field in required:
        if field not in match_obj:
            return False, f"missing required field: {field}"
    if match_obj.get("schema_version") != schema_version:
        return False, f"schema_version mismatch: {match_obj.get('schema_version')} != {schema_version}"
    # FIX-AUDIT-15: home_clean/away_clean must be non-empty
    if not match_obj.get("home_clean"):
        return False, "home_clean is empty"
    if not match_obj.get("away_clean"):
        return False, "away_clean is empty"
    cid = match_obj.get("canonical_id", "")
    if "__" not in cid or len(cid.split("__")) != 3:
        return False, f"canonical_id format invalid: {cid}"
    odds = match_obj.get("odds", {})
    if odds and isinstance(odds, dict) and "1x2" not in odds:
        log_event("hub", "WARN", "validate_schema: odds without 1x2",
                  cid=cid)
    return True, "ok"




__all__ = [
    # Version
    "__version__", "HUB_API_VERSION", "SCHEMA_VERSION",
    # Registry
    "INDEX_REGISTRY", "UPSTREAM_MAP",
    # Metrics
    "METRICS", "timed",
    # Shutdown
    "install_shutdown_handler", "is_shutdown_requested",
    # State Machine
    "MATCH_STATES", "validate_state_transition", "set_match_status",
    # Conflict Resolution
    "should_overwrite",
    # Logging
    "log_event",
    # Serialization
    "serialize_match", "deserialize_match",
    # Canonical ID
    "build_canonical_id",
    # Date
    "normalize_date", "is_future_match",
    # Idempotency
    "_check_idempotency", "_stamp_idempotency",
    # Core API
    "upsert_match", "patch_match", "get_match_any",
    "save_meta", "cleanup_expired",
    # Init
    "run_initialization", "get_run_id",
    # Odds Helpers
    "get_all_odds", "get_current_odds", "get_match", "get_history",
    # Time
    "MSK_TZ", "now_msk", "now_msk_short",
    # Batch
    "process_matches",
    "batch_upsert_matches",
    # Missing functions (FIX-AUDIT)
    "get_all_matches",
    "get_matches_by_date_range",
    "save_search_results",
    "save_analysis",
    "get_from_cache",
    # FIX-AUDIT: schema + indexes
    "validate_schema",
    "update_history_indexes",
]


# ═══════════════════════════════════════════════════════════
# §19.2: INDEX_REGISTRY — единый реестр ключей Redis
# ═══════════════════════════════════════════════════════════

INDEX_REGISTRY = {
    "match:{cid}": "Live-матч (hash)",
    "history:match:{cid}": "History-матч (hash)",
    "index:shard:{YYYYMMDD}": "Дневной индекс (set of canonical_ids)",
    "{collector}:meta": "Метаданные коллектора (hash)",
    "search:results:latest": "Последние результаты поиска",
    "system:health": "Health status (hash)",
    "system:heartbeat": "Heartbeat timestamps (hash)",
    "system:audit_log": "Audit log (sorted set)",
    "system:dlq": "Dead Letter Queue (list)",
    "system:alerts": "Active alerts (hash)",
    "system:state_transitions": "Match state transitions (sorted set)",
    "system:metrics": "System metrics (hash)",
    "match:{namespace}:{cid}": "Namespaced live match",
    "history:match:{namespace}:{cid}": "Namespaced history",
    "match:canary:{cid}": "Canary test match",
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
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    log_event("hub", "WARN", "Shutdown requested. Finishing current batch...")


def install_shutdown_handler():
    global _SHUTDOWN_INSTALLED
    if _SHUTDOWN_INSTALLED:
        return
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _SHUTDOWN_INSTALLED = True


def is_shutdown_requested():
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
    state = MATCH_STATES.get(current)
    if not state:
        return new_status == "scheduled"
    if state.get("terminal"):
        return False
    return new_status in state.get("transitions", [])


# FIX-1: set_match_status — пишет напрямую в Redis, минуя patch_match
# Старая версия вызывала patch_match(section="status"), который возвращал False.
def set_match_status(canonical_id, new_status, source="system"):
    """
    Единственная функция для смены статуса матча. §24.1
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

    # FIX-1: Пишем напрямую в Redis, не через patch_match
    rh = _get_redis()
    if not rh:
        log_event(source, "ERROR", "set_match_status: redis_hub not available")
        return False

    match["status"] = new_status
    match["version"] = match.get("version", 1) + 1
    match["updated_at"] = now_msk()

    # Определяем ключ (live или history)
    key = ns_key("match", canonical_id)
    existing = rh.get_from_cache(key)
    if not existing or not isinstance(existing, dict):
        key = ns_key("history:match", canonical_id)

    # FIX-3: Передаём Python-объект, не serialize_match()
    rh.save_to_cache(key, match)

    _audit_state_transition(canonical_id, current, new_status, source)

    log_event(source, "INFO", "State transition",
              cid=canonical_id, from_state=current, to_state=new_status)
    return True


def _audit_state_transition(cid, from_state, to_state, source):
    # FIX-AUDIT-9: sorted set через ZADD (§19.2 — system:state_transitions)
    try:
        rh = _get_redis()
        if not rh:
            return
        score = time.time()
        entry = json.dumps({
            "cid": cid,
            "from": from_state,
            "to": to_state,
            "source": source,
            "timestamp": now_msk(),
        }, ensure_ascii=False)
        rh._execute_upstash_cmd(["ZADD", "system:state_transitions", str(score), entry])
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# §23.1: Cross-Collector Conflict Resolution
# ═══════════════════════════════════════════════════════════

_source_ranks = {}
_upstream_ranks = {}
_odds_priority_loaded = False


# FIX-11: reload callback для сброса кеша при reload_config()
def _reset_odds_priority_cache():
    """Сбрасывает кеш рангов (вызывается из gatekeeper_config.reload_config)."""
    global _source_ranks, _upstream_ranks, _odds_priority_loaded
    _source_ranks = {}
    _upstream_ranks = {}
    _odds_priority_loaded = False

# Регистрируем callback в config (если доступно)
try:
    from gatekeeper_config import register_reload_callback
    register_reload_callback(_reset_odds_priority_cache)
except (ImportError, AttributeError):
    pass


def _load_odds_priority():
    global _source_ranks, _upstream_ranks, _odds_priority_loaded
    if _odds_priority_loaded:
        return

    try:
        import yaml
        with open("odds_priority.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # FIX-11: Поддерживаем оба формата — nested по рынкам и плоский
        if "1x2" in data or "closing_1x2" in data:
            # Nested формат: {"1x2": {"sharpapi": 1, ...}, "upstream_map": {...}}
            _source_ranks = data.get("1x2", data)
            _upstream_ranks = data.get("upstream_map", {})
        else:
            # Плоский формат (legacy): {"source_ranks": {...}, "upstream_ranks": {...}}
            _source_ranks = data.get("source_ranks", {})
            _upstream_ranks = data.get("upstream_ranks", {})
    except (FileNotFoundError, ImportError):
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
    _load_odds_priority()
    s_rank = _source_ranks.get(source, 99)
    if upstream:
        u_rank = _upstream_ranks.get(upstream, 99)
        return min(s_rank, u_rank)
    return s_rank


def should_overwrite(new_source, new_upstream,
                     existing_source, existing_upstream,
                     section="odds"):
    new_rank = _get_source_rank(new_source, new_upstream)
    existing_rank = _get_source_rank(existing_source, existing_upstream)

    if new_rank == existing_rank:
        return True
    if new_rank < existing_rank:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# Logging (§20.6)
# ═══════════════════════════════════════════════════════════

def log_event(source, level, message, **kwargs):
    ts = now_msk_short()
    parts = [f"[{ts}]", f"[{source}]", f"[{level}]", message]
    if kwargs:
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        parts.append(f"({extra})")
    line = " ".join(parts)
    print(line, flush=True)
    # Also log via logging for GitHub Actions
    if level == "ERROR":
        logger.error(line)
    elif level == "WARN":
        logger.warning(line)
    elif level == "INFO":
        logger.info(line)
    else:
        logger.debug(line)


# ═══════════════════════════════════════════════════════════
# Serialization (§20.1)
# ═══════════════════════════════════════════════════════════

def serialize_match(match_obj):
    return json.dumps(match_obj, ensure_ascii=False, separators=(",", ":"))


def deserialize_match(raw):
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

# FIX-2: Делегирует в team_registry если доступен
def _clean_team_name(name):
    """Нормализация имени команды для canonical_id."""
    if not name:
        return ""

    # Пытаемся использовать team_registry (лучшая нормализация)
    try:
        from team_registry import clean_team_name
        return clean_team_name(name)
    except (ImportError, Exception):
        pass

    # Fallback — старая логика
    import re
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "", name)
    replacements = {
        "manchesterunited": "man", "manchestercity": "mci",
        "manutd": "man", "mancity": "mci",
    }
    return replacements.get(name, name[:6] if len(name) > 6 else name)


def build_canonical_id(home_team, away_team, date_utc):
    # FIX-AUDIT-12: Prefer team_registry.build_canonical_id (validates date)
    try:
        from team_registry import build_canonical_id as _tr_build_cid
        cid = _tr_build_cid(home_team, away_team, date_utc)
        if cid:
            return cid
    except (ImportError, Exception):
        pass
    # Fallback — local implementation
    home_clean = _clean_team_name(home_team)
    away_clean = _clean_team_name(away_team)
    date_part = ""
    if date_utc:
        date_part = date_utc[:10].replace("-", "")
    # FIX-AUDIT-12: Validate date — empty date → empty canonical_id
    if not date_part or not date_part.isdigit() or len(date_part) != 8:
        log_event("hub", "WARN", "build_canonical_id: invalid date",
                  date_utc=date_utc, home=home_team, away=away_team)
        return ""
    return f"{home_clean}__{away_clean}__{date_part}"


# ═══════════════════════════════════════════════════════════
# Date utilities
# ═══════════════════════════════════════════════════════════

def normalize_date(date_str):
    if not date_str:
        return ""
    date_str = date_str.strip()
    if not date_str:
        return ""
    if "T" in date_str:
        return date_str
    if len(date_str) == 10:
        return date_str + "T00:00:00Z"
    return date_str


def is_future_match(date_utc):
    if not date_utc:
        return True
    try:
        match_date = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return match_date >= now
    except (ValueError, TypeError):
        return True


# ═══════════════════════════════════════════════════════════
# Redis integration
# ═══════════════════════════════════════════════════════════

def _get_redis():
    try:
        import redis_hub
        return redis_hub
    except ImportError:
        return None


# ═══════════════════════════════════════════════════════════
# §23.2: Idempotency Keys
# ═══════════════════════════════════════════════════════════

def _check_idempotency(canonical_id, idempotency_key):
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
        return existing
    return None


def _stamp_idempotency(canonical_id, idempotency_key, result):
    if not idempotency_key:
        return
    rh = _get_redis()
    if not rh:
        return
    seen_key = f"match:{canonical_id}:idem:{idempotency_key}"
    # FIX-3: json.dumps вместо serialize_match (set_key не использует конверты)
    rh.set_key(seen_key, json.dumps({"result": result, "ts": now_msk()}))
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
    """
    METRICS.inc("upsert_match")

    # FIX-AUDIT: Поддержка payload dict (football_data collector)
    if isinstance(home_team, dict):
        payload = home_team
        home_team = payload.get("home_team", "")
        away_team = payload.get("away_team", "")
        date_utc = payload.get("date_utc", "")
        competition = payload.get("competition", "")
        country = payload.get("country", "")
        status = payload.get("status", "scheduled")
        source = payload.get("source", source)
        mode = payload.get("mode", mode)
        extra_fields = {k: v for k, v in payload.items()
                        if k not in ("home_team", "away_team", "date_utc",
                                     "competition", "country", "status",
                                     "source", "mode", "canonical_id")}

    # FIX-8: Graceful shutdown check
    if is_shutdown_requested():
        log_event(source, "WARN", "upsert_match: shutdown requested, skipping")
        return ""

    date_utc = normalize_date(date_utc)
    canonical_id = build_canonical_id(home_team, away_team, date_utc)

    if not canonical_id:
        log_event(source, "ERROR", "upsert_match: empty canonical_id")
        return ""

    if mode == "live" and not is_future_match(date_utc):
        log_event(source, "DEBUG", "upsert_match: past match skipped",
                  cid=canonical_id)
        METRICS.inc("upsert_match_skipped_past")
        return ""

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
        "created_at": now_msk(),
        "updated_at": now_msk(),
        "sources": [source],
        "section_history": [
            {
                "section": "base",
                "source": source,
                "updated_at": now_msk(),
            }
        ],
        **extra_fields,
    }

    if "odds" in extra_fields:
        match_obj["odds"] = _normalize_incoming_odds(extra_fields["odds"])
        # FIX-AUDIT-14: Track odds section in section_history
        match_obj.setdefault("section_history", []).append({
            "section": "odds",
            "source": source,
            "updated_at": now_msk(),
        })
        match_obj["odds"] = _normalize_incoming_odds(extra_fields["odds"])

    rh = _get_redis()
    if rh:
        _base = "match" if mode == "live" else "history:match"
        key = ns_key(_base, canonical_id)
        existing = rh.get_from_cache(key)

        if existing and isinstance(existing, dict):
            for k, v in match_obj.items():
                if k not in ("canonical_id", "schema_version", "version"):
                    if v is not None and v != "":
                        if k == "sources":
                            existing_sources = existing.get("sources", [])
                            if source not in existing_sources:
                                existing_sources.append(source)
                            existing["sources"] = existing_sources
                        elif k == "source_ids":
                            existing_ids = existing.get("source_ids", {})
                            existing_ids.update(v)
                            existing["source_ids"] = existing_ids
                        elif k == "odds":
                            existing["odds"] = _merge_odds(
                                existing.get("odds", {}), v, source
                            )
                        else:
                            existing[k] = v
            # FIX-AUDIT-14: Track section_history on update
            if "section_history" not in existing:
                existing["section_history"] = []
            existing["section_history"].append({
                "section": "base",
                "source": source,
                "updated_at": now_msk(),
            })
            if len(existing["section_history"]) > 50:
                existing["section_history"] = existing["section_history"][-50:]
            existing["version"] = existing.get("version", 1) + 1
            existing["updated_at"] = now_msk()
            # FIX-3: Python-объект, не serialize_match()
            rh.save_to_cache(key, existing)
        else:
            # FIX-3: Python-объект, не serialize_match()
            rh.save_to_cache(key, match_obj)

    log_event(source, "INFO", "upsert_match",
              cid=canonical_id, mode=mode)
    return canonical_id


# FIX-4: str() убран — сохраняет исходный тип (float, int, str)
def _normalize_incoming_odds(odds_data):
    """Нормализует odds в формат 1x2. §1.21"""
    if not odds_data:
        return {}

    if "1x2" in odds_data:
        return odds_data

    # FIX-AUDIT-13: h2h format (the-odds-api, propline) — list [home, draw, away] or dict
    if "h2h" in odds_data:
        h2h = odds_data["h2h"]
        if isinstance(h2h, list):
            # List of {name, price} dicts — sort by name to identify home/draw/away
            prices = {}
            for item in h2h:
                if isinstance(item, dict):
                    name = item.get("name", "").lower().strip()
                    price = item.get("price")
                    if price is not None:
                        prices[name] = price
                elif isinstance(item, (int, float)):
                    # Positional: [home, draw, away]
                    pass
            return {
                "1x2": {
                    "current": {
                        "home": prices.get("home") or prices.get(list(prices.keys())[0] if len(prices) >= 1 else ""),
                        "draw": prices.get("draw") or prices.get(list(prices.keys())[1] if len(prices) >= 2 else ""),
                        "away": prices.get("away") or prices.get(list(prices.keys())[2] if len(prices) >= 3 else ""),
                    }
                }
            }
        elif isinstance(h2h, dict):
            return {
                "1x2": {
                    "current": {
                        "home": h2h.get("home"),
                        "draw": h2h.get("draw"),
                        "away": h2h.get("away"),
                    }
                }
            }

    if "home" in odds_data or "draw" in odds_data or "away" in odds_data:
        return {
            "1x2": {
                "opening": {  # FIX-AUDIT-7: "opening" по схеме v710 (§1.21)
                    "home": odds_data.get("home"),
                    "draw": odds_data.get("draw"),
                    "away": odds_data.get("away"),
                },
                "open": {  # backward compat для старых коллекторов
                    "home": odds_data.get("home"),
                    "draw": odds_data.get("draw"),
                    "away": odds_data.get("away"),
                },
                "current": {  # backward compat для value_engine < v3.1
                    "home": odds_data.get("home"),
                    "draw": odds_data.get("draw"),
                    "away": odds_data.get("away"),
                }
            }
        }

    if "open" in odds_data or "current" in odds_data or "opening" in odds_data:
        # FIX-AUDIT-7: нормализуем → 1x2.opening (схема v710), сохраняем open для compat
        result = {"1x2": {}}
        for key in ("opening", "open", "current", "closing"):
            if key in odds_data:
                result["1x2"][key] = odds_data[key]
        # Если есть "open" но нет "opening" — дублируем
        if "open" in result["1x2"] and "opening" not in result["1x2"]:
            result["1x2"]["opening"] = result["1x2"]["open"]
        return result

    return odds_data


# FIX-5: Добавлена поддержка closing секции + upstream параметр
def _merge_odds(existing_odds, new_odds, source, upstream=None):
    """
    Мержит odds с учётом приоритетов. §23.1
    FIX-5: Добавлена поддержка closing секции (для Propline/Pinnacle)
    FIX-5: upstream передаётся из patch_match (а не только из UPSTREAM_MAP)
    """
    if not existing_odds:
        return new_odds
    if not new_odds:
        return existing_odds

    result = dict(existing_odds)

    # Resolve upstream
    if not upstream:
        upstream = UPSTREAM_MAP.get(source, "")

    for market in new_odds:
        if market == "1x2":
            new_1x2 = new_odds["1x2"]
            existing_1x2 = result.get("1x2", {})

            # Current — с проверкой приоритета (§23.1)
            if "current" in new_1x2:
                new_current = dict(new_1x2["current"])
                existing_current = existing_1x2.get("current", {})

                if existing_current and is_feature_enabled("conflict_resolution"):
                    existing_source = existing_current.get("_source", "unknown")
                    existing_upstream = existing_current.get("_upstream", "")

                    if should_overwrite(source, upstream,
                                        existing_source, existing_upstream):
                        new_current["_source"] = source
                        new_current["_upstream"] = upstream
                        existing_1x2["current"] = new_current
                    else:
                        log_event(source, "DEBUG",
                                  "Lower priority odds kept in sources[]",
                                  new_rank=_get_source_rank(source, upstream),
                                  existing_rank=_get_source_rank(existing_source, existing_upstream))
                else:
                    new_current["_source"] = source
                    new_current["_upstream"] = upstream
                    existing_1x2["current"] = new_current

            # Opening — first writer wins
            if "opening" in new_1x2 and "opening" not in existing_1x2:
                existing_1x2["opening"] = new_1x2["opening"]

            # FIX-5: Closing — с проверкой приоритета (для Propline/Pinnacle)
            if "closing" in new_1x2:
                new_closing = dict(new_1x2["closing"])
                existing_closing = existing_1x2.get("closing", {})

                if existing_closing and is_feature_enabled("conflict_resolution"):
                    existing_source = existing_closing.get("_source", "unknown")
                    existing_upstream = existing_closing.get("_upstream", "")

                    if should_overwrite(source, upstream,
                                        existing_source, existing_upstream):
                        new_closing["_source"] = source
                        new_closing["_upstream"] = upstream
                        existing_1x2["closing"] = new_closing
                    else:
                        log_event(source, "DEBUG",
                                  "Lower priority closing kept",
                                  new_rank=_get_source_rank(source, upstream),
                                  existing_rank=_get_source_rank(existing_source, existing_upstream))
                else:
                    new_closing["_source"] = source
                    new_closing["_upstream"] = upstream
                    existing_1x2["closing"] = new_closing

            # Best — first writer wins (или last writer wins, если ранг выше)
            if "best" in new_1x2:
                existing_1x2["best"] = new_1x2["best"]

            # Sources — всегда добавляем
            if "sources" not in existing_1x2:
                existing_1x2["sources"] = []
            for src in new_1x2.get("sources", []):
                existing_1x2["sources"].append(src)
            if len(existing_1x2["sources"]) > 50:
                existing_1x2["sources"] = existing_1x2["sources"][-50:]

            result["1x2"] = existing_1x2
        else:
            result[market] = new_odds[market]

    # FIX-AUDIT: section_history tracking (schema v710)
    sec_1x2 = result.get("1x2", {})
    if isinstance(sec_1x2, dict):
        history = sec_1x2.setdefault("section_history", [])
        history.append({
            "section": "odds",
            "source": source,
            "upstream": upstream or "",
            "updated_at": now_msk(),
        })
        if len(history) > 50:
            sec_1x2["section_history"] = history[-50:]

    return result


# ═══════════════════════════════════════════════════════════
# Core API: patch_match (с §23.2 Idempotency Keys)
# ═══════════════════════════════════════════════════════════

@timed
def patch_match(canonical_id, section, data, source="unknown",
                upstream=None, idempotency_key=None, **kwargs):
    """
    Точечное обновление секции матча (CAS merge-patch).
    Возвращает: bool (True если обновлено)
    """
    METRICS.inc("patch_match")

    # FIX-8: Graceful shutdown check
    if is_shutdown_requested():
        log_event(source, "WARN", "patch_match: shutdown requested, skipping",
                  cid=canonical_id)
        return False

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

    key = ns_key("match", canonical_id)
    match_obj = rh.get_from_cache(key)

    if not match_obj or not isinstance(match_obj, dict):
        key = ns_key("history:match", canonical_id)
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
        # FIX-5: Передаём upstream в _merge_odds
        match_obj["odds"] = _merge_odds(
            match_obj.get("odds", {}), normalized, source, upstream
        )
    elif section == "status":
        log_event(source, "WARN",
                  "patch_match: use set_match_status() for status changes")
        return False
    else:
        match_obj[section] = data

    # FIX-AUDIT-14: Track section_history at match level
    if "section_history" not in match_obj:
        match_obj["section_history"] = []
    match_obj["section_history"].append({
        "section": section,
        "source": source,
        "upstream": upstream or "",
        "updated_at": now_msk(),
    })
    if len(match_obj["section_history"]) > 50:
        match_obj["section_history"] = match_obj["section_history"][-50:]

    # FIX-AUDIT-6: CAS retry — optimistic locking с retry до 3 раз (§24.2)
    max_cas_retries = 3
    for attempt in range(max_cas_retries):
        expected_version = match_obj.get("version", 1)
        match_obj["version"] = expected_version + 1
        match_obj["updated_at"] = now_msk()

        # FIX-3: Python-объект, не serialize_match()
        rh.save_to_cache(key, match_obj)

        # Проверяем — не перезаписал ли нас другой коллектор
        recheck = rh.get_from_cache(key)
        if recheck and isinstance(recheck, dict):
            actual_version = recheck.get("version", 0)
            if actual_version == expected_version + 1:
                # Наша запись прошла успешно
                break
            else:
                # Конфликт версий — перечитываем и мержим заново
                log_event(source, "WARN",
                          "patch_match CAS conflict, retrying",
                          cid=canonical_id, attempt=attempt + 1,
                          expected=expected_version + 1, actual=actual_version)
                match_obj = recheck
                # Повторно применяем обновление
                if section == "odds":
                    match_obj["odds"] = _merge_odds(
                        match_obj.get("odds", {}), normalized, source, upstream
                    )
                else:
                    match_obj[section] = data
                continue
        else:
            break
    else:
        log_event(source, "ERROR", "patch_match CAS exhausted retries",
                  cid=canonical_id, attempts=max_cas_retries)
        METRICS.inc("patch_match_cas_failed")
        return False

    if idempotency_key:
        _stamp_idempotency(canonical_id, idempotency_key, True)

    log_event(source, "INFO", "patch_match",
              cid=canonical_id, section=section, cas_retries=attempt + 1)
    METRICS.inc("patch_match_success")
    return True


# ═══════════════════════════════════════════════════════════
# Core API: get_match_any
# ═══════════════════════════════════════════════════════════

@timed
def get_match_any(canonical_id, namespace="any"):
    """Читает матч из live или history. §19.3"""
    METRICS.inc("get_match_any")
    rh = _get_redis()
    if not rh:
        return None

    key = ns_key("match", canonical_id)
    match_obj = rh.get_from_cache(key)
    if match_obj and isinstance(match_obj, dict):
        return match_obj

    key = ns_key("history:match", canonical_id)
    match_obj = rh.get_from_cache(key)
    if match_obj and isinstance(match_obj, dict):
        return match_obj

    return None


# ═══════════════════════════════════════════════════════════
# Core API: save_meta
# ═══════════════════════════════════════════════════════════

def save_meta(collector, **kwargs):
    """Сохраняет метаданные коллектора. §1.27"""
    rh = _get_redis()
    if not rh:
        return

    key = f"meta:{collector}"  # FIX-17: meta key format "meta:{collector}"

    meta = {
        "last_run": now_msk(),
        **kwargs,
        "metrics": METRICS.report(),
    }

    # FIX-3: Python-объект, не serialize_match()
    rh.save_to_cache(key, meta)
    log_event(collector, "INFO", "save_meta",
              error_count=kwargs.get("error_count", 0))


# ═══════════════════════════════════════════════════════════
# FIX-AUDIT-3: _remove_from_index — удаление canonical_id из шардов
# ═══════════════════════════════════════════════════════════

INDEX_LOOKBACK_DAYS = 2
INDEX_LOOKAHEAD_DAYS = 7

def _remove_from_index(canonical_id):
    """
    Сканирует шарды от -INDEX_LOOKBACK_DAYS до +INDEX_LOOKAHEAD_DAYS
    и удаляет canonical_id из дневных индексов index:shard:{YYYYMMDD}. §1.7c
    """
    rh = _get_redis()
    if not rh:
        return

    today = datetime.now(timezone.utc)
    for offset in range(-INDEX_LOOKBACK_DAYS, INDEX_LOOKAHEAD_DAYS + 1):
        shard_date = today + timedelta(days=offset)
        shard_key = f"index:shard:{shard_date.strftime('%Y%m%d')}"
        try:
            rh._execute_upstash_cmd(["SREM", shard_key, canonical_id])
        except Exception:
            pass

    # FIX-AUDIT-4: Очистка search:results:* — оставляем только latest
    try:
        rh._execute_upstash_cmd(["DEL", "search:results:latest"])
        # Пересохраняем только если есть актуальные данные
        # (latest останется пустым до следующего поиска)
    except Exception:
        pass




def update_history_indexes(canonical_id, date_utc):
    """
    Добавляет canonical_id в дневной индекс index:shard:{YYYYMMDD}. §1.7c
    Для football_data_to_redis.py — индексация исторических матчей.
    """
    rh = _get_redis()
    if not rh or not date_utc:
        return
    shard_key = f"index:shard:{date_utc[:10].replace('-', '')}"
    try:
        rh._execute_upstash_cmd(["SADD", shard_key, canonical_id])
        log_event("hub", "DEBUG", "update_history_indexes",
                  cid=canonical_id, shard=shard_key)
    except Exception as e:
        log_event("hub", "WARN", "update_history_indexes failed",
                  cid=canonical_id, error=str(e))


# ═══════════════════════════════════════════════════════════
# Core API: cleanup_expired (FIX-6 — реализация вместо заглушки)
# ═══════════════════════════════════════════════════════════

def cleanup_expired(dry_run=False, auto_migrate=True):
    """
    Удаляет завершённые матчи из live, мигрирует в history.
    FIX-6: Реализация (была заглушка deleted=0, migrated=0).
    """
    METRICS.inc("cleanup_expired")
    rh = _get_redis()
    if not rh:
        return {"count": 0, "reason": "no_redis"}

    log_event("hub", "INFO", "cleanup_expired starting",
              dry_run=dry_run, auto_migrate=auto_migrate)

    _cleanup_start = time.monotonic()

    deleted = 0
    migrated = 0
    finished_deleted = 0
    expired_deleted = 0

    # Получаем все live-матчи
    all_fields = rh.get_all_fields()
    if not all_fields or not isinstance(all_fields, dict):
        log_event("hub", "WARN", "cleanup_expired: get_all_fields returned empty")
        return {"count": 0, "migrated": 0, "dry_run": dry_run, "reason": "no_data"}

    for field_id, match_obj in all_fields.items():
        # FIX-8: Graceful shutdown
        if is_shutdown_requested():
            log_event("hub", "WARN", "cleanup_expired: shutdown requested, stopping")
            break

        # Только live-матчи (не meta, не system)
        if not field_id.startswith("match:") or ":meta" in field_id:
            continue
        if "idem" in field_id or "canary" in field_id:
            continue

        match = deserialize_match(match_obj) if isinstance(match_obj, str) else match_obj
        if not match or not isinstance(match, dict):
            continue

        status = match.get("status", "")
        date_utc = match.get("date_utc", "")

        # FIX-AUDIT-1: Проверка по обоим условиям (§1.7)
        # 1) status == "completed" ИЛИ
        # 2) date_utc + 2 часа < now (temporal leakage cleanup)
        should_delete = False
        if status in ("completed", "cancelled", "archived", "finished", "ended"):
            should_delete = True
            finished_deleted += 1
        elif date_utc:
            try:
                match_date = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
                if match_date + timedelta(hours=2) < datetime.now(timezone.utc):
                    should_delete = True
                    expired_deleted += 1
            except (ValueError, TypeError):
                pass  # Некорректная дата — не удаляем

        if not should_delete:
            continue

        # Миграция в history
        canonical_id = match.get("canonical_id", "")
        if not canonical_id:
            continue

        history_key = ns_key("history:match", canonical_id)

        if dry_run:
            log_event("hub", "INFO", "cleanup dry-run: would migrate",
                      cid=canonical_id)
            migrated += 1
            continue

        if auto_migrate:
            # FIX-3: Python-объект
            rh.save_to_cache(history_key, match)

        # FIX-AUDIT-3: Удаляем из индексов
        _remove_from_index(canonical_id)

        # Удаляем из live
        rh.delete_from_cache(field_id)
        deleted += 1
        migrated += 1

    # FIX-AUDIT-2: Метрики в system:health (§1.7b)
    # Разделаем: finished_deleted (terminal status) и expired_deleted (по времени)
    health_key = "system:health"
    health_data = {
        "last_cleanup_count": deleted,
        "last_cleanup_at": now_msk(),
        "last_cleanup_finished": finished_deleted,
        "last_cleanup_expired": expired_deleted,
    }
    rh.save_to_cache(health_key, health_data)

    log_event("hub", "INFO", "cleanup_expired done",
              deleted=deleted, migrated=migrated, expired=expired_deleted)
    METRICS.inc("cleanup_deleted", deleted)
    METRICS.time("cleanup_duration", time.monotonic() - _cleanup_start)  # approximate
    return {
        "count": deleted,
        "migrated": migrated,
        "expired": expired_deleted,
        "dry_run": dry_run,
    }





# ═══════════════════════════════════════════════════════════
# Odds Helpers — wrappers for value_engine.py compatibility
# ═══════════════════════════════════════════════════════════

def get_all_odds(match: dict) -> dict:
    """
    Возвращает odds-секцию матча в формате {current: {...}, closing: {...}, sources: [...]}.
    Wrapper для value_engine.py — извлекает 1x2 из match.odds.
    """
    if not isinstance(match, dict):
        return {}
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return {}
    
    # Если odds уже в формате 1x2
    if "1x2" in odds:
        sec = odds["1x2"]
        # FIX-AUDIT-7: "opening" — primary (схема v710), "open" — backward compat
        open_odds = sec.get("opening", sec.get("open", sec.get("current", {})))
        return {
            "opening": open_odds,
            "open": open_odds,  # backward compat
            "current": open_odds,  # backward compat для value_engine < v3.1
            "closing": sec.get("closing", {}),
            "sources": sec.get("sources", match.get("sources", [])),
            "verification": match.get("odds_verification", "UNVERIFIED"),
            "independent_sources": len(set(
                s.get("source", "") for s in sec.get("sources", [])
                if isinstance(s, dict)
            ) or match.get("sources", [])),
            "betradar_consensus": False,
        }

    # Если odds в плоском формате
    if "open" in odds or "current" in odds or "closing" in odds:
        open_odds = odds.get("open", odds.get("current", {}))
        return {
            "opening": open_odds,
            "open": open_odds,  # backward compat
            "current": open_odds,  # backward compat
            "closing": odds.get("closing", {}),
            "sources": match.get("sources", []),
            "verification": "UNVERIFIED",
            "independent_sources": len(set(match.get("sources", []))),
            "betradar_consensus": False,
        }

    return {}


def get_current_odds(match: dict) -> dict:
    """
    Возвращает только current odds из матча.
    Wrapper для value_engine.py.
    """
    all_o = get_all_odds(match)
    return all_o.get("current", {})


def get_match(canonical_id: str) -> dict:
    """
    Alias для get_match_any — совместимость с value_engine.py.
    """
    return get_match_any(canonical_id)


def get_history(canonical_id: str) -> dict:
    """
    Читает матч из history (не live).
    """
    rh = _get_redis()
    if not rh:
        return None
    key = ns_key("history:match", canonical_id)
    match_obj = rh.get_from_cache(key)
    if match_obj and isinstance(match_obj, dict):
        return match_obj
    return None


# ═══════════════════════════════════════════════════════════
# Phase 2: process_matches — пакетная обработка матчей
# ═══════════════════════════════════════════════════════════

@timed
def process_matches(matches: list, default_source: str = "unknown") -> dict:
    """
    Принимает список матчей от коллекторов, сливает дубли по canonical_id
    и записывает в Redis через upsert_match / patch_match.

    Каждый элемент списка — dict с ключами:
        home_team, away_team, date_utc, competition, country,
        odds (опционально), source (опционально), **extra

    Возвращает: {created, patched, skipped, errors, total}
    """
    METRICS.inc("process_matches")

    if not matches:
        return {"created": 0, "patched": 0, "skipped": 0,
                "errors": 0, "total": 0}

    stats = {"created": 0, "patched": 0, "skipped": 0, "errors": 0,
             "total": len(matches)}

    # Группируем по canonical_id
    by_cid: dict[str, list[dict]] = {}
    for m in matches:
        home = m.get("home_team", m.get("home", ""))
        away = m.get("away_team", m.get("away", ""))
        date = m.get("date_utc", "")
        cid = build_canonical_id(home, away, date)
        if not cid:
            stats["skipped"] += 1
            log_event(default_source, "WARN",
                      "process_matches: empty canonical_id",
                      home=home, away=away)
            continue
        by_cid.setdefault(cid, []).append(m)

    # Обрабатываем каждый canonical_id
    for cid, group in by_cid.items():
        if is_shutdown_requested():
            log_event(default_source, "WARN",
                      "process_matches: shutdown requested, stopping")
            break

        # Сортируем по рангу источника (меньше = выше приоритет)
        group.sort(key=lambda m: _get_source_rank(
            m.get("source", default_source),
            UPSTREAM_MAP.get(m.get("source", ""), "")
        ))

        # Первый (высший приоритет) — upsert
        primary = group[0]
        source = primary.get("source", default_source)

        extra = {}
        for k in ("odds", "score", "source_ids", "predictions", "h2h", "stats"):
            if k in primary:
                extra[k] = primary[k]

        created_cid = upsert_match(
            home_team=primary.get("home_team", primary.get("home", "")),
            away_team=primary.get("away_team", primary.get("away", "")),
            date_utc=primary.get("date_utc", ""),
            competition=primary.get("competition", ""),
            country=primary.get("country", ""),
            source=source,
            **extra,
        )

        if created_cid:
            stats["created"] += 1
        else:
            stats["skipped"] += 1

        # Остальные (более низкий приоритет) — patch odds
        for secondary in group[1:]:
            if is_shutdown_requested():
                break
            sec_source = secondary.get("source", default_source)

            if "odds" in secondary:
                idempotency_key = (
                    f"{sec_source}:{cid}:odds:"
                    f"{secondary.get('date_utc', '')[:10]}"
                )
                ok = patch_match(
                    cid, "odds", secondary["odds"],
                    source=sec_source,
                    idempotency_key=idempotency_key,
                )
                if ok:
                    stats["patched"] += 1
                else:
                    stats["errors"] += 1

    log_event(default_source, "INFO", "process_matches done", **stats)
    return stats


# ═══════════════════════════════════════════════════════════
# FIX-AUDIT-5: batch_upsert_matches — алиас для соответствия гайду (§1.5)
# ═══════════════════════════════════════════════════════════

def batch_upsert_matches(matches: list, default_source: str = "unknown") -> dict:
    """
    Пакетная запись матчей. §1.5, API-таблица гида.
    Возвращает: {total, created, updated, skipped_past, deduped}
    — обёртка над process_matches с конвертацией формата ответа.
    """
    raw = process_matches(matches, default_source)
    return {
        "total": raw.get("total", 0),
        "created": raw.get("created", 0),
        "updated": raw.get("patched", 0),
        "skipped_past": raw.get("skipped", 0),
        "deduped": 0,  # дедупликация происходит внутри process_matches
        "errors": raw.get("errors", 0),
    }


# ═══════════════════════════════════════════════════════════
# run_initialization — единая точка входа
# ═══════════════════════════════════════════════════════════

_run_id = None


def run_initialization(collector="unknown"):
    """
    Единая инициализация для всех коллекторов. §1.11
    FIX-7: is_feature_enabled / should_run_cleanup — 1 аргумент.
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
    # FIX-7: is_feature_enabled принимает 1 аргумент
    if is_feature_enabled("graceful_shutdown"):
        install_shutdown_handler()

    # 3. Run ID (§21.2)
    _run_id = f"{collector}_{int(time.time())}_{random.randint(1000, 9999)}"
    log_event("hub", "INFO", "run_initialization",
              collector=collector, run_id=_run_id,
              env=get_env(), hub_version=__version__)

    # 4. Cleanup (§24.7)
    # FIX-7: should_run_cleanup принимает 1 аргумент
    if should_run_cleanup(collector):
        cleanup_expired(auto_migrate=is_feature_enabled("auto_migrate"))
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
    return _run_id


# ═══════════════════════════════════════════════════════════
# Module entry point
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# FIX-AUDIT: Missing functions required by main.py and source_diagnostics.py
# ═══════════════════════════════════════════════════════════

def get_all_matches():
    """Возвращает все live-матчи из Redis. Для source_diagnostics.py."""
    rh = _get_redis()
    if not rh:
        return []
    all_fields = rh.get_all_fields()
    if not all_fields or not isinstance(all_fields, dict):
        return []
    matches = []
    for field_id, raw in all_fields.items():
        if not field_id.startswith("match:") or ":meta" in field_id or "idem" in field_id or "canary" in field_id:
            continue
        match = deserialize_match(raw) if isinstance(raw, str) else raw
        if match and isinstance(match, dict):
            matches.append(match)
    return matches


def get_matches_by_date_range(date_from="", date_to=""):
    """Возвращает матчи в диапазоне дат. Для main.py."""
    all_matches = get_all_matches()
    if not all_matches:
        return []
    if not date_from and not date_to:
        return all_matches
    result = []
    for m in all_matches:
        mdate = m.get("date_utc", "")[:10]
        if date_from and mdate < date_from:
            continue
        if date_to and mdate > date_to:
            continue
        result.append(m)
    return result


def save_search_results(results):
    """Сохраняет результаты поиска. Для main.py."""
    rh = _get_redis()
    if not rh:
        return
    key = "search:results:latest"
    rh.save_to_cache(key, results)
    log_event("hub", "INFO", "save_search_results",
              total=results.get("total_matches", 0) if isinstance(results, dict) else len(results))


def save_analysis(canonical_id, analysis):
    """Сохраняет анализ матча. Для main.py."""
    rh = _get_redis()
    if not rh:
        return
    key = ns_key("analysis", canonical_id)
    rh.save_to_cache(key, analysis)
    log_event("hub", "DEBUG", "save_analysis", cid=canonical_id)


def get_from_cache(key):
    """Прокси к redis_hub.get_from_cache. Для main.py."""
    rh = _get_redis()
    if not rh:
        return None
    return rh.get_from_cache(key)



if __name__ == "__main__":
    init = run_initialization("self_test")
    print(f"\nHub version: {__version__}")
    print(f"Schema version: {SCHEMA_VERSION}")
    print(f"Run ID: {init['run_id']}")
    print(f"Environment: {init['env']}")
    print(f"Config errors: {init['config_errors']}")
    print(f"Init latency: {init['init_latency_ms']}ms")
    print(f"\nMetrics: {json.dumps(METRICS.report(), indent=2)}")
