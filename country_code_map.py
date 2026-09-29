"""
Единый справочник стран для Gatekeeper-AI v700-prod.
Любое название страны -> код (ISO 3166-1 alpha-2 или FIFA).

Используется всеми коллекторами и оркестратором main.py.
Заменяет COUNTRY_FLAGS, TEAM_COUNTRY_MAP и _get_flag из старого main.py.
"""
import unicodedata

# ---------------------------------------------------------------------------
# Прямой маппинг: любое написание -> код
# ---------------------------------------------------------------------------
_DIRECT_MAP = {
    # --- Europe ---
    "england": "EN", "english": "EN",
    "scotland": "SCT", "scottish": "SCT",
    "wales": "WAL", "welsh": "WAL",
    "northern ireland": "NIR", "n. ireland": "NIR",
    "ireland": "IE", "republic of ireland": "IE", "irish": "IE",
    "france": "FR", "french": "FR",
    "germany": "DE", "german": "DE", "deutschland": "DE",
    "spain": "ES", "spanish": "ES", "espana": "ES", "espana": "ES",
    "italy": "IT", "italian": "IT", "italia": "IT",
    "netherlands": "NL", "dutch": "NL", "holland": "NL", "nederland": "NL",
    "belgium": "BE", "belgian": "BE", "belgie": "BE", "belgie": "BE",
    "portugal": "PT", "portuguese": "PT",
    "switzerland": "CH", "swiss": "CH", "schweiz": "CH", "suisse": "CH",
    "austria": "AT", "austrian": "AT", "osterreich": "AT", "osterreich": "AT",
    "denmark": "DK", "danish": "DK", "danmark": "DK",
    "sweden": "SE", "swedish": "SE", "sverige": "SE",
    "norway": "NO", "norwegian": "NO", "norge": "NO",
    "finland": "FI", "finnish": "FI", "suomi": "FI",
    "iceland": "IS", "icelandic": "IS", "island": "IS",
    "poland": "PL", "polish": "PL", "polska": "PL",
    "czech republic": "CZ", "czechia": "CZ", "czech": "CZ", "cesko": "CZ",
    "slovakia": "SK", "slovak": "SK", "slovensko": "SK",
    "slovenia": "SI", "slovenian": "SI", "slovenija": "SI",
    "hungary": "HU", "hungarian": "HU", "magyarorszag": "HU", "magyarorszag": "HU",
    "romania": "RO", "romanian": "RO", "romania": "RO",
    "bulgaria": "BG", "bulgarian": "BG",
    "greece": "GR", "greek": "GR", "hellas": "GR", "ellada": "GR",
    "turkey": "TR", "turkish": "TR", "turkiye": "TR", "turkiye": "TR",
    "croatia": "HR", "croatian": "HR", "hrvatska": "HR",
    "serbia": "RS", "serbian": "RS", "srbija": "RS",
    "bosnia and herzegovina": "BA", "bosnia": "BA", "bih": "BA",
    "montenegro": "ME", "crna gora": "ME",
    "north macedonia": "MK", "macedonia": "MK", "fyrom": "MK", "makedonija": "MK",
    "albania": "AL", "albanian": "AL", "shqiperia": "AL",
    "kosovo": "XK", "kosova": "XK",
    "cyprus": "CY", "cypriot": "CY", "kypros": "CY",
    "malta": "MT", "maltese": "MT",
    "luxembourg": "LU", "luxemburg": "LU",
    "andorra": "AD", "andorran": "AD",
    "san marino": "SM", "sanmarinese": "SM",
    "gibraltar": "GI",
    "liechtenstein": "LI",
    "faroe islands": "FO", "faroes": "FO", "foroyar": "FO",
    "estonia": "EE", "estonian": "EE", "eesti": "EE",
    "latvia": "LV", "latvian": "LV", "latvija": "LV",
    "lithuania": "LT", "lithuanian": "LT", "lietuva": "LT",
    "russia": "RU", "russian": "RU", "rossiya": "RU",
    "ukraine": "UA", "ukrainian": "UA", "ukrayina": "UA",
    "belarus": "BY", "belarusian": "BY",
    "moldova": "MD", "moldovan": "MD",
    "georgia": "GE", "georgian": "GE", "sakartvelo": "GE",
    "armenia": "AM", "armenian": "AM",
    "azerbaijan": "AZ", "azeri": "AZ", "azerbaycan": "AZ",
    "kazakhstan": "KZ", "kazakh": "KZ", "qazaqstan": "KZ",

    # --- South America ---
    "brazil": "BR", "brazilian": "BR", "brasil": "BR",
    "argentina": "AR", "argentine": "AR",
    "uruguay": "UY", "uruguayan": "UY",
    "paraguay": "PY", "paraguayan": "PY",
    "chile": "CL", "chilean": "CL",
    "peru": "PE", "peruvian": "PE",
    "ecuador": "EC", "ecuadorian": "EC",
    "colombia": "CO", "colombian": "CO",
    "venezuela": "VE", "venezuelan": "VE",
    "bolivia": "BO", "bolivian": "BO",

    # --- North/Central America ---
    "usa": "US", "united states": "US", "us": "US", "america": "US", "american": "US",
    "canada": "CA", "canadian": "CA",
    "mexico": "MX", "mexican": "MX", "mexico": "MX",
    "costa rica": "CR", "costarican": "CR",
    "honduras": "HN", "honduran": "HN",
    "guatemala": "GT", "guatemalan": "GT",
    "panama": "PA", "panamanian": "PA",
    "el salvador": "SV", "salvadoran": "SV",
    "dominican republic": "DO", "dominican": "DO",
    "jamaica": "JM", "jamaican": "JM",
    "trinidad and tobago": "TT", "t&t": "TT", "trinidad": "TT",
    "puerto rico": "PR",
    "cuba": "CU", "cuban": "CU",

    # --- Asia ---
    "japan": "JP", "japanese": "JP", "nihon": "JP", "nippon": "JP",
    "south korea": "KR", "korea republic": "KR", "korea": "KR", "korean": "KR",
    "north korea": "KP", "dpr korea": "KP",
    "china": "CN", "chinese": "CN", "prc": "CN",
    "australia": "AU", "australian": "AU", "aussie": "AU",
    "new zealand": "NZ", "kiwi": "NZ", "nz": "NZ",
    "india": "IN", "indian": "IN",
    "thailand": "TH", "thai": "TH",
    "vietnam": "VN", "vietnamese": "VN",
    "indonesia": "ID", "indonesian": "ID",
    "philippines": "PH", "filipino": "PH",
    "malaysia": "MY", "malaysian": "MY",
    "singapore": "SG", "singaporean": "SG",
    "saudi arabia": "SA", "saudi": "SA", "saudi arabian": "SA",
    "qatar": "QA", "qatari": "QA",
    "uae": "AE", "united arab emirates": "AE",
    "iran": "IR", "iranian": "IR",
    "iraq": "IQ", "iraqi": "IQ",
    "israel": "IL", "israeli": "IL",
    "jordan": "JO", "jordanian": "JO",
    "uzbekistan": "UZ", "uzbek": "UZ",
    "kuwait": "KW", "kuwaiti": "KW",
    "bahrain": "BH",
    "oman": "OM", "omani": "OM",
    "lebanon": "LB", "lebanese": "LB",
    "syria": "SY", "syrian": "SY",
    "palestine": "PS", "palestinian": "PS",
    "yemen": "YE", "yemeni": "YE",

    # --- Africa ---
    "south africa": "ZA", "south african": "ZA",
    "nigeria": "NG", "nigerian": "NG",
    "egypt": "EG", "egyptian": "EG",
    "morocco": "MA", "moroccan": "MA",
    "tunisia": "TN", "tunisian": "TN",
    "algeria": "DZ", "algerian": "DZ",
    "ghana": "GH", "ghanaian": "GH",
    "senegal": "SN", "senegalese": "SN",
    "cameroon": "CM", "cameroonian": "CM",
    "ivory coast": "CI", "cote d ivoire": "CI", "cote d ivoire": "CI",
    "kenya": "KE", "kenyan": "KE",
    "ethiopia": "ET", "ethiopian": "ET",
    "tanzania": "TZ", "tanzanian": "TZ",
    "zambia": "ZM", "zambian": "ZM",
    "zimbabwe": "ZW", "zimbabwean": "ZW",
    "angola": "AO", "angolan": "AO",
    "congo": "CG", "congolese": "CG",
    "dr congo": "CD", "democratic republic of congo": "CD",
    "mali": "ML", "malian": "ML",
    "burkina faso": "BF",
    "guinea": "GN", "guinean": "GN",
    "libya": "LY", "libyan": "LY",
    "sudan": "SD", "sudanese": "SD",
    "gabon": "GA", "gabonese": "GA",
    "togo": "TG", "togolese": "TG",
    "benin": "BJ",
    "rwanda": "RW", "rwandan": "RW",
    "uganda": "UG", "ugandan": "UG",
    "mozambique": "MZ", "mozambican": "MZ",
    "botswana": "BW", "botswanan": "BW",
    "namibia": "NA", "namibian": "NA",
    "madagascar": "MG", "madagascan": "MG",
}

# Reverse map: code -> русское название (для дашборда)
_CODE_TO_NAME = {
    "EN": "Англия", "SCT": "Шотландия", "WAL": "Уэльс", "NIR": "Сев. Ирландия",
    "IE": "Ирландия", "FR": "Франция", "DE": "Германия", "ES": "Испания",
    "IT": "Италия", "NL": "Нидерланды", "BE": "Бельгия", "PT": "Португалия",
    "CH": "Швейцария", "AT": "Австрия", "DK": "Дания", "SE": "Швеция",
    "NO": "Норвегия", "FI": "Финляндия", "IS": "Исландия", "PL": "Польша",
    "CZ": "Чехия", "SK": "Словакия", "SI": "Словения", "HU": "Венгрия",
    "RO": "Румыния", "BG": "Болгария", "GR": "Греция", "TR": "Турция",
    "HR": "Хорватия", "RS": "Сербия", "BA": "Босния", "ME": "Черногория",
    "MK": "Македония", "AL": "Албания", "XK": "Косово", "CY": "Кипр",
    "MT": "Мальта", "LU": "Люксембург", "AD": "Андорра", "SM": "Сан-Марино",
    "GI": "Гибралтар", "LI": "Лихтенштейн", "FO": "Фареры", "EE": "Эстония",
    "LV": "Латвия", "LT": "Литва", "RU": "Россия", "UA": "Украина",
    "BY": "Беларусь", "MD": "Молдова", "GE": "Грузия", "AM": "Армения",
    "AZ": "Азербайджан", "KZ": "Казахстан",
    "BR": "Бразилия", "AR": "Аргентина", "UY": "Уругвай", "PY": "Парагвай",
    "CL": "Чили", "PE": "Перу", "EC": "Эквадор", "CO": "Колумбия",
    "VE": "Венесуэла", "BO": "Боливия",
    "US": "США", "CA": "Канада", "MX": "Мексика", "CR": "Коста-Рика",
    "HN": "Гондурас", "GT": "Гватемала", "PA": "Панама", "SV": "Сальвадор",
    "DO": "Доминикана", "JM": "Ямайка", "TT": "Тринидад", "PR": "Пуэрто-Рико",
    "CU": "Куба",
    "JP": "Япония", "KR": "Юж. Корея", "KP": "Сев. Корея", "CN": "Китай",
    "AU": "Австралия", "NZ": "Н. Зеландия", "IN": "Индия", "TH": "Таиланд",
    "VN": "Вьетнам", "ID": "Индонезия", "PH": "Филиппины", "MY": "Малайзия",
    "SG": "Сингапур", "SA": "Сауд. Аравия", "QA": "Катар", "AE": "ОАЭ",
    "IR": "Иран", "IQ": "Ирак", "IL": "Израиль", "JO": "Иордания",
    "UZ": "Узбекистан", "KW": "Кувейт", "BH": "Бахрейн", "OM": "Оман",
    "LB": "Ливан", "SY": "Сирия", "PS": "Палестина", "YE": "Йемен",
    "ZA": "ЮАР", "NG": "Нигерия", "EG": "Египет", "MA": "Марокко",
    "TN": "Тунис", "DZ": "Алжир", "GH": "Гана", "SN": "Сенегал",
    "CM": "Камерун", "CI": "Кот-д'Ивуар", "KE": "Кения", "ET": "Эфиопия",
    "TZ": "Танзания", "ZM": "Замбия", "ZW": "Зимбабве", "AO": "Ангола",
    "CG": "Конго", "CD": "ДР Конго", "ML": "Мали", "BF": "Буркина-Фасо",
    "GN": "Гвинея", "LY": "Ливия", "SD": "Судан", "GA": "Габон",
    "TG": "Того", "BJ": "Бенин", "RW": "Руанда", "UG": "Уганда",
    "MZ": "Мозамбик", "BW": "Ботсвана", "NA": "Намибия", "MG": "Мадагаскар",
}


def _strip_diacritics(s: str) -> str:
    """Убирает диакритики (умлауты, ударения) через NFKD-нормализацию."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_country(country: str, source: str = "") -> str:
    """
    Преобразует любое написание страны в код (ISO alpha-2 / FIFA).
    Возвращает пустую строку, если страна не распознана.
    """
    if not country or not isinstance(country, str):
        return ""

    country_lower = country.lower().strip()
    if not country_lower:
        return ""

    # Прямой поиск
    code = _DIRECT_MAP.get(country_lower)
    if code:
        return code

    # С диакритиками
    stripped = _strip_diacritics(country_lower)
    code = _DIRECT_MAP.get(stripped)
    if code:
        return code

    # Апострофы -> пробелы
    no_apostrophe = stripped.replace("'", " ").replace("\'", " ")
    no_apostrophe = " ".join(no_apostrophe.split())
    code = _DIRECT_MAP.get(no_apostrophe)
    if code:
        return code

    # Fuzzy: ищем по словам
    words = no_apostrophe.split()
    for w in words:
        if len(w) >= 3:
            code = _DIRECT_MAP.get(w)
            if code:
                return code

    # Уже 2-буквенный код
    if len(country_lower) == 2 and country_lower.isalpha():
        return country_lower.upper()

    return ""


def code_to_name(code: str) -> str:
    """Reverse lookup: код страны -> русское название для дашборда."""
    if not code:
        return ""
    return _CODE_TO_NAME.get(code.upper(), code.upper())
