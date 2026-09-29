#!/usr/bin/env python3
"""
Коллектор football-data.org для Gatekeeper-AI v700-prod.
История матчей — результаты, счёт, статистика.
Upstream: bet365
Роль: HISTORY — даёт исторические данные для H2H и form-анализа.

Secret: FOOTBALL_DATA_API_KEY
API: football-data.org v4 (X-Auth-Token header, 10 req/sec free tier)
Запуск: вручную.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from gatekeeper_hub import (
    run_initialization,
    upsert_history_match,
    update_history_indexes,
    normalize_date,
    now_msk,
    save_meta,
)

FD_BASE = "https://api.football-data.org/v4"
FD_UPSTREAM = "bet365"

# Топ-лиги для загрузки истории (competition codes football-data.org)
# https://api.football-data.org/v4/competitions
COMPETITIONS = [
    ("PL", "Premier League", "England"),
    ("ELC", "Championship", "England"),
    ("PD", "La Liga", "Spain"),
    ("SA", "Serie A", "Italy"),
    ("BL1", "Bundesliga", "Germany"),
    ("BL2", "2. Bundesliga", "Germany"),
    ("FL1", "Ligue 1", "France"),
    ("FL2", "Ligue 2", "France"),
    ("PPL", "Primeira Liga", "Portugal"),
    ("DED", "Eredivisie", "Netherlands"),
    ("BSA", "Brasileirão", "Brazil"),
    ("CL", "Champions League", "Europe"),
    ("ELC_", "Europa League", "Europe"),  # нестандартный код, уточнить
]

# Days of history to fetch (default: 30 — последний месяц)
HISTORY_DAYS = int(os.environ.get("FOOTBALL_DATA_HISTORY_DAYS", "30"))
RATE_DELAY = float(os.environ.get("FOOTBALL_DATA_RATE_DELAY", "0.15"))
MAX_RETRIES = int(os.environ.get("FOOTBALL_DATA_MAX_RETRIES", "2"))
SEASON = os.environ.get("FOOTBALL_DATA_SEASON", "")  # напр. "2024" — пусто = текущий


def _fetch_fd(url: str, headers: dict, max_retries: int = 2) -> Optional[dict]:
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("X-Requests-Available-Minute")
                return {"data": data, "quota_remaining": remaining}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** (attempt + 2)
                print(f"[FD] HTTP 429 — Rate Limit. Ждём {wait}с")
                time.sleep(wait)
                continue
            print(f"[FD] HTTP {e.code}: {e.reason}")
            try:
                body = e.read().decode("utf-8", errors="replace")
                print(f"[FD] Body: {body[:300]}")
            except Exception:
                pass
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 3)
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"[FD] Error: {e}")
            if attempt < max_retries:
                time.sleep(RATE_DELAY * 3)
                continue
            return None
    return None


def _extract_score(match: dict) -> Optional[dict]:
    """Извлекает счёт из матча football-data.org."""
    score = match.get("score")
    if not isinstance(score, dict):
        return None
    full_time = score.get("fullTime", {})
    half_time = score.get("halfTime", {})

    home = full_time.get("home")
    away = full_time.get("away")

    if home is None and away is None:
        return None

    result = {
        "home": home if home is not None else 0,
        "away": away if away is not None else 0,
        "ht_home": half_time.get("home"),
        "ht_away": half_time.get("away"),
    }
    return result


def _extract_match_date(match: dict) -> str:
    """Извлекает дату матча."""
    date_str = match.get("utcDate", "") or match.get("matchday", "")
    if date_str:
        return normalize_date(date_str)
    return ""


def _build_competition_url(comp_code: str, season: str, date_from: str, date_to: str) -> str:
    """Строит URL для получения матчей competitions/{code}/matches."""
    url = f"{FD_BASE}/competitions/{comp_code}/matches"
    params = []
    if season:
        params.append(f"season={season}")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")
    if params:
        url += "?" + "&".join(params)
    return url


def collect_football_data() -> Dict[str, Any]:
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        print("[FD] FOOTBALL_DATA_API_KEY не задан")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    init_metrics = run_initialization()
    if not init_metrics.get("redis_available", False):
        print("[FD] Redis недоступен")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    headers = {
        "X-Auth-Token": api_key,
        "Accept": "application/json",
    }

    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    print(f"[FD] Football-Data Collector v700 started")
    print(f"[FD] Окно: {date_from} → {date_to} ({HISTORY_DAYS} дней)")
    print(f"[FD] Лиг: {len(COMPETITIONS)}")

    stored = 0
    skipped = 0
    error_count = 0
    total_events = 0
    quota_remaining = "?"

    for comp_code, comp_name, country in COMPETITIONS:
        # Пропускаем нестандартные коды
        if comp_code.endswith("_"):
            print(f"[FD] Пропуск {comp_code} — код не подтверждён")
            continue

        url = _build_competition_url(comp_code, SEASON, date_from, date_to)
        print(f"[FD] {comp_code} ({comp_name}) → fetching...")

        result = _fetch_fd(url, headers, max_retries=MAX_RETRIES)

        if result is None:
            print(f"[FD] {comp_code} — ошибка, пропуск")
            error_count += 1
            continue

        quota_remaining = result.get("quota_remaining", quota_remaining)
        data = result.get("data", {})

        matches = data.get("matches", [])
        if not isinstance(matches, list):
            print(f"[FD] {comp_code} — нет матчей")
            continue

        print(f"[FD] {comp_code} — {len(matches)} матчей")

        for match in matches:
            status = match.get("status", "").upper()

            # Только завершённые матчи (история)
            if status not in ("FINISHED", "AWARDED", "CANCELLED"):
                skipped += 1
                continue

            home_team = match.get("homeTeam", {}).get("name", "") or match.get("homeTeam", {}).get("shortName", "")
            away_team = match.get("awayTeam", {}).get("name", "") or match.get("awayTeam", {}).get("shortName", "")

            if not home_team or not away_team:
                skipped += 1
                continue

            date_utc = _extract_match_date(match)
            match_id = str(match.get("id", ""))
            total_events += 1

            # Счёт
            score = _extract_score(match)
            if not score:
                skipped += 1
                continue

            # Статистика (если есть в matchday-данных)
            stats = {}
            # football-data.org v4 не даёт детальную статистику в /matches,
            # но сохраняем matchday и stage
            matchday = match.get("matchday")
            stage = match.get("stage", "")
            group = match.get("group", "")
            if matchday:
                stats["matchday"] = matchday
            if stage:
                stats["stage"] = stage
            if group:
                stats["group"] = group

            # Статус результата
            if status == "FINISHED":
                result_status = "finished"
            elif status == "AWARDED":
                result_status = "awarded"
            else:
                result_status = "cancelled"

            # Запись через хаб → upsert_history_match
            cid = upsert_history_match(
                home_team=home_team,
                away_team=away_team,
                date_utc=date_utc,
                competition=comp_name,
                country=country,
                score=score,
                result_status=result_status,
                stats=stats if stats else None,
                source_ids={"football_data": match_id},
            )

            if cid:
                stored += 1
            else:
                skipped += 1

        time.sleep(RATE_DELAY)

    # Обновление индексов history
    print(f"[FD] Обновление индексов history...")
    try:
        update_history_indexes()
        print(f"[FD] Индексы обновлены")
    except Exception as e:
        print(f"[FD] Ошибка обновления индексов: {e}")

    print(f"[FD] Готово: сохранено {stored}, пропущено {skipped}, ошибок {error_count}")

    meta = {
        "last_run": now_msk(),
        "total_events": total_events,
        "stored_matches": stored,
        "skipped": skipped,
        "error_count": error_count,
        "quota_remaining": quota_remaining,
        "history_days": HISTORY_DAYS,
        "date_from": date_from,
        "date_to": date_to,
    }
    save_meta("football_data", **meta)
    return meta


if __name__ == "__main__":
    result = collect_football_data()
    print(f"[FD] Result: {result}")
