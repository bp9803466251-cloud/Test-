#!/usr/bin/env python3
"""
SearchModule v6.1.0 — Gatekeeper-AI v8.9 — Gatekeeper-AI v700-prod
Поиск value bets, нормализация команд, маппинг лиг.
"""
import re
import os
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

try:
    from gatekeeper_config import now_msk as _cfg_now_msk, MSK_TZ
except ImportError:
    MSK_TZ = timezone(timedelta(hours=3))

VALUE_THRESHOLD = float(os.environ.get("VALUE_BET_THRESHOLD", "0.03"))


# ---------------------------------------------------------------------------
# Нормализация названий команд (правило 4 гида)
# ---------------------------------------------------------------------------
_REMOVE_SUFFIXES = (
    "fc", "cf", "afc", "united", "utd", "city", "town",
    "sc", "afc", "if", "bk", "ac", "asd", "ssd", "ss",
)

_REMOVE_WORDS = {
    "fc", "cf", "afc", "sc", "ac", "asd", "ssd", "ss", "if",
    "bk", "club", "de", "the", "ca", "cd", "sd",
}

_REPLACE = {
    "manchester": "man",
    "man utd": "man",
    "man united": "man",
    "tottenham": "spurs",
    "newcastle": "newc",
    "wolverhampton": "wolves",
    "leicester": "leic",
    "west bromwich": "wba",
    "west ham": "whu",
    "brighton & hove albion": "brighton",
    "brighton and hove albion": "brighton",
    "real sociedad": "rsoc",
    "athletic bilbao": "athbil",
    "atletico madrid": "atm",
    "atletico de madrid": "atm",
    "club atletico de madrid": "atm",
    "club brugge": "clbr",
    "club bruges": "clbr",
    "club leon": "leon",
    "club tijuana": "tijuana",
    "club puebla": "puebla",
    "clubQueretaro": "queretaro",
}


def clean_team_name(name: str) -> str:
    """
    Нормализация названия команды (правило 4 гида).
    FIX-1: Делегирует в team_registry.normalize_team_name() — единый источник истины.
    Fallback — локальная логика (если team_registry недоступен).
    """
    if not name:
        return ""
    # FIX-1: team_registry — единый реестр (75+ алиасов, умляуты, суффиксы)
    try:
        from team_registry import normalize_team_name
        return normalize_team_name(name)
    except ImportError:
        pass
    # Fallback — локальная нормализация
    s = name.lower().strip()
    s = s.replace("_", " ")
    # Замены (без break — применяем все подходящие)
    for old, new in _REPLACE.items():
        if s == old or s.startswith(old + " "):
            s = s.replace(old, new)
    # Удаление слов (fc, cf, club, etc.) из любой позиции
    tokens = s.split()
    tokens = [t for t in tokens if t.strip(".,") not in _REMOVE_WORDS]
    s = " ".join(tokens)
    # Удаление спецсимволов
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else name.lower().strip()


# ---------------------------------------------------------------------------
# Маппинг соревнований → короткие коды
# ---------------------------------------------------------------------------
_COMP_KEYWORDS = [
    ("premier league", "ENG PL"),
    ("championship", "ENG CH"),
    ("league one", "ENG L1"),
    ("league two", "ENG L2"),
    ("conference", "ENG NC"),
    ("la liga", "ESP LL"),
    ("laliga", "ESP LL"),
    ("primera division", "ESP LL"),
    ("segunda division", "ESP SD"),
    ("la liga 2", "ESP SD"),
    ("primera fef", "ESP PF"),
    ("serie a", "ITA SA"),
    ("serie b", "ITA SB"),
    ("serie c", "ITA SC"),
    ("serie d", "ITA SD"),
    ("bundesliga", "GER BL"),
    ("2. bundesliga", "GER B2"),
    ("3. liga", "GER 3L"),
    ("ligue 1", "FRA L1"),
    ("ligue 2", "FRA L2"),
    ("primeira liga", "POR PL"),
    ("liga portugal", "POR PL"),
    ("eredivisie", "NED ER"),
    ("eerste divisie", "NED ED"),
    ("brasileirao", "BRA SA"),
    ("brasileirão", "BRA SA"),
    ("serie a brasileiro", "BRA SA"),
    ("serie b brasileiro", "BRA SB"),
    ("primera nacional", "ARG PN"),
    ("primera division argentina", "ARG PD"),
    ("liga profesional", "ARG LP"),
    ("liga mx", "MEX LM"),
    ("liga bbva mx", "MEX LM"),
    ("major league soccer", "USA MLS"),
    ("mls", "USA MLS"),
    ("k league", "KOR KL"),
    ("k league 1", "KOR KL"),
    ("j1 league", "JPN J1"),
    ("j2 league", "JPN J2"),
    ("nations league", "UE NL"),
    ("uefa nations league", "UE NL"),
    ("european championship", "UE EC"),
    ("euro 202", "UE EC"),
    ("champions league", "UE CL"),
    ("europa league", "UE EL"),
    ("conference league", "UE CF"),
    ("europa conference league", "UE CF"),
    ("concacaf", "CON"),
    ("gold cup", "CON GC"),
    ("concacaf nations league", "CON NL"),
    ("africa cup", "CAF AC"),
    ("afcon", "CAF AC"),
    ("caf", "CAF"),
    ("world cup", "FIFA WC"),
    ("world cup qualification", "FIFA WQ"),
    ("qualifiers", "FIFA WQ"),
    ("austin bold", "USL"),
]

_COUNTRY_CODES = {
    "england": "ENG", "spain": "ES", "italy": "ITA", "germany": "GER",
    "france": "FR", "portugal": "POR", "netherlands": "NED",
    "brazil": "BRA", "argentina": "AR", "mexico": "MEX",
    "united states": "US", "usa": "US", "korea": "KOR", "south korea": "KOR",
    "japan": "JPN", "china": "CHN", "australia": "AUS",
    "russia": "RUS", "ukraine": "UKR", "poland": "POL",
    "turkey": "TUR", "greece": "GRE", "croatia": "CRO",
    "serbia": "SRB", "scotland": "SCO", "ireland": "IRL",
    "northern ireland": "NIR", "wales": "WAL",
    "sweden": "SWE", "norway": "NOR", "finland": "FIN",
    "denmark": "DEN", "switzerland": "SUI", "austria": "AUT",
    "czech republic": "CZE", "belgium": "BEL",
    "slovenia": "SVN", "slovakia": "SVK", "hungary": "HUN",
    "romania": "ROU", "bulgaria": "BUL",
    "colombia": "COL", "chile": "CHI", "peru": "PER",
    "ecuador": "ECU", "uruguay": "URU", "paraguay": "PAR",
    "bolivia": "BOL", "venezuela": "VEN",
    "costa rica": "CR", "honduras": "HON", "guatemala": "GUA",
    "el salvador": "ESV", "panama": "PAN", "jamaica": "JAM",
    "trinidad": "TTO", "haiti": "HAI",
    "nigeria": "NGA", "ghana": "GHA", "senegal": "SEN", "mali": "MLI",
    "ivory coast": "CIV", "cameroon": "CMR",
    "egypt": "EGY", "morocco": "MAR", "tunisia": "TUN", "algeria": "ALG",
    "south africa": "RSA",
}


def _get_competition_code(competition: str, country: str) -> str:
    comp = (competition or "").lower().strip()
    country_lower = (country or "").lower().strip()
    for keyword, code in _COMP_KEYWORDS:
        if keyword in comp:
            return code
    if country_lower and country_lower in _COUNTRY_CODES:
        return _COUNTRY_CODES[country_lower]
    if comp:
        return comp[:3].upper()
    return "INT"


def _parse_date_msk(date_utc: str) -> Tuple[str, str]:
    if not date_utc:
        return ("", "")
    try:
        s = date_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_msk = dt.astimezone(MSK_TZ)
        return (dt_msk.strftime("%d.%m"), dt_msk.strftime("%H:%M"))
    except Exception:
        try:
            ts = float(date_utc)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            dt_msk = dt.astimezone(MSK_TZ)
            return (dt_msk.strftime("%d.%m"), dt_msk.strftime("%H:%M"))
        except Exception:
            return ("", "")


def _dt_from_utc(date_utc: str) -> Optional[datetime]:
    if not date_utc:
        return None
    try:
        s = date_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK_TZ)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Извлечение коэффициентов — поддержка v700 и v600
# ---------------------------------------------------------------------------
def _get_odds(match: dict) -> Tuple[float, float, float]:
    """
    Возвращает (home, draw, away) как float, или (0, 0, 0).
    Поддерживает 3 формата:
      - v700: odds.1x2.current
      - промежуточный: odds.current
      - v600 (плоский): odds.home
    """
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return (0.0, 0.0, 0.0)

    # v700: odds.1x2.current
    if "1x2" in odds:
        section = odds.get("1x2", {})
        if isinstance(section, dict):
            current = section.get("current", {})
            if isinstance(current, dict) and current:
                return _extract_odds_tuple(current)

    # v700: odds.1x2.closing (fallback — Propline closing odds)
    if isinstance(section, dict):
        closing = section.get("closing", {})
        if isinstance(closing, dict) and closing:
            return _extract_odds_tuple(closing)

    # промежуточный: odds.current
    if "current" in odds and isinstance(odds["current"], dict):
        return _extract_odds_tuple(odds["current"])

    # v600 плоский
    return _extract_odds_tuple(odds)


def _extract_odds_tuple(d: dict) -> Tuple[float, float, float]:
    def _val(key):
        v = d.get(key, "-")
        if v is None or v == "-" or v == "":
            return 0.0
        try:
            f = float(v)
            return f if f > 1.0 else 0.0
        except (ValueError, TypeError):
            return 0.0
    return (_val("home"), _val("draw"), _val("away"))


def _get_odds_metadata(match: dict) -> dict:
    """Возвращает метаданные odds из v700 формата."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return _empty_metadata()
    if "1x2" in odds:
        section = odds.get("1x2", {})
        if isinstance(section, dict):
            return {
                "verification": section.get("_verification", "UNVERIFIED"),
                "independent_sources": section.get("_independent_sources", 0),
                "upstream": section.get("_upstream", []),
                "movement": section.get("movement", {}),
                "betradar_consensus": section.get("_betradar_consensus", False),
                "outlier_detected": section.get("_outlier_detected", False),
                "sources_count": len(section.get("sources", [])),
                "opening": section.get("opening") or section.get("_open_snapshot") or {},
            }
    return _empty_metadata()


def _empty_metadata() -> dict:
    return {
        "verification": "UNVERIFIED", "independent_sources": 0,
        "upstream": [], "movement": {}, "betradar_consensus": False,
        "outlier_detected": False, "sources_count": 0, "opening": {},
    }


# ---------------------------------------------------------------------------
# Извлечение прогнозов (predictions)
# ---------------------------------------------------------------------------
def _get_predictions(match: dict) -> Optional[Tuple[float, float, float]]:
    pred = match.get("predictions", {})
    if not isinstance(pred, dict) or not pred:
        return None
    home_keys = ("home_win", "home", "1", "home_win_prob", "home_probability",
                 "home_win_probability", "h")
    draw_keys = ("draw", "X", "x", "draw_prob", "draw_probability", "d")
    away_keys = ("away_win", "away", "2", "away_win_prob", "away_probability",
                 "away_win_probability", "a")

    def _extract(keys):
        for k in keys:
            v = pred.get(k)
            if v is None:
                continue
            try:
                f = float(v)
                if f > 1.0:
                    return f / 100.0
                elif f > 0:
                    return f
            except (ValueError, TypeError):
                continue
        return None

    h = _extract(home_keys)
    d = _extract(draw_keys)
    a = _extract(away_keys)
    if h is not None and d is not None and a is not None:
        total = h + d + a
        if 0.5 < total < 1.5:
            if total > 1.01:
                return (h / total, d / total, a / total)
            return (h, d, a)
        elif 50 < total < 150:
            return (h / 100, d / 100, a / 100)

    inner = pred.get("prediction", pred.get("probabilities"))
    if isinstance(inner, dict) and inner:
        pred = inner
        h = _extract(home_keys)
        d = _extract(draw_keys)
        a = _extract(away_keys)
        if h is not None and d is not None and a is not None:
            total = h + d + a
            if 0.5 < total < 1.5:
                if total > 1.01:
                    return (h / total, d / total, a / total)
                return (h, d, a)
            elif 50 < total < 150:
                return (h / 100, d / 100, a / 100)
    return None


# ---------------------------------------------------------------------------
# Source display
# ---------------------------------------------------------------------------
def _source_display(src: str) -> str:
    if not src:
        return "unknown"
    s = src.lower().strip()
    if s == "sharpapi":
        return "SharpAPI"
    if s == "odds_api":
        return "OddsAPI"
    if s == "propline":
        return "PropLine"
    if s == "bzzoiro":
        return "Bzzoiro"
    if s == "football_data":
        return "football-data"
    return s


# ---------------------------------------------------------------------------
# SearchModule
# ---------------------------------------------------------------------------
class SearchModule:
    def __init__(self, value_threshold: float = VALUE_THRESHOLD):
        self.value_threshold = value_threshold

    def process(self, matches: Dict[str, dict]) -> dict:
        now = datetime.now(MSK_TZ)
        hot = []
        warm = []
        with_odds = 0
        with_pred = 0
        with_h2h = 0
        with_stats = 0
        value_bets = 0

        for cid, match in matches.items():
            if not isinstance(match, dict):
                continue
            o_h, o_d, o_a = _get_odds(match)
            if o_h <= 1.0 or o_d <= 1.0 or o_a <= 1.0:
                continue
            with_odds += 1

            dt = _dt_from_utc(match.get("date_utc", ""))
            if dt is None:
                continue
            if dt < now - timedelta(hours=2):
                continue
            if dt > now + timedelta(hours=48):
                continue

            pred = _get_predictions(match)
            has_pred = pred is not None
            if has_pred:
                with_pred += 1

            h2h = match.get("h2h", {})
            if isinstance(h2h, dict) and h2h:
                with_h2h += 1
            stats = match.get("stats", {})
            if isinstance(stats, dict) and stats:
                with_stats += 1

            # Метаданные odds (v700)
            odds_meta = _get_odds_metadata(match)
            is_verified = odds_meta["verification"] == "VERIFIED"
            independent_sources = odds_meta["independent_sources"]

            sources = match.get("sources", [])
            if not isinstance(sources, list) or not sources:
                sources = ["unknown"]
            # FIX-6: выбираем источник с лучшим рангом (минимальный priority)
            _priority_map = {"sharpapi": 1, "odds_api": 3, "propline": 6, "bzzoiro": 8, "football_data": 10}
            src_raw = min(sources, key=lambda s: _priority_map.get(s.lower().strip(), 99)) if sources else "unknown"

            imp_h = 1.0 / o_h
            imp_d = 1.0 / o_d
            imp_a = 1.0 / o_a
            imp_total = imp_h + imp_d + imp_a
            p_h = imp_h / imp_total
            p_d = imp_d / imp_total
            p_a = imp_a / imp_total

            value_side = None
            value_ev = 0.0
            value_odds = 0.0
            value_prob = 0.0

            if has_pred:
                ph_pred, pd_pred, pa_pred = pred
                ev_h = ph_pred * o_h - 1.0
                ev_d = pd_pred * o_d - 1.0
                ev_a = pa_pred * o_a - 1.0
                evs = [("HOME", ev_h, o_h, ph_pred),
                        ("DRAW", ev_d, o_d, pd_pred),
                        ("AWAY", ev_a, o_a, pa_pred)]
                best = max(evs, key=lambda x: x[1])
                if best[1] > self.value_threshold:
                    value_side = best[0]
                    value_ev = best[1]
                    value_odds = best[2]
                    value_prob = best[3]
                    value_bets += 1

            if value_side:
                v_side = value_side
                v_odds = value_odds
                v_prob = value_prob
                is_fire = True
            else:
                if has_pred:
                    ph_pred, pd_pred, pa_pred = pred
                    probs = [("HOME", o_h, ph_pred),
                             ("DRAW", o_d, pd_pred),
                             ("AWAY", o_a, pa_pred)]
                else:
                    probs = [("HOME", o_h, p_h),
                             ("DRAW", o_d, p_d),
                             ("AWAY", o_a, p_a)]
                best = max(probs, key=lambda x: x[2])
                v_side = best[0]
                v_odds = best[1]
                v_prob = best[2]
                is_fire = False

            comp_code = _get_competition_code(
                match.get("competition", ""),
                match.get("country", "")
            )
            date_str, time_str = _parse_date_msk(match.get("date_utc", ""))
            home_team = match.get("home_team", "?")
            away_team = match.get("away_team", "?")

            info = {
                "home_team": home_team,
                "away_team": away_team,
                "comp_code": comp_code,
                "date": date_str,
                "time": time_str,
                "dt": dt,
                "odds": (o_h, o_d, o_a),
                "probs": (p_h, p_d, p_a),
                "v_side": v_side,
                "v_odds": v_odds,
                "v_prob": v_prob,
                "value_ev": value_ev,
                "is_fire": is_fire,
                "source": src_raw,
                "source_display": _source_display(src_raw),
                "has_pred": has_pred,
                "odds_verification": odds_meta["verification"],
                "independent_sources": independent_sources,
            }

            # HOT = fire bet ИЛИ verified (2+ independent sources) ИЛИ sharpapi
            if is_fire or is_verified or "sharpapi" in [s.lower() for s in sources]:
                hot.append(info)
            else:
                warm.append(info)

        hot.sort(key=lambda x: (-int(x["is_fire"]), -x.get("value_ev", 0), x["dt"]))
        warm.sort(key=lambda x: x["dt"])
        hot = hot[:10]
        warm = warm[:10]

        return {
            "hot": hot,
            "warm": warm,
            "stats": {
                "total": len(matches),
                "with_odds": with_odds,
                "with_pred": with_pred,
                "with_h2h": with_h2h,
                "with_stats": with_stats,
                "value_bets": value_bets,
            },
        }

__version__ = "6.1"

__all__ = [
    "clean_team_name",
    "SearchModule",
    "VALUE_THRESHOLD",
    "__version__",
]
