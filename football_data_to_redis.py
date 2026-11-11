#!/usr/bin/env python3
"""
Football-Data Collector v8.18-registry-strict (CSV) — engine-manifest payload, GatekeeperAI v710

v8.18-registry-strict (Strict Degradation Protocol, §14.1):
  - D1: OPENING_CASCADE без мёртвого кортежа PPH (колонка PPH в CSV 2425+ отсутствует,
    факт сверки заголовка 2526 live-проверкой). Единственный Pinnacle-key: PSH/PSD/PSA.
  - Fallback-словарь TEAM_ALIASES удалён целиком (вторая копия алиасов, §14.1);
    при недоступности team_registry прогон честно падает с exit 1, а не пишет мимо реестра.
  - clean_team_name / build_canonical_id — чистая делегация без глотания ошибок
    (try/except с pass = тихий отказ, §6.4). payload-формат не тронут.

v8.17-slim (манифест полей движка; §23/§29/§39 prompt №2):
  - PAYLOAD SLIM: csv_raw (все колонки CSV) удалён из payload целиком, build_csv_raw
    исключён. Движку нужны 4 блока: identity+score, stats (12 метрик + xG),
    odds 1X2/OU/AH closing, odds_opening (CLV-entry). Остальное не потребляется
    ни одной математической секцией Prompt №1.
  - +odds_opening: плоский вектор {"1","X","2"} по OPENING_CASCADE (PSH → B365H). CLV-entry (§28): opening = единственный легитимный источник признаков;
    в v8.12..8.16 opening жил только в csv_raw (анти-leakage для live-контура;
    в history-payload поля легальны как признак CLV).
  - Сжатие снапшотов: CLOSING_CASCADE остаётся полным для выбора closing-вектора
    (первый полный набор = наивысший приоритет), но snapshots пишутся только для
    pinnacle и bet365 — DQS Odds Verification (§7: ≥2 независимые цены) сохранён.
    OU/AH каскады обрезаны до pinnacle closing — 1 снапшот на рынок.
  - source_map синхронизирован: soft_bookmakers=["bet365"], types=["closing","opening"].
  - Вес payload: ~4.2 КБ/матч → ~1.3 КБ/матч. Потерю аудита csv_raw компенсирует
    исходник CSV на football-data.co.uk.
  - Guide-delta §6: изменение структуры history-payload фиксируется в гид-дельте цикла.

v8.16-echelon-fix (фикс дефекта #26):
  - FIX #26: инверсия completed = not file_done в v8.15 роняла эшелон на первом
    же файле из processed_files с полным дедуп-путём (все матчи — дубли):
    completed=0, count=0 -> break после первой лиги. Прогон #26: смерть на E0.
    Теперь completed=True по умолчанию; False ставят только три точки прерывания
    (shutdown / limit / стоп-лосс).
  - Guard пустого env: int(os.getenv(...) or "9000") — пустая переменная при
    API-запуске без input больше не валит прогон int("").
  - Статус меты всегда актуален: успешный прогон снимает carry-over degraded.
  - Формат payload не тронут (совместимость с базой полная).

v8.14-dedup-mget (CSV) — ALL columns, GatekeeperAI v710
Источник: football-data.co.uk
Сезоны: --seasons "2425,2526,2627"
Лиги:   --leagues "E0,E1,SP1"

v8.15-stopfix (фикс дефекта #24 + настраиваемый лимит):
  - FIX #24: process_csv возвращает (count, completed); при прерывании пасса 2
    (стоп-лосс/shutdown/limit) файл НЕ помечается в processed_files — уходит в
    pending_files, мета получает status: degraded. Перманентные дыры исключены.
  - Эшелон прерывается на первом незавершённом файле (обе точки цикла:
    collect_and_process и main) — не тратим префетч MGET на непишущиеся файлы.
  - COMMAND_SOFT_LIMIT настраиваемый: env GATEKEEPER_COMMAND_SOFT_LIMIT,
    0 = отключён (восстановительные прогоны). Дефолт 9000 сохранён.
  - Поле skipped добавлено в мету (корень расхождения written+deduped vs
    total_events в #24/#83: skipped(1) не печатался и не читался диагностом).
  - pending_files мержатся в _flush_meta (объединение минус завершённые),
    дозагрузка снимает пометку.

v8.14-dedup-mget (дедуп через MGET-чанки, §13.4):
  - process_csv трёхфазный: пасс 1 — парсинг без Redis; пасс 1.5 — MGET-префетч
    дедупа чанками по 50 ключей (1 команда на чанк вместо 50 GET на матч);
    пасс 2 — дедуп/force-merge §12.6.1, запись через хаб, индексы в тот же батч
  - Fallback: сбой MGET -> поштучный GET (семантика v8.13); _manual_commands
    считает чанки MGET, а не матчи — бюджет §13.4 экономится ~15k команд
  - FIX: в dry-run переменная result могла быть не определена (латентный NameError)
  - Нюанс телеметрии: при limit>0 счётчики skipped_* отражают весь файл
    (пасс 1 парсит всё), а не только отсканированную до лимита часть

v8.13-metrics (метрическое расширение):
  - parse_stat: NA/пусто -> None для метрик stats (различение «нет данных» и «0»)
  - build_stats: + xg_home/xg_away из HxG/AxG (optional; None если колонки нет)
  - odds_over_under: closing-каскад O/U 2.5 (PC -> MaxC -> B365C -> AvgC -> BFEC);
    линии 1.5/3.5 = null (резерв под живой сбор Prompt №2)
  - odds_asian_handicap: линия AHCh (float, 0 валиден) + closing-каскад квот
  - CLOSING_CASCADE: + X1XBCH перед B365, + BFDCH/BFECH в конце
  - source_map: soft_bookmakers синхронизированы с каскадом; xG-блок с provider-атрибуцией

v8.12-patched (Шаг 13-B):
  - Строгий импорт хаба (13-G1): без gatekeeper_hub прогон не стартует;
    локальные fallback-реализации upsert/PipelineBatch удалены (§1.11)
  - Плоский odds_1x2 с ключами "1"/"X"/"2" + closing-снапшоты odds_history
    (Гейт 28, §1.21/§12.8)
  - CLOSING_CASCADE: PPCH → MaxCH → AvgCH → B365CH → BWH → IWH → PSH (§12.8)
  - source_map: types=["closing"], upstream="multi_bookmaker";
    opening-колонки живут только в csv_raw (анти-leakage)
  - predictions/value_analysis из payload удалены (§13.2)
  - Дедупликация через GET history:match:{cid} + force-merge (§12.6.1)
  - Индексы history:league/team/index:* — Root-ключи в том же PipelineBatch (R-5, R-7)
  - Стоп-лосс COMMAND_SOFT_LIMIT (§13.4) + processed_files для дозагрузки
  - Плоская football_data:meta через redis_hub (12-10, R-1); статус degraded (13-G1)
  - --history-days с env HISTORY_DAYS (12-11)

Schema: v710 (Гейт 28)
CSV:    120 колонок читаются; в payload — только поля манифеста движка (v8.17-slim)
Stats:  12 метрик + xG (optional) + _source + _updated_at
Transport: только gatekeeper_hub.upsert_match(mode="history") — §23
"""


import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
# ─── Строгий импорт (13-G1): хаб обязателен, дублей записи нет (§1.11) ───
try:
    from gatekeeper_hub import (
        upsert_match,
        is_shutdown_requested,
        run_initialization,
        UPSTREAM_MAP,
    )
except ImportError as _hub_err:
    print(f"[FATAL] gatekeeper_hub недоступен ({_hub_err}) — Strict Degradation Protocol "
          "(13-G1): запись мимо доменного шлюза запрещена, прогон прерван.")
    raise SystemExit(1)

try:
    from gatekeeper_hub import register_module, log_event
except ImportError:
    # Только декоратор/логирование — не запись данных, fallback легален
    def register_module(name, **kwargs):
        def deco(func):
            return func
        return deco
    def log_event(source, level, message, **kwargs):
        pass

from redis_hub import PipelineBatch, is_redis_available

# Единый реестр команд
try:
    from team_registry import clean_team_name as _registry_normalize
    from team_registry import build_canonical_id as _registry_build_cid
    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False
if not _REGISTRY_AVAILABLE:
    print("[FATAL] team_registry недоступен — Strict Degradation Protocol (§14.1): "
          "fallback-словарь TEAM_ALIASES исключён, запись мимо единого реестра запрещена.")
    raise SystemExit(1)

# Единое MSK-время
try:
    from gatekeeper_config import now_msk as _now_msk
    _NOW_MSK_AVAILABLE = True
except ImportError:
    _NOW_MSK_AVAILABLE = False
# ============================================================================
# CONFIG
# ============================================================================

VERSION = "8.18-registry-strict"
__version__ = "8.18-registry-strict"

# FIX-3: Убран logging.basicConfig
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())
__all__ = [
    "FootballDataCollector",
    "PipelineBatch",
    "collect_and_process",
    "build_payload",
    "build_canonical_id",
    "clean_team_name",
    "__version__",
]
SCHEMA_VERSION = "v710"
SOURCE_NAME = "football_data"

BASE_URL = "https://www.football-data.co.uk/mmz4281"
# §12.8: closing-каскад PPCH → MaxCH → AvgCH → B365CH → BWH → IWH → PSH.
# Порядок = приоритет; первый полный набор "1"/"X"/"2" становится odds_1x2.
# IW/PS в CSV 2425+ отсутствуют (IW мёртв, PS → PP rebranded) — оставлены для legacy CSV.
CLOSING_CASCADE = [
    ("PPCH",   "PPCD",   "PPCA",   "pinnacle"),
    ("MaxCH",  "MaxCD",  "MaxCA",  "market_max"),
    ("AvgCH",  "AvgCD",  "AvgCA",  "market_avg"),
    ("X1XBCH", "X1XBD",  "X1XBA",  "1xbet"),
    ("B365CH", "B365CD", "B365CA", "bet365"),
    ("BWCH",   "BWCD",   "BWCA",   "bwin"),
    ("IWCH",   "IWD",    "IWA",    "interwetten"),
    ("PSCH",   "PSCD",   "PSCA",   "pinnacle"),
    ("BFDCH",  "BFDCD",  "BFDCA",  "betfair_sportsbook"),
    ("BFECH",  "BFECD",  "BFECA",  "betfair_exchange"),
]

# v8.18: opening-каскад для CLV-entry (§28 prompt №1). Opening-колонки CSV
# без суффикса C. Факт D1: колонки PPH/PPD/PPA в CSV 2425+ отсутствуют (rebranded),
# сверено по живому заголовку 2526 — PSH/PSD/PSA единственный Pinnacle-key.
# Первый полный набор = entry.
OPENING_CASCADE = [
    ("PSH",   "PSD",   "PSA",   "pinnacle"),
    ("B365H", "B365D", "B365A", "bet365"),
]

# v8.17-slim: снапшоты odds_history пишутся только для независимых апстримов,
# покрывающих DQS Odds Verification (§7: ≥2 независимые цены). Остальные записи
# каскада нужны лишь для выбора closing-вектора (наивысший приоритет).
SNAPSHOT_UPSTREAMS = {"pinnacle", "bet365"}

# v8.13: closing-каскады O/U и AH. В CSV football-data линия O/U только 2.5;
# 1.5/3.5 зарезервированы под живой сбор Prompt №2 (null в структуре, не 0).
# Max/Avg — агрегаты агрегатора, в декларации независимости источниками не считаются.
OU_CLOSING_CASCADE = [
    ("PC>2.5",    "PC<2.5",    "pinnacle"),   # v8.17-slim: closing-only, 1 снапшот
]
OU_LINES = {"1.5": None, "2.5": OU_CLOSING_CASCADE, "3.5": None}

AH_CLOSING_CASCADE = [
    ("PCAHH",    "PCAHA",    "pinnacle"),   # v8.17-slim: closing-only, 1 снапшот
]

# §13.4: стоп-лосс — дисциплина 90% суточного бюджета команд Upstash.
# [8.15] Лимит настраиваемый: env GATEKEEPER_COMMAND_SOFT_LIMIT, 0 = отключён
# (восстановительные прогоны; свободный план Upstash жёсткую квоту не режет).
# Дефолт 9000 сохранён для рутинных прогонов.
# [8.16] Guard пустого env: при API-запуске без input переменная может быть ""
# — int("") валит прогон. Пустое значение -> дефолт 9000.
_SOFT_LIMIT_ENV = int(os.getenv("GATEKEEPER_COMMAND_SOFT_LIMIT") or "9000")
COMMAND_SOFT_LIMIT = _SOFT_LIMIT_ENV if _SOFT_LIMIT_ENV > 0 else None

# v8.14: дедуп через MGET-чанки (пасс 1.5) — 1 команда на 50 матчей вместо 50 GET
MGET_CHUNK_SIZE = 50
HISTORY_KEY_PREFIX = "history:match:"  # синхронно с redis_hub.HISTORY_PREFIX

LEAGUES = {
    "E0":  {"name": "Premier League",       "country": "England",    "division": 1},
    "E1":  {"name": "Championship",          "country": "England",    "division": 2},
    "E2":  {"name": "League One",            "country": "England",    "division": 3},
    "E3":  {"name": "League Two",           "country": "England",    "division": 4},
    "EC":  {"name": "National League",      "country": "England",    "division": 5},
    "SC0": {"name": "Scottish Premiership",  "country": "Scotland",   "division": 1},
    "SC1": {"name": "Scottish Championship", "country": "Scotland",  "division": 2},
    "SC2": {"name": "Scottish League One",   "country": "Scotland",   "division": 3},
    "SC3": {"name": "Scottish League Two",   "country": "Scotland",   "division": 4},
    "D1":  {"name": "Bundesliga",            "country": "Germany",     "division": 1},
    "D2":  {"name": "2. Bundesliga",         "country": "Germany",     "division": 2},
    "I1":  {"name": "Serie A",               "country": "Italy",      "division": 1},
    "I2":  {"name": "Serie B",               "country": "Italy",      "division": 2},
    "SP1": {"name": "La Liga",               "country": "Spain",      "division": 1},
    "SP2": {"name": "La Liga 2",             "country": "Spain",      "division": 2},
    "F1":  {"name": "Ligue 1",              "country": "France",      "division": 1},
    "F2":  {"name": "Ligue 2",              "country": "France",      "division": 2},
    "N1":  {"name": "Eredivisie",            "country": "Netherlands", "division": 1},
    "B1":  {"name": "Jupiler Pro League",    "country": "Belgium",    "division": 1},
    "P1":  {"name": "Primeira Liga",         "country": "Portugal",   "division": 1},
    "T1":  {"name": "Süper Lig",             "country": "Turkey",     "division": 1},
    "G1":  {"name": "Super League",          "country": "Greece",     "division": 1},
}
# [v8.18] Локальный словарь TEAM_ALIASES удалён (§14.1): единственный источник
# алиасов — Reep-снапшот через team_registry. Старый словарь не мигрируется
# (§1.5: «второго источника не будет»). Резолюция имён — только через реестр.
# ============================================================================
# HELPERS
# ============================================================================

def now_msk():
    """Единое MSK-время через gatekeeper_config. Fallback: UTC+3."""
    if _NOW_MSK_AVAILABLE:
        return _now_msk()
    # Fallback
    from datetime import timedelta
    return datetime.now(timezone(timedelta(hours=3)))


def now_iso():
    """ISO timestamp для created_at/updated_at."""
    ts = now_msk()
    if isinstance(ts, str):
        return ts.replace("+03:00", "Z") if "+03:00" in ts else ts
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_team_name(name):
    """Делегирует в team_registry.clean_team_name() — единый реестр (§14.1).

    Без обёртки try/except: ошибка реестра честно роняет прогон (§6.4),
    а не подменяется локальным fallback'ом — второй копии алиасов больше нет.
    """
    if not name:
        return ""
    return _registry_normalize(name)


def build_canonical_id(home_team, away_team, date_utc):
    """Единый canonical_id — только team_registry (§14.1, сборщик один).

    Без локальной fallback-реализации: при недоступности реестра прогон
    не стартует (строгий отказ в блоке импорта).
    """
    return _registry_build_cid(home_team, away_team, date_utc)


def parse_date(raw):
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
        except (ValueError, TypeError):
            continue
    return None


def parse_time(raw, date_utc):
    """Parse Time column (HH:MM) -> ISO 8601 combined with date."""
    if not raw or not date_utc:
        return None
    raw = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            date_part = date_utc[:10]
            return f"{date_part}T{t.strftime('%H:%M:%S')}Z"
        except (ValueError, TypeError):
            continue
    return None


def safe_int(val):
    if val is None or val == "" or val == "NA":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def parse_stat(val):
    """Статистика матча: пусто/NA/None -> None («данных нет»), отличимо от 0 (v8.13).
    Заменяет safe_int в build_stats: фальшивые нули искажали дисперсию для
    шринкейджа (§8), пост-аудит E003 и DQS-Statistics."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.upper() == "NA":
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def safe_str(val):
    if val is None:
        return ""
    return str(val).strip()


def safe_float(val):
    """Return float if valid, else None. Odds сохраняются как числа (FIX-1)."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s == "NA":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None

def _price_1x2(row, h_key, d_key, a_key):
    """Плоский вектор {"1", "X", "2"} из трёх CSV-колонок (Гейт 28, §1.21)."""
    h = safe_float(row.get(h_key, ""))
    d = safe_float(row.get(d_key, ""))
    a = safe_float(row.get(a_key, ""))
    if h and d and a:
        return {"1": h, "X": d, "2": a}
    return None


def detect_flags(stats, score):
    total_goals = score["home"] + score["away"]
    extreme = total_goals >= 7
    abnormal = total_goals >= 8
    red_home = stats.get("red_home") or 0
    red_away = stats.get("red_away") or 0
    red_driven = red_home > 0 or red_away > 0
    return {
        "extreme_result": extreme,
        "abnormal_score": abnormal,
        "red_card_driven": red_driven,
    }


def build_stats(row, ts):
    """12 метрик + xG (optional). NA/пусто -> None, не 0 (v8.13).
    xG (HxG/AxG) появился в CSV только с сезона 2526: отсутствие колонки
    -> None, чтобы Model A откатывался в xG-NULL, а не активировался с нулевым xG.
    xGA команды = xG соперника из той же записи (добывать не нужно)."""
    return {
        "shots_home": parse_stat(row.get("HS")),
        "shots_away": parse_stat(row.get("AS")),
        "shots_on_target_home": parse_stat(row.get("HST")),
        "shots_on_target_away": parse_stat(row.get("AST")),
        "corners_home": parse_stat(row.get("HC")),
        "corners_away": parse_stat(row.get("AC")),
        "fouls_home": parse_stat(row.get("HF")),
        "fouls_away": parse_stat(row.get("AF")),
        "yellow_home": parse_stat(row.get("HY")),
        "yellow_away": parse_stat(row.get("AY")),
        "red_home": parse_stat(row.get("HR")),
        "red_away": parse_stat(row.get("AR")),
        "xg_home": safe_float(row.get("HxG", "")),
        "xg_away": safe_float(row.get("AxG", "")),
        "_source": SOURCE_NAME,
        "_updated_at": ts,
    }
def build_source_map(ts):
    """§12.8: source_map загрузчика. Заголовочный upstream — multi_bookmaker;
    upstream по источнику колонки даёт каскад CLOSING_CASCADE.
    v8.17-slim: opening идёт в поле odds_opening (CLV-entry §28), не в снапшоты."""
    return {
        "odds": {
            "source": SOURCE_NAME,
            "upstream": "multi_bookmaker",
            "timestamp": ts,
            "independent": True,
        },
        "stats": {
            "source": SOURCE_NAME,
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
        "xg": {
            "source": SOURCE_NAME,
            "provider": "football_data",
            "upstream": None,
            "timestamp": ts,
            "independent": True,
        },
        "types": ["closing", "opening"],
        "sharp_benchmark": "pinnacle",
        "soft_bookmakers": ["bet365"],  # v8.17-slim: синхронизировано со снапшот-каскадом
    }


def build_odds_flat(row, ts):
    """§12.8: плоский odds_1x2 по closing-каскаду + снапшоты odds_history.
    Возвращает (odds_1x2, snapshots). Дедупликация снапшотов по {upstream, type} (12-7)."""
    odds_1x2 = {}
    snapshots = []
    seen_upstream = set()
    for h_key, d_key, a_key, upstream in CLOSING_CASCADE:
        price = _price_1x2(row, h_key, d_key, a_key)
        if not price:
            continue
        if not odds_1x2:
            odds_1x2 = price  # первый полный набор каскада = наивысший приоритет
        if upstream in seen_upstream:
            continue
        if upstream not in SNAPSHOT_UPSTREAMS:
            continue  # v8.17-slim: снапшот только pinnacle/bet365 (DQS §7)
        snapshots.append({
            "source": SOURCE_NAME,
            "upstream": upstream,
            "price": price,
            "timestamp": ts,
            "type": "closing",
        })
        seen_upstream.add(upstream)
    return odds_1x2, snapshots



def build_odds_opening(row):
    """v8.17-slim: плоский opening-вектор {"1","X","2"} по OPENING_CASCADE.
    CLV-entry (§28 prompt №1): entry-цена против closing. {} если полного набора
    нет — поле опускается, never invent data."""
    for h_key, d_key, a_key, _upstream in OPENING_CASCADE:
        price = _price_1x2(row, h_key, d_key, a_key)
        if price:
            return price
    return {}


def build_odds_over_under(row, ts):
    """v8.13: closing-каскад O/U по линии 2.5 (§12.8 по образцу 1X2).
    Возвращает (ou_block, snapshots). Линии 1.5/3.5 -> null (рынка нет в CSV,
    добирается живым сбором Prompt №2; null ≠ 0 — line identity §35.2).
    Снапшот пишется только при полной паре over+under."""
    snapshots = []
    closing = None
    for over_key, under_key, upstream in OU_CLOSING_CASCADE:
        over = safe_float(row.get(over_key, ""))
        under = safe_float(row.get(under_key, ""))
        if over is None or under is None:
            continue
        price = {"over": over, "under": under}
        if closing is None:
            closing = price  # первый полный набор каскада = наивысший приоритет
        snapshots.append({
            "source": SOURCE_NAME,
            "upstream": upstream,
            "market": "over_under",
            "line": 2.5,
            "price": price,
            "timestamp": ts,
            "type": "closing",
        })
    block_25 = {"closing": closing, "odds_history": snapshots} if closing else None
    return {"1.5": None, "2.5": block_25, "3.5": None}, snapshots


def build_odds_asian_handicap(row, ts):
    """v8.13: closing-каскад AH. Линия AHCh (float) + пары home/away квот.
    Возвращает (ah_block, snapshots); ah_block=None если линии или квот нет
    (линия остаётся только в CSV-источнике). ВАЖНО: AHCh=0 (равная фора) — валидная линия,
    проверка на is not None, не на truthiness (кейс легаси-записи #5)."""
    line = safe_float(row.get("AHCh", ""))
    if line is None:
        return None, []
    snapshots = []
    closing = None
    for h_key, a_key, upstream in AH_CLOSING_CASCADE:
        home = safe_float(row.get(h_key, ""))
        away = safe_float(row.get(a_key, ""))
        if home is None or away is None:
            continue
        price = {"home": home, "away": away}
        if closing is None:
            closing = price
        snapshots.append({
            "source": SOURCE_NAME,
            "upstream": upstream,
            "market": "asian_handicap",
            "line": line,
            "price": price,
            "timestamp": ts,
            "type": "closing",
        })
    if closing is None:
        return None, []  # линия есть, квот нет — рынок не верифицируем структурно
    return {"line": line, "closing": closing, "odds_history": snapshots}, snapshots


def build_payload(row, season, league_code, run_id=None):
    """Build slim match payload — только поля манифеста движка (v8.17-slim):
    identity+score, stats, odds (closing+opening), source_map. csv_raw исключён."""
    league_info = LEAGUES.get(league_code, {"name": league_code, "country": "Unknown", "division": 0})

    home_team = safe_str(row.get("HomeTeam", ""))
    away_team = safe_str(row.get("AwayTeam", ""))
    date_raw = parse_date(safe_str(row.get("Date", "")))
    time_raw = parse_time(safe_str(row.get("Time", "")), date_raw) if date_raw else None

    if not home_team or not away_team or not date_raw:
        return None

    # date_utc включает время, если доступно
    date_utc = time_raw if time_raw else date_raw

    home_score = safe_int(row.get("FTHG", 0))
    away_score = safe_int(row.get("FTAG", 0))

    ht_home = safe_int(row.get("HTHG", 0))
    ht_away = safe_int(row.get("HTAG", 0))

    ts = now_iso()

    stats = build_stats(row, ts)
    odds_1x2, odds_snapshots = build_odds_flat(row, ts)
    odds_opening = build_odds_opening(row)
    odds_over_under, _ = build_odds_over_under(row, ts)
    odds_asian_handicap, _ = build_odds_asian_handicap(row, ts)
    source_map = build_source_map(ts)

    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)
    canonical_id = build_canonical_id(home_team, away_team, date_utc)

    flags = detect_flags(stats, {"home": home_score, "away": away_score})

    payload = {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_clean,
        "away_clean": away_clean,
        "competition": league_info["name"],
        "country": league_info["country"],
        "season": str(season),
        "league_code": league_code,
        "division": league_info["division"],
        "date_utc": date_utc,
        "time_utc": time_raw,
        "status": "completed",
        "score": {"home": home_score, "away": away_score},
        "half_time_score": {"home": ht_home, "away": ht_away},
        "half_time_result": safe_str(row.get("HTR", "")),
        "full_time_result": safe_str(row.get("FTR", "")),
        "referee": safe_str(row.get("Referee", "")),
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "sources": [SOURCE_NAME],
        "odds_1x2": odds_1x2,
        "odds_history": odds_snapshots,
        "odds_opening": odds_opening,
        "odds_over_under": odds_over_under,
        "odds_asian_handicap": odds_asian_handicap,
        "source_ids": {"football_data": f"csv_{season}_{league_code}"},
        "h2h": {},
        "stats": stats,
        "source_map": source_map,
        "flags": flags,
        "created_at": ts,
        "updated_at": ts,
    }

    return payload
# ============================================================================
# COLLECTOR
# ============================================================================

class FootballDataCollector:
    """CSV-загрузчик §13: запись только через gatekeeper_hub.upsert_match(mode="history").
    13-G1: Strict Degradation Protocol; 13-G2: batch-режим PipelineBatch."""

    def __init__(self, dry_run=False, run_id=None):
        self.dry_run = dry_run
        self.run_id = run_id or "manual"
        self.matches_processed = 0
        self.errors = 0
        self.skipped = 0
        self.skipped_past = 0
        self.skipped_future = 0
        self.written = 0
        self.deduped = 0
        self.collision_warnings = 0
        self.alias_misses = 0
        self._manual_commands = 0      # GET/SET вне батча (дедуп, meta)
        self._batch = PipelineBatch(dry_run=dry_run, max_batch=50, batch_delay=0.15)
        self._meta_fields = {}
        self.processed_files = self._load_processed_files()
        self.pending_files = []  # [8.15] файлы, не долитые до конца

    def _load_processed_files(self):
        """processed_files из football_data:meta (§13.4) — 1 GET на прогон."""
        if self.dry_run:
            return []
        try:
            from redis_hub import redis_command
            raw = redis_command(["GET", "football_data:meta"])
            self._manual_commands += 1
            if raw:
                meta = json.loads(raw) if isinstance(raw, str) else {}
                pf = meta.get("processed_files", [])
                if isinstance(pf, list):
                    return list(pf)
        except Exception as e:
            logger.warning(f"processed_files не загружены: {e}")
        return []

    @property
    def commands_used(self):
        """Суммарный расход команд §13.4: батч + одиночные GET/SET."""
        return self._batch.commands_used + self._manual_commands

    def download_csv(self, season, league_code):
        """Download CSV for a season+league from football-data.co.uk. 3 попытки."""
        url = f"{BASE_URL}/{season}/{league_code}.csv"
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"User-Agent": f"FootballDataCollector/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    content = resp.read().decode("utf-8-sig", errors="replace")
                return content
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.info(f"  [SKIP] {season}/{league_code} — сезон ещё не начался (404)")
                    return None
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    logger.info(f"  [RETRY] HTTP {e.code} for {url}, ждём {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"HTTP {e.code} for {url}")
                return None
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    wait = 3 * (attempt + 1)
                    logger.info(f"  [RETRY] {e} for {url}, ждём {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"Error {e} for {url}")
                return None
        return None

    def archive_csv(self, season, league_code, content):
        """Save raw CSV to /tmp for archival (all 22 leagues, always — even dry-run)."""
        archive_dir = os.environ.get("ARCHIVE_DIR", "/tmp/football_data_archive")
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"{season}_{league_code}.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    def process_csv(self, csv_content, season, league_code, limit=0, history_days=0):
        """Парсинг CSV и запись через хаб.

        v8.14-dedup-mget: трёхфазная схема (§13.4):
          Пасс 1   — парсинг всех строк без обращений к Redis (фильтры §23, счётчики);
          Пасс 1.5 — MGET-префетч дедупа чанками по 50 ключей (1 команда на чанк);
          Пасс 2   — дедуп/force-merge (§12.6.1), запись через хаб, индексы в тот же батч.
        Семантика счётчиков (written/deduped/collision_warnings) сохранена байт в байт.
        """
        reader = csv.DictReader(io.StringIO(csv_content))
        now = datetime.now(timezone.utc)
        file_key = f"{season}_{league_code}"
        file_done = file_key in set(self.processed_files)
        # [8.16 FIX #26] Файл из processed_files ЗАВЕРШЁН: completed=True.
        # Инверсия v8.15 (completed = not file_done) роняла эшелон на первом же
        # помеченном файле с полным дедуп-путём (прогон #26: смерть на E0).
        # completed=False ставят ТОЛЬКО три точки прерывания ниже.
        completed = True

        # ── Пасс 1: парсинг всех строк без обращений к Redis ──
        prepared = []
        for row in reader:
            if is_shutdown_requested():
                logger.info("    [SHUTDOWN] Graceful shutdown — прерываем CSV-обработку")
                completed = False  # [8.15]
                break
            try:
                payload = build_payload(row, season, league_code, run_id=self.run_id)
            except Exception as e:
                self.errors += 1
                logger.error(f"Parse error: {e}")
                continue

            if payload is None:
                self.skipped += 1
                continue
            if not payload.get("full_time_result"):
                self.skipped += 1
                continue

            # Фильтр глубины (§23): прошедшие записываются, будущие отклоняются
            match_dt = None
            if history_days > 0:
                try:
                    match_dt = datetime.fromisoformat(payload["date_utc"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    self.skipped += 1
                    continue
                if match_dt >= now:
                    self.skipped_future += 1
                    continue
                if (now - match_dt).days > history_days:
                    self.skipped_past += 1
                    continue

            prepared.append((payload, match_dt))

        # ── Пасс 1.5: MGET-префетч дедупа чанками по 50 ключей ──
        from redis_hub import get_history_match, redis_command
        existing_map = {}
        cids = [p[0]["canonical_id"] for p in prepared]
        if not self.dry_run and cids:
            for i in range(0, len(cids), MGET_CHUNK_SIZE):
                chunk = cids[i:i + MGET_CHUNK_SIZE]
                keys = [f"{HISTORY_KEY_PREFIX}{c}" for c in chunk]
                res = redis_command(["MGET", *keys])
                self._manual_commands += 1
                if res is None or not isinstance(res, list) or len(res) != len(chunk):
                    # Деградация MGET: fallback на поштучный GET (как в v8.13)
                    logger.warning(f"    [DEDUP] MGET-чанк {i // MGET_CHUNK_SIZE} не доставлен — fallback на GET")
                    for c in chunk:
                        existing_map[c] = get_history_match(c)
                        self._manual_commands += 1
                else:
                    for c, raw in zip(chunk, res):
                        existing_map[c] = self._parse_existing(raw) if raw else None

        # ── Пасс 2: дедуп, force-merge, запись через хаб, индексы ──
        count = 0
        for payload, match_dt in prepared:
            if is_shutdown_requested():
                logger.info("    [SHUTDOWN] Graceful shutdown — прерываем запись")
                completed = False  # [8.15]
                break
            if limit and count >= limit:
                completed = False  # [8.15] файл записан не целиком — не помечать
                break
            if not self.dry_run and COMMAND_SOFT_LIMIT and self.commands_used >= COMMAND_SOFT_LIMIT:
                logger.warning("[STOP-LOSS §13.4] 90% дневного бюджета команд — финализация прогона")
                completed = False  # [8.15]
                break

            cid = payload["canonical_id"]

            # Дедуп: существование и protected-поля одной операцией (из MGET-префетча)
            existing = existing_map.get(cid) if not self.dry_run else None
            if isinstance(existing, dict):
                if file_done:
                    self.deduped += 1
                    continue
                self.collision_warnings += 1
                logger.warning(f"    [COLLISION] {cid}: запись вне processed_files — force-merge (§12.6.1)")
                ex_ids = existing.get("source_ids")
                if isinstance(ex_ids, dict) and ex_ids:
                    payload["source_ids"] = {**ex_ids, **(payload.get("source_ids") or {})}
                ex_h2h = existing.get("h2h")
                if isinstance(ex_h2h, dict) and ex_h2h:
                    payload["h2h"] = ex_h2h
                if existing.get("created_at"):
                    payload["created_at"] = existing["created_at"]

            result = None  # FIX v8.14: в dry-run result не определялся (латентный NameError)
            if not self.dry_run:
                try:
                    # §23/13-G2: запись через доменный шлюз; batch — пакетно; merge — до вызова
                    result = upsert_match(
                        payload["home_team"], payload["away_team"], payload["date_utc"],
                        competition=payload["competition"], country=payload["country"],
                        status=payload["status"], source=SOURCE_NAME,
                        mode="history", force=True, batch=self._batch,
                        **{k: v for k, v in payload.items()
                           if k not in ("canonical_id", "home_team", "away_team", "date_utc",
                                        "competition", "country", "status", "source")},
                    )
                    if not result:
                        self.errors += 1
                        continue
                except Exception as e:
                    self.errors += 1
                    logger.error(f"Write error {cid}: {e}")
                    continue

            self.written += 1
            count += 1
            self.matches_processed += 1

            # Индексы — Root-ключи через тот же batch (§13.1, R-5/R-7)
            score = "0"
            try:
                dt = match_dt or datetime.fromisoformat(payload["date_utc"].replace("Z", "+00:00"))
                score = str(dt.timestamp())
            except (ValueError, TypeError):
                pass
            cid_written = result.get("canonical_id") if isinstance(result, dict) else None
            cid_key = cid_written or cid  # canonical_id строит хаб (H-6)
            league_val = payload.get("league_code", "")
            if league_val:
                self._batch.add("ZADD", f"history:league:{league_val}", score, cid_key)
            if payload.get("home_clean"):
                self._batch.add("SADD", f"history:team:{payload['home_clean'].replace(' ', '_')}", cid_key)
            if payload.get("away_clean"):
                self._batch.add("SADD", f"history:team:{payload['away_clean'].replace(' ', '_')}", cid_key)
            day = payload["date_utc"][:10].replace("-", "")
            if day:
                self._batch.add("RPUSH", f"history:index:{day}", cid_key)

        return count, completed

    @staticmethod
    def _parse_existing(raw):
        """Разбор raw-значения из MGET: JSON с распаковкой конверта v700-prod (§13.1).
        Повреждённая запись считается отсутствующей -> путь force-merge (безопаснее скипа)."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            logger.warning("    [DEDUP] Повреждённый JSON в существующей записи — считаем отсутствующей")
            return None
        if isinstance(data, dict) and "payload" in data:
            data = data["payload"]
        return data if isinstance(data, dict) else None

    def save_meta(self, season, league_code, matches, errors, file_completed=True):
        """Плоская football_data:meta (12-10, §13.1) — flush после каждой лиги (crash-safe).
        [8.15] file_completed=False: файл не долит до конца (стоп-лосс/shutdown/limit) —
        помечается в pending_files, а не в processed_files (фикс дефекта #24)."""
        ts = now_iso()
        self._meta_fields.update({
            "last_run": ts,
            "version": VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "last_league": f"{season}_{league_code}",
            "error_count": self.errors,
            "total_events": self.matches_processed + self.deduped + self.skipped
                            + self.skipped_past + self.skipped_future,
            "stored_matches": self.matches_processed,
            "skipped": self.skipped,
            "skipped_past": self.skipped_past,
            "skipped_future": self.skipped_future,
            "deduped": self.deduped,
            "written": self.written,
            "commands_used": self.commands_used,
            "collision_warnings": self.collision_warnings,
            "alias_misses": self.alias_misses,
        })
        if season and league_code:
            file_key = f"{season}_{league_code}"
            if file_completed:
                pf = self._meta_fields.setdefault("processed_files", [])
                if file_key not in pf:
                    pf.append(file_key)
                if file_key in self.pending_files:
                    self.pending_files.remove(file_key)
            else:
                if file_key not in self.pending_files:
                    self.pending_files.append(file_key)
                self._meta_fields["status"] = "degraded"
            self._meta_fields["pending_files"] = self.pending_files
        # 13-G1: тихий пустой прогон фиксируется как degraded
        if (self.written == 0 and self.deduped == 0 and self.skipped == 0
                and self.skipped_past == 0 and self.skipped_future == 0 and self.errors == 0):
            self._meta_fields["status"] = "degraded"
        elif not self.pending_files:
            # [8.16] Статус всегда актуален: успешный прогон снимает carry-over
            # degraded из прошлой меты (иначе verify показал бы degraded и после
            # успешного восстановительного прогона).
            self._meta_fields["status"] = "completed"
        self._flush_meta()

    def _flush_meta(self):
        """Merge-перезапись Root-ключа football_data:meta (R-1: через redis_hub, не live-хэш)."""
        if self.dry_run or not self._meta_fields:
            return
        try:
            from redis_hub import redis_command
            existing = {}
            raw = redis_command(["GET", "football_data:meta"])
            self._manual_commands += 1
            if raw:
                try:
                    existing = json.loads(raw) if isinstance(raw, str) else {}
                except json.JSONDecodeError:
                    logger.warning("football_data:meta повреждён — перезаписывается новой мета")
                    existing = {}
            if not isinstance(existing, dict):
                existing = {}
            merged = {**existing, **self._meta_fields}
            merged["processed_files"] = sorted(set(
                (existing.get("processed_files") or [])
                + (self._meta_fields.get("processed_files") or [])
            ))
            # [8.15] pending_files: объединение с прошлой метой минус завершённые
            done = set(merged["processed_files"])
            merged["pending_files"] = sorted(
                (set(existing.get("pending_files") or [])
                 | set(self._meta_fields.get("pending_files") or [])) - done
            )
            res = redis_command(["SET", "football_data:meta", json.dumps(merged, ensure_ascii=False)])
            self._manual_commands += 1
            if res is None:
                logger.error("flush_meta: SET не доставлен")
        except Exception as e:
            logger.error(f"flush_meta failed: {e}")

    def flush_remaining(self):
        """Финальный flush PipelineBatch (§13.4)."""
        self._batch.flush()

# ============================================================================
# COLLECT_AND_PROCESS — единая точка входа для CI/CD
# ============================================================================

def collect_and_process(seasons: str = "", leagues: str = "",
                       limit: int = 0, history_days: int = 0,
                       dry_run: bool = False) -> dict:
    """
    Единая точка входа для CI/CD.
    Возвращает dict с метриками запуска.
    """
    # Parse seasons
    if seasons:
        season_list = [s.strip() for s in seasons.split(",") if s.strip()]
    else:
        now = datetime.now(timezone.utc)
        season_list = [f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"]

    # Parse leagues
    if leagues:
        league_list = [l.strip() for l in leagues.split(",") if l.strip()]
    else:
        league_list = list(LEAGUES.keys())

    # Инициализация хаба
    init_metrics = run_initialization()
    run_id = init_metrics.get("run_id", "manual")

    if not dry_run:
        if not init_metrics.get("redis_available", False) and not is_redis_available():
            return {"error": "Redis недоступен", "stored_matches": 0, "run_id": run_id}

    collector = FootballDataCollector(dry_run=dry_run, run_id=run_id)

    total_matches = 0
    total_errors = 0
    degraded = False  # [8.15]

    for season in season_list:
        for league_code in league_list:
            if is_shutdown_requested():
                break

            csv_content = collector.download_csv(season, league_code)
            if csv_content is None:
                total_errors += 1
                continue

            collector.archive_csv(season, league_code, csv_content)
            matches, file_completed = collector.process_csv(
                csv_content, season, league_code,
                limit=limit, history_days=history_days
            )
            total_matches += matches
            # v7.0: save_meta после каждой лиги (crash-safe)
            collector.save_meta(season, league_code, matches, collector.errors,
                                file_completed=file_completed)
            # [8.15] файл не долит до конца — эшелон прерывается (фикс #24)
            if not file_completed:
                logger.info("    [STOP-LOSS] Файл не завершён — прерываем эшелон")
                degraded = True
                break

        if is_shutdown_requested():
            break

    collector.flush_remaining()

    return {
        "stored_matches": total_matches,
        "total_errors": total_errors,
        "collector_errors": collector.errors,
        "skipped": collector.skipped,
        "skipped_past": collector.skipped_past,
        "skipped_future": collector.skipped_future,
        "written": collector.written,
        "deduped": collector.deduped,
        "collision_warnings": collector.collision_warnings,
        "commands_used": collector.commands_used,
        "processed_files": collector.processed_files,
        "pending_files": collector.pending_files,
        "skipped": collector.skipped,
        "status": "degraded" if degraded else "completed",
        "dry_run": dry_run,
        "run_id": run_id,
    }
# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description=f"Football-Data Collector v{VERSION}")
    parser.add_argument("--seasons", default="", help="Seasons comma-separated (e.g. 2425,2526)")
    parser.add_argument("--leagues", default="", help="League codes (empty = all 22)")
    parser.add_argument("--limit", type=int, default=0, help="Max matches per league (0 = all)")
    parser.add_argument("--history-days", type=int,
                        default=int(os.getenv("HISTORY_DAYS", "0")),
                        help="Только матчи в пределах N дней (0 = весь CSV; 12-11)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Redis")
    args = parser.parse_args()

    logger.info(f"=== Football-Data Collector v{VERSION} (schema {SCHEMA_VERSION}) ===")
    logger.info(f"  Dry run: {args.dry_run}")
    logger.info(f"  Seasons: {args.seasons or 'auto'}")
    logger.info(f"  Leagues: {args.leagues or 'all 22'}")
    logger.info(f"  Limit: {args.limit or 'none'}")
    logger.info(f"  History days: {args.history_days or 'all'}")
    logger.info(f"  Soft limit: {COMMAND_SOFT_LIMIT if COMMAND_SOFT_LIMIT else 'disabled (0)'} commands")
    logger.info("  Hub: strict mode (13-G1) — запись только через gatekeeper_hub")
    logger.info("  Registry: unified (team_registry, Strict Degradation §14.1)")

    # Инициализация хаба
    init_metrics = run_initialization()
    run_id = init_metrics.get("run_id", "manual")

    if not init_metrics.get("redis_available", False) and not args.dry_run:
        if not is_redis_available():
            logger.info("[FATAL] Redis недоступен. Проверьте SHARED_UPSTASH_REDIS_REST_URL/TOKEN.")
            sys.exit(1)
        logger.info("  Redis: OK (direct)")
    else:
        logger.info(f"  Redis: {'OK (hub)' if init_metrics.get('redis_available') else 'dry-run'}")
        logger.info(f"  Run ID: {run_id}")

    # Parse seasons
    if args.seasons:
        seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    else:
        now = datetime.now(timezone.utc)
        seasons = [f"{now.year % 100:02d}{(now.year + 1) % 100:02d}"]

    # Parse leagues
    if args.leagues:
        leagues = [l.strip() for l in args.leagues.split(",") if l.strip()]
    else:
        leagues = list(LEAGUES.keys())

    logger.info(f"  Seasons parsed: {seasons}")
    logger.info(f"  Leagues parsed: {leagues} ({len(leagues)})")

    collector = FootballDataCollector(dry_run=args.dry_run, run_id=run_id)

    total_matches = 0
    total_errors = 0

    for season in seasons:
        logger.info(f"\n--- Season {season} ---")
        for league_code in leagues:
            league_name = LEAGUES.get(league_code, {}).get("name", league_code)
            logger.info(f"  [{league_code}] {league_name}...")

            csv_content = collector.download_csv(season, league_code)
            if csv_content is None:
                total_errors += 1
                continue

            collector.archive_csv(season, league_code, csv_content)

            matches, file_completed = collector.process_csv(
                csv_content, season, league_code,
                limit=args.limit, history_days=args.history_days
            )
            total_matches += matches
            logger.info(f"    Matches: {matches}")

            # v7.0: save_meta после каждой лиги (crash-safe)
            collector.save_meta(season, league_code, matches, collector.errors,
                                file_completed=file_completed)
            # [8.15] файл не долит до конца — эшелон прерывается (фикс #24)
            if not file_completed:
                logger.info("    [STOP-LOSS] Файл не завершён — прерываем эшелон")
                break

        if is_shutdown_requested():
            logger.info("\n  [SHUTDOWN] Graceful shutdown — прерываем")
            break

    collector.flush_remaining()

    logger.info(f"  Total matches: {total_matches}")
    logger.info(f"  Total download errors: {total_errors}")
    logger.info(f"  Collector errors: {collector.errors}")
    logger.info(f"  Skipped (invalid/old): {collector.skipped}")
    logger.info(f"  Skipped past: {collector.skipped_past}, skipped future: {collector.skipped_future}")
    logger.info(f"  Written: {collector.written}, deduped: {collector.deduped}")
    logger.info(f"  Collision warnings: {collector.collision_warnings}")
    logger.info(f"  Commands used: {collector.commands_used}")
    if collector.pending_files:
        logger.warning(f"  Pending files (не долиты): {collector.pending_files}")
    if args.dry_run:
        logger.info(f"  (dry-run: nothing written to Redis)")

    return 0 if total_errors == 0 and collector.errors == 0 else 1


if __name__ == "__main__":
    # v8.17.1-obs: FIX-3 убрал basicConfig ради библиотечного импорта (правильно),
    # но script-режим остался с NullHandler — INFO-сводки (Season/Matches/итоги)
    # не печатались вовсе, dry-run и боевые прогоны немы в CI-логе.
    # Консольный хендлер ставится ТОЛЬКО при запуске как скрипта; библиотечное
    # поведение (импорт движком) не затронуто.
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sys.exit(main())
