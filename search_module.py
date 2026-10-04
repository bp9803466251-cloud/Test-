"""
search_module.py — Модуль поиска и нормализации команд.
Импортирует clean_team_name из team_registry (§19.2).
Устраняет циклическую зависимость между hub и search.

v8.11-patched:
  FIX-1: build_canonical_id fallback — None → локальная реализация
  FIX-2: normalize_team_name re-export для backward compat коллекторов
  FIX-3: search_teams — fuzzy fallback при 0 точных + 0 partial совпадений
  FIX-4: find_match_candidates — reverse (home↔away) fallback
  FIX-5: __version__ в __all__ исправлен (был строкой, не переменной)
"""

import logging

from team_registry import clean_team_name, TEAM_ALIASES

__version__ = "8.11-patched"

logger = logging.getLogger(__name__)

# build_canonical_id — реэкспорт из team_registry (с локальным fallback)
try:
    from team_registry import build_canonical_id
except ImportError:
    logger.debug("build_canonical_id недоступен в team_registry, используем локальный")

    def build_canonical_id(home, away, date_str):
        """Локальный fallback: home__away__YYYYMMDD."""
        h = clean_team_name(home) if home else ""
        a = clean_team_name(away) if away else ""
        d = (date_str or "")[:10].replace("-", "")
        if not h or not a or len(d) != 8 or not d.isdigit():
            return ""
        return f"{h}__{a}__{d}"


# normalize_team_name — re-export для backward compat
# Некоторые коллекторы импортируют normalize_team_name из search_module
try:
    from team_registry import normalize_team_name
except ImportError:
    # Fallback: normalize_team_name = clean_team_name (синоним)
    normalize_team_name = clean_team_name


# Реэкспорт для удобства (коллекторы импортируют из search_module)
__all__ = [
    "clean_team_name",
    "normalize_team_name",
    "TEAM_ALIASES",
    "search_teams",
    "find_match_candidates",
    "build_canonical_id",
    "__version__",
]


def search_teams(query, all_teams):
    """
    Поиск команд по частичному совпадению.
    query — строка поиска
    all_teams — список названий команд
    Возвращает список совпадений (пустой список при ошибке).
    Точные совпадения по clean_team_name идут первыми,
    partial — по подстроке, fuzzy — по нормализованному ключу.
    """
    try:
        if not query or not all_teams:
            return []
        if not isinstance(all_teams, (list, tuple)):
            return []
        q = query.strip().lower()
        clean_q = clean_team_name(query)
        exact = []
        partial = []
        fuzzy = []
        for team in all_teams:
            if not isinstance(team, str):
                continue
            clean_t = clean_team_name(team)
            if clean_q == clean_t:
                exact.append(team)
            elif q in team.lower():
                partial.append(team)
            elif clean_q and clean_t and clean_q in clean_t:
                fuzzy.append(team)
        result = exact + partial + fuzzy
        # Дедупликация с сохранением порядка
        seen = set()
        deduped = []
        for t in result:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped
    except Exception as e:
        logger.warning("search_teams error: %s", e)
        return []


def find_match_candidates(home, away, matches):
    """
    Поиск кандидатов матча по нормализованным именам.
    matches — dict {canonical_id: match_obj}
    Возвращает список canonical_id (пустой список при ошибке).
    Результаты сортируются по дате (ближайшие первыми).
    Fallback: поиск с reversed home↔away (на случай перепутанных сторон).
    """
    try:
        if not home or not away or not matches:
            return []
        if not isinstance(matches, dict):
            return []
        clean_home = clean_team_name(home)
        clean_away = clean_team_name(away)
        candidates = []
        for cid, match in matches.items():
            if not isinstance(match, dict):
                continue
            m_home = match.get("home_clean", "")
            m_away = match.get("away_clean", "")
            # Прямое совпадение
            if m_home == clean_home and m_away == clean_away:
                date_str = match.get("date_utc", "")
                candidates.append((date_str, cid, 0))  # priority 0 = exact
            # Reverse fallback (home↔away перепутаны)
            elif m_home == clean_away and m_away == clean_home:
                date_str = match.get("date_utc", "")
                candidates.append((date_str, cid, 1))  # priority 1 = reversed
        # Сортировка: по дате, затем по priority (exact перед reversed)
        candidates.sort(key=lambda x: (x[0], x[2]))
        return [cid for _, cid, _ in candidates]
    except Exception as e:
        logger.warning("find_match_candidates error: %s", e)
        return []
