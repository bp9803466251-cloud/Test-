"""
search_module.py — Модуль поиска и нормализации команд.
Импортирует clean_team_name из team_registry (§19.2).
Устраняет циклическую зависимость между hub и search.
"""

from team_registry import clean_team_name, TEAM_ALIASES

__version__ = "8.10-patched"

# Реэкспорт для удобства (коллекторы импортируют из search_module)
__all__ = ["clean_team_name", "TEAM_ALIASES", "search_teams", "find_match_candidates", "__version__"]


def search_teams(query, all_teams):
    """
    Поиск команд по частичному совпадению.
    query — строка поиска
    all_teams — список названий команд
    Возвращает список совпадений (пустой список при ошибке).
    """
    try:
        if not query or not all_teams:
            return []
        if not isinstance(all_teams, (list, tuple)):
            return []
        q = query.strip().lower()
        clean_q = clean_team_name(query)
        results = []
        for team in all_teams:
            if not isinstance(team, str):
                continue
            clean_t = clean_team_name(team)
            if q in team.lower() or clean_q == clean_t:
                results.append(team)
        return results
    except Exception:
        return []


def find_match_candidates(home, away, matches):
    """
    Поиск кандидатов матча по нормализованным именам.
    matches — dict {canonical_id: match_obj}
    Возвращает список canonical_id (пустой список при ошибке).
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
                candidates.append(cid)
        return candidates
    except Exception:
        return []
