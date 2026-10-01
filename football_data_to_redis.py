#!/usr/bin/env python3
"""
Коллектор football-data.co.uk CSV для Gatekeeper-AI v700-prod.
История матчей — результаты, счёт, статистика.
Upstream: bet365
Роль: HISTORY — даёт исторические данные для H2H и form-анализа.

Источник: football-data.co.uk (CSV-файлы, ~65 колонок)
Запуск: вручную (Termux) или через GitHub Actions.

v5.0: 26 полей payload, odds 1x2, source_map, flags, season, league_code, 12 stats.
       Запись через gatekeeper_hub.upsert_history_match() (TEAM_ALIASES, индексы).
"""

import os
import sys
import csv
import time
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple

from gatekeeper_hub import (
    upsert_history_match,
    build_canonical_id,
    clean_team_name,
    detect_upstream,
    now_msk,
    save_meta,
    _build_1x2,
)
from redis_hub import is_redis_available

FD_SOURCE = "football_data"
FD_BASE = "https://www.football-data.co.uk/mmz4281"

# 22 лиги football-data.co.uk (коды CSV, не API)
COMPETITIONS = [
    ("E0",  "Premier League",   "England"),
    ("E1",  "Championship",     "England"),
    ("E2",  "League One",       "England"),
    ("E3",  "League Two",       "England"),
    ("EC",  "National League",  "England"),
    ("SC0", "Premiership",      "Scotland"),
    ("SC1", "Championship",     "Scotland"),
    ("SC2", "League One",       "Scotland"),
    ("SC3", "League Two",       "Scotland"),
    ("D1",  "Bundesliga",       "Germany"),
    ("D2",  "2. Bundesliga",    "Germany"),
    ("I1",  "Serie A",          "Italy"),
    ("I2",  "Serie B",          "Italy"),
    ("SP1", "La Liga",          "Spain"),
    ("SP2", "La Liga 2",        "Spain"),
    ("F1",  "Ligue 1",          "France"),
    ("F2",  "Ligue 2",          "France"),
    ("N1",  "Eredivisie",       "Netherlands"),
    ("P1",  "Primeira Liga",    "Portugal"),
    ("B1",  "Brasileirão",      "Brazil"),
    ("G1",  "Super League",     "Greece"),
    ("T1",  "Süper Lig",        "Turkey"),
]

MAX_RETRIES = int(os.environ.get("FOOTBALL_DATA_MAX_RETRIES", "3"))
ARCHIVE_DIR = os.environ.get("FOOTBALL_DATA_ARCHIVE_DIR", "archive")
SEASON = os.environ.get("FOOTBALL_DATA_SEASON", "")  # напр. "2526" — пусто = авто

# Приоритет коэффициентов: B365 → BbAv → IW → LB → WH → VC
ODDS_PRIORITY = [
    ("B365",  "B365H",  "B365D",  "B365A"),
    ("BbAv",  "BbAvH",  "BbAvD",  "BbAvA"),
    ("IW",    "IWH",    "IWD",    "IWA"),
    ("LB",    "LBH",    "LBD",    "LBA"),
    ("WH",    "WHH",    "WHD",    "WHA"),
    ("VC",    "VCH",    "VCD",    "VCA"),
]


def _p(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Сезон
# ---------------------------------------------------------------------------
def _auto_season() -> str:
    """Текущий сезон в формате YY/YY: октябрь 2026 → '2627'."""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    if m >= 7:
        return f"{y % 100:02d}{(y + 1) % 100:02d}"
    return f"{(y - 1) % 100:02d}{y % 100:02d}"


# ---------------------------------------------------------------------------
# Скачивание CSV
# ---------------------------------------------------------------------------
def _download_csv(season: str, league_code: str) -> Optional[str]:
    """Скачивание CSV с football-data.co.uk (urllib следует redirect 302 автоматически)."""
    url = f"{FD_BASE}/{season}/{league_code}.csv"
    filename = f"{season}_{league_code}.csv"

    for attempt in range(MAX_RETRIES):
        try:
            _p(f"[CSV] Скачивание {url} (попытка {attempt + 1}/{MAX_RETRIES})")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if len(data) < 100:
                    _p(f"[CSV] Файл слишком маленький ({len(data)} байт) — пропускаем")
                    return None
                with open(filename, "wb") as f:
                    f.write(data)
                _p(f"[CSV] Скачано: {filename} ({len(data)} байт)")
                return filename
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _p(f"[CSV] 404 — нет файла для {league_code} сезон {season}")
                return None
            _p(f"[CSV] HTTP {e.code}: {e.reason}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
        except Exception as e:
            _p(f"[CSV] Ошибка: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------
def _parse_csv_filename(filename: str) -> Tuple[str, str]:
    """2526_E0.csv → season='2526', league_code='E0'."""
    base = os.path.basename(filename).replace(".csv", "")
    parts = base.split("_")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", ""


def _parse_date(date_str: str, time_str: str = "") -> str:
    """DD/MM/YYYY [+ HH:MM] → ISO 8601 UTC."""
    if not date_str:
        return ""
    dt = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return ""
    if time_str and time_str.strip():
        try:
            parts = time_str.strip().split(":")
            dt = dt.replace(hour=int(parts[0]), minute=int(parts[1]))
        except (ValueError, IndexError):
            pass
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_odds(row: dict) -> dict:
    """
    Извлечение odds с приоритетом: B365 → BbAv → IW → LB → WH → VC.
    Возвращает плоский price dict (значения могут быть пустыми).
    """
    for _name, h_col, d_col, a_col in ODDS_PRIORITY:
        h = (row.get(h_col) or "").strip()
        d = (row.get(d_col) or "").strip()
        a = (row.get(a_col) or "").strip()
        if h and d and a:
            try:
                float(h), float(d), float(a)
                return {"home": h, "draw": d, "away": a}
            except (ValueError, TypeError):
                continue
    return {"home": "", "draw": "", "away": ""}


def _safe_int(val) -> Optional[int]:
    if val is None or val == "" or val == "-":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _parse_stats(row: dict) -> dict:
    """Парсинг 12 метрик из CSV колонок."""
    stats = {}
    mapping = [
        ("shots_home",              "HS"),
        ("shots_away",              "AS"),
        ("shots_on_target_home",    "HST"),
        ("shots_on_target_away",    "AST"),
        ("corners_home",            "HC"),
        ("corners_away",            "AC"),
        ("fouls_home",              "HF"),
        ("fouls_away",              "AF"),
        ("yellow_home",             "HY"),
        ("yellow_away",             "AY"),
        ("red_home",                "HR"),
        ("red_away",                "AR"),
    ]
    for field, col in mapping:
        v = _safe_int(row.get(col))
        if v is not None:
            stats[field] = v
    return stats


def _calc_flags(score: dict, stats: dict) -> dict:
    """extreme_result, abnormal_score, red_card_driven."""
    flags = {
        "extreme_result": False,
        "abnormal_score": False,
        "red_card_driven": False,
    }
    diff = abs(score.get("home", 0) - score.get("away", 0))
    if diff >= 4:
        flags["abnormal_score"] = True
        flags["extreme_result"] = True
    if stats.get("red_home", 0) or stats.get("red_away", 0):
        flags["red_card_driven"] = True
        flags["extreme_result"] = True
    return flags


def _build_source_map(upstream: str, ts: str) -> dict:
    """Source map для history: odds (bet365, closing) + stats (match_data)."""
    return {
        "odds": {
            "source": FD_SOURCE,
            "upstream": upstream,
            "timestamp": ts,
            "independent": True,
            "type": "closing",
        },
        "stats": {
            "source": FD_SOURCE,
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
    }


# ---------------------------------------------------------------------------
# Сборка payload (26 полей)
# ---------------------------------------------------------------------------
def _build_payload(row: dict, season: str, league_code: str,
                   comp_name: str, country: str) -> Optional[dict]:
    """Сборка 26-полевого payload из строки CSV по гайду v5.0."""
    home_team = (row.get("HomeTeam") or "").strip()
    away_team = (row.get("AwayTeam") or "").strip()
    if not home_team or not away_team:
        return None

    # Дата
    date_utc = _parse_date(row.get("Date", ""), row.get("Time", ""))
    if not date_utc:
        return None

    # FTR — только завершённые
    ftr = (row.get("FTR") or "").strip().upper()
    if ftr not in ("H", "D", "A"):
        return None

    # Score
    try:
        score_home = int(float(row.get("FTHG", 0)))
        score_away = int(float(row.get("FTAG", 0)))
    except (ValueError, TypeError):
        return None

    # Half-time
    half_time_score = {}
    hthg = _safe_int(row.get("HTHG"))
    htag = _safe_int(row.get("HTAG"))
    if hthg is not None:
        half_time_score["home"] = hthg
    if htag is not None:
        half_time_score["away"] = htag

    # Stats
    stats = _parse_stats(row)

    # Flags
    flags = _calc_flags({"home": score_home, "away": score_away}, stats)

    # Odds (1x2 через хаб)
    upstream = detect_upstream(FD_SOURCE)  # "bet365"
    ts = now_msk()
    odds_price = _extract_odds(row)
    odds = _build_1x2(odds_price, FD_SOURCE, upstream, ts, "closing")

    # Source map
    source_map = _build_source_map(upstream, ts)

    # Canonical ID — через хаб (TEAM_ALIASES)
    canonical_id = build_canonical_id(home_team, away_team, date_utc)

    return {
        "canonical_id":      canonical_id,
        "home_team":         home_team,
        "away_team":         away_team,
        "home_clean":        clean_team_name(home_team),
        "away_clean":        clean_team_name(away_team),
        "competition":       comp_name,
        "country":           country,
        "season":            season,
        "league_code":       league_code,
        "date_utc":          date_utc,
        "status":            "completed",
        "score":             {"home": score_home, "away": score_away},
        "half_time_score":   half_time_score,
        "full_time_result":  ftr,
        "referee":           (row.get("Referee") or "").strip(),
        "version":           1,
        "schema_version":    "v700",
        "odds":              odds,
        "predictions":       {},
        "stats":             stats,
        "h2h":               {},
        "source_map":        source_map,
        "source_ids":        {FD_SOURCE: league_code},
        "sources":           [FD_SOURCE],
        "section_history":   [],
        "flags":             flags,
    }


# ---------------------------------------------------------------------------
# Архивация
# ---------------------------------------------------------------------------
def _archive_csv(filename: str):
    """Перемещение CSV в archive/ после загрузки (предотвращает повторный импорт)."""
    if not os.path.exists(filename):
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    dest = os.path.join(ARCHIVE_DIR, filename)
    try:
        shutil.move(filename, dest)
        _p(f"[CSV] Архивирован: {dest}")
    except Exception as e:
        _p(f"[CSV] Ошибка архивации: {e}")


# ---------------------------------------------------------------------------
# Обработка CSV
# ---------------------------------------------------------------------------
def process_csv(filepath: str, comp_name: str, country: str) -> dict:
    """Обработка одного CSV файла."""
    season, league_code = _parse_csv_filename(filepath)
    if not season or not league_code:
        _p(f"[CSV] Не удалось разобрать имя файла: {filepath}")
        return {"matches": 0, "errors": 0, "skipped": 0}

    _p(f"[CSV] Обработка: {filepath} (season={season}, league={league_code})")

    matches = 0
    errors = 0
    skipped = 0
    consecutive_errors = 0

    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    payload = _build_payload(row, season, league_code, comp_name, country)
                    if payload is None:
                        skipped += 1
                        continue

                    cid = payload["canonical_id"]
                    if upsert_history_match(cid, payload):
                        matches += 1
                        consecutive_errors = 0
                    else:
                        errors += 1
                        consecutive_errors += 1
                        if consecutive_errors >= 10:
                            _p("[CSV] 10 ошибок подряд — останавливаем файл")
                            break
                except Exception as e:
                    errors += 1
                    consecutive_errors += 1
                    if errors <= 5:
                        _p(f"[CSV] Ошибка в строке: {e}")
                    if consecutive_errors >= 10:
                        _p("[CSV] 10 ошибок подряд — останавливаем файл")
                        break
    except Exception as e:
        _p(f"[CSV] Ошибка чтения файла: {e}")
        return {"matches": matches, "errors": errors, "skipped": skipped}

    _p(f"[CSV] Готово: {matches} матчей, {skipped} пропущено, {errors} ошибок")
    return {"matches": matches, "errors": errors, "skipped": skipped}


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def collect_football_data() -> Dict[str, Any]:
    """Главная функция коллектора."""
    _p("[FD] Football-Data Collector v5.0 (CSV) started")
    _p(f"[FD] Источник: football-data.co.uk")

    if not is_redis_available():
        _p("[FD] Redis недоступен — остановка")
        return {"stored_matches": 0, "total_events": 0, "error_count": 1}

    season = SEASON or _auto_season()
    _p(f"[FD] Сезон: {season}")
    _p(f"[FD] Лиг: {len(COMPETITIONS)}")

    total_stored = 0
    total_errors = 0
    total_skipped = 0

    for league_code, comp_name, country in COMPETITIONS:
        _p(f"\n[FD] --- {comp_name} ({league_code}) ---")

        filename = _download_csv(season, league_code)
        if filename is None:
            continue

        result = process_csv(filename, comp_name, country)
        total_stored += result["matches"]
        total_errors += result["errors"]
        total_skipped += result.get("skipped", 0)

        _archive_csv(filename)
        time.sleep(0.5)

    _p(f"\n[FD] === ИТОГ ===")
    _p(f"[FD] Сохранено матчей: {total_stored}")
    _p(f"[FD] Пропущено:       {total_skipped}")
    _p(f"[FD] Ошибок:          {total_errors}")

    save_meta(
        "football_data_loader",
        stored_matches=total_stored,
        total_events=total_stored + total_skipped,
        error_count=total_errors,
        skipped=total_skipped,
        season=season,
    )

    return {
        "stored_matches": total_stored,
        "total_events": total_stored + total_skipped,
        "error_count": total_errors,
    }


if __name__ == "__main__":
    collect_football_data()
