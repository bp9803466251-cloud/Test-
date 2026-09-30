#!/usr/bin/env python3
"""
football_data_loader_v2.py — загрузка CSV в Redis в формате GatekeeperAI v5.0

26 полей payload, единый формат odds 1x2, TEAM_ALIASES, stats из 12 колонок,
source_map, flags, индексы history:team:* / history:league:* / history:index:*

Запуск:
  python football_data_loader_v2.py                    # загрузить все CSV из csv_data/
  python football_data_loader_v2.py --dir csv_data     # указать директорию
  python football_data_loader_v2.py --season 2526      # только один сезон
  python football_data_loader_v2.py --league E0         # только одну лигу
  python football_data_loader_v2.py --dry-run          # без записи в Redis
"""

import os
import sys
import csv
import json
import time
import glob
import traceback
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
MSK_TZ = timezone(timedelta(hours=3))

LEAGUE_MAP = {
    "E0":  ("Premier League",      "England"),
    "E1":  ("Championship",         "England"),
    "E2":  ("League 1",             "England"),
    "E3":  ("League 2",             "England"),
    "EC":  ("National League",      "England"),
    "SC0": ("Premiership",          "Scotland"),
    "SC1": ("Scottish Division 1",  "Scotland"),
    "SC2": ("Scottish Division 2",  "Scotland"),
    "SC3": ("Scottish Division 3",  "Scotland"),
    "SP1": ("La Liga",              "Spain"),
    "SP2": ("Segunda Division",     "Spain"),
    "I1":  ("Serie A",              "Italy"),
    "I2":  ("Serie B",              "Italy"),
    "D1":  ("Bundesliga 1",         "Germany"),
    "D2":  ("Bundesliga 2",         "Germany"),
    "F1":  ("Ligue 1",              "France"),
    "F2":  ("Ligue 2",              "France"),
    "N1":  ("Eredivisie",           "Netherlands"),
    "B1":  ("First Division A",     "Belgium"),
    "P1":  ("Primeira Liga",        "Portugal"),
    "T1":  ("Super Lig",            "Turkey"),
    "G1":  ("Super League",         "Greece"),
}

# ---------------------------------------------------------------------------
# TEAM_ALIASES — единый словарь (как в gatekeeper_hub.py)
# Ключ — вариант написания (lowercase), значение — canonical clean name
# ---------------------------------------------------------------------------
TEAM_ALIASES = {
    # England — Premier League
    "man united": "man", "manchester united": "man", "man utd": "man",
    "manchester utd": "man", "manchester": "man",
    "man city": "man_city", "manchester city": "man_city", "manchester c": "man_city",
    "tottenham": "tottenham", "spurs": "tottenham", "tottenham hotspur": "tottenham",
    "newcastle": "newcastle", "newcastle united": "newcastle",
    "west ham": "west_ham", "west ham united": "west_ham", "west ham utd": "west_ham",
    "wolves": "wolves", "wolverhampton": "wolves", "wolverhampton wanderers": "wolves",
    "brighton": "brighton", "brighton hove albion": "brighton",
    "nott'm forest": "nottingham_forest", "nottingham forest": "nottingham_forest",
    "nottingham": "nottingham_forest", "nottm forest": "nottingham_forest",
    "sheffield united": "sheffield_united", "sheffield utd": "sheffield_united",
    "sheffield wed": "sheffield_wed", "sheffield wednesday": "sheffield_wed",
    "qpr": "qpr", "queens park rangers": "qpr",
    "wigan": "wigan", "wigan athletic": "wigan",
    "blackburn": "blackburn", "blackburn rovers": "blackburn",
    "bolton": "bolton", "bolton wanderers": "bolton",
    "sunderland": "sunderland",
    "fulham": "fulham",
    "chelsea": "chelsea",
    "liverpool": "liverpool",
    "arsenal": "arsenal",
    "everton": "everton",
    "aston villa": "aston_villa", "villa": "aston_villa",
    "brentford": "brentford",
    "crystal palace": "crystal_palace", "c palace": "crystal_palace",
    "bournemouth": "bournemouth",
    "leicester": "leicester", "leicester city": "leicester",
    "ipswich": "ipswich", "ipswich town": "ipswich",
    "southampton": "southampton",
    "leeds": "leeds", "leeds united": "leeds",
    "norwich": "norwich", "norwich city": "norwich",
    "watford": "watford",
    "burnley": "burnley",
    "luton": "luton", "luton town": "luton",
    "hull": "hull", "hull city": "hull",
    "middlesbrough": "middlesbrough",
    "stoke": "stoke", "stoke city": "stoke",
    "west brom": "west_brom", "west bromwich": "west_brom",
    "west bromwich albion": "west_brom",
    "portsmouth": "portsmouth",
    "cardiff": "cardiff", "cardiff city": "cardiff",
    "swansea": "swansea", "swansea city": "swansea",
    "reading": "reading",
    "bristol city": "bristol_city",
    "birmingham": "birmingham", "birmingham city": "birmingham",
    "blackpool": "blackpool",
    "millwall": "millwall",
    "preston": "preston", "preston north end": "preston",
    "coventry": "coventry", "coventry city": "coventry",
    "huddersfield": "huddersfield", "huddersfield town": "huddersfield",
    "peterborough": "peterborough", "peterborough united": "peterborough",
    "derby": "derby", "derby county": "derby",
    "barnsley": "barnsley",
    "rotherham": "rotherham", "rotherham united": "rotherham",
    "wycombe": "wycombe", "wycombe wanderers": "wycombe",
    "fleetwood": "fleetwood", "fleetwood town": "fleetwood",
    "burton": "burton", "burton albion": "burton",
    "charlton": "charlton", "charlton athletic": "charlton",
    "doncaster": "doncaster", "doncaster rovers": "doncaster",
    "rochdale": "rochdale",
    "gillingham": "gillingham",
    "shrewsbury": "shrewsbury", "shrewsbury town": "shrewsbury",
    "wimbledon": "wimbledon", "afc wimbledon": "wimbledon",
    "oxford": "oxford", "oxford united": "oxford",
    "bristol rovers": "bristol_rovers",
    "plymouth": "plymouth", "plymouth argyle": "plymouth",
    "portsmouth": "portsmouth",
    "accrington": "accrington", "accrington stanley": "accrington",
    "lincoln": "lincoln", "lincoln city": "lincoln",
    "sutton": "sutton", "sutton united": "sutton",
    "morecambe": "morecambe",
    "cambridge": "cambridge", "cambridge united": "cambridge",
    "cheltenham": "cheltenham", "cheltenham town": "cheltenham",
    "crewe": "crewe", "crewe alexandra": "crewe",
    "walsall": "walsall",
    "barrow": "barrow",
    "stevenage": "stevenage",
    "mansfield": "mansfield", "mansfield town": "mansfield",
    "newport": "newport", "newport county": "newport",
    "tranmere": "tranmere", "tranmere rovers": "tranmere",
    "forest green": "forest_green", "forest green rovers": "forest_green",
    "grimsby": "grimsby", "grimsby town": "grimsby",
    "hartlepool": "hartlepool", "hartlepool united": "hartlepool",
    "colchester": "colchester", "colchester united": "colchester",
    "crawley": "crawley", "crawley town": "crawley",
    "harrogate": "harrogate", "harrogate town": "harrogate",
    "bradford": "bradford", "bradford city": "bradford",
    "scunthorpe": "scunthorpe", "scunthorpe united": "scunthorpe",
    "swindon": "swindon", "swindon town": "swindon",
    "northampton": "northampton", "northampton town": "northampton",
    "carlisle": "carlisle", "carlisle united": "carlisle",
    "exeter": "exeter", "exeter city": "exeter",
    "salford": "salford", "salford city": "salford",
    "leyton orient": "leyton_orient",
    "wigan": "wigan",

    # Scotland
    "celtic": "celtic",
    "rangers": "rangers",
    "aberdeen": "aberdeen",
    "hearts": "hearts", "heart of midlothian": "hearts",
    "hibernian": "hibernian", "hibs": "hibernian",
    "dundee united": "dundee_united",
    "dundee": "dundee",
    "dundee fc": "dundee",
    "st mirren": "st_mirren",
    "kilmarnock": "kilmarnock",
    "motherwell": "motherwell",
    "ross county": "ross_county",
    "st johnstone": "st_johnstone",
    "livingston": "livingston",
    "partick": "partick", "partick thistle": "partick",
    "greenock morton": "greenock_morton", "morton": "greenock_morton",
    "queen of south": "queen_of_south",
    "inverness": "inverness", "inverness ct": "inverness",
    "arbroath": "arbroath",
    "raith": "raith", "raith rovers": "raith",
    "ayr": "ayr", "ayr united": "ayr",
    "dunfermline": "dunfermline", "dunfermline athletic": "dunfermline",
    "falkirk": "falkirk",
    "alloa": "alloa",
    "caledonia": "caledonia",

    # Spain
    "barcelona": "barcelona",
    "real madrid": "real_madrid",
    "atletico madrid": "atletico_madrid", "atletico": "atletico_madrid",
    "athletic bilbao": "athletic_bilbao", "ath club": "athletic_bilbao",
    "athletic club": "athletic_bilbao",
    "sevilla": "sevilla",
    "valencia": "valencia",
    "villarreal": "villarreal",
    "real sociedad": "real_sociedad",
    "betis": "betis", "real betis": "betis",
    "celta": "celta", "celta vigo": "celta",
    "getafe": "getafe",
    "osasuna": "osasuna",
    "rayo vallecano": "rayo_vallecano", "rayo": "rayo_vallecano",
    "elche": "elche",
    "alaves": "alaves", "deportivo alaves": "alaves",
    "mallorca": "mallorca",
    "cadiz": "cadiz",
    "granada": "granada",
    "levante": "levante",
    "almeria": "almeria",
    "girona": "girona",
    "las palmas": "las_palmas",
    "espanyol": "espanyol", "r.c.d. espanyol": "espanyol",
    "leganes": "leganes",
    "valladolid": "valladolid", "real valladolid": "valladolid",
    "huesca": "huesca",
    "albacete": "albacete",
    "tenerife": "tenerife",
    "oviedo": "oviedo", "real oviedo": "oviedo",
    "gijon": "gijon", "sporting gijon": "gijon",
    "zaragoza": "zaragoza", "real zaragoza": "zaragoza",
    "elche": "elche",

    # Italy
    "inter": "inter", "inter milan": "inter", "internazionale": "inter",
    "milan": "milan", "ac milan": "milan",
    "juventus": "juventus", "juve": "juventus",
    "napoli": "napoli",
    "roma": "roma", "as roma": "roma",
    "lazio": "lazio",
    "atalanta": "atalanta",
    "fiorentina": "fiorentina",
    "torino": "torino",
    "bologna": "bologna",
    "sassuolo": "sassuolo",
    "udinese": "udinese",
    "genoa": "genoa",
    "cagliari": "cagliari",
    "verona": "verona", "hellas verona": "verona",
    "sampdoria": "sampdoria",
    "empoli": "empoli",
    "salernitana": "salernitana",
    "lecce": "lecce",
    "spezia": "spezia",
    "cremonese": "cremonese",
    "monza": "monza",
    "frosinone": "frosinone",
    "parma": "parma",
    "pisa": "pisa",
    "venezia": "venezia",
    "como": "como",
    "brescia": "brescia",
    "palerma": "palermo", "palermo": "palermo",
    "modena": "modena",
    "reggiana": "reggiana",
    "ternana": "ternana",
    "bari": "bari",
    "cosenza": "cosenza",
    "ascoli": "ascoli",
    "benevento": "benevento",
    "cittadella": "cittadella",
    "perugia": "perugia",
    "virtus entella": "virtus_entella",

    # Germany
    "bayern munich": "bayern_munich", "bayern": "bayern_munich",
    "bayern munchen": "bayern_munich",
    "dortmund": "dortmund", "borussia dortmund": "dortmund",
    "b. dortmund": "dortmund",
    "leverkusen": "leverkusen", "bayer leverkusen": "leverkusen",
    "rb leipzig": "rb_leipzig",
    "frankfurt": "frankfurt", "eintracht frankfurt": "frankfurt",
    "wolfsburg": "wolfsburg",
    "freiburg": "freiburg",
    "union berlin": "union_berlin",
    "mainz": "mainz", "mainz 05": "mainz",
    "monchengladbach": "monchengladbach", "b. monchengladbach": "monchengladbach",
    "borussia monchengladbach": "monchengladbach",
    "stuttgart": "stuttgart",
    "augsburg": "augsburg",
    "hoffenheim": "hoffenheim", "tsg hoffenheim": "hoffenheim",
    "werder bremen": "werder_bremen", "bremen": "werder_bremen",
    "bochum": "bochum", "vfl bochum": "bochum",
    "hertha": "hertha", "hertha bsc": "hertha",
    "schalke 04": "schalke", "schalke": "schalke",
    "koln": "koln", "fc koln": "koln",
    "hamburg": "hamburg", "hsv": "hamburg",
    "darmstadt": "darmstadt",
    "heidenheim": "heidenheim",
    "paderborn": "paderborn",
    "hannover": "hannover", "hannover 96": "hannover",
    "kaiserslautern": "kaiserslautern",
    "karlsruher": "karlsruher", "karlsruhe": "karlsruher",
    "greuther furth": "greuther_furth",
    "nurnberg": "nurnberg", "1. fc nurnberg": "nurnberg",
    "st. pauli": "st_pauli",
    "dusseldorf": "dusseldorf", "fortuna dusseldorf": "dusseldorf",
    "magdeburg": "magdeburg",
    "braunschweig": "braunschweig",
    "wehen": "wehen", "sv wehen": "wehen",
    "osnabruck": "osnabruck",
    "hansa rostock": "rostock", "rostock": "rostock",

    # France
    "paris saint germain": "psg", "psg": "psg", "paris sg": "psg",
    "paris saint-germain": "psg",
    "marseille": "marseille",
    "lyon": "lyon", "olympique lyonnais": "lyon",
    "monaco": "monaco",
    "lille": "lille",
    "rennes": "rennes",
    "nice": "nice",
    "lens": "lens",
    "montpellier": "montpellier",
    "nantes": "nantes",
    "reims": "reims",
    "strasbourg": "strasbourg",
    "toulouse": "toulouse",
    "brest": "brest",
    "metz": "metz",
    "lorient": "lorient",
    "clermont": "clermont",
    "le havre": "le_havre",
    "angers": "angers",
    "bordeaux": "bordeaux",
    "saint-etienne": "saint_etienne", "saint etienne": "saint_etienne",
    "auxerre": "auxerre",
    "troyes": "troyes",
    "dijon": "dijon",
    "amiens": "amiens",
    "caen": "caen",
    "grenoble": "grenoble",
    "sochaux": "sochaux",
    "paris fc": "paris_fc",
    "guingamp": "guingamp",
    "valenciennes": "valenciennes",
    "annecy": "annecy",
    "niort": "niort",
    "rodez": "rodez",
    "quevilly": "quevilly",
    "le mans": "le_mans",

    # Netherlands
    "ajax": "ajax",
    "psv": "psv", "psv eindhoven": "psv",
    "feyenoord": "feyenoord",
    "az alkmaar": "az_alkmaar", "az": "az_alkmaar",
    "twente": "twente",
    "utrecht": "utrecht",
    "vitesse": "vitesse",
    "heerenveen": "heerenveen",
    "sparta rotterdam": "sparta_rotterdam", "sparta": "sparta_rotterdam",
    "groningen": "groningen",
    "zwolle": "zwolle", "pec zwolle": "zwolle",
    "heracles": "heracles",
    "emmen": "emmen",
    "fortuna sittard": "fortuna_sittard",
    "waalwijk": "waalwijk", "rkc waalwijk": "waalwijk",
    "go ahead eagles": "go_ahead_eagles", "go ahead": "go_ahead_eagles",
    "campen": "campen",
    "almere": "almere", "almere city": "almere",
    "excelsior": "excelsior",
    "nec": "nec", "nec nijmegen": "nec",
    "willem ii": "willem_ii",
    " Den Haag": "den_haag", "ado den haag": "den_haag",

    # Belgium
    "club brugge": "club_brugge",
    "anderlecht": "anderlecht",
    "genk": "genk", "krc genk": "genk",
    "antwerp": "antwerp",
    "union sg": "union_sg", "saint-gilloise": "union_sg",
    "st. liege": "st_liege", "standard liege": "st_liege",
    "gent": "gent",
    "mechelen": "mechelen", "kv mechelen": "mechelen",
    "kortrijk": "kortrijk",
    "charleroi": "charleroi",
    "cercle brugge": "cercle_brugge",
    "oh leuven": "oh_leuven",
    "eupen": "eupen",
    "stvv": "stvv", "sint-truiden": "stvv",
    "westerlo": "westerlo",
    "RFC": "rfc_seraing", "seraing": "rfc_seraing",
    "beerschot": "beerschot",
    "lommel": "lommel",

    # Portugal
    "benfica": "benfica",
    "porto": "porto",
    "sporting": "sporting", "sporting cp": "sporting",
    "braga": "braga",
    "vitoria": "vitoria", "vitoria sc": "vitoria",
    "guimaraes": "vitoria",
    "famalicao": "famalicao",
    "portimonense": "portimonense",
    "moreirense": "moreirense",
    "rio ave": "rio_ave",
    "gil vicente": "gil_vicente",
    "estoril": "estoril",
    "boavista": "boavista",
    "maritimo": "maritimo",
    "pacos ferreira": "pacos_ferreira",
    "belenenses": "belenenses",
    "vizela": "vizela",
    "casa pia": "casa_pia",
    "estrela": "estrela", "estrela amadora": "estrela",
    "aveda": "aveda",
    "tondela": "tondela",
    "mafra": "mafra",
    "nacional": "nacional",

    # Turkey
    "galatasaray": "galatasaray",
    "fenerbahce": "fenerbahce",
    "besiktas": "besiktas",
    "trabzonspor": "trabzonspor",
    "basaksehir": "basaksehir",
    "adana demir": "adana_demir",
    "antalyaspor": "antalyaspor",
    "konyaspor": "konyaspor",
    "kasimpasa": "kasimpasa",
    "alanyaspor": "alanyaspor",
    "gaziantep": "gaziantep", "gaziantep fk": "gaziantep",
    "rizespor": "rizespor",
    "sivasspor": "sivasspor",
    "kayserispor": "kayserispor",
    "ankaragucu": "ankaragucu",
    "fatih karagumruk": "karagumruk", "karagumruk": "karagumruk",
    "hatayspor": "hatayspor",
    "giresunspor": "giresunspor",
    "malatyaspor": "malatyaspor",
    "goztepe": "goztepe",
    "altay": "altay",
    "yeni malatyaspor": "malatyaspor",
    "umbaniye": "umbaniye", "umlaniyespor": "umbaniye",

    # Greece
    "olympiacos": "olympiacos",
    "panathinaikos": "panathinaikos",
    "paok": "paok",
    "aek": "aek", "aek athens": "aek",
    "aris": "aris", "aris thessaloniki": "aris",
    "volos": "volos", "volos npf": "volos",
    "asteras": "asteras", "asteras tripolis": "asteras",
    "ofi": "ofi", "ofi crete": "ofi",
    "atromitos": "atromitos",
    "lamia": "lamia",
    "ioannina": "ioannina", "pas giannina": "ioannina",
    "apollon": "apollon", "apollon smyrnis": "apollon",
    "panaitolikos": "panaitolikos",
    "levadiakos": "levadiakos",
    "xanthi": "xanthi",
}

UPSTREAM_MAP = {
    "sharpapi": "betradar",
    "odds_api": "betradar",
    "bzzoiro": "opta",
    "propline": "pinnacle",
    "football_data": "bet365",
}

# Odds priority: B365 → BbAv → IW → LB → WH → VC
ODDS_PRIORITY = [
    ("B365H", "B365D", "B365A", "bet365"),
    ("BbAvH", "BbAvD", "BbAvA", "betbrain_avg"),
    ("IWH", "IWD", "IWA", "interwetten"),
    ("LBH", "LBD", "LBA", "ladbrokes"),
    ("WHH", "WHD", "WHA", "william_hill"),
    ("VCH", "VCD", "VCA", "vc_bet"),
]

# Opening odds priority (same sources, but "opening" columns if available)
ODDS_OPENING_PRIORITY = [
    ("Bb1X2", None, None),  # not standard, skip
]


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def now_msk():
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def _p(msg, end="\n"):
    print(msg, flush=True, end=end)


def clean_team_name(name):
    """Нормализация имени команды через TEAM_ALIASES."""
    if not name:
        return ""
    name_lower = name.strip().lower()
    # Прямой lookup в алиасах
    if name_lower in TEAM_ALIASES:
        return TEAM_ALIASES[name_lower]
    # Fallback: убрать артикли, привести к нижнему регистру, заменить пробелы на _
    cleaned = name_lower
    for prefix in ["fc ", "afc ", "ssc ", "ss ", "sc ", "us ", "as ", "ac ",
                   "vfl ", "vfb ", "tsg ", "sv ", "rb ", "1. ", "1fc ", "b. "]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    cleaned = cleaned.strip()
    cleaned = cleaned.replace(" ", "_").replace("-", "_").replace(".", "")
    cleaned = cleaned.replace("__", "_").strip("_")
    return cleaned


def parse_csv_filename(filepath):
    """Парсит путь к CSV. Поддерживает два формата:
    - csv_data/2526/E0.csv  → season='2526', league_code='E0'  (из директории + файла)
    - 2526_E0.csv           → season='2526', league_code='E0'  (из имени файла)
    """
    basename = os.path.basename(filepath).replace(".csv", "")  # E0 or 2526_E0
    dirname = os.path.basename(os.path.dirname(filepath))      # 2526 or ""

    # Формат csv_data/2526/E0.csv: season из директории, league из файла
    if dirname and len(dirname) == 4 and dirname.isdigit():
        # basename is just the league code (e.g. "E0")
        # But could also be "2526_E0" — check
        if "_" in basename:
            parts = basename.split("_")
            if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isdigit():
                return parts[0], parts[1]
        return dirname, basename

    # Формат 2526_E0.csv: split по _
    parts = basename.split("_")
    if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isdigit():
        return parts[0], parts[1]

    # fallback
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def normalize_date(date_str):
    """DD/MM/YYYY → ISO 8601 UTC (00:00:00Z — время неизвестно для history)."""
    if not date_str:
        return None
    date_str = date_str.strip()
    # Форматы: DD/MM/YYYY или DD/MM/YY
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            continue
    # Формат ISO уже
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT00:00:00Z")
    except Exception:
        return None


def date_to_yyyymmdd(iso_date):
    """2024-12-04T00:00:00Z → 20241204"""
    if not iso_date or len(iso_date) < 10:
        return None
    return iso_date[:10].replace("-", "")


def parse_match_stats(row, ts):
    """Парсит 12 метрик статистики матча из CSV колонок."""
    stats = {}

    def _safe_int(val):
        if val is None or val == "" or val == "-":
            return None
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    sh = _safe_int(row.get("HS"))
    sa = _safe_int(row.get("AS"))
    if sh is not None:
        stats["shots_home"] = sh
    if sa is not None:
        stats["shots_away"] = sa

    hst = _safe_int(row.get("HST"))
    ast = _safe_int(row.get("AST"))
    if hst is not None:
        stats["shots_on_target_home"] = hst
    if ast is not None:
        stats["shots_on_target_away"] = ast

    hc = _safe_int(row.get("HC"))
    ac = _safe_int(row.get("AC"))
    if hc is not None:
        stats["corners_home"] = hc
    if ac is not None:
        stats["corners_away"] = ac

    hf = _safe_int(row.get("HF"))
    af = _safe_int(row.get("AF"))
    if hf is not None:
        stats["fouls_home"] = hf
    if af is not None:
        stats["fouls_away"] = af

    hy = _safe_int(row.get("HY"))
    ay = _safe_int(row.get("AY"))
    if hy is not None:
        stats["yellow_home"] = hy
    if ay is not None:
        stats["yellow_away"] = ay

    hr = _safe_int(row.get("HR"))
    ar = _safe_int(row.get("AR"))
    if hr is not None:
        stats["red_home"] = hr
    if ar is not None:
        stats["red_away"] = ar

    if stats:
        stats["_source"] = "football_data"
        stats["_updated_at"] = ts
    return stats if stats else {}


def parse_odds(row, ts):
    """Парсит коэффициенты в формате 1x2 с приоритетом источников."""
    current = {}
    opening = {}
    source_name = None
    upstream = "bet365"

    # Current odds — по приоритету
    for h_col, d_col, a_col, bk_name in ODDS_PRIORITY:
        h = row.get(h_col, "").strip() if row.get(h_col) else ""
        d = row.get(d_col, "").strip() if row.get(d_col) else ""
        a = row.get(a_col, "").strip() if row.get(a_col) else ""
        if h and d and a and h != "-" and d != "-" and a != "-":
            try:
                float(h)
                float(d)
                float(a)
                current = {"home": h, "draw": d, "away": a}
                source_name = bk_name
                break
            except ValueError:
                continue

    # Opening odds — Bb1X2 или те же B365 (если нет отдельного opening)
    # football-data.co.uk не имеет отдельных opening колонок для 1x2
    # Используем те же коэффициенты как opening (conservative)
    if current:
        opening = dict(current)

    if not current:
        return None

    # Best — максимум по каждому исходу
    best = {}
    all_odds = {}
    for h_col, d_col, a_col, bk_name in ODDS_PRIORITY:
        h = row.get(h_col, "").strip() if row.get(h_col) else ""
        d = row.get(d_col, "").strip() if row.get(d_col) else ""
        a = row.get(a_col, "").strip() if row.get(a_col) else ""
        if h and d and a and h != "-" and d != "-" and a != "-":
            try:
                fh, fd, fa = float(h), float(d), float(a)
                all_odds.setdefault("home", []).append(fh)
                all_odds.setdefault("draw", []).append(fd)
                all_odds.setdefault("away", []).append(fa)
            except ValueError:
                continue

    if all_odds:
        best = {
            "home": str(max(all_odds["home"])) if "home" in all_odds else current.get("home", ""),
            "draw": str(max(all_odds["draw"])) if "draw" in all_odds else current.get("draw", ""),
            "away": str(max(all_odds["away"])) if "away" in all_odds else current.get("away", ""),
        }
    else:
        best = dict(current)

    source_entry = {
        "source": "football_data",
        "upstream": upstream,
        "price": current,
        "timestamp": ts,
        "type": "closing",
    }

    return {
        "1x2": {
            "current": current,
            "opening": opening,
            "best": best,
            "sources": [source_entry],
        }
    }


def compute_flags(score, stats):
    """Вычисляет флаги: extreme_result, abnormal_score, red_card_driven.
    По гайду v5.0 раздел 22 + 39.2.
    """
    flags = {}
    if not score:
        return flags

    h = score.get("home", 0) or 0
    a = score.get("away", 0) or 0
    total = h + a
    diff = abs(h - a)

    # extreme_result: разница в 3+ голов ИЛИ 5+ всего
    if diff >= 3 or total >= 5:
        flags["extreme_result"] = True

    # abnormal_score: разница в 4+ голов (сверхэкстремальный результат)
    if diff >= 4:
        flags["abnormal_score"] = True

    # red_card_driven: красная карточка в матче
    if stats:
        if stats.get("red_home", 0) and stats["red_home"] > 0:
            flags["red_card_driven"] = True
        elif stats.get("red_away", 0) and stats["red_away"] > 0:
            flags["red_card_driven"] = True

    return flags


def build_payload(row, season, league_code, ts):
    """Собирает 26-полейный payload v5.0 из строки CSV."""
    home_team = (row.get("HomeTeam") or "").strip()
    away_team = (row.get("AwayTeam") or "").strip()

    home_clean = clean_team_name(home_team)
    away_clean = clean_team_name(away_team)

    date_str = (row.get("Date") or "").strip()
    date_iso = normalize_date(date_str)
    if not date_iso:
        return None, None, None

    date_yyyymmdd = date_to_yyyymmdd(date_iso)
    if not date_yyyymmdd:
        return None, None, None

    canonical_id = f"{home_clean}__{away_clean}__{date_yyyymmdd}"

    # Score
    try:
        score_home = int(float(row.get("FTHG", 0) or 0))
    except (ValueError, TypeError):
        score_home = 0
    try:
        score_away = int(float(row.get("FTAG", 0) or 0))
    except (ValueError, TypeError):
        score_away = 0
    score = {"home": score_home, "away": score_away}

    # Half-time score
    half_time = {}
    try:
        hthg = int(float(row.get("HTHG", 0) or 0))
        htag = int(float(row.get("HTAG", 0) or 0))
        half_time = {"home": hthg, "away": htag}
    except (ValueError, TypeError):
        pass

    # Full-time result
    ftr = (row.get("FTR") or "").strip()
    full_time_result = ftr if ftr in ("H", "A", "D") else None

    # Referee
    referee = (row.get("Referee") or "").strip() or None

    # Competition / country
    comp_info = LEAGUE_MAP.get(league_code, (league_code, None))
    competition = comp_info[0]
    country = comp_info[1]

    # Stats
    stats = parse_match_stats(row, ts)

    # Odds
    odds = parse_odds(row, ts)

    # Flags
    flags = compute_flags(score, stats)

    # Source map
    source_map = {
        "odds": {
            "source": "football_data",
            "upstream": "bet365",
            "timestamp": ts,
            "independent": True,
            "type": "closing",
        },
        "stats": {
            "source": "football_data",
            "upstream": "match_data",
            "timestamp": ts,
            "independent": True,
        },
    }

    # 26-field payload
    payload = {
        "canonical_id": canonical_id,
        "home_team": home_team,
        "away_team": away_team,
        "home_clean": home_clean,
        "away_clean": away_clean,
        "competition": competition,
        "country": country,
        "date_utc": date_iso,
        "status": "completed",
        "score": score,
        "version": 1,
        "schema_version": "v700",
        "season": season,
        "league_code": league_code,
        "odds": odds if odds else {
            "1x2": {"current": {}, "opening": {}, "best": {}, "sources": []}
        },
        "predictions": {},
        "stats": stats,
        "h2h": {},
        "form": None,
        "ratings": None,
        "context": None,
        "source_map": source_map,
        "source_ids": {"football_data": league_code},
        "sources": ["football_data"],
        "section_history": [],
        "flags": flags,
        "created_at": ts,
        "updated_at": ts,
    }

    # Дополнительные поля (не входят в 26, но добавляются при наличии)
    if half_time:
        payload["half_time_score"] = half_time
    if full_time_result:
        payload["full_time_result"] = full_time_result
    if referee:
        payload["referee"] = referee

    # Wrapper (конверт)
    wrapper = {
        "version": "v700-prod",
        "sender_repo": "football_data_loader",
        "timestamp": ts,
        "payload": payload,
    }

    return canonical_id, wrapper, date_yyyymmdd


# ---------------------------------------------------------------------------
# Redis interaction
# Primary: redis_hub (local/Termux)
# Fallback: Upstash REST API (GitHub Actions)
# ---------------------------------------------------------------------------
_REDIS_MODE = None  # "redis_hub" | "upstash" | None
_REDIS_URL = None
_REDIS_TOKEN = None

import urllib.request
import urllib.error


def _init_redis():
    """Инициализация Redis: redis_hub или Upstash REST API."""
    global _REDIS_MODE, _REDIS_URL, _REDIS_TOKEN

    # Попытка 1: redis_hub (локально в Termux)
    try:
        import redis_hub
        # Проверяем что функция доступна
        _ = redis_hub._execute_upstash_cmd
        _REDIS_MODE = "redis_hub"
        _p("[LOADER] Redis backend: redis_hub (local)")
        return True
    except Exception:
        pass

    # Попытка 2: Upstash REST API через env vars (GitHub Actions)
    _REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[LOADER] Redis backend: Upstash REST API")
        return True

    # Попытка 3: shared Upstash
    _REDIS_URL = os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", "")
    _REDIS_TOKEN = os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN", "")

    if _REDIS_URL and _REDIS_TOKEN:
        _REDIS_MODE = "upstash"
        _p("[LOADER] Redis backend: Shared Upstash REST API")
        return True

    _p("[WARN] No Redis backend available (redis_hub nor Upstash env vars)")
    _REDIS_MODE = None
    return False


def _exec_upstash_rest(cmd):
    """Выполняет команду через Upstash REST API."""
    # Upstash REST: POST {url}/{command} with Bearer token
    # cmd = ["HMSET", "GatekeeperAI", "key1", "val1", ...]
    if not cmd:
        return None

    command = cmd[0].lower()
    args = cmd[1:]

    # URL: https://xxx.upstash.io/hmset/ (pipe-separated)
    url = f"{_REDIS_URL.rstrip('/')}/{command}"
    if args:
        # URL-encode and join with /
        encoded_args = []
        for a in args:
            a_str = str(a)
            # Upstash expects URL-encoded values
            from urllib.parse import quote
            encoded_args.append(quote(a_str, safe=""))
        url += "/" + "/".join(encoded_args)

    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", f"Bearer {_REDIS_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"result": "OK"}
    except urllib.error.HTTPError as e:
        _p(f"[ERROR] Upstash REST HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        return None
    except Exception as e:
        _p(f"[ERROR] Upstash REST: {e}")
        return None


def _exec(cmd):
    """Выполняет Redis команду через доступный backend."""
    if _REDIS_MODE == "redis_hub":
        try:
            import redis_hub
            return redis_hub._execute_upstash_cmd(cmd)
        except Exception as e:
            _p(f"[ERROR] redis_hub._execute_upstash_cmd({cmd[0]}) failed: {e}")
            return None
    elif _REDIS_MODE == "upstash":
        return _exec_upstash_rest(cmd)
    else:
        _p(f"[ERROR] No Redis backend for command: {cmd[0]}")
        return None


def save_batch_to_redis(batch):
    """Записывает батч матчей через HMSET (по 10)."""
    if not batch:
        return 0
    # batch = list of (history_key, json_wrapper)
    hset_args = []
    for history_key, json_wrapper in batch:
        hset_args.append(history_key)
        hset_args.append(json.dumps(json_wrapper, ensure_ascii=False))

    result = _exec(["HMSET", "GatekeeperAI"] + hset_args)
    return len(batch) if result is not None else 0


def save_index(index_key, cid_list):
    """Сохраняет индексный ключ."""
    if not cid_list:
        return
    # Сохраняем как JSON-список
    wrapper = {
        "version": "v700-prod",
        "sender_repo": "football_data_loader",
        "timestamp": now_msk(),
        "payload": cid_list,
    }
    _exec(["HSET", "GatekeeperAI", index_key, json.dumps(wrapper, ensure_ascii=False)])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_csv(filepath, dry_run=False):
    """Обрабатывает один CSV файл. Возвращает статистику."""
    season, league_code = parse_csv_filename(filepath)
    if not season or not league_code:
        _p(f"  [SKIP] Cannot parse filename: {filepath}")
        return {"total": 0, "loaded": 0, "errors": 0}

    _p(f"\n[CSV] {os.path.basename(filepath)} — season={season}, league={league_code}")

    stats_summary = {"total": 0, "loaded": 0, "errors": 0, "skipped": 0}
    batch = []
    batch_size = 10

    # Индексы для этого файла
    team_index = {}  # team_clean → [cid, ...]
    league_index = []  # [cid, ...]
    date_index = {}  # yyyymmdd → [cid, ...]

    try:
        with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats_summary["total"] += 1
                ts = now_msk()

                try:
                    cid, wrapper, date_yyyymmdd = build_payload(row, season, league_code, ts)
                    if not cid:
                        stats_summary["errors"] += 1
                        continue
                except Exception as e:
                    stats_summary["errors"] += 1
                    if stats_summary["errors"] <= 3:
                        _p(f"  [ERROR] Row {stats_summary['total']}: {e}")
                    continue

                history_key = f"history:match:{cid}"
                json_wrapper = json.dumps(wrapper, ensure_ascii=False)

                # Накапливаем индексы
                payload = wrapper["payload"]
                home_clean = payload["home_clean"]
                away_clean = payload["away_clean"]

                team_index.setdefault(home_clean, []).append(cid)
                team_index.setdefault(away_clean, []).append(cid)
                league_index.append(cid)
                date_index.setdefault(date_yyyymmdd, []).append(cid)

                if not dry_run:
                    batch.append((history_key, json_wrapper))
                    if len(batch) >= batch_size:
                        saved = save_batch_to_redis(batch)
                        stats_summary["loaded"] += saved
                        _p(f"  [BATCH] Loaded {stats_summary['loaded']}/{stats_summary['total']}...", end="\r")
                        batch = []
                        time.sleep(0.2)
                else:
                    stats_summary["loaded"] += 1

                # Прогресс
                if stats_summary["total"] % 100 == 0:
                    _p(f"  [PROG] Parsed {stats_summary['total']} rows, loaded {stats_summary['loaded']}...")

        # Остаток батча
        if batch and not dry_run:
            saved = save_batch_to_redis(batch)
            stats_summary["loaded"] += saved

        # Сохраняем индексы
        if not dry_run and league_index:
            _p(f"  [INDEX] Creating indexes for {len(league_index)} matches...")

            # League index
            league_key = f"history:league:{league_code}"
            save_index(league_key, league_index)
            time.sleep(0.1)

            # Team indexes (батч по 10)
            team_items = list(team_index.items())
            for i in range(0, len(team_items), 10):
                batch_team = team_items[i:i+10]
                for team, cids in batch_team:
                    team_key = f"history:team:{team}"
                    save_index(team_key, cids)
                time.sleep(0.1)

            # Date indexes
            for date_str, cids in date_index.items():
                date_key = f"history:index:{date_str}"
                save_index(date_key, cids)
                time.sleep(0.05)

        _p(f"  [DONE] {os.path.basename(filepath)}: total={stats_summary['total']}, "
            f"loaded={stats_summary['loaded']}, errors={stats_summary['errors']}")

    except FileNotFoundError:
        _p(f"  [ERROR] File not found: {filepath}")
        stats_summary["errors"] = 1
    except Exception as e:
        _p(f"  [ERROR] {filepath}: {e}")
        traceback.print_exc()
        stats_summary["errors"] += 1

    return stats_summary


def main():
    _p("=" * 60)
    _p("  football_data_loader_v2.py — v5.0 / 26-field payload")
    _p(f"  Time: {now_msk()}")
    _p("=" * 60)

    # Parse args
    base_dir = "csv_data"
    filter_season = None
    filter_league = None
    dry_run = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--dir" and i + 1 < len(args):
            base_dir = args[i + 1]
            i += 2
        elif args[i] == "--season" and i + 1 < len(args):
            filter_season = args[i + 1]
            i += 2
        elif args[i] == "--league" and i + 1 < len(args):
            filter_league = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    # Find CSV files
    pattern = os.path.join(base_dir, "**", "*.csv")
    all_csvs = sorted(glob.glob(pattern, recursive=True))

    if not all_csvs:
        _p(f"[FATAL] No CSV files found in {base_dir}/")
        _p("  Run football_data_downloader.py first to download CSV files.")
        sys.exit(1)

    # Filter
    if filter_season or filter_league:
        filtered = []
        for f in all_csvs:
            season, league = parse_csv_filename(f)
            if filter_season and season != filter_season:
                continue
            if filter_league and league != filter_league:
                continue
            filtered.append(f)
        all_csvs = filtered

    _p(f"\n[INFO] Found {len(all_csvs)} CSV files")
    if dry_run:
        _p("[INFO] DRY RUN — no Redis writes")
    else:
        if not _init_redis():
            _p("[FATAL] Cannot connect to Redis. Use --dry-run to test without Redis.")
            sys.exit(1)
    _p(f"[INFO] TEAM_ALIASES: {len(TEAM_ALIASES)} entries")
    _p(f"[INFO] LEAGUE_MAP: {len(LEAGUE_MAP)} leagues")

    # Process
    grand_total = {"total": 0, "loaded": 0, "errors": 0, "files": 0}

    for filepath in all_csvs:
        s = process_csv(filepath, dry_run=dry_run)
        for k in ("total", "loaded", "errors"):
            grand_total[k] += s.get(k, 0)
        grand_total["files"] += 1
        time.sleep(0.3)

    # Summary
    _p("\n" + "=" * 60)
    _p("  SUMMARY")
    _p("=" * 60)
    _p(f"  Files processed: {grand_total['files']}")
    _p(f"  Total rows:      {grand_total['total']}")
    _p(f"  Loaded to Redis: {grand_total['loaded']}")
    _p(f"  Errors:          {grand_total['errors']}")
    if dry_run:
        _p("  (DRY RUN — nothing was written to Redis)")
    _p("=" * 60)


if __name__ == "__main__":
    main()
