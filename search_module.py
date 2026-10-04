"""
search_module.py — Модуль поиска и нормализации команд.
Импортирует clean_team_name из team_registry (§19.2).
Устраняет циклическую зависимость между hub и search.
"""

from team_registry import clean_team_name, TEAM_ALIASES

# Реэкспорт для удобства (коллекторы импортируют из search_module)
__all__ = ["clean_team_name", "TEAM_ALIASES", "search_teams", "find_match_candidates"]


def search_teams(query: str, all_teams: list) -> list:
    """
    Поиск команд по частичному совеарию.
    query — строка поиска
    all_teams — список названий команд
    Возвращает список совпадений.
    """
    if not query:
        return []
    q = query.strip().lower()
    clean_q = clean_team_name(query)
    results = []
    for team in all_teams:
        clean_t = clean_team_name(team)
        if q in team.lower() or clean_q == clean_t:
            results.append(team)
    return results


def find_match_candidates(home: str, away: str, matches: dict) -> list:
    """
    Поиск кандидатов матча по нормализованным именам.
    matches — dict {canonical_id: match_obj}
    Возвращает список canonical_id.
    """
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
