#!/usr/bin/env python3
"""
Collector Bzzoiro v9.6.8-patched
PRIMARY Ingestion-коллектор (§17.4): первичный upsert_match каркаса матчей,
bulk-загрузка котировок через /odds/ (§1.33-1.34), per-event H2H (§1.34),
stats для прошедших матчей.
Контур внешних predictions полностью деактивирован (§1.2, §1.29).
Транспорт: curl_cffi impersonate="chrome" (§1.23), Redis-слой не затронут (§1.3).
Rate limiting: BZZOIRO_RATE_DELAY=0.5, BZZOIRO_ENRICH_DELAY=0.2, BZZOIRO_MAX_RETRIES=1,
BZZOIRO_HTTP_TIMEOUT=10, глобальный abort по N подряд 429 (§1.8) + внутренний дедлайн
fetch-фазы (BZZOIRO_FETCH_DEADLINE, сек) во избежание простоя до внешнего таймаута.
Мета (§1.26): last_run, error_count, total_events, stored_matches, h2h_count.
"""

import os
import sys
import time
import json
import datetime as dt
from typing import Any
import logging

# ─── Logging setup ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [BZZOIRO] %(message)s'
)

# ─── HTTP-транспорт для внешних фидов (§1.23) ───
try:
    from curl_cffi import requests as http_requests
    _HTTP_IMPL = "curl_cffi"
except ImportError:
    try:
        import requests as http_requests  # fallback
        _HTTP_IMPL = "requests"
        logging.warning("curl_cffi недоступен — fallback на requests. "
                        "Для обхода Cloudflare/WAF рекомендуется curl_cffi impersonate='chrome' (§1.23)")
    except ImportError:
        logging.error("Ни curl_cffi, ни requests не установлены")
        sys.exit(1)

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
    logging.error("gatekeeper_hub не найден")
    sys.exit(1)

# ─── Константы и маркеры ───
_NOT_FOUND = object()
DEBUG_EVENT_COUNT = int(os.environ.get("DEBUG_EVENT_COUNT", "3"))

BZZOIRO_BASE = os.environ.get("BZZOIRO_BASE_URL", "https://sports.bzzoiro.com/api/v2")
BZZOIRO_API_KEY = os.environ.get("BZZOIRO_API_KEY", "")
RATE_DELAY = float(os.environ.get("BZZOIRO_RATE_DELAY", "0.5"))     # v9.6.8: 0.5 (§1.8, config)
ENRICH_DELAY = float(os.environ.get("BZZOIRO_ENRICH_DELAY", "0.2"))
MAX_RETRIES = int(os.environ.get("BZZOIRO_MAX_RETRIES", "1"))
HTTP_TIMEOUT = int(os.environ.get("BZZOIRO_HTTP_TIMEOUT", "10"))    # v9.6.8: контролирующая переменная (§1.8)
ABORT_THRESHOLD = int(os.environ.get("BZZOIRO_429_ABORT_THRESHOLD", "3"))  # подряд 429 → abort (§1.8)
FETCH_DEADLINE = int(os.environ.get("BZZOIRO_FETCH_DEADLINE", "1200"))     # сек, внутренний дедлайн fetch-фазы
DAYS_AHEAD = 7
PAGE_LIMIT = 200

FINISHED_STATUSES = {"finished", "completed", "ended", "cancelled", "awarded", "forfeited"}
PREMATCH_STATUSES = ("notstarted", "", "scheduled", "postponed")

# ─── Кэширование ───
_cache: dict[str, Any] = {}

def _cache_response(url: str, data: Any):
    _cache[url] = data

def _check_cache(url: str) -> Any:
    return _cache.get(url)

# ─── Глобальный 429-abort (§1.8) ───
class RateLimitAbort(Exception):
    """Стабильный 429 от апстрима — немедленное прерывание внешнего контура (§1.8)."""

_consecutive_429 = 0
_deadline_ts: float | None = None

def _start_fetch_deadline():
    global _deadline_ts
    _deadline_ts = time.monotonic() + FETCH_DEADLINE

def _deadline_exceeded() -> bool:
    return _deadline_ts is not None and time.monotonic() > _deadline_ts

def _429_wait(resp: Any, attempt: int) -> int:
    """Пауза при 429: уважаем Retry-After, иначе экспонента 2/4 с (§1.35)."""
    headers = getattr(resp, "headers", None) or {}
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return max(1, int(float(ra)))
        except (ValueError, TypeError):
            pass
    return 2 ** (attempt + 1)


def _fetch_bzzoiro(url: str, headers: dict, max_retries: int | None = None) -> Any:
    """Изолированный fetch (§1.24). При N подряд 429 — RateLimitAbort (§1.8)."""
    max_retries = MAX_RETRIES if max_retries is None else max_retries
    cached = _check_cache(url)
    if cached is not None:
        return cached

    global _consecutive_429
    for attempt in range(max_retries + 1):
        if _deadline_exceeded():
            raise RateLimitAbort("внутренний дедлайн fetch-фазы")
        try:
            kwargs: dict = dict(headers=headers, timeout=HTTP_TIMEOUT)
            if _HTTP_IMPL == "curl_cffi":
                kwargs["impersonate"] = "chrome"
            resp = http_requests.get(url, **kwargs)
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            logging.error(f"[BZZOIRO] Request error: {type(e).__name__}")
            return None

        if resp.status_code == 404:
            _cache_response(url, _NOT_FOUND)
            return _NOT_FOUND

        if resp.status_code == 429:
            _consecutive_429 += 1
            if _consecutive_429 >= ABORT_THRESHOLD:
                raise RateLimitAbort(f"{_consecutive_429} подряд 429 от апстрима")
            wait = _429_wait(resp, attempt)
            logging.warning(f"[BZZOIRO] Rate limit 429. Waiting {wait}s ({_consecutive_429}/{ABORT_THRESHOLD})")
            time.sleep(wait)
            continue

        _consecutive_429 = 0

        if resp.status_code != 200:
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 2)
                continue
            logging.error(f"[BZZOIRO] HTTP {resp.status_code}: {url}")
            return None

        try:
            data = resp.json()
        except Exception:
            logging.error("[BZZOIRO] JSON decode error")
            return None
        _cache_response(url, data)
        time.sleep(RATE_DELAY)
        return data
    return None


# ─── Извлечение полей события ───
def _parse_event_date(ev: dict) -> dt.datetime:
    """Парсит дату события для сортировки."""
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
    """Извлекает дату матча в формате ISO или UTC строку (date_utc — единственный источник, §1.18)."""
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
    """Извлекает счет матча (плоский вид home/away, §1.18)."""
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


def _extract_best_odds(odds_data: Any) -> dict:
    """Плоский odds_1x2 с ключами '1'/'X'/'2' (Схема v710 / Гейт 28, §1.21).
    Текстовые маркеры home/draw/away не производятся — только числовые исходы."""
    if not isinstance(odds_data, dict):
        return {}
    odds = odds_data.get("odds", odds_data)
    if not isinstance(odds, dict):
        return {}

    mapping = {
        "1": ("home_win", "home", "1"),
        "X": ("draw", "X"),
        "2": ("away_win", "away", "2"),
    }
    result: dict = {}
    for target_key, keys in mapping.items():
        val = None
        for k in keys:
            if k in odds and odds[k] is not None:
                val = odds[k]
                break
        if val in (None, "-", ""):
            continue
        try:
            f = float(val)
            if f > 0:
                result[target_key] = f
        except (ValueError, TypeError):
            pass
    return result


def _bulk_event_id(item: dict) -> str:
    """Извлечение ID события из bulk-ответа с учетом вложенности (§1.39)."""
    return str(item.get("event_id") or (item.get("event") or {}).get("id") or item.get("id", ""))


def _flatten_stats(stats_data: dict, parent_key: str = "", sep: str = "_") -> dict:
    """Превращает вложенный словарь статистики в плоский."""
    items: list = []
    if not isinstance(stats_data, dict):
        return {}
    for k, v in stats_data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_stats(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


# ─── League pre-fetch ───
_league_cache: dict[int, dict] = {}

def _fetch_league_info(league_id: int, headers: dict) -> dict:
    """Получает информацию о лиге (name, country) через /leagues/{id}."""
    if not league_id:
        return {"name": "", "country": ""}
    if league_id in _league_cache:
        return _league_cache[league_id]

    url = f"{BZZOIRO_BASE}/leagues/{league_id}"
    data = _fetch_bzzoiro(url, headers)

    if data is _NOT_FOUND or not isinstance(data, dict):
        result = {"name": "", "country": ""}
    else:
        result = {"name": data.get("name", "") or "", "country": data.get("country", "") or ""}

    _league_cache[league_id] = result
    return result


# ─── Pre-flight /coverage/ (документация BSD) ───
def _coverage_preflight(headers: dict) -> bool:
    """Один бесплатный вызов /coverage/: True — события впереди есть, H2H-контур целесообразен.
    При недоступности эндпоинта не блокируем пайплайн (возвращаем True)."""
    data = _fetch_bzzoiro(f"{BZZOIRO_BASE}/coverage/", headers)
    if data is _NOT_FOUND or not isinstance(data, dict):
        return True
    fb = data.get("football", data)
    if isinstance(fb, dict):
        nxt = fb.get("events_next_7d", fb.get("priced_next_7d"))
        if nxt is not None:
            try:
                return int(nxt) > 0
            except (ValueError, TypeError):
                pass
    return True


# ─── Bulk odds (§1.33-1.34) ───
def _load_bulk_odds(headers: dict) -> dict:
    """Bulk-загрузка котировок через /odds/?date_from=&date_to=.
    Возвращает {bzzoiro_event_id: odds_1x2_flat}. Пустой ответ → {} (per-event fallback, §1.33)."""
    now = dt.datetime.now(dt.timezone.utc)
    bulk: dict = {}
    url = (f"{BZZOIRO_BASE}/odds/?date_from={now:%Y-%m-%d}"
           f"&date_to={(now + dt.timedelta(days=DAYS_AHEAD)):%Y-%m-%d}")

    while url:
        if _deadline_exceeded():
            raise RateLimitAbort("внутренний дедлайн bulk-odds")
        data = _fetch_bzzoiro(url, headers)
        if data is None or data is _NOT_FOUND:
            break
        if isinstance(data, dict):
            items = data.get("results", data.get("data", data.get("events", [])))
        elif isinstance(data, list):
            items = data
        else:
            break
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            eid = _bulk_event_id(item)
            if not eid:
                continue
            flat = _extract_best_odds(item)
            if flat:
                bulk[eid] = flat
        url = data.get("next") if isinstance(data, dict) else None
        if url and len(bulk):
            pass  # пагинация допускается; пустой bulk не пагинируем
    return bulk


def collect_bzzoiro(events_only: bool = False) -> dict:
    headers = {
        "Authorization": f"Token {BZZOIRO_API_KEY}",
        "Accept": "application/json",
    }

    print("=" * 60)
    print("[BZZOIRO] Collector Bzzoiro v9.6.8-patched started "
          f"(HTTP: {_HTTP_IMPL}, bulk odds §1.33, no predictions §1.29)")
    print("=" * 60)

    _start_fetch_deadline()

    # ─── Шаг 0: Инициализация Redis ───
    print("[BZZOIRO] Шаг 0: Инициализация Redis...")
    init_metrics = run_initialization()
    if not init_metrics or not init_metrics.get("redis_available"):
        print("[BZZOIRO] ERROR: Redis init failed")
        save_meta("bzzoiro", stored_matches=0, error_count=1)
        return {"error": "redis_init_failed"}

    print(f"[BZZOIRO] Cleanup: {init_metrics.get('cleanup_count', 0)} ключей удалено")

    existing_keys = set(get_all_fields().keys())

    now = dt.datetime.now(dt.timezone.utc)
    rate_limited = False

    # ─── Шаг 1: Загрузка событий (§1.33: bulk-маршруты, per-event — fallback) ───
    print("[BZZOIRO] Шаг 1: Загрузка событий...")
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + dt.timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    pages_fetched = 0
    url = f"{BZZOIRO_BASE}/events?date_from={date_from}&date_to={date_to}&limit={PAGE_LIMIT}"

    try:
        while url:
            if _deadline_exceeded():
                raise RateLimitAbort("дедлайн fetch-фазы на загрузке страниц")
            data = _fetch_bzzoiro(url, headers)
            pages_fetched += 1

            if data is None:
                print(f"[BZZOIRO] Ошибка загрузки страницы {pages_fetched}")
                break
            if data is _NOT_FOUND:
                break

            if isinstance(data, dict):
                items = data.get("results", data.get("data", data.get("events", [])))
            elif isinstance(data, list):
                items = data
            else:
                print(f"[BZZOIRO] Неожиданный тип ответа страницы: {type(data).__name__}")
                break

            if not isinstance(items, list) or not items:
                break

            all_events.extend(items)
            total_count = data.get("count", 0) if isinstance(data, dict) else 0
            print(f"[BZZOIRO] Загружено: {len(all_events)}/{total_count}")

            url = data.get("next") if isinstance(data, dict) else None
            if not url:
                break
            if total_count and len(all_events) >= total_count:
                break
    except RateLimitAbort as e:
        rate_limited = True
        logging.warning(f"[BZZOIRO] Внешний контур прерван ({e}) — переход на кэш (§1.8)")

    # ─── Сортировка по дате (ближайшие первыми) ───
    all_events.sort(key=_parse_event_date)
    print(f"[BZZOIRO] Всего событий: {len(all_events)} (страниц: {pages_fetched})")

    # ─── Шаг 1.5: Pre-fetch лиг ───
    league_ids = set()
    for ev in all_events:
        lid = ev.get("league_id")
        if lid:
            league_ids.add(lid)

    if league_ids and not rate_limited:
        print(f"[BZZOIRO] Шаг 1.5: Pre-fetch {len(league_ids)} лиг...")
        try:
            for lid in league_ids:
                if _deadline_exceeded():
                    raise RateLimitAbort("дедлайн на pre-fetch лиг")
                _fetch_league_info(lid, headers)
                time.sleep(RATE_DELAY)
            print(f"[BZZOIRO] Pre-fetch лиг завершён: {len(_league_cache)} в кэше")
        except RateLimitAbort as e:
            rate_limited = True
            logging.warning(f"[BZZOIRO] Внешний контур прерван ({e}) — переход на кэш (§1.8)")

    # ─── Шаг 2: Запись матчей ───
    print("[BZZOIRO] Шаг 2: Запись матчей в Redis...")
    stored_matches: list[tuple[str, int, dict]] = []
    created = 0
    updated = 0
    skipped_past = 0
    skipped_finished = 0
    deduped = 0
    no_date_count = 0
    seen = set()

    for idx, ev in enumerate(all_events):
        bzzoiro_id = ev.get("id")
        if not bzzoiro_id:
            continue

        home_team = ev.get("home_team", "")
        away_team = ev.get("away_team", "")
        if not home_team or not away_team:
            continue

        status = ev.get("status", "scheduled") or "scheduled"

        if status in FINISHED_STATUSES:
            skipped_finished += 1
            continue

        date_str = _extract_date(ev)
        dedup_key = f"{home_team}|{away_team}|{date_str}"
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
            print(f"[BZZOIRO DEBUG] Событие #{idx}: поля={sorted(ev.keys())}")

        league_id = ev.get("league_id")
        league_info = _fetch_league_info(league_id, headers) if league_id else {"name": "", "country": ""}

        competition = league_info.get("name", "") or ev.get("stage_name", "") or ev.get("stage", "")
        country = league_info.get("country", "")

        extra = {
            "source_ids": {"bzzoiro": str(bzzoiro_id)},
        }

        result_id = upsert_match(
            home_team=home_team,
            away_team=away_team,
            date_utc=date_str,
            competition=competition,
            country=country,
            status=status,
            source="bzzoiro",
            **extra,
        )
        if result_id:
            if f"match:{result_id}" in existing_keys:
                updated += 1
            else:
                created += 1
            stored_matches.append((result_id, bzzoiro_id, ev))
        else:
            skipped_past += 1

    print(f"[BZZOIRO] Записано {len(stored_matches)} матчей (создано {created}, обновлено {updated}), "
          f"пропущено past={skipped_past}, finished={skipped_finished}, дубликатов={deduped}")
    if no_date_count:
        print(f"[BZZOIRO] Без даты: {no_date_count} событий")

    # ─── Шаг 3: Enrichment (bulk odds §1.33 + per-event H2H §1.34) ───
    odds_enriched = 0
    stats_enriched = 0
    h2h_enriched = 0
    score_enriched = 0
    enrichment_errors = 0
    not_found = 0
    h2h_count = 0
    bulk_odds: dict = {}

    if events_only:
        print("[BZZOIRO] Режим events-only — enrichment пропущен")
    elif stored_matches and not rate_limited:
        print(f"[BZZOIRO] Шаг 3: Обогащение {len(stored_matches)} матчей...")
        try:
            if not _coverage_preflight(headers):
                print("[BZZOIRO] /coverage/: событий впереди нет — per-event H2H пропущен")
                stored_matches = []
            else:
                bulk_odds = _load_bulk_odds(headers)
                print(f"[BZZOIRO] Bulk odds: линий получено {len(bulk_odds)} (§1.33)")

            for idx, (cid, bzzoiro_id, ev) in enumerate(stored_matches):
                if _deadline_exceeded():
                    raise RateLimitAbort("дедлайн на enrichment-цикле")

                status = ev.get("status", "")
                is_prematch = status in PREMATCH_STATUSES

                # Score + Stats для прошедших матчей
                if not is_prematch:
                    score = _extract_score(ev)
                    if score:
                        if patch_match(cid, "score", score, source="bzzoiro"):
                            score_enriched += 1
                        else:
                            enrichment_errors += 1

                # ─── Odds: bulk сначала, per-event — только fallback (§1.33) ───
                flat_odds = bulk_odds.get(str(bzzoiro_id))
                if flat_odds is None:
                    odds_data = _fetch_bzzoiro(f"{BZZOIRO_BASE}/events/{bzzoiro_id}/odds", headers)
                    if odds_data is _NOT_FOUND:
                        not_found += 1
                    elif isinstance(odds_data, dict):
                        flat_odds = _extract_best_odds(odds_data)
                if flat_odds:
                    if patch_match(cid, "odds", flat_odds, source="bzzoiro", upstream="opta"):
                        odds_enriched += 1
                    else:
                        enrichment_errors += 1
                elif not bulk_odds:
                    not_found += 1

                time.sleep(ENRICH_DELAY)

                # ─── H2H (per-event; bulk-маршрута нет, §1.34; legacy-fallback удален) ───
                h2h_data = _fetch_bzzoiro(f"{BZZOIRO_BASE}/events/{bzzoiro_id}/h2h", headers)
                if h2h_data is _NOT_FOUND:
                    not_found += 1
                elif h2h_data and isinstance(h2h_data, dict):
                    inner_h2h = h2h_data.get("head_to_head", h2h_data.get("h2h", h2h_data))
                    if isinstance(inner_h2h, dict) and inner_h2h:
                        if patch_match(cid, "h2h", inner_h2h, source="bzzoiro"):
                            h2h_enriched += 1
                        else:
                            enrichment_errors += 1
                    else:
                        not_found += 1
                else:
                    enrichment_errors += 1

                time.sleep(ENRICH_DELAY)

                # ─── Stats (только для прошедших) ───
                if not is_prematch:
                    stats_data = _fetch_bzzoiro(f"{BZZOIRO_BASE}/events/{bzzoiro_id}/stats", headers)
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
                                if patch_match(cid, "stats", flat_stats, source="bzzoiro"):
                                    stats_enriched += 1
                                else:
                                    enrichment_errors += 1
                            else:
                                not_found += 1
                        else:
                            not_found += 1
                    else:
                        enrichment_errors += 1

                    time.sleep(ENRICH_DELAY)

                if (idx + 1) % 50 == 0:
                    print(f"[BZZOIRO] Обогащение: {idx + 1}/{len(stored_matches)} "
                          f"(odds={odds_enriched}, h2h={h2h_enriched}, stats={stats_enriched}, score={score_enriched})")
        except RateLimitAbort as e:
            rate_limited = True
            logging.warning(f"[BZZOIRO] Внешний контур прерван ({e}) — переход на кэш (§1.8)")

    h2h_count = h2h_enriched

    # ─── Шаг 4: Итоги (Safe Logging §1.22: только атомарные метаданные) ───
    print(f"[BZZOIRO] Готово: матчей {len(stored_matches)} (создано {created}, обновлено {updated})")
    print(f"[BZZOIRO]   Odds: {odds_enriched}, H2H: {h2h_enriched}, Stats: {stats_enriched}, Score: {score_enriched}")
    print(f"[BZZOIRO]   Ошибки: {enrichment_errors}, Not Found: {not_found}, Aborted (429): {rate_limited}")

    result = {
        "last_run": now_msk(),
        "total_events": len(all_events),
        "stored_matches": len(stored_matches),
        "created": created,
        "updated": updated,
        "error_count": enrichment_errors,
        "h2h_count": h2h_count,
        "odds_enriched": odds_enriched,
        "stats_enriched": stats_enriched,
        "score_enriched": score_enriched,
        "pages_fetched": pages_fetched,
        "aborted_by_rate_limit": rate_limited,
        "events_only": events_only,
        "skipped_past": skipped_past,
        "skipped_finished": skipped_finished,
        "deduped": deduped,
        "no_date": no_date_count,
    }

    # ─── Шаг 5: Сохранение мета (§1.26: без enrichment, с h2h_count) ───
    save_meta("bzzoiro",
              total_events=len(all_events),
              stored_matches=len(stored_matches),
              created=created,
              updated=updated,
              skipped_past=skipped_past,
              deduped=deduped,
              pages_fetched=pages_fetched,
              error_count=enrichment_errors,
              h2h_count=h2h_count)

    return result


if __name__ == "__main__":
    events_only = "--events-only" in sys.argv
    collect_bzzoiro(events_only=events_only)
