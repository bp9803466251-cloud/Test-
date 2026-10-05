"""
country_code_map.py — Маппинг кодов стран и лиг.
Используется коллекторами для нормализации country и competition.
"""

__version__ = "8.11-patched"

__all__ = [
    "COUNTRY_CODE_MAP", "LEAGUE_NAME_MAP",
    "get_country_name", "get_league_name",
    "__version__",
]

COUNTRY_CODE_MAP = {
    # ── 2-буквенные ISO-коды ──────────────────────────
    "AR": "Argentina",
    "AU": "Australia",
    "AT": "Austria",
    "BE": "Belgium",
    "BR": "Brazil",
    "BG": "Bulgaria",
    "CA": "Canada",
    "CL": "Chile",
    "CO": "Colombia",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DK": "Denmark",
    "EC": "Ecuador",
    "EG": "Egypt",
    "EN": "England",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IS": "Iceland",
    "IE": "Ireland",
    "IL": "Israel",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "MA": "Morocco",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "RS": "Serbia",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ZA": "South Africa",
    "ES": "Spain",
    "SE": "Sweden",
    "CH": "Switzerland",
    "TR": "Turkey",
    "UA": "Ukraine",
    "AE": "United Arab Emirates",
    "US": "USA",
    "UY": "Uruguay",
    # ── 3-буквенные FIFA/IOC коды ──────────────────────
    "ENG": "England",
    "SCO": "Scotland",
    "WAL": "Wales",
    "NIR": "Northern Ireland",
    "GER": "Germany",
    "NED": "Netherlands",
    "SUI": "Switzerland",
    "CRO": "Croatia",
    "DEN": "Denmark",
    "SWE": "Sweden",
    "NOR": "Norway",
    "FIN": "Finland",
    "POL": "Poland",
    "POR": "Portugal",
    "ESP": "Spain",
    "FRA": "France",
    "ITA": "Italy",
    "BEL": "Belgium",
    "AUT": "Austria",
    "CZE": "Czech Republic",
    "TUR": "Turkey",
    "GRE": "Greece",
    "UKR": "Ukraine",
    "SRB": "Serbia",
    "SVK": "Slovakia",
    "SVN": "Slovenia",
    "ROU": "Romania",
    "HUN": "Hungary",
    "BUL": "Bulgaria",
    "ISR": "Israel",
    "RUS": "Russia",
    "ALG": "Algeria",
    "MAR": "Morocco",
    "TUN": "Tunisia",
    "EGY": "Egypt",
    "NGA": "Nigeria",
    "RSA": "South Africa",
    "AUS": "Australia",
    "JPN": "Japan",
    "KOR": "South Korea",
    "CHN": "China",
    "MEX": "Mexico",
    "USA": "USA",
    "CAN": "Canada",
    "CRC": "Costa Rica",
    "HON": "Honduras",
    "PAN": "Panama",
    "JAM": "Jamaica",
    "URU": "Uruguay",
    "PAR": "Paraguay",
    "ECU": "Ecuador",
    "COL": "Colombia",
    "CHI": "Chile",
    "PER": "Peru",
    "BOL": "Bolivia",
    "VEN": "Venezuela",
    "ARG": "Argentina",
    "BRA": "Brazil",
    # ── football-data.co.uk однобуквенные коды ─────────
    "E": "England",    # E0, E1, E2, E3, EC
    "S": "Scotland",   # S0, S1, S2, SC
    "G": "Germany",     # D1, D2 (football-data использует D, не G)
    "D": "Germany",
    "I": "Italy",       # I1, I2
    "SP": "Spain",      # SP1, SP2
    "F": "France",      # F1, F2
    "N": "Netherlands", # N1
    "B": "Belgium",     # B1
    "P": "Portugal",    # P1
    "T": "Turkey",      # T1
    "G2": "Germany",    # Bundesliga 2 в некоторых источниках
}

# Маппинг названий лиг (slug → читаемое название)
LEAGUE_NAME_MAP = {
    # ── Slug-форматы ──────────────────────────────────
    "primera_a": "Primera A",
    "primera_division": "Primera Divisi\u00f3n",
    "premier_league": "Premier League",
    "la_liga": "La Liga",
    "bundesliga": "Bundesliga",
    "bundesliga_2": "Bundesliga 2",
    "serie_a": "Serie A",
    "serie_b": "Serie B",
    "ligue_1": "Ligue 1",
    "ligue_2": "Ligue 2",
    "primeira_liga": "Primeira Liga",
    "eredivisie": "Eredivisie",
    "eerste_divisie": "Eerste Divisie",
    "jupiler_pro_league": "Jupiler Pro League",
    "super_lig": "S\u00fcper Lig",
    "superliga": "Superliga",
    "eliteserien": "Eliteserien",
    "allsvenskan": "Allsvenskan",
    "scottish_premiership": "Scottish Premiership",
    "championship": "Championship",
    "league_one": "League One",
    "league_two": "League Two",
    "mls": "MLS",
    "j1_league": "J1 League",
    "super_lig_greece": "Super League Greece",
    "super_lig_swiss": "Swiss Super League",
    "bundesliga_austria": "Austrian Bundesliga",
    "ekstraklasa": "Ekstraklasa",
    "liga_mx": "Liga MX",
    "serie_a_brazil": "S\u00e9rie A",
    "serie_b_brazil": "S\u00e9rie B",
    "nwsl": "NWSL",
    "wsl": "WSL",
    "uefa_nations_league": "UEFA Nations League",
    "copa_argentina": "Copa Argentina",
    # ── football-data.co.uk league codes ───────────────
    "E0": "Premier League",
    "E1": "Championship",
    "E2": "League One",
    "E3": "League Two",
    "EC": "National League",
    "SP1": "La Liga",
    "SP2": "La Liga 2",
    "SP3": "Primera Divisi\u00f3n RFEF",
    "I1": "Serie A",
    "I2": "Serie B",
    "I3": "Serie C",
    "D1": "Bundesliga",
    "D2": "Bundesliga 2",
    "F1": "Ligue 1",
    "F2": "Ligue 2",
    "N1": "Eredivisie",
    "B1": "Jupiler Pro League",
    "P1": "Primeira Liga",
    "T1": "S\u00fcper Lig",
    "G1": "Bundesliga",
    # ── Scottish league codes ──────────────────────────
    "SC0": "Scottish Premiership",
    "SC1": "Scottish Championship",
    "SC2": "Scottish League One",
    "SC3": "Scottish League Two",
    "SC4": "Scottish Highland/Lowland",
}


def get_country_name(code: str) -> str:
    """Получить название страны по коду (case-insensitive)."""
    if not code:
        return ""
    return COUNTRY_CODE_MAP.get(code.upper(), code)


def get_league_name(slug: str) -> str:
    """Получить читаемое название лиги по slug или коду football-data."""
    if not slug:
        return ""
    # FIX: football-data коды (E0, SP1, D1 ...) — заглавные,
    # но .lower() ломает их. Трёхуровневый lookup:
    # 1) точное совпадение (E0, SC0 ...)
    # 2) uppercase (e0 → E0)
    # 3) lowercase (Premier_League → premier_league)
    # 4) fallback: slug с заменой _ на пробел
    if slug in LEAGUE_NAME_MAP:
        return LEAGUE_NAME_MAP[slug]
    upper = slug.upper()
    if upper in LEAGUE_NAME_MAP:
        return LEAGUE_NAME_MAP[upper]
    lower = slug.lower()
    if lower in LEAGUE_NAME_MAP:
        return LEAGUE_NAME_MAP[lower]
    return slug.replace("_", " ").title()
