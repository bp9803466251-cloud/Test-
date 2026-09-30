#!/usr/bin/env python3
"""Скачивает CSV-файлы с football-data.co.uk.

Использование:
    python football_data_downloader.py                          # все сезоны и лиги
    python football_data_downloader.py --seasons 2425,2526      # только 2 сезона
    python football_data_downloader.py --leagues E0,SP1,I1      # только 3 лиги
    python football_data_downloader.py --seasons 2425 --leagues E0  # один файл
"""

import os
import sys
import time
import urllib.request
import urllib.error

# ── 22 лиги football-data.co.uk ──────────────────────────────────────────────
LEAGUES = [
    # Англия
    "E0",   # Premier League
    "E1",   # Championship
    "E2",   # League One
    "E3",   # League Two
    "EC",   # National League
    # Шотландия
    "SC0",  # Premiership
    "SC1",  # Division 1
    "SC2",  # Division 2
    "SC3",  # Division 3
    # Испания
    "SP1",  # La Liga
    "SP2",  # Segunda Division
    # Италия
    "I1",   # Serie A
    "I2",   # Serie B
    # Германия
    "D1",   # Bundesliga 1
    "D2",   # Bundesliga 2
    # Франция
    "F1",   # Ligue 1
    "F2",   # Ligue 2
    # Нидерланды
    "N1",   # Eredivisie
    # Бельгия
    "B1",   # First Division
    # Португалия
    "P1",   # Liga I
    # Турция
    "T1",   # Super Lig
    # Греция
    "G1",   # Super League
]

# ── 11 сезонов: 2015/16 — 2025/26 ────────────────────────────────────────────
SEASONS = [
    "1516",
    "1617",
    "1718",
    "1819",
    "1920",
    "2021",
    "2122",
    "2223",
    "2324",
    "2425",
    "2526",
]

BASE_URL = "https://www.football-data.co.uk/mmz4281"
OUTPUT_DIR = "csv_data"
DELAY = 0.3  # секунды между файлами


def download_csv(season: str, league: str) -> bool:
    """Скачивает один CSV-файл. Возвращает True при успехе."""
    url = f"{BASE_URL}/{season}/{league}.csv"
    season_dir = os.path.join(OUTPUT_DIR, season)
    os.makedirs(season_dir, exist_ok=True)
    filepath = os.path.join(season_dir, f"{league}.csv")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if len(data) < 100:
                print(f"  SKIP {season}/{league}: too small ({len(data)} bytes)")
                return False
            with open(filepath, "wb") as f:
                f.write(data)
            print(f"  OK   {season}/{league}: {len(data)} bytes")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  N/A  {season}/{league}: 404 (not available)")
        else:
            print(f"  ERR  {season}/{league}: HTTP {e.code}")
        return False
    except Exception as e:
        print(f"  ERR  {season}/{league}: {e}")
        return False


def main():
    # Парсинг аргументов
    seasons = SEASONS
    leagues = LEAGUES

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--seasons" and i + 1 < len(args):
            seasons = [s.strip() for s in args[i + 1].split(",") if s.strip()]
            i += 2
        elif args[i] == "--leagues" and i + 1 < len(args):
            leagues = [l.strip() for l in args[i + 1].split(",") if l.strip()]
            i += 2
        else:
            i += 1

    print(f"=== Football Data CSV Downloader ===")
    print(f"Seasons: {len(seasons)} ({', '.join(seasons)})")
    print(f"Leagues: {len(leagues)} ({', '.join(leagues)})")
    print(f"Total combinations: {len(seasons) * len(leagues)}")
    print()

    ok = 0
    fail = 0

    for season in seasons:
        print(f"--- Season {season} ---")
        for league in leagues:
            if download_csv(season, league):
                ok += 1
            else:
                fail += 1
            time.sleep(DELAY)

    print()
    print(f"=== Done ===")
    print(f"Downloaded: {ok}")
    print(f"Failed/404: {fail}")
    print(f"Output dir: {OUTPUT_DIR}/")

    # Размер по сезонам
    print(f"\n=== By season ===")
    for season in seasons:
        season_dir = os.path.join(OUTPUT_DIR, season)
        if os.path.isdir(season_dir):
            files = [f for f in os.listdir(season_dir) if f.endswith(".csv")]
            total_size = sum(
                os.path.getsize(os.path.join(season_dir, f)) for f in files
            )
            print(f"  {season}: {len(files)} files, {total_size / 1024:.0f} KB")
        else:
            print(f"  {season}: 0 files")


if __name__ == "__main__":
    main()
                                    
