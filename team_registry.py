"""

__version__ = "2.1"
__all__ = ["normalize_team_name", "build_canonical_id"]
Team Registry — нормализация имён команд (§19.2)
================================================
Устраняет циклическую зависимость gatekeeper_hub.py ↔ team_registry.

Использование:
    from team_registry import normalize_team_name
    clean = normalize_team_name("Man Utd")  # → "manchester united"
"""


# ── Diacritic normalization (umlauts, accents) ────────────
def _normalize_diacritics(text: str) -> str:
    """Converts diacritics to ASCII: ä→ae, ö→oe, ü→ue, ß→ss, à→a, é→e, etc."""
    if not text:
        return ""
    # German umlauts
    text = text.replace("\u00e4", "ae").replace("\u00f6", "oe").replace("\u00fc", "ue")
    text = text.replace("\u00c4", "Ae").replace("\u00d6", "Oe").replace("\u00dc", "Ue")
    text = text.replace("\u00df", "ss")
    # French/Portuguese/Spanish accents
    text = text.replace("\u00e0", "a").replace("\u00e1", "a").replace("\u00e2", "a").replace("\u00e3", "a")
    text = text.replace("\u00e8", "e").replace("\u00e9", "e").replace("\u00ea", "e").replace("\u00eb", "e")
    text = text.replace("\u00ec", "i").replace("\u00ed", "i").replace("\u00ee", "i")
    text = text.replace("\u00f2", "o").replace("\u00f3", "o").replace("\u00f4", "o").replace("\u00f5", "o")
    text = text.replace("\u00f8", "o")
    text = text.replace("\u00f9", "u").replace("\u00fa", "u").replace("\u00fb", "u")
    text = text.replace("\u00e7", "c")
    text = text.replace("\u00f1", "n")
    return text

# ── Prefix/suffix stripping ───────────────────────────────
_PREFIXES = ["fc ", "cf ", "afc ", "sc ", "ac ", "as ", "ss ", "vfl ", "vfb ", "tsg ", "1. fc ", "1 fc ", "the "]
_SUFFIXES = [" fc", " cf", " afc", " sc", " ac", " as", " ss", " vfl", " vfb", " tsg"]

def _strip_prefix(text: str) -> str:
    for p in _PREFIXES:
        if text.startswith(p):
            return text[len(p):]
    return text

def _strip_suffix(text: str) -> str:
    for s in _SUFFIXES:
        if text.endswith(s):
            return text[:-len(s)]
    return text

# ── Реестр нормализации: alias → canonical (lowercase) ──────────────
_TEAM_ALIASES = {
    # ── Premier League ──────────────────────────────────────────────
    "man utd": "manchester united",
    "chelsea": "chelsea",
    "arsenal": "arsenal",
    "barcelona": "fc barcelona",
    "real madrid": "real madrid",
    "sevilla": "sevilla",
    "valencia": "valencia",
    "villarreal": "villarreal",
    "liverpool": "liverpool",
    "everton": "everton",
    "fulham": "fulham",
    "burnley": "burnley",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "newcastle": "newcastle united",
    "newcastle utd": "newcastle united",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "brighton": "brighton hove albion",
    "brighton hove": "brighton hove albion",
    "west ham": "west ham united",
    "west ham utd": "west ham united",
    "nottm forest": "nottingham forest",
    "nottingham": "nottingham forest",

    # ── La Liga ─────────────────────────────────────────────────────
    "atletico": "atletico madrid",
    "atletico mad": "atletico madrid",
    "athletic bilbao": "athletic club",
    "athletic": "athletic club",
    "celta": "celta vigo",
    "celta v": "celta vigo",
    "rayo": "rayo vallecano",
    "rayo v": "rayo vallecano",
    "real soc": "real sociedad",
    "real betis": "real betis balompie",
    "betis": "real betis balompie",

    # ── Serie A ──────────────────────────────────────────────────────
    "inter": "inter milan",
    "internazionale": "inter milan",
    "milan": "ac milan",
    "ac": "ac milan",
    "napoli": "ssc napoli",
    "roma": "as roma",
    "lazio": "ss lazio",
    "atalanta": "atalanta bergamo",
    "fiorentina": "acf fiorentina",
    "juventus": "juventus turin",
    "juve": "juventus turin",
    "torino": "torino fc",
    "verona": "hellas verona",
    "hellas": "hellas verona",
    "bologna": "bologna fc",
    "cagliari": "cagliari calcio",
    "genoa": "genoa cfc",
    "lecce": "us lecce",
    "monza": "ac monza",
    "udinese": "udinese calcio",
    "sassuolo": "us sassuolo",
    "empoli": "empoli fc",
    "frosinone": "frosinone calcio",
    "salernitana": "us salernitana",
    "cremonese": "us cremonese",
    "spezia": "spezia calcio",

    # ── Bundesliga ──────────────────────────────────────────────────
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "bayern muenchen": "bayern munich",
    "muenchen": "bayern munich",
    "bavaria": "bayern munich",
    "dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "bayer": "bayer leverkusen",
    "rb leipzig": "rb leipzig",
    "leipzig": "rb leipzig",
    "frankfurt": "eintracht frankfurt",
    "eintracht": "eintracht frankfurt",
    "freiburg": "sc freiburg",
    "wolfsburg": "vfl wolfsburg",
    "vfl wolfsburg": "vfl wolfsburg",
    "mainz": "mainz 05",
    "1 fsv mainz 05": "mainz 05",
    "stuttgart": "vfb stuttgart",
    "vfb": "vfb stuttgart",
    "augsburg": "fc augsburg",
    "fc augsburg": "fc augsburg",
    "hoffenheim": "tsg hoffenheim",
    "tsg": "tsg hoffenheim",
    "union berlin": "1 fc union berlin",
    "hertha": "hertha bsc",
    "werder": "werder bremen",
    "bremen": "werder bremen",
    "bochum": "vfl bochum",
    "greuther furth": "spvgg greuther furth",
    "darmstadt": "sv darmstadt 98",
    "heidenheim": "1 fc heidenheim",
    "koln": "1 fc koln",
    "koeln": "1 fc koln",
    "cologne": "1 fc koln",
    "fc koeln": "1 fc koln",
    "fc koln": "1 fc koln",
    "gladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",

    # ── Ligue 1 ─────────────────────────────────────────────────────
    "psg": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "om": "olympique marseille",
    "olympique marseille": "olympique marseille",
    "marseille": "olympique marseille",
    "lyon": "olympique lyonnais",
    "lille": "losc lille",
    "monaco": "as monaco",
    "rennes": "stade rennais",
    "nantes": "fc nantes",
    "strasbourg": "rc strasbourg",
    "brest": "stade brestois",
    "clermont": "clermont foot",
    "lorient": "fc lorient",
    "reims": "stade de reims",
    "toulouse": "toulouse fc",

    # ── Eredivisie ──────────────────────────────────────────────────
    "ajax": "afc ajax",
    "psv": "psv eindhoven",
    "feyenoord": "feyenoord rotterdam",
    "az": "az alkmaar",
    "twente": "fc twente",
    "utrecht": "fc utrecht",

    # ── Primeira Liga ───────────────────────────────────────────────
    "benfica": "sl benfica",
    "porto": "fc porto",
    "sporting": "sporting cp",
    "braga": "sc braga",

    # ── Championship ────────────────────────────────────────────────
    "qpr": "queens park rangers",
    "hull": "hull city",
    "cardiff": "cardiff city",
    "middlesbrough": "middlesbrough",
    "swansea": "swansea city",

    # ── Russian PL ──────────────────────────────────────────────────
    "zenit": "zenit saint petersburg",
    "cska": "cska moscow",
    "spartak": "spartak moscow",
    "lokomotiv": "lokomotiv moscow",
    "krasnodar": "fc krasnodar",
    "rostov": "fc rostov",

    # ── Europe ──────────────────────────────────────────────────────
    "salzburg": "rb salzburg",
    "celtic": "celtic",
    "shakhtar": "shakhtar donetsk",
    "galatasaray": "galatasaray",
    "fenerbahce": "fenerbahce",
    "copenhagen": "fc kobenhavn",

}


def normalize_team_name(raw: str) -> str:
    """
    Нормализует имя команды к каноническому виду.
    Цепочка: lowercase → diacritics → alias → strip suffix → alias → strip prefix → alias.
    """
    if not raw or not isinstance(raw, str):
        return ""
    t = raw.strip().lower()
    t = " ".join(t.split())  # схлопнуть множественные пробелы
    t = _normalize_diacritics(t)

    # 1. Прямой alias lookup
    if t in _TEAM_ALIASES:
        return _TEAM_ALIASES[t]

    # 2. Strip suffix → alias
    t_suf = _strip_suffix(t)
    if t_suf in _TEAM_ALIASES:
        return _TEAM_ALIASES[t_suf]

    # 3. Strip prefix → alias
    t_pref = _strip_prefix(t_suf)
    if t_pref in _TEAM_ALIASES:
        return _TEAM_ALIASES[t_pref]

    # 4. Fallback: если stripping что-то изменил, возвращаем stripped версию
    if t_pref != t:
        return t_pref
    return t


def build_canonical_id(home: str, away: str, date_utc: str) -> str:
    """
    Строит canonical_id: home__away__YYYYMMDD
    """
    home_clean = normalize_team_name(home).replace(" ", "_")
    away_clean = normalize_team_name(away).replace(" ", "_")
    date_part = date_utc[:10].replace("-", "") if date_utc else ""
    return f"{home_clean}__{away_clean}__{date_part}"


# ── Self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        ("Man Utd", "manchester united"),
        ("spurs", "tottenham hotspur"),
        ("BVB", "borussia dortmund"),
        ("inter", "inter milan"),
        ("Bayern Munchen", "bayern munich"),
        ("Real Soc", "real sociedad"),
        ("  Wolves  ", "wolverhampton wanderers"),
        ("K\u00f6ln", "1 fc koln"),
        ("FC K\u00f6ln", "1 fc koln"),
        ("PSG", "paris saint germain"),
        ("Chelsea FC", "chelsea"),
        ("FC Barcelona", "fc barcelona"),
        ("CSKA", "cska moscow"),
    ]
    failed = 0
    for raw, expected in tests:
        result = normalize_team_name(raw)
        ok = "✅" if result == expected else "❌"
        if result != expected:
            failed += 1
        print(f"  {ok} normalize_team_name({raw!r}) = {result!r} (expected {expected!r})")
    cid = build_canonical_id("Man Utd", "Spurs", "2026-10-03T15:00:00")
    ok = "✅" if cid == "manchester_united__tottenham_hotspur__20261003" else "❌"
    if "manchester_united__tottenham_hotspur__20261003" != cid:
        failed += 1
    print(f"  {ok} build_canonical_id = {cid!r}")
    print(f"\n{'All passed' if failed == 0 else f'{failed} FAILED'}")
