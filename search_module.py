"""
search_module.py — Модуль поиска и нормализации команд.
Импортирует clean_team_name из team_registry (§19.2).
Устраняет циклическую зависимость между hub и search.

v8.10-patched:
  - logging добавлен для отладки
  - search_teams: сортировка результатов (точные совпадения первыми)
  - find_match_candidates: сортировка по дате (ближайшие первыми)
  - build_canonical_id реэкспорт (с fallback) для удобства коллекторов
"""

import logging

from team_registry import clean_team_name, TEAM_ALIASES

__version__ = "8.10-patched"

logger = logging.getLogger(__name__)

# Реэкспорт для удобства (коллекторы импортируют из search_module)
__all__ = [
    "clean_team_name",
    "TEAM_ALIASES",
    "search_teams",
    "find_match_candidates",
    "build_canonical_id",
    "__version__",
]

# build_canonical_id — реэкспорт из team_registry (если доступен)
try:
    from team_registry import build_canonical_id
except ImportError:
    build_canonical_id = None
    logger.debug("build_canonical_id недоступен в team_registry")


def search_teams(query, all_teams):
    """
    Поиск команд по частичному совпадению.
    query — строка поиска
    all_teams — список названий команд
    Возвращает список совпадений (пустой список при ошибке).
    Точные совпадения по clean_team_name идут первыми.
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
        for team in all_teams:
            if not isinstance(team, str):
                continue
            clean_t = clean_team_name(team)
            if clean_q == clean_t:
                exact.append(team)
            elif q in team.lower():
                partial.append(team)
        return exact + partial
    except Exception as e:
        logger.warning("search_teams error: %s", e)
        return []


def find_match_candidates(home, away, matches):
    """
    Поиск кандидатов матча по нормализованным именам.
    matches — dict {canonical_id: match_obj}
    Возвращает список canonical_id (пустой список при ошибке).
    Результаты сортируются по дате (ближайшие первыми).
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
            if m_home == clean_home and m_away == clean_away:
                date_str = match.get("date_utc", "")
                candidates.append((date_str, cid))
        # Сортировка по дате (ближайшие первыми)
        candidates.sort(key=lambda x: x[0])
        return [cid for _, cid in candidates]
    except Exception as e:
        logger.warning("find_match_candidates error: %s", e)
        return []
