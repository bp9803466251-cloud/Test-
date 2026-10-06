"""
test_fixtures.py — Канонические тестовые фикстуры GatekeeperAI v9.3-audited.
§20.8: 4 фикстуры для контрактных тестов и CI.
"""
import json
import os

__all__ = [
    "CANONICAL_LIVE_MATCH",
    "CANONICAL_HISTORY_MATCH",
    "CANONICAL_COMPLETED_MATCH",
    "CANONICAL_MINIMAL_MATCH",
    "validate_fixtures",
    "__version__",
]

__version__ = "9.3-audited"

CANONICAL_LIVE_MATCH = {
    "canonical_id": "man__fulham__20260921",
    "home_team": "Manchester United",
    "away_team": "Fulham",
    "home_clean": "man",
    "away_clean": "fulham",
    "date_utc": "2026-09-21T14:00:00Z",
    "competition": "Premier League",
    "country": "England",
    "status": "scheduled",
    "schema_version": "v710",
    "score": {},
    "odds": {
        "1x2": {
            "current": {"home": 1.85, "draw": 3.40, "away": 4.20},
            "sources": ["sharpapi"],
        }
    },
    "stats": {},
    "h2h": {},
    "predictions": {},
    "section_history": [
        {"section": "odds", "source": "sharpapi", "upstream": "betradar", "timestamp": "2026-09-21T10:00:00Z"}
    ],
    "sources": ["sharpapi"],
    "source": "sharpapi",
    "created_at": "2026-09-21T10:00:00Z",
    "updated_at": "2026-09-21T10:00:00Z",
}

CANONICAL_HISTORY_MATCH = {
    "canonical_id": "arsenal__chelsea__20251015",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "home_clean": "arsenal",
    "away_clean": "chelsea",
    "date_utc": "2025-10-15T16:30:00Z",
    "competition": "Premier League",
    "country": "England",
    "status": "completed",
    "schema_version": "v710",
    "score": {"home": 2, "away": 1, "ht_home": 1, "ht_away": 0},
    "odds": {
        "1x2": {
            "current": {"home": 1.90, "draw": 3.50, "away": 3.80},
            "sources": ["odds_api"],
        }
    },
    "stats": {"home_shots": 15, "away_shots": 8, "home_possession": 62, "away_possession": 38},
    "h2h": {},
    "predictions": {},
    "section_history": [
        {"section": "score", "source": "football_data", "upstream": "bet365", "timestamp": "2025-10-15T18:00:00Z"}
    ],
    "sources": ["football_data", "odds_api"],
    "source": "football_data",
    "created_at": "2025-10-15T12:00:00Z",
    "updated_at": "2025-10-15T18:00:00Z",
}

CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "liverpool__everton__20251105",
    "home_team": "Liverpool",
    "away_team": "Everton",
    "home_clean": "liverpool",
    "away_clean": "everton",
    "date_utc": "2025-11-05T17:30:00Z",
    "competition": "Premier League",
    "country": "England",
    "status": "completed",
    "schema_version": "v710",
    "score": {"home": 3, "away": 0, "ht_home": 2, "ht_away": 0},
    "odds": {},
    "stats": {},
    "h2h": {},
    "predictions": {},
    "section_history": [
        {"section": "score", "source": "football_data", "upstream": "bet365", "timestamp": "2025-11-05T19:00:00Z"}
    ],
    "sources": ["football_data"],
    "source": "football_data",
    "created_at": "2025-11-05T15:00:00Z",
    "updated_at": "2025-11-05T19:00:00Z",
}

CANONICAL_MINIMAL_MATCH = {
    "canonical_id": "spurs__newcastle__20261201",
    "home_team": "Tottenham",
    "away_team": "Newcastle",
    "home_clean": "tottenham",
    "away_clean": "newcastle",
    "date_utc": "2026-12-01T20:00:00Z",
    "status": "scheduled",
    "schema_version": "v710",
    "section_history": [],
    "sources": [],
    "source": "unknown",
}


def validate_fixtures():
    """Валидация всех 4 фикстур. Возвращает (True, []) или (False, [errors])."""
    errors = []
    fixtures = [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
        ("CANONICAL_MINIMAL_MATCH", CANONICAL_MINIMAL_MATCH),
    ]
    for name, fixture in fixtures:
        if not isinstance(fixture, dict):
            errors.append(f"{name} is not dict")
            continue
        for field in ["canonical_id", "home_clean", "away_clean", "date_utc", "status", "schema_version"]:
            if field not in fixture:
                errors.append(f"{name}: missing {field}")
            elif not fixture[field]:
                errors.append(f"{name}: empty {field}")
        if fixture.get("schema_version") != "v710":
            errors.append(f"{name}: schema_version != v710")
        # section_history entries should have upstream (except minimal)
        if name != "CANONICAL_MINIMAL_MATCH":
            sh = fixture.get("section_history", [])
            if sh:
                for entry in sh:
                    if isinstance(entry, dict) and not entry.get("upstream"):
                        errors.append(f"{name}: section_history entry missing upstream")
    if errors:
        return False, errors
    return True, []
