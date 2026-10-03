"""
Team Registry — нормализация имён команд (§19.2)
================================================
Устраняет циклическую зависимость gatekeeper_hub.py ↔ team_registry.

Использование:
    from team_registry import normalize_team_name
    clean = normalize_team_name("Man Utd")  # → "manchester united"
"""

# ── Реестр нормализации: alias → canonical (lowercase) ──────────────
_TEAM_ALIASES = {
    # ── Premier League ──────────────────────────────────────────────
    "man utd": "manchester united",
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
    "cologne": "1 fc koln",
    "gladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
}


def normalize_team_name(raw: str) -> str:
    """
    Нормализует имя команды к каноническому виду.
    Возвращает lowercase-строку без лишних пробелов.
    """
    if not raw or not isinstance(raw, str):
        return ""
    clean = raw.strip().lower()
    clean = " ".join(clean.split())  # схлопнуть множественные пробелы
    return _TEAM_ALIASES.get(clean, clean)


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
