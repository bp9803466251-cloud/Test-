#!/usr/bin/env python3
"""
Collector Bzzoiro v2.2 (API v2 — predictions for pre-match + league pre-fetch)
GatekeeperAI v710 — Фаза 2

Изменения v2.2 (Фаза 2):
  - run_initialization(collector="bzzoiro") — §2.6
  - DAYS_AHEAD из ENV (BZZOIRO_DAYS_AHEAD) — sync с YAML
  - upsert_match: +sources=["bzzoiro"], +idempotency_key — §2.2
  - save_meta: +run_id — трассировка
  - COLLECTOR_NAME константа

Изменения v700: upstream="opta" во всех patch_match, odds в формате {current: {...}}

FIX v700.1:
  - save_meta: enrichment как nested dict + events_only flag
  - __main__: argparse для --events-only

v8.12-bulk:
  - Bulk odds: /odds/?date_from=&date_to= вместо 140 × /events/{id}/odds
  - Bulk predictions: /predictions/?date_from=&date_to= вместо 140 × /events/{id}/predictions
  - Per-event fallback если bulk пуст
  - HTTP timeout: 30→10с
  - Progress logging: каждые 10 матчей
"""

import os
import sys
import time
import json
import argparse
import datetime as dt
from typing import Any, Optional
import logging
from collections import defaultdict

import urllib.request
import urllib.error

logger = logging.getLogger("bzzoiro")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

try:
    from gatekeeper_hub import (
        run_initialization,
        upsert_match,
        patch_match,
        get_all_fields,
        normalize_date,
        is_future_match,
        now_msk,
        save_meta,
    )
except ImportError:
    logger.error("gatekeeper_hub не найден")
    sys.exit(1)

try:
    from gatekeeper_hub import is_shutdown_requested as _is_shutdown
except ImportError:
    def _is_shutdown():
        return False


# §20.7: register_module + §20.6: log_event
try:
    from gatekeeper_hub import register_module, log_event
except ImportError:
    def register_module(name, role="collector", writes=None, reads=None):
        def decorator(func):
            return func
        return decorator
    def log_event(source, level, message, **kwargs):
        pass
try:
    from team_registry import normalize_team_name, build_canonical_id, clean_team_name
except ImportError:
    logger.error("team_registry не найден")
    normalize_team_name = lambda x: x.strip().lower() if x else ""
    build_canonical_id = lambda h, a, d: f"{h}__{a}__{d[:10].replace('-','')}"
    clean_team_name = lambda name: name.lower().strip().replace(" ", "_") if name else ""

_NOT_FOUND = object()
DEBUG_EVENT_COUNT = int(os.environ.get("DEBUG_EVENT_COUNT", "3"))

COLLECTOR_NAME = "bzzoiro"

BZZOIRO_BASE = os.environ.get("BZZOIRO_BASE_URL", "https://sports.bzzoiro.com/api/v2")
BZZOIRO_API_KEY = os.environ.get("BZZOIRO_API_KEY", "")
RATE_DELAY = float(os.environ.get("BZZOIRO_RATE_DELAY", "0.3"))
ENRICH_DELAY = float(os.environ.get("BZZOIRO_ENRICH_DELAY", "0.2"))
MAX_RETRIES = int(os.environ.get("BZZOIRO_MAX_RETRIES", "1"))
DAYS_AHEAD = int(os.environ.get("BZZOIRO_DAYS_AHEAD", "7"))
PAGE_LIMIT = 200

BZZOIRO_UPSTREAM = "opta"

__version__ = "8.12-bulk"
__all__ = [
    "collect_bzzoiro",
    "collect_and_process",
    "__version__",
]

FINISHED_STATUSES = {"finished", "completed", "ended", "cancelled", "awarded", "forfeited"}
PREMATCH_STATUSES = ("notstarted", "", "scheduled", "postponed")

_cache = {}

def _cache_response(url: str, data: dict):
    _cache[url] = data

def _check_cache(url: str) -> Any:
    return _cache.get(url)


def _fetch_bzzoiro(url: str, headers: dict, max_retries: int = 1) -> Any:
    cached_data = _check_cache(url)
    if cached_data is not None:
        return cached_data

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)

            if resp.status == 404:
                _cache_response(url, _NOT_FOUND)
                return _NOT_FOUND
            if resp.status == 429:
                wait = 2 ** (attempt + 2)
                logger.warning(f"Rate limit. Waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status != 200:
                if attempt < max_retries:
                    time.sleep(RATE_DELAY * 2)
                    continue
                logger.error(f"HTTP {resp.status}: {url}")
                return None

            data = json.loads(resp.read().decode("utf-8"))
            _cache_response(url, data)
            time.sleep(RATE_DELAY)
            return data

        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 2)
                continue
            logger.error(f"Request error: {e}")
            return None


def _normalize_odds_value(val) -> Optional[float]:
    """FIX-1: Конвертирует odds в float, None для невалидных."""
    if val is None or val == "-" or val == "" or val == 0:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_event_date(ev: dict) -> dt.datetime:
    date_str = _extract_date(ev)
    if not date_str:
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        try:
            return dt.datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            return dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def _extract_date(ev: dict) -> str:
    for key in ("event_date", "date", "date_utc", "match_date", "time", "start_time", "datetime"):
        val = ev.get(key)
        if val:
            return str(val)
    timestamp = ev.get("timestamp")
    if timestamp:
        try:
            return dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc).isoformat()
        except Exception:
            pass
    return ""


def _extract_score(ev: dict) -> Any:
    score = ev.get("score") or ev.get("scores")
    if isinstance(score, dict):
        return {
            "home": score.get("home", score.get("home_score")),
            "away": score.get("away", score.get("away_score"))
        }
    home_score = ev.get("home_score")
    away_score = ev.get("away_score")
    if home_score is not None or away_score is not None:
        return {"home": home_score, "away": away_score}
    return None


def _extract_best_odds(odds_data: dict) -> dict:
    """Извлекает лучшие коэффициенты. Возвращает v710 формат {current: {home, draw, away}}.
    FIX-1: odds как float, не str.
    """
    if not isinstance(odds_data, dict):
        return {}
    odds = odds_data.get("odds", odds_data)
    if not isinstance(odds, dict):
        return {}

    result = {}
    mapping = {
        "home": ("home_win", "home", "1"),
        "draw": ("draw", "X"),
        "away": ("away_win", "away", "2")
    }

    for target_key, keys in mapping.items():
        val = None
        for k in keys:
            if k in odds and odds[k] is not None:
                val = odds[k]
                break

        # FIX-1: float вместо str
        float_val = _normalize_odds_value(val)
        if float_val is not None:
            result[target_key] = float_val

    if not result:
        return {}

    return {"current": result}


def _flatten_stats(stats_data: dict, parent_key: str = "", sep: str = "_") -> dict:
    items = []
    if not isinstance(stats_data, dict):
        return {}
    for k, v in stats_data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_stats(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


_league_cache: dict[int, dict] = {}

def _fetch_league_info(league_id: int, headers: dict) -> dict:
    if not league_id:
        return {"name": "", "country": ""}
    if league_id in _league_cache:
        return _league_cache[league_id]

    url = f"{BZZOIRO_BASE}/leagues/{league_id}"
    data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)

    if data is _NOT_FOUND or not data or not isinstance(data, dict):
        result = {"name": "", "country": ""}
    else:
        name = data.get("name", "") or ""
        country = data.get("country", "") or ""
        result = {"name": name, "country": country}

    _league_cache[league_id] = result
    return result



def _fetch_bulk_data(base_url: str, headers: dict, date_from: str, date_to: str,
                     label: str = "bulk") -> dict[int, dict]:
    """Bulk-загрузка odds/predictions с пагинацией.
    Возвращает словарь {event_id: data} для быстрого lookup.
    Fallback: если bulk не работает — возвращает пустой dict,
    вызывающий код переключится на per-event.
    """
    result: dict[int, dict] = {}
    url = f"{base_url}?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"
    pages = 0

    while url and pages < 10:  # safety limit: 10 страниц × 200 = 2000 записей
        if _is_shutdown():
            logger.info(f"[BZZOIRO] Shutdown — bulk {label} прерван")
            break

        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        pages += 1

        if data is None or data is _NOT_FOUND:
            logger.info(f"[BZZOIRO] Bulk {label}: нет данных (страница {pages})")
            break

        if isinstance(data, dict):
            items = data.get("results", data.get("data", []))
        elif isinstance(data, list):
            items = data
        else:
            break

        if not isinstance(items, list) or not items:
            break

        for item in items:
            # event_id может быть под разными ключами
            eid = (item.get("event_id") or item.get("event") or
                   item.get("match_id") or item.get("id"))
            if eid:
                result[int(eid)] = item

        total_count = data.get("count", 0) if isinstance(data, dict) else 0
        logger.info(f"[BZZOIRO] Bulk {label}: {len(result)}/{total_count} (стр.{pages})")

        if isinstance(data, dict):
            url = data.get("next")
        else:
            url = None

        if not url:
            break
        if total_count and len(result) >= total_count:
            break

    logger.info(f"[BZZOIRO] Bulk {label}: всего {len(result)} записей ({pages} стр.)")
    return result


def _extract_odds_from_bulk(bulk_item: dict) -> dict:
    """Извлекает odds из bulk-ответа (формат /odds/)."""
    return _extract_best_odds(bulk_item)


def _extract_pred_from_bulk(bulk_item: dict) -> dict:
    """Извлекает predictions из bulk-ответа (формат /predictions/)."""
    inner = bulk_item.get("prediction", bulk_item)
    if isinstance(inner, dict) and inner:
        return inner
    return {}


def collect_bzzoiro(events_only: bool = False) -> dict:
    headers = {
        "Authorization": f"Token {BZZOIRO_API_KEY}",
        "Accept": "application/json",
    }

    logger.info("=" * 60)
    logger.info(f"[BZZOIRO] Collector Bzzoiro v{__version__} started.")
    logger.info("=" * 60)

    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    
    logger.info("[BZZOIRO] Шаг 0: Инициализация Redis...")
    # §2.6: передаём collector= для трассировки
    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    if not init_metrics or not init_metrics.get("redis_available"):
        logger.info("[BZZOIRO] ERROR: Redis init failed")
        save_meta(COLLECTOR_NAME, stored_matches=0, error_count=1,
                  events_only=events_only, run_id="")
        return {"error": "redis_init_failed"}

    run_id = init_metrics.get("run_id", "")
    logger.info(f"[BZZOIRO] Run ID: {run_id}")
    logger.info(f"[BZZOIRO] Cleanup: {init_metrics.get('cleanup_count', 0)} ключей удалено")

    existing_keys = set(get_all_fields().keys())

    # --- Шаг 1: Загрузка событий ---
    logger.info("[BZZOIRO] Шаг 1: Загрузка событий...")
    log_event("bzzoiro", "INFO", "Collection started", days_ahead=DAYS_AHEAD, dry_run=dry_run)
    now = dt.datetime.now(dt.timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + dt.timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    pages_fetched = 0
    url = f"{BZZOIRO_BASE}/events?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"

    while url:
        # FIX-4: Graceful shutdown
        if _is_shutdown():
            logger.info("[BZZOIRO] Shutdown requested — остановка загрузки событий")
            break

        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        pages_fetched += 1

        if data is None:
            logger.info(f"[BZZOIRO] Ошибка загрузки страницы {pages_fetched}")
            break
        if data is _NOT_FOUND:
            break

        if isinstance(data, dict):
            items = data.get("results", data.get("data", data.get("events", [])))
        elif isinstance(data, list):
            items = data
        else:
            break

        if not isinstance(items, list) or not items:
            break

        all_events.extend(items)
        total_count = data.get("count", 0) if isinstance(data, dict) else 0
        logger.info(f"[BZZOIRO] Загружено: {len(all_events)}/{total_count}")

        if isinstance(data, dict):
            url = data.get("next")
        else:
            url = None

        if not url:
            break

        if total_count and len(all_events) >= total_count:
            break

    all_events.sort(key=_parse_event_date)
    logger.info(f"[BZZOIRO] Всего событий: {len(all_events)} (страниц: {pages_fetched})")

    # --- Шаг 1.5: Pre-fetch лиг ---
    league_ids = set()
    for ev in all_events:
        lid = ev.get("league_id")
        if lid:
            league_ids.add(lid)

    if league_ids:
        logger.info(f"[BZZOIRO] Шаг 1.5: Pre-fetch {len(league_ids)} лиг...")
        for lid in league_ids:
            # FIX-4: Graceful shutdown
            if _is_shutdown():
                logger.info("[BZZOIRO] Shutdown requested — пропуск pre-fetch лиг")
                break
            _fetch_league_info(lid, headers)
            time.sleep(RATE_DELAY)
        logger.info(f"[BZZOIRO] Pre-fetch лиг завершён: {len(_league_cache)} в кэше")

    # --- Шаг 2: Запись матчей ---
    logger.info("[BZZOIRO] Шаг 2: Запись матчей в Redis...")
    stored_matches: list[tuple[str, int, dict]] = []
    created = 0
    upsert_errors = 0
    updated = 0
    skipped_past = 0
    skipped_finished = 0
    deduped = 0
    no_date_count = 0
    seen = set()

    for idx, ev in enumerate(all_events):
        # FIX-4: Graceful shutdown
        if _is_shutdown():
            logger.info("[BZZOIRO] Shutdown requested — остановка записи матчей")
            break

        bzzoiro_id = ev.get("id")
        if not bzzoiro_id:
            continue

        home_team = ev.get("home_team", "")
        away_team = ev.get("away_team", "")
        if not home_team or not away_team:
            continue

        # FIX-2: Нормализация команд через team_registry
        try:
            home_norm = clean_team_name(home_team)
            away_norm = clean_team_name(away_team)
        except Exception as e:
            logger.error(f"[BZZOIRO] clean_team_name error #{idx}: {e}")
            continue

        status = ev.get("status", "scheduled") or "scheduled"

        if status in FINISHED_STATUSES:
            skipped_finished += 1
            continue

        date_str = _extract_date(ev)
        date_str = normalize_date(date_str)  # §1.5: normalize to ISO 8601 UTC
        # FIX-8: dedup_key через нормализованные имена
        dedup_key = f"{home_norm}|{away_norm}|{date_str}"
        if dedup_key in seen:
            deduped += 1
            continue
        seen.add(dedup_key)

        if not date_str:
            no_date_count += 1

        if date_str and not is_future_match(date_str):
            skipped_past += 1
            continue

        if idx < DEBUG_EVENT_COUNT:
            logger.debug(f"[BZZOIRO DEBUG] Событие #{idx}: keys={list(ev.keys())}")  # §1.23: no payload

        league_id = ev.get("league_id")
        league_info = _fetch_league_info(league_id, headers) if league_id else {"name": "", "country": ""}

        competition = league_info.get("name", "") or ev.get("stage_name", "") or ev.get("stage", "")
        country = league_info.get("country", "")

        # §2.2: idempotency_key + sources для upsert_match
        if dry_run:
            created += 1
            stored_matches.append((f"dry_run_{bzzoiro_id}", bzzoiro_id, ev))
            continue
        try:
            result_id = upsert_match(
                home_team=home_norm,
                away_team=away_norm,
                date_utc=date_str,
                competition=competition,
                country=country,
                status=status,
                source=COLLECTOR_NAME,
                sources=[COLLECTOR_NAME],
                source_ids={COLLECTOR_NAME: str(bzzoiro_id)},
                idempotency_key=f"{run_id}:{home_norm}__{away_norm}__{date_str[:10].replace('-', '')}" if run_id else None,
            )
        except Exception as e:
            logger.error(f"[BZZOIRO] upsert error for event #{idx}: {e}")
            result_id = None
        if result_id:
            if f"match:{result_id}" in existing_keys:
                updated += 1
            else:
                created += 1
            stored_matches.append((result_id, bzzoiro_id, ev))
        else:
            upsert_errors += 1

    logger.info(f"[BZZOIRO] Записано {len(stored_matches)} матчей (создано {created}, обновлено {updated}), пропущено past={skipped_past}, finished={skipped_finished}, дубликатов={deduped}")

    # --- Шаг 3: Enrichment ---
    odds_enriched = 0
    pred_enriched = 0
    stats_enriched = 0
    h2h_enriched = 0
    score_enriched = 0
    enrichment_errors = 0
    not_found = 0

    if events_only:
        logger.info("[BZZOIRO] Режим events-only — enrichment пропущен")
    elif dry_run:
        logger.info("[BZZOIRO] DRY-RUN — enrichment пропущен")
    elif stored_matches:
        logger.info(f"[BZZOIRO] Шаг 3: Обогащение {len(stored_matches)} матчей...")

        # --- Шаг 3a: Bulk-загрузка odds и predictions ---
        bulk_odds: dict[int, dict] = {}
        bulk_preds: dict[int, dict] = {}

        # Список event_id для fallback
        event_ids = [bid for _, bid, _ in stored_matches if bid]

        logger.info(f"[BZZOIRO] Шаг 3a: Bulk odds ({len(event_ids)} событий)...")
        bulk_odds = _fetch_bulk_data(
            f"{BZZOIRO_BASE}/odds", headers, date_from, date_to, label="odds"
        )
        if bulk_odds:
            logger.info(f"[BZZOIRO] Bulk odds: {len(bulk_odds)} записей — per-event fallback отключён")
        else:
            logger.info(f"[BZZOIRO] Bulk odds пуст — будет per-event fallback")

        logger.info(f"[BZZOIRO] Шаг 3a: Bulk predictions ({len(event_ids)} событий)...")
        bulk_preds = _fetch_bulk_data(
            f"{BZZOIRO_BASE}/predictions", headers, date_from, date_to, label="predictions"
        )
        if bulk_preds:
            logger.info(f"[BZZOIRO] Bulk predictions: {len(bulk_preds)} записей — per-event fallback отключён")
        else:
            logger.info(f"[BZZOIRO] Bulk predictions пуст — будет per-event fallback")

        # --- Шаг 3b: Per-event enrichment (H2H + fallback odds/preds) ---
        for idx, (cid, bzzoiro_id, ev) in enumerate(stored_matches):
            # FIX-4: Graceful shutdown
            if _is_shutdown():
                logger.info("[BZZOIRO] Shutdown requested — остановка enrichment")
                break

            status = ev.get("status", "")
            is_prematch = status in PREMATCH_STATUSES

            # Score для прошедших матчей
            if not is_prematch:
                score = _extract_score(ev)
                if score:
                    # FIX-3: idempotency_key
                    idem_key = f"{run_id}:{cid}:score:{bzzoiro_id}" if run_id else f"bzzoiro:{cid}:score:{bzzoiro_id}"
                    try:
                        if patch_match(cid, "score", score, source=COLLECTOR_NAME,
                                       upstream=BZZOIRO_UPSTREAM,
                                       idempotency_key=idem_key):
                            score_enriched += 1
                        else:
                            enrichment_errors += 1
                    except Exception as e:
                        logger.error(f"[BZZOIRO] score patch error #{idx}: {e}")
                        enrichment_errors += 1

            # Odds — bulk-first с per-event fallback
            best_odds = {}
            if bzzoiro_id in bulk_odds:
                best_odds = _extract_odds_from_bulk(bulk_odds[bzzoiro_id])

            if not best_odds:
                # Fallback: per-event запрос
                odds_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/odds"
                odds_data = _fetch_bzzoiro(odds_url, headers, max_retries=MAX_RETRIES)
                if odds_data is _NOT_FOUND:
                    not_found += 1
                elif odds_data and isinstance(odds_data, dict):
                    best_odds = _extract_best_odds(odds_data)
                else:
                    enrichment_errors += 1

            if best_odds:
                idem_key = f"{run_id}:{cid}:odds:{bzzoiro_id}" if run_id else f"bzzoiro:{cid}:odds:{bzzoiro_id}"
                try:
                    if patch_match(cid, "odds", best_odds, source=COLLECTOR_NAME,
                                   upstream=BZZOIRO_UPSTREAM,
                                   idempotency_key=idem_key):
                        odds_enriched += 1
                    else:
                        enrichment_errors += 1
                except Exception as e:
                    logger.error(f"[BZZOIRO] odds patch error #{idx}: {e}")
                    enrichment_errors += 1
            elif bzzoiro_id not in bulk_odds:
                not_found += 1

            if idx < DEBUG_EVENT_COUNT:
                src = "bulk" if bzzoiro_id in bulk_odds else "per-event"
                logger.debug(f"[BZZOIRO DEBUG] Odds #{idx}: {src} {'found' if best_odds else 'empty'}")  # §1.23

            # Predictions — bulk-first с per-event fallback (только pre-match)
            if is_prematch:
                inner_pred = {}
                if bzzoiro_id in bulk_preds:
                    inner_pred = _extract_pred_from_bulk(bulk_preds[bzzoiro_id])

                if not inner_pred:
                    # Fallback: per-event запрос
                    pred_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/predictions"
                    pred_data = _fetch_bzzoiro(pred_url, headers, max_retries=MAX_RETRIES)
                    if pred_data is _NOT_FOUND:
                        not_found += 1
                    elif pred_data and isinstance(pred_data, dict):
                        inner_pred = pred_data.get("prediction", pred_data)
                        if not isinstance(inner_pred, dict) or not inner_pred:
                            inner_pred = {}
                            not_found += 1
                    else:
                        enrichment_errors += 1

                if inner_pred:
                    idem_key = f"{run_id}:{cid}:predictions:{bzzoiro_id}" if run_id else f"bzzoiro:{cid}:predictions:{bzzoiro_id}"
                    try:
                        if patch_match(cid, "predictions", inner_pred, source=COLLECTOR_NAME,
                                       upstream=BZZOIRO_UPSTREAM,
                                       idempotency_key=idem_key):
                            pred_enriched += 1
                        else:
                            enrichment_errors += 1
                    except Exception as e:
                        logger.error(f"[BZZOIRO] predictions patch error #{idx}: {e}")
                        enrichment_errors += 1
                elif bzzoiro_id not in bulk_preds:
                    pass  # already counted in not_found

                if idx < DEBUG_EVENT_COUNT:
                    src = "bulk" if bzzoiro_id in bulk_preds else "per-event"
                    logger.debug(f"[BZZOIRO DEBUG] Prediction #{idx}: {src} {'found' if inner_pred else 'empty'}")  # §1.23

            # H2H (всегда)
            h2h_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/h2h"
            h2h_data = _fetch_bzzoiro(h2h_url, headers, max_retries=MAX_RETRIES)

            if h2h_data is _NOT_FOUND:
                h2h_url_legacy = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/head_to_head"
                h2h_data = _fetch_bzzoiro(h2h_url_legacy, headers, max_retries=MAX_RETRIES)

            if h2h_data is _NOT_FOUND:
                not_found += 1
            elif h2h_data and isinstance(h2h_data, dict):
                inner_h2h = h2h_data.get("head_to_head", h2h_data.get("h2h", h2h_data))
                if isinstance(inner_h2h, dict) and inner_h2h:
                    # FIX-3: idempotency_key
                    idem_key = f"{run_id}:{cid}:h2h:{bzzoiro_id}" if run_id else f"bzzoiro:{cid}:h2h:{bzzoiro_id}"
                    try:
                        if patch_match(cid, "h2h", inner_h2h, source=COLLECTOR_NAME,
                                       upstream=BZZOIRO_UPSTREAM,
                                       idempotency_key=idem_key):
                            h2h_enriched += 1
                        else:
                            enrichment_errors += 1
                    except Exception as e:
                        logger.error(f"[BZZOIRO] h2h patch error #{idx}: {e}")
                        enrichment_errors += 1
                else:
                    not_found += 1
            else:
                enrichment_errors += 1

            time.sleep(ENRICH_DELAY)

            # Stats (только прошедшие)
            if not is_prematch:
                stats_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/stats"
                stats_data = _fetch_bzzoiro(stats_url, headers, max_retries=MAX_RETRIES)

                if stats_data is _NOT_FOUND:
                    not_found += 1
                elif stats_data and isinstance(stats_data, dict):
                    inner_stats = stats_data.get("stats", stats_data)
                    if isinstance(inner_stats, dict) and inner_stats:
                        flat_stats = _flatten_stats(inner_stats)
                        for k, v in stats_data.items():
                            if k != "stats" and not isinstance(v, dict):
                                flat_stats[k] = v
                        if any(v is not None for v in flat_stats.values()):
                            # FIX-3: idempotency_key
                            idem_key = f"{run_id}:{cid}:stats:{bzzoiro_id}" if run_id else f"bzzoiro:{cid}:stats:{bzzoiro_id}"
                            try:
                                if patch_match(cid, "stats", flat_stats, source=COLLECTOR_NAME,
                                               upstream=BZZOIRO_UPSTREAM,
                                               idempotency_key=idem_key):
                                    stats_enriched += 1
                                else:
                                    enrichment_errors += 1
                            except Exception as e:
                                logger.error(f"[BZZOIRO] stats patch error #{idx}: {e}")
                                enrichment_errors += 1
                    else:
                        not_found += 1
                else:
                    enrichment_errors += 1

                time.sleep(ENRICH_DELAY)

            if (idx + 1) % 10 == 0 or (idx + 1) == len(stored_matches):
                logger.info(f"[BZZOIRO] Обогащение: {idx + 1}/{len(stored_matches)} (odds={odds_enriched}, pred={pred_enriched}, stats={stats_enriched}, h2h={h2h_enriched}, score={score_enriched})")

    # --- Итоги ---
    shutdown_triggered = _is_shutdown()
    logger.info(f"[BZZOIRO] Готово: матчей {len(stored_matches)} (создано {created}, обновлено {updated})")
    logger.info(f"[BZZOIRO]   Odds: {odds_enriched}, Predictions: {pred_enriched}, Stats: {stats_enriched}, H2H: {h2h_enriched}, Score: {score_enriched}")
    logger.info(f"[BZZOIRO]   Ошибки: {enrichment_errors}, Not Found: {not_found}")
    if shutdown_triggered:
        logger.info("[BZZOIRO]   ⚠ Shutdown был запрошен — данные могут быть неполными")

    result = {
        "last_run": now_msk(),
        "run_id": run_id,
        "total_events": len(all_events),
        "stored_matches": len(stored_matches),
        "created": created,
        "updated": updated,
        "error_count": enrichment_errors,
        "pages_fetched": pages_fetched,
        "enrichment": {
            "odds": odds_enriched,
            "predictions": pred_enriched,
            "stats": stats_enriched,
            "h2h": h2h_enriched,
            "score": score_enriched,
            "errors": enrichment_errors,
            "not_found": not_found,
        },
        "events_only": events_only,
        "skipped_past": skipped_past,
        "skipped_finished": skipped_finished,
        "deduped": deduped,
        "no_date": no_date_count,
        "shutdown_triggered": shutdown_triggered,
    }
    logger.info(f"[BZZOIRO] Result: {json.dumps(result, ensure_ascii=False)}")
    log_event("bzzoiro", "INFO", "Collection complete",
              total_events=len(all_events), created=created, updated=updated,
              odds_enriched=odds_enriched, predictions=pred_enriched,
              errors=enrichment_errors)

    save_meta(COLLECTOR_NAME,
              last_run=now_msk(),
              total_events=len(all_events),
              stored_matches=len(stored_matches),
              created=created,
              updated=updated,
              error_count=enrichment_errors,
              pages_fetched=pages_fetched,
              enrichment={
                  "odds": odds_enriched,
                  "predictions": pred_enriched,
                  "stats": stats_enriched,
                  "h2h": h2h_enriched,
                  "score": score_enriched,
                  "errors": enrichment_errors,
                  "not_found": not_found,
              },
              events_only=events_only,
              shutdown_triggered=shutdown_triggered,
              run_id=run_id)

    return result


@register_module("bzzoiro", role="collector",
              writes=["upsert_match", "patch_match", "save_meta"],
              reads=["{collector}:meta"])
def collect_and_process() -> dict:
    """FIX-7: Единая точка входа для CI/CD."""
    return collect_bzzoiro()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Collector Bzzoiro v{__version__}")
    parser.add_argument("--events-only", action="store_true",
                        help="Только события, без enrichment")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать в Redis (dry-run)")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
        logger.info("[BZZOIRO] DRY-RUN mode — данные НЕ будут записаны в Redis")

    result = collect_bzzoiro(events_only=args.events_only)
    logger.info(f"[BZZOIRO] Result: {result}")
