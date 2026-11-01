# search_module.py
"""
Gatekeeper-AI Ecosystem: Модуль поиска и нормализации команд.
Импортирует clean_team_name из team_registry (§19.2).
Устраняет циклическую зависимость между hub и search.
"""

import logging
from team_registry import clean_team_name, TEAM_ALIASES

__version__ = "9.3-audited"  # Контрактная строка версии для прохождения тестов

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

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
try:
    from team_registry import normalize_team_name
except ImportError:
    normalize_team_name = clean_team_name

def save_search_results(results, namespace=""):
    """Реэкспорт save_search_results из gatekeeper_hub с защитой сигнатуры аргументов."""
    try:
        from gatekeeper_hub import save_search_results as _hub_save
        # Защита сигнатуры: вызываем метод хаба без передачи неиспользуемого namespace
        return _hub_save(results)
    except Exception as e:
        logger.warning(f"gatekeeper_hub недоступен или ошибка вызова save_search_results: {e}")
        return False

# Реэкспорт интерфейсов
__all__ = [
    "clean_team_name",
    "normalize_team_name",
    "TEAM_ALIASES",
    "search_teams",
    "find_match_candidates",
    "save_search_results",
    "build_canonical_id",
    "__version__",
]

def search_teams(query, all_teams):
    """Поиск команд в пуле по точному, частичному или fuzzy совпадению."""
    try:
        if not query or not all_teams:
            return []
        if not isinstance(all_teams, (list, tuple)):
            return []
        q = query.strip().lower()
        clean_q = clean_team_name(query)
        exact, partial, fuzzy = [], [], []
        
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
    """Поиск кандидатов матча по нормализованным именам с reverse-подменой сторон."""
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
                candidates.append((date_str, cid, 0))
            elif m_home == clean_away and m_away == clean_home:
                date_str = match.get("date_utc", "")
                candidates.append((date_str, cid, 1))
                
        candidates.sort(key=lambda x: (x[0], x[2]))
        return [cid for _, cid, _ in candidates]
    except Exception as e:
        logger.warning("find_match_candidates error: %s", e)
        return []
      
