#!/usr/bin/env python3
"""
xG Coverage Map v1.0 — карта xG-покрытия CSV football-data.co.uk

Задача: до патча обогатителя (Understat) показать точную карту — в каких
лигах×сезонах xG есть в самом CSV (HxG/AxG, появилась с сезона 2526),
а где пайплайну нужен дозагружаемый xG. Заодно аудит остальных блоков
манифеста: 12 метрик, 1X2 closing/opening, O/U 2.5, AH.

Источник данных: сначала /tmp/football_data_archive (архив прогона загрузчика),
при отсутствии файла — HTTP-скачивание (read-only, ничего не пишет в Redis).
Режим selftest проверяет сканер на синтетических CSV без сети.

Запуск:
  python xg_coverage_map.py                          # все 4 сезона × 22 лиги
  python xg_coverage_map.py --seasons 2324,2425      # только нужные сезоны
  python xg_coverage_map.py --leagues E0,D1          # только нужные лиги
  python xg_coverage_map.py --selftest               # проверка без сети
"""

import argparse
import csv
import io
import os
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "https://www.football-data.co.uk/mmz4281"
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/tmp/football_data_archive")
USER_AGENT = "FootballDataXGMap/1.0"

SEASONS_DEFAULT = ["2324", "2425", "2526", "2627"]
LEAGUES = ["E0", "E1", "E2", "E3", "EC", "SC0", "SC1", "SC2", "SC3",
           "D1", "D2", "I1", "I2", "SP1", "SP2", "F1", "F2", "N1", "B1", "P1", "T1", "G1"]

# Блоки манифеста движка (v8.17-slim): колонки CSV, которые ищем в заголовке
XG_COLS = ["HxG", "AxG"]
STATS_COLS = ["HS", "AS", "HST", "AST", "HC", "AC", "HF", "AF", "HY", "AY", "HR", "AR"]
ODDS_CLOSING_COLS = ["PPCH", "MaxCH", "AvgCH", "X1XBCH", "B365CH", "BWCH", "IWCH", "PSCH", "BFDCH", "BFECH"]
ODDS_OPENING_COLS = ["PPH", "PSH", "B365H"]
OU_COLS = ["PC>2.5", "PC<2.5"]           # в заголовке CSV литеральные ">2.5"
AH_COLS = ["AHCh", "PCAHH", "PCAHA"]

BLOCKS = {
    "xg": XG_COLS,
    "stats": STATS_COLS,
    "1x2_close": ODDS_CLOSING_COLS,
    "1x2_open": ODDS_OPENING_COLS,
    "ou": OU_COLS,
    "ah": AH_COLS,
}


def is_number(val):
    if val is None:
        return False
    s = str(val).strip()
    if s == "" or s.upper() == "NA":
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def fetch_http(season, league):
    """Скачивание CSV с 2 ретраями. None при 404 (сезон не начался)."""
    url = f"{BASE_URL}/{season}/{league}.csv"
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt < 1:
                time.sleep(3)
                continue
            return None
        except (urllib.error.URLError, OSError):
            if attempt < 1:
                time.sleep(3)
                continue
            return None
    return None


def get_csv(season, league, allow_download=True):
    """Приоритет: архив загрузчика -> HTTP."""
    path = os.path.join(ARCHIVE_DIR, f"{season}_{league}.csv")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), "archive"
    if allow_download:
        content = fetch_http(season, league)
        if content is not None:
            return content, "http"
    return None, "missing"


def scan_csv(content):
    """Скан одного CSV: карта блоков по заголовку + счётчик заполненного xG."""
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration:
        return None
    cols = set(header)

    row_stats = {"rows": 0, "xg_filled": 0, "stats_filled": 0}
    has_xg_col = all(c in cols for c in XG_COLS)
    ix_h = header.index("HxG") if "HxG" in header else None
    ix_a = header.index("AxG") if "AxG" in header else None
    stats_needed = ["HS", "AS", "HST", "AST", "HC", "AC"]
    stat_idx = [header.index(c) for c in stats_needed if c in header]

    for row in reader:
        if not row:
            continue
        row_stats["rows"] += 1
        if has_xg_col and ix_h is not None and ix_a is not None \
                and is_number(row[ix_h] if ix_h < len(row) else None) \
                and is_number(row[ix_a] if ix_a < len(row) else None):
            row_stats["xg_filled"] += 1
        if stat_idx and all(i < len(row) and is_number(row[i]) for i in stat_idx):
            row_stats["stats_filled"] += 1

    return {"header": cols, **row_stats}


def block_status(cols, block_cols):
    """OK: все колонки блока есть; PARTIAL: часть; MISSING: нет ни одной."""
    present = [c for c in block_cols if c in cols]
    if len(present) == len(block_cols):
        return "OK"
    if present:
        return f"partial({len(present)}/{len(block_cols)})"
    return "-"


def run_map(seasons, leagues, allow_download=True):
    print(f"=== xG Coverage Map: {len(seasons)} сезонов × {len(leagues)} лиг ===\n")
    print(f"{'Сезон':<6} {'Лига':<5} {'Src':<8} {'Матчей':>6}  {'xG':<9} {'xG-зап.':>7}  "
          f"{'stats':<9} {'1x2c':<10} {'1x2o':<7} {'ou':<7} {'ah':<7}")
    print("-" * 92)

    missing = []
    summary = {}
    for season in seasons:
        for league in leagues:
            content, src = get_csv(season, league, allow_download)
            if content is None:
                missing.append(f"{season}/{league}")
                print(f"{season:<6} {league:<5} {'MISSING':<8}")
                continue
            info = scan_csv(content)
            if info is None:
                missing.append(f"{season}/{league} (пустой)")
                print(f"{season:<6} {league:<5} {'EMPTY':<8}")
                continue
            cells = {name: block_status(info["header"], cols) for name, cols in BLOCKS.items()}
            xg_pct = f"{100 * info['xg_filled'] / info['rows']:.0f}%" if info["rows"] else "-"
            print(f"{season:<6} {league:<5} {src:<8} {info['rows']:>6}  {cells['xg']:<9} {xg_pct:>7}  "
                  f"{cells['stats']:<9} {cells['1x2c']:<10} {cells['1x2o']:<7} {cells['ou']:<7} {cells['ah']:<7}")
            summary[(season, league)] = {"xg": cells["xg"], "xg_filled": info["xg_filled"],
                                         "rows": info["rows"], "src": src}
            if src == "http":
                time.sleep(0.5)  # вежливость к источнику

    # ── Итог: где xG нативен, а где нужен Understat ──
    print("\n=== ИТОГ: xG-покрытие ===")
    for season in seasons:
        native = [lg for lg in leagues if summary.get((season, lg), {}).get("xg") == "OK"
                  and summary.get((season, lg), {}).get("xg_filled", 0) > 0]
        covered = [lg for lg in leagues if (season, lg) in summary]
        null_side = [lg for lg in covered if lg not in native]
        print(f"  {season}: нативный xG в CSV — {', '.join(native) if native else 'нет'}; "
              f"xG-NULL (кандидаты Understat/добор) — {', '.join(null_side) if null_side else 'нет'}")
    if missing:
        print(f"\n  Файлы не получены ({len(missing)}): {', '.join(missing)}")
    return 0


def run_selftest():
    """Синтетические CSV: два профиля — с xG-колонками и без."""
    base_cols = ("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,HS,AS,HST,AST,"
                 "HC,AC,HF,AF,HY,AY,HR,AR,B365H,B365D,B365A,PPH,PSD,PSA,AHCh,PCAHH,PCAHA,PC>2.5,PC<2.5,B365CH,B365CD,B365CA")
    with_xg = base_cols + ",HxG,AxG"
    r1 = "E0,13/08/2023,15:00,Alpha,Beta,2,1,H,1,0,H,10,8,4,3,5,4,11,10,1,2,0,0,2.1,3.4,3.6,2.2,3.3,3.5,0.5,1.95,1.9,1.85,2.05"
    r2 = "E0,20/08/2023,16:30,Gamma,Delta,0,0,D,0,0,D,9,9,3,3,4,6,12,9,2,1,1,0,1.9,3.5,4.0,1.8,3.4,4.2,-0.25,2.1,1.75,1.8,2.15"
    close_row = ",2.0,3.5,3.7"
    xg_row = ",1.42,1.05"

    content_with = with_xg + "\n" + r1 + close_row + xg_row + "\n" + r2 + close_row + xg_row + "\n"
    content_without = base_cols + "\n" + r1 + close_row + "\n" + r2 + close_row + "\n"

    a = scan_csv(content_with)
    b = scan_csv(content_without)
    assert a and a["rows"] == 2 and a["xg_filled"] == 2, f"selftest A: {a}"
    for c in ["HxG", "AxG", "PC>2.5", "PC<2.5", "AHCh", "PPH", "B365CH"]:
        assert c in a["header"], f"selftest A: нет {c}"
    assert block_status(a["header"], XG_COLS) == "OK"
    assert block_status(a["header"], OU_COLS) == "OK"
    assert b and b["rows"] == 2 and b["xg_filled"] == 0, f"selftest B: {b}"
    assert block_status(b["header"], XG_COLS) == "-"
    assert block_status(b["header"], OU_COLS) == "OK"       # OU-колонки есть в базовом профиле
    assert block_status(b["header"], AH_COLS) == "OK"
    assert block_status(b["header"], ODDS_CLOSING_COLS).startswith("partial")  # B365 есть, каскада нет
    print("Selftest OK: оба профиля разобраны корректно (с xG / без xG)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="xG Coverage Map — карта xG в CSV football-data")
    parser.add_argument("--seasons", default=",".join(SEASONS_DEFAULT))
    parser.add_argument("--leagues", default=",".join(LEAGUES))
    parser.add_argument("--no-download", action="store_true", help="только архив, без HTTP")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    leagues = [l.strip() for l in args.leagues.split(",") if l.strip()]
    return run_map(seasons, leagues, allow_download=not args.no_download)


if __name__ == "__main__":
    sys.exit(main())
