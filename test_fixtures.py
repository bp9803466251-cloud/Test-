# test_fixtures.py
"""
Gatekeeper-AI Ecosystem: Канонические фикстуры для контрактов.
Версия v9.6.1-audited — Полная синхронизация со схемой v710 required keys.
"""

import json

CANONICAL_LIVE_MATCH = {
    "canonical_id": "man__fulham__20260921",
    "home_team": "Manchester United",
    "away_team": "Fulham",
    "home_clean": "man",
    "away_clean": "fulham",
    "date_utc": "2026-09-21T14:00:00Z",
    "status": "scheduled",
    "competition": "Premier League",
    "league_code": "E0",
    "country": "England",
    "season": "2627",
    "schema_version": "v710",
    "score": {"home": None, "away": None},
    "odds": {
        "1x2": {
            "current": {"home": 1.85, "draw": 3.40, "away": 4.20},
            "sources": [
                {"source": "sharpapi", "upstream": "betradar", "price": {"home": 1.85, "draw": 3.40, "away": 4.20}}
            ]
        }
    },
    "predictions": {
        "home_win": 55,
        "draw": 25,
        "away_win": 20
    },
    "stats": {
        "xg_home": 1.65,
        "xg_away": 1.10
    },
    "sources": ["sharpapi", "bzzoiro"],
}

CANONICAL_HISTORY_MATCH = {
    "canonical_id": "arsenal__chelsea__20251015",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "home_clean": "arsenal",
    "away_clean": "chelsea",
    "date_utc": "2025-10-15T16:30:00Z",
    "status": "completed",
    "competition": "Premier League",
    "league_code": "E0",
    "country": "England",
    "season": "2526",
    "schema_version": "v710",
    "score": {"home": 2, "away": 1},
    "sources": ["football_data"]
}

CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "liverpool__everton__20251004",
    "home_team": "Liverpool",
    "away_team": "Everton",
    "home_clean": "liverpool",
    "away_clean": "everton",
    "date_utc": "2025-10-04T12:30:00Z",
    "status": "completed",
    "competition": "Premier League",
    "league_code": "E0",
    "country": "England",
    "season": "2526",
    "schema_version": "v710",
    "score": {"home": 3, "away": 0},
    "odds": {
        "1x2": {
            "current": {"home": 1.50, "draw": 4.50, "away": 6.00}
        }
    },
    "sources": ["odds_api"]
}

CANONICAL_MINIMAL_MATCH = {
    "canonical_id": "barcelona__real_madrid__20261025",
    "home_team": "Barcelona",
    "away_team": "Real Madrid",
    "home_clean": "barcelona",
    "away_clean": "real_madrid",
    "date_utc": "2026-10-25T19:00:00Z",
    "status": "scheduled",
    "competition": "La Liga",
    "league_code": "SP1",
    "country": "Spain",
    "season": "2627",
    "schema_version": "v710",
    "score": {"home": None, "away": None},
    "sources": []
}

def validate_fixtures():
    """Валидирует все 4 фикстуры на строгое соответствие JSON Schema v710."""
    errors = []
    fixtures = [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
        ("CANONICAL_MINIMAL_MATCH", CANONICAL_MINIMAL_MATCH),
    ]
    required = ["canonical_id", "home_team", "away_team", "home_clean", "away_clean", "date_utc", "status", "competition", "schema_version"]
    valid_statuses = ["scheduled", "live", "completed", "postponed", "cancelled"]
    
    for name, fixture in fixtures:
        for field in required:
            if field not in fixture:
                errors.append(f"{name}: missing {field}")
        if fixture.get("status") not in valid_statuses:
            errors.append(f"{name}: invalid status {fixture.get('status')}")
        if not fixture.get("canonical_id"):
            errors.append(f"{name}: empty canonical_id")
        if "source" in fixture:
            errors.append(f"{name}: root 'source' field is forbidden by rule §1.14")
            
    return (len(errors) == 0, errors)

__all__ = [
    "CANONICAL_LIVE_MATCH", "CANONICAL_HISTORY_MATCH",
    "CANONICAL_COMPLETED_MATCH", "CANONICAL_MINIMAL_MATCH",
    "validate_fixtures",
]
