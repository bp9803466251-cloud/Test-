"""
country_code_map.py — Маппинг кодов стран и лиг.
Используется коллекторами для нормализации country и competition.
"""

COUNTRY_CODE_MAP = {
    # Коды → полные названия
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
    "SCO": "Scotland",
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
    "WLS": "Wales",
}

# Маппинг названий лиг (slug → читаемое название)
LEAGUE_NAME_MAP = {
    "primera_a": "Primera A",
    "primera_division": "Primera División",
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
    "super_lig": "Süper Lig",
    "superliga": "Superliga",
    "eliteserien": "Eliteserien",
    "allsvenskan": "Allsvenskan",
    "super_lig_tr": "Süper Lig",
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
    "serie_a_brazil": "Série A",
    "serie_b_brazil": "Série B",
    "nwsj": "NWSL",
    "wsl": "WSL",
    "uefa_nations_league": "UEFA Nations League",
    "copa_argentina": "Copa Argentina",
}


def get_country_name(code: str) -> str:
    """Получить название страны по коду."""
    if not code:
        return ""
    return COUNTRY_CODE_MAP.get(code.upper(), code)


def get_league_name(slug: str) -> str:
    """Получить читаемое название лиги по slug."""
    if not slug:
        return ""
    return LEAGUE_NAME_MAP.get(slug.lower(), slug.replace("_", " ").title())


__all__ = ["COUNTRY_CODE_MAP", "LEAGUE_NAME_MAP", "get_country_name", "get_league_name"]
