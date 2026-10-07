#!/usr/bin/env python3
"""
Collector Bzzoiro v8.14-fix (API v2 — bulk odds + predictions + per-event H2H/stats)
GatekeeperAI v9.3-audited

Изменения v8.14-fix (поверх v8.12-bulk):
  - FIX-1: Per-event predictions URL /events/{id}/prediction (ед.ч.) вместо /predictions (мн.ч.) → 404
  - FIX-2: Bulk predictions event_id извлечение из event.id (вложенный) вместо item.id (prediction ID)
  - FIX-3: 404 handling — except HTTPError возвращает _NOT_FOUND (urlopen бросает HTTPError)
  - FIX-4: Prediction parsing — markets.match_result → {home_win, draw, away_win, confidence}

Изменения v8.12-bulk:
  - Bulk odds: GET /odds/?date_from=&date_to= (2-3 запроса вместо 140 per-event)
  - Bulk predictions: GET /predictions/?date_from=&date_to= (2-3 запроса)
  - Per-event fallback: если bulk пуст → per-event запрос
  - HTTP timeout: 10с (вместо хардкода 30с)
  - Progress logging: каждые 10 матчей (вместо 50)
  - Coverage check: GET /coverage/ перед стартом
  - RATE_DELAY 0.5, ENRICH_DELAY 0.2, MAX_RETRIES 1 (из env)

Изменения v2.2 (Фаза 2):
  - run_initialization(collector="bzzoiro") — §2.6
  - DAYS_AHEAD из ENV (BZZOIRO_DAYS_AHEAD) — sync с YAML
  - upsert_match: +sources=["bzzoiro"], +idempotency_key — §2.2
  - save_meta: +run_id — трассировка
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
RATE_DELAY = float(os.environ.get("BZZOIRO_RATE_DELAY", "0.5"))
ENRICH_DELAY = float(os.environ.get("BZZOIRO_ENRICH_DELAY", "0.2"))
MAX_RETRIES = int(os.environ.get("BZZOIRO_MAX_RETRIES", "1"))
DAYS_AHEAD = int(os.environ.get("BZZOIRO_DAYS_AHEAD", "7"))
PAGE_LIMIT = 200
HTTP_TIMEOUT = int(os.environ.get("BZZOIRO_HTTP_TIMEOUT", "10"))

BZZOIRO_UPSTREAM = "opta"

__version__ = "8.14-fix"
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
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)

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

        except urllib.error.HTTPError as e:
            if e.code == 404:
                _cache_response(url, _NOT_FOUND)
                return _NOT_FOUND
            if e.code == 429:
                wait = 2 ** (attempt + 2)
                logger.warning(f"Rate limit (429). Waiting {wait}s")
                time.sleep(wait)
                continue
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 2)
                continue
            logger.error(f"HTTP {e.code}: {e.reason} — {url}")
            return None
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 2)
                continue
            logger.error(f"Request error: {e}")
            return None


def _normalize_odds_value(val) -> Optional[float]:
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
    data = _fetch_bzzoiro(url, headers, max_retries=1)
    if data and isinstance(data, dict):
        info = {
            "name": data.get("name", ""),
            "country": data.get("country", ""),
        }
        _league_cache[league_id] = info
        return info
    return {"name": "", "country": ""}


# ── Bulk endpoints ──────────────────────────────────────────

def _check_coverage(headers: dict) -> dict:
    """GET /coverage/ — проверка наличия данных без токена."""
    url = f"{BZZOIRO_BASE}/coverage/"
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        data = json.loads(resp.read().decode("utf-8"))
        sports = data.get("sports", [])
        for sport in sports:
            if sport.get("sport") == "football":
                priced = sport.get("priced_next_7d", 0)
                events_7d = sport.get("events_next_7d", 0)
                logger.info(f"[BZZOIRO] Coverage: football in_season, events_7d={events_7d}, priced={priced}")
                return sport
        logger.info(f"[BZZOIRO] Coverage: football not found in {len(sports)} sports")
        return {}
    except Exception as e:
        logger.warning(f"[BZZOIRO] Coverage check failed: {e}")
        return {}


def _fetch_bulk_odds(headers: dict, date_from: str, date_to: str) -> dict:
    """Bulk odds: GET /odds/?date_from=&date_to= — возвращает dict {event_id: odds_data}."""
    result = {}
    url = f"{BZZOIRO_BASE}/odds/?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"
    
    pages = 0
    while url and pages < 10:
        pages += 1
        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        if data is _NOT_FOUND or not data:
            break
        if not isinstance(data, dict):
            break
        
        items = data.get("results", data.get("data", data.get("events", [])))
        if not isinstance(items, list):
            # Может быть dict с event_id ключами
            if isinstance(items, dict):
                for eid, odds_val in items.items():
                    if odds_val:
                        result[str(eid)] = odds_val
                break
            break
        
        for item in items:
            eid = str(item.get("event_id", item.get("id", "")))
            if eid and eid != "None":
                result[eid] = item
        
        url = data.get("next")
        if not url:
            break
    
    logger.info(f"[BZZOIRO] Bulk odds: {len(result)} записей ({pages} стр.)")
    return result


def _fetch_bulk_predictions(headers: dict, date_from: str, date_to: str) -> dict:
    """Bulk predictions: GET /predictions/?date_from=&date_to= — возвращает dict {event_id: prediction_data}."""
    result = {}
    url = f"{BZZOIRO_BASE}/predictions/?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"
    
    pages = 0
    while url and pages < 10:
        pages += 1
        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        if data is _NOT_FOUND or not data:
            break
        if not isinstance(data, dict):
            break
        
        items = data.get("results", data.get("data", data.get("events", data.get("predictions", []))))
        if not isinstance(items, list):
            if isinstance(items, dict):
                for eid, pred_val in items.items():
                    if pred_val:
                        result[str(eid)] = pred_val
                break
            break
        
        for item in items:
            # BSD prediction shape: { "id": 88123, "event": { "id": 223510, ... } }
            # event_id is nested in event.id, not top-level
            eid = str(item.get("event_id") or
                      (item.get("event", {}) or {}).get("id") or
                      item.get("id", ""))
            if eid and eid != "None":
                result[eid] = item
        
        url = data.get("next")
        if not url:
            break
    
    logger.info(f"[BZZOIRO] Bulk predictions: {len(result)} записей ({pages} стр.)")
    return result


# ── Main collect ────────────────────────────────────────────

def collect_bzzoiro(events_only: bool = False) -> dict:
    if not BZZOIRO_API_KEY:
        logger.error("[BZZOIRO] BZZOIRO_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    headers = {
        "Authorization": f"Token {BZZOIRO_API_KEY}",
        "Accept": "application/json",
    }

    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    init_metrics = run_initialization(collector=COLLECTOR_NAME)
    run_id = init_metrics.get("run_id", "unknown")
    if not init_metrics.get("redis_available", False):
        logger.error("[BZZOIRO] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    logger.info(f"[BZZOIRO] Collection started | days_ahead={DAYS_AHEAD}, dry_run={dry_run}")
    log_event("bzzoiro", "INFO", "Collection started",
              days_ahead=DAYS_AHEAD, dry_run=dry_run, run_id=run_id)

    # ── Coverage check ──
    coverage = _check_coverage(headers)
    if coverage and coverage.get("priced_next_7d", 0) == 0 and DAYS_AHEAD <= 7:
        logger.info("[BZZOIRO] Coverage: 0 priced events — odds будут пустыми")

    # ── Шаг 1: Загрузка событий ──
    now_utc = dt.datetime.now(dt.timezone.utc)
    date_from = now_utc.strftime("%Y-%m-%d")
    date_to = (now_utc + dt.timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    url = (f"{BZZOIRO_BASE}/events/?date_from={date_from}&date_to={date_to}"
           f"&limit={PAGE_LIMIT}&status=notstarted")
    
    all_events: list[dict] = []
    pages_fetched = 0
    existing_keys = set(get_all_fields().keys())

    while url and pages_fetched < 50:
        if _is_shutdown():
            logger.info("[BZZOIRO] Shutdown requested — остановка загрузки событий")
            break
        pages_fetched += 1
        data = _fetch_bzzoiro(url, headers, max_retries=MAX_RETRIES)
        if data is _NOT_FOUND or not data:
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

    # ── Шаг 1.5: Pre-fetch лиг ──
    league_ids = set()
    for ev in all_events:
        lid = ev.get("league_id")
        if lid:
            league_ids.add(lid)

    if league_ids:
        logger.info(f"[BZZOIRO] Шаг 1.5: Pre-fetch {len(league_ids)} лиг...")
        for lid in league_ids:
            if _is_shutdown():
                logger.info("[BZZOIRO] Shutdown requested — пропуск pre-fetch лиг")
                break
            _fetch_league_info(lid, headers)
            time.sleep(RATE_DELAY)
        logger.info(f"[BZZOIRO] Pre-fetch лиг завершён: {len(_league_cache)} в кэше")

    # ── Шаг 2: Запись матчей ──
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
        if _is_shutdown():
            logger.info("[BZZOIRO] Shutdown requested — остановка записи матчей")
            break

        bzzoiro_id = ev.get("id")
        if not bzzoiro_id:
            continue

        home_raw = ev.get("home_team") or ev.get("home_team_name") or ""
        away_raw = ev.get("away_team") or ev.get("away_team_name") or ""
        if not home_raw or not away_raw:
            continue

        home_norm = clean_team_name(home_raw)
        away_norm = clean_team_name(away_raw)

        if not home_norm or not away_norm:
            continue

        status = ev.get("status", "notstarted")

        date_str = _extract_date(ev)
        date_str = normalize_date(date_str)
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
            logger.debug(f"[BZZOIRO DEBUG] Событие #{idx}: keys={list(ev.keys())}")

        league_id = ev.get("league_id")
        league_info = _fetch_league_info(league_id, headers) if league_id else {"name": "", "country": ""}

        competition = league_info.get("name", "") or ev.get("stage_name", "") or ev.get("stage", "")
        country = league_info.get("country", "")

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

    logger.info(f"[BZZOIRO] Записано {len(stored_matches)} матчей (создано {created}, обновлено {updated}), "
                f"пропущено past={skipped_past}, finished={skipped_finished}, дубликатов={deduped}")

    # ── Шаг 3: Enrichment ──
    odds_enriched = 0
    pred_enriched = 0
    stats_enriched = 0
    h2h_enriched = 0
    score_enriched = 0
    enrichment_errors = 0
    not_found = 0
    bulk_used = False

    if events_only:
        logger.info("[BZZOIRO] Режим events-only — enrichment пропущен")
    elif dry_run:
        logger.info("[BZZOIRO] DRY-RUN — enrichment пропущен")
    elif stored_matches:
        logger.info(f"[BZZOIRO] Шаг 3: Обогащение {len(stored_matches)} матчей...")

        # ── 3a: Bulk odds ──
        bulk_odds: dict[str, dict] = {}
        try:
            bulk_odds = _fetch_bulk_odds(headers, date_from, date_to)
        except Exception as e:
            logger.warning(f"[BZZOIRO] Bulk odds failed: {e}")
        
        # ── 3b: Bulk predictions ──
        bulk_preds: dict[str, dict] = {}
        try:
            bulk_preds = _fetch_bulk_predictions(headers, date_from, date_to)
        except Exception as e:
            logger.warning(f"[BZZOIRO] Bulk predictions failed: {e}")

        bulk_used = bool(bulk_odds) or bool(bulk_preds)
        logger.info(f"[BZZOIRO] Bulk mode: {'ON' if bulk_used else 'OFF (per-event fallback)'}")

        for idx, (cid, bzzoiro_id, ev) in enumerate(stored_matches):
            if _is_shutdown():
                logger.info("[BZZOIRO] Shutdown requested — остановка enrichment")
                break

            status = ev.get("status", "")
            is_prematch = status in PREMATCH_STATUSES
            bid_str = str(bzzoiro_id)

            # Score для прошедших матчей
            if not is_prematch:
                score = _extract_score(ev)
                if score:
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

            # ── Odds ──
            odds_data = None
            if bulk_used and bid_str in bulk_odds:
                odds_data = bulk_odds[bid_str]
            else:
                odds_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/odds"
                odds_data = _fetch_bzzoiro(odds_url, headers, max_retries=MAX_RETRIES)

            if odds_data is _NOT_FOUND:
                not_found += 1
            elif odds_data and isinstance(odds_data, dict):
                best_odds = _extract_best_odds(odds_data)
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
                else:
                    not_found += 1
            else:
                enrichment_errors += 1

            if idx < DEBUG_EVENT_COUNT:
                logger.debug(f"[BZZOIRO DEBUG] Odds #{idx}: {'found' if odds_data is not _NOT_FOUND else 404}")

            if not bulk_used:
                time.sleep(ENRICH_DELAY)

            # ── Predictions (только pre-match) ──
            if is_prematch:
                pred_data = None
                if bulk_used and bid_str in bulk_preds:
                    pred_data = bulk_preds[bid_str]
                else:
                    pred_url = f"{BZZOIRO_BASE}/events/{bzzoiro_id}/prediction"
                    pred_data = _fetch_bzzoiro(pred_url, headers, max_retries=MAX_RETRIES)

                if pred_data is _NOT_FOUND:
                    not_found += 1
                elif pred_data and isinstance(pred_data, dict):
                    # BSD prediction: { "id":..., "event":..., "markets": {"match_result": {...}}, "model": {...} }
                    markets = pred_data.get("markets", {})
                    match_result = markets.get("match_result", {})
                    model = pred_data.get("model", {})
                    inner_pred = {
                        "home_win": match_result.get("prob_home"),
                        "draw": match_result.get("prob_draw"),
                        "away_win": match_result.get("prob_away"),
                        "predicted": match_result.get("predicted"),
                        "confidence": model.get("confidence"),
                        "model_version": model.get("version"),
                        "expected_goals": markets.get("expected_goals", {}),
                        "over_under": markets.get("over_under", {}),
                        "btts": markets.get("btts", {}),
                    }
                    inner_pred = {k: v for k, v in inner_pred.items() if v is not None}
                    if isinstance(inner_pred, dict) and inner_pred:
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
                    else:
                        not_found += 1
                else:
                    enrichment_errors += 1

                if idx < DEBUG_EVENT_COUNT:
                    logger.debug(f"[BZZOIRO DEBUG] Prediction #{idx}: {'found' if pred_data is not _NOT_FOUND else 404}")

                if not bulk_used:
                    time.sleep(ENRICH_DELAY)

            # ── H2H (per-event, bulk недоступен) ──
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

            # ── Stats (только finished) ──
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

            if (idx + 1) % 10 == 0:
                logger.info(f"[BZZOIRO] Обогащение: {idx + 1}/{len(stored_matches)} "
                           f"(odds={odds_enriched}, pred={pred_enriched}, h2h={h2h_enriched}, "
                           f"stats={stats_enriched}, score={score_enriched}, errors={enrichment_errors})")

    # ── Итоги ──
    shutdown_triggered = _is_shutdown()
    logger.info(f"[BZZOIRO] Готово: матчей {len(stored_matches)} (создано {created}, обновлено {updated})")
    logger.info(f"[BZZOIRO]   Odds: {odds_enriched}, Predictions: {pred_enriched}, "
                f"Stats: {stats_enriched}, H2H: {h2h_enriched}, Score: {score_enriched}")
    logger.info(f"[BZZOIRO]   Ошибки: {enrichment_errors}, Not Found: {not_found}, Bulk: {'ON' if bulk_used else 'OFF'}")
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
        "bulk_used": bulk_used,
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
              bulk_used=bulk_used,
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
    """Единая точка входа для CI/CD."""
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
