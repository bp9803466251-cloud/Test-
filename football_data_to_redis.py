#!/usr/bin/env python3
"""
Football-Data Collector v5.0 (CSV)
Источник: football-data.co.uk/mmz4281/{season}/{league}.csv
Запись: gatekeeper_hub.upsert_history_match() → history:match:* + 3 индекса

Параметры:
  --seasons      Сезоны через запятую (напр. 2425,2526,2627). Пусто = авто
  --leagues      Лиги через запятую (напр. E0,E1,SP1). Пусто = все 22
  --history-days Окно истории (0 = весь CSV)
  --limit        Лимит матчей на лигу (для теста)
"""

import argparse
import csv
import io
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Импорт хаба
# ---------------------------------------------------------------------------
from gatekeeper_hub import (
    upsert_history_match,
    update_history_indexes,
    build_canonical_id,
    clean_team_name,
    save_meta,
    now_msk,
)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
BASE_URL = "https://www.football-data.co.uk/mmz4281"
ARCHIVE_DIR = "archive"
MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds

ALL_LEAGUES = [
    ("E0", "Premier League", "England"),
    ("E1", "Championship", "England"),
    ("E2", "League One", "England"),
    ("E3", "League Two", "England"),
    ("EC", "National League", "England"),
    ("SC0", "Premiership", "Scotland"),
    ("SC1", "Championship", "Scotland"),
    ("SC2", "League One", "Scotland"),
    ("SC3", "League Two", "Scotland"),
    ("D1", "Bundesliga", "Germany"),
    ("D2", "2. Bundesliga", "Germany"),
    ("I1", "Serie A", "Italy"),
    ("I2", "Serie B", "Italy"),
    ("SP1", "La Liga", "Spain"),
    ("SP2", "La Liga 2", "Spain"),
    ("F1", "Ligue 1", "France"),
    ("F2", "Ligue 2", "France"),
    ("N1", "Eredivisie", "Netherlands"),
    ("P1", "Primeira Liga", "Portugal"),
    ("B1", "First Division A", "Belgium"),
    ("G1", "Super League", "Greece"),
    ("T1", "Super Lig", "Turkey"),
]

LEAGUE_MAP = {code: (name, country) for code, name, country in ALL_LEAGUES}

# Приоритет odds: B365 → BbAv → IW → LB → WH → VC
ODDS_PRIORITY = [
    ("B365", "B365H", "B365D", "B365A", "bet365"),
    ("BbAv", "BbAvH", "BbAvD", "BbAvA", "betbrain_avg"),
    ("IW", "IWH", "IWD", "IWA", "interwetten"),
    ("LB", "LBH", "LBD", "LBA", "ladbrokes"),
    ("WH", "WHH", "WHD", "WHA", "william_hill"),
    ("VC", "VCH", "VCD", "VCA", "vc_bet"),
]


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def auto_season() -> str:
    """Текущий сезон в формате YYYY (напр. 2627 для 2026/2027)."""
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    # Сезон начинается в августе: если месяц >= 8, сезон = YY(YY+1)
    if month >= 8:
        return f"{year % 100:02d}{(year + 1) % 100:02d}"
    else:
        return f"{(year - 1) % 100:02d}{year % 100:02d}"


def parse_seasons(seasons_input: str) -> list:
    """Парсим seasons: '2425,2526,2627' → ['2425', '2526', '2627']. Пусто = [auto]."""
    if not seasons_input or not seasons_input.strip():
        return [auto_season()]
    return [s.strip() for s in seasons_input.split(",") if s.strip()]


def parse_leagues(leagues_input: str) -> list:
    """Парсим leagues: 'E0,E1,SP1' → [('E0','Premier League','England'), ...]. Пусто = все."""
    if not leagues_input or not leagues_input.strip():
        return list(ALL_LEAGUES)
    codes = [l.strip().upper() for l in leagues_input.split(",") if l.strip()]
    result = []
    for code in codes:
        if code in LEAGUE_MAP:
            name, country = LEAGUE_MAP[code]
            result.append((code, name, country))
        else:
            print(f"[FD] Неизвестный код лиги: {code} — пропускаю")
    return result


def safe_int(val) -> int:
    if val is None or val == "" or val == "-":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def safe_float(val) -> str:
    if val is None or val == "" or val == "-":
        return ""
    try:
        return str(float(val))
    except (ValueError, TypeError):
        return ""


def parse_date(row: dict) -> str:
    """Date (DD/MM/YYYY) + Time (HH:MM) → ISO 8601 UTC."""
    date_str = row.get("Date", "").strip()
    if not date_str:
        return ""
    try:
        # Формат DD/MM/YYYY
        dt = datetime.strptime(date_str, "%d/%m/%Y")
        time_str = row.get("Time", "").strip()
        if time_str:
            try:
                t = datetime.strptime(time_str, "%H:%M")
                dt = dt.replace(hour=t.hour, minute=t.minute)
            except ValueError:
                pass
        return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        try:
            # Формат DD/MM/YY
            dt = datetime.strptime(date_str, "%d/%m/%y")
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            return ""


def within_history_window(date_utc: str, history_days: int) -> bool:
    """Если history_days=0 — без фильтра. Иначе — только последние N дней."""
    if history_days <= 0:
        return True
    if not date_utc:
        return False
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
        return dt >= cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Odds: приоритет B365 → BbAv → IW → LB → WH → VC
# ---------------------------------------------------------------------------
def build_1x2_odds(row: dict) -> dict:
    """Построить odds.1x2 блок с приоритетом по источникам."""
    ts = now_msk()
    for prefix, h_col, d_col, a_col, upstream_name in ODDS_PRIORITY:
        h = safe_float(row.get(h_col))
        d = safe_float(row.get(d_col))
        a = safe_float(row.get(a_col))
        if h and d and a:
            price = {"home": h, "draw": d, "away": a}
            return {
                "1x2": {
                    "current": dict(price),
                    "opening": dict(price),
                    "best": dict(price),
                    "sources": [{
                        "source": "football_data",
                        "upstream": upstream_name,
                        "price": price,
                        "timestamp": ts,
                        "type": "closing",
                    }],
                }
            }
    return {}


# ---------------------------------------------------------------------------
# Stats: 12 метрик
# ---------------------------------------------------------------------------
def build_stats(row: dict) -> dict:
    return {
        "shots_home": safe_int(row.get("HS")),
        "shots_away": safe_int(row.get("AS")),
        "shots_on_target_home": safe_int(row.get("HST")),
        "shots_on_target_away": safe_int(row.get("AST")),
        "corners_home": safe_int(row.get("HC")),
        "corners_away": safe_int(row.get("AC")),
        "fouls_home": safe_int(row.get("HF")),
        "fouls_away": safe_int(row.get("AF")),
        "yellow_home": safe_int(row.get("HY")),
        "yellow_away": safe_int(row.get("AY")),
        "red_home": safe_int(row.get("HR")),
        "red_away": safe_int(row.get("AR")),
    }


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
def build_flags(score: dict, stats: dict) -> dict:
    diff = abs(score.get("home", 0) - score.get("away", 0))
    red = (stats.get("red_home", 0) > 0 or stats.get("red_away", 0) > 0)
    return {
        "extreme_result": diff >= 4 or red,
        "abnormal_score": score.get("home", 0) >= 5 or score.get("away", 0) >= 5,
        "red_card_driven": red and diff >= 2,
    }


# ---------------------------------------------------------------------------
# Скачать CSV
# ---------------------------------------------------------------------------
def download_csv(season: str, league_code: str) -> str:
    """Скачать CSV с retries. Возвращает содержимое или пустую строку."""
    url = f"{BASE_URL}/{season}/{league_code}.csv"
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[CSV] Скачивание {url} (попытка {attempt}/{MAX_RETRIES})")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                print(f"[CSV] Скачано: {season}_{league_code}.csv ({len(data)} байт)")
                return data.decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"[CSV] 404 — файл не существует для {season}/{league_code}")
                return ""
            print(f"[CSV] HTTP {e.code}, попытка {attempt}")
        except Exception as e:
            print(f"[CSV] Ошибка: {e}, попытка {attempt}")
        if attempt < MAX_RETRIES:
            import time
            time.sleep(RETRY_DELAY)
    return ""


def archive_csv(season: str, league_code: str, content: str) -> None:
    """Сохранить CSV в archive/."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"{season}_{league_code}.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[CSV] Архивирован: {path}")


# ---------------------------------------------------------------------------
# Обработка CSV
# ---------------------------------------------------------------------------
def process_csv(content: str, season: str, league_code: str, league_name: str,
                country: str, history_days: int, limit: int = 0) -> dict:
    """Парсинг CSV и запись в Redis через хаб. Возвращает статистику."""
    stats = {"total": 0, "written": 0, "skipped": 0, "errors": 0}

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        print(f"[CSV] Пустой CSV для {league_code}")
        return stats

    for row in reader:
        stats["total"] += 1

        if limit > 0 and stats["written"] >= limit:
            break

        home_team = row.get("HomeTeam", "").strip()
        away_team = row.get("AwayTeam", "").strip()
        if not home_team or not away_team:
            stats["skipped"] += 1
            continue

        date_utc = parse_date(row)
        if not date_utc:
            stats["skipped"] += 1
            continue

        if not within_history_window(date_utc, history_days):
            stats["skipped"] += 1
            continue

        score = {
            "home": safe_int(row.get("FTHG")),
            "away": safe_int(row.get("FTAG")),
        }
        half_time_score = {
            "home": safe_int(row.get("HTHG")),
            "away": safe_int(row.get("HTAG")),
        }
        full_time_result = row.get("FTR", "").strip()
        referee = row.get("Referee", "").strip()

        odds = build_1x2_odds(row)
        match_stats = build_stats(row)
        flags = build_flags(score, match_stats)

        canonical_id = build_canonical_id(home_team, away_team, date_utc)

        payload = {
            "canonical_id": canonical_id,
            "home_team": home_team,
            "away_team": away_team,
            "competition": league_name,
            "country": country,
            "season": season,
            "league_code": league_code,
            "date_utc": date_utc,
            "status": "completed",
            "score": score,
            "half_time_score": half_time_score,
            "full_time_result": full_time_result,
            "referee": referee,
            "version": 1,
            "schema_version": "v700",
            "odds": odds,
            "predictions": {},
            "stats": match_stats,
            "h2h": {},
            "source_map": {
                "odds": {
                    "source": "football_data",
                    "upstream": "bet365" if odds else "unknown",
                    "type": "closing",
                },
                "stats": {
                    "source": "football_data",
                    "upstream": "match_data",
                },
            },
            "source_ids": {"football_data": league_code},
            "sources": ["football_data"],
            "section_history": [],
            "flags": flags,
        }

        try:
            ok = upsert_history_match(canonical_id, payload)
            if ok:
                stats["written"] += 1
            else:
                stats["errors"] += 1
        except Exception as e:
            print(f"[CSV] Ошибка записи {canonical_id}: {e}")
            stats["errors"] += 1

    return stats


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Football-Data Collector v5.0 (CSV)")
    parser.add_argument("--seasons", default="", help="Сезоны через запятую (напр. 2425,2526,2627)")
    parser.add_argument("--leagues", default="", help="Лиги через запятую (напр. E0,E1,SP1)")
    parser.add_argument("--history-days", type=int, default=0, help="Окно истории (0 = весь CSV)")
    parser.add_argument("--limit", type=int, default=0, help="Лимит матчей на лигу (для теста)")
    args = parser.parse_args()

    seasons = parse_seasons(args.seasons)
    leagues = parse_leagues(args.leagues)

    print(f"[FD] Football-Data Collector v5.0 (CSV) started")
    print(f"[FD] Источник: football-data.co.uk")
    print(f"[FD] Сезоны: {', '.join(seasons)}")
    print(f"[FD] Лиг: {len(leagues)}")
    print(f"[FD] History days: {args.history_days} ({'без фильтра' if args.history_days == 0 else f'последние {args.history_days} дней'})")
    if args.limit > 0:
        print(f"[FD] Лимит матчей на лигу: {args.limit}")
    print()

    grand_total = {"total": 0, "written": 0, "skipped": 0, "errors": 0}

    for season in seasons:
        print(f"[FD] {'='*50}")
        print(f"[FD] Сезон {season}")
        print(f"[FD] {'='*50}")

        for league_code, league_name, country in leagues:
            print(f"[FD] --- {league_name} ({league_code}) ---")

            content = download_csv(season, league_code)
            if not content:
                print(f"[FD] Нет данных для {league_code} в сезоне {season}")
                print()
                continue

            archive_csv(season, league_code, content)

            print(f"[CSV] Обработка: {season}_{league_code}.csv (season={season}, league={league_code})")
            result = process_csv(
                content, season, league_code, league_name, country,
                args.history_days, args.limit,
            )

            print(f"[CSV] Готово: {result['written']} матчей, {result['skipped']} пропущено, {result['errors']} ошибок")
            print()

            for k in grand_total:
                grand_total[k] += result[k]

        # Сохранить мета после каждого сезона
        save_meta("football_data", season=season, leagues=len(leagues), **grand_total)

    print(f"[FD] {'='*50}")
    print(f"[FD] ИТОГО")
    print(f"[FD] {'='*50}")
    print(f"[FD] Всего строк:  {grand_total['total']}")
    print(f"[FD] Записано:     {grand_total['written']}")
    print(f"[FD] Пропущено:    {grand_total['skipped']}")
    print(f"[FD] Ошибок:       {grand_total['errors']}")
    print(f"[FD] Done.")


if __name__ == "__main__":
    main()
