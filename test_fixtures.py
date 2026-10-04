"""
test_fixtures.py — Эталонные объекты для тестов (§20.8).
Канонические объекты матчей для валидации схемы.
"""

import json
from gatekeeper_hub import validate_schema if hasattr(__import__("gatekeeper_hub"), "validate_schema") else None

# Канонический live-матч (полный валидный объект)
CANONICAL_LIVE_MATCH = {
    "canonical_id": "man__fulham__20260921",
    "home_team": "Manchester United",
    "away_team": "Fulham",
    "home_clean": "man",
    "away_clean": "fulham",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-09-21T18:00:00Z",
    "status": "scheduled",
    "score": None,
    "version": 1,
    "schema_version": "v710",
    "sources": ["sharpapi"],
    "source_ids": {"sharpapi": "evt_123"},
    "odds": {
        "1x2": {
            "current": {"home": "1.85", "draw": "3.60", "away": "2.10"},
            "opening": {"home": "1.80", "draw": "3.50", "away": "2.20"},
            "best": {"home": "1.85", "draw": "3.60", "away": "2.20"},
            "sources": [
                {
                    "source": "sharpapi",
                    "upstream": "betradar",
                    "price": {"home": "1.85", "draw": "3.60", "away": "2.10"},
                    "timestamp": "2026-09-30T14:09:29+03:00",
                    "type": "live"
                }
            ]
        }
    },
    "predictions": None,
    "stats": None,
    "h2h": None,
    "created_at": "2026-09-30T14:00:00+03:00",
    "updated_at": "2026-09-30T14:09:29+03:00",
    "section_history": [
        {"section": "base", "source": "sharpapi", "updated_at": "2026-09-30T14:00:00+03:00"},
        {"section": "odds", "source": "sharpapi", "updated_at": "2026-09-30T14:09:29+03:00"}
    ]
}

# Канонический history-матч (завершённый)
CANONICAL_HISTORY_MATCH = {
    "canonical_id": "man__arsenal__20260115",
    "home_team": "Manchester United",
    "away_team": "Arsenal",
    "home_clean": "man",
    "away_clean": "arsenal",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-01-15T17:30:00Z",
    "status": "completed",
    "score": {"home": 2, "away": 1},
    "version": 1,
    "schema_version": "v710",
    "sources": ["football_data"],
    "source_ids": {"football_data": "E0_20260115_MAN_ARS"},
    "season": "2526",
    "league_code": "E0",
    "csv_raw": {
        "B365H": "1.85",
        "B365D": "3.60",
        "B365A": "4.20",
        "FTHG": 2,
        "FTAG": 1,
        "FTR": "H"
    },
    "odds": {
        "1x2": {
            "opening": {"home": "1.85", "draw": "3.60", "away": "4.20"},
            "sources": [
                {
                    "source": "football_data",
                    "upstream": "bet365",
                    "price": {"home": "1.85", "draw": "3.60", "away": "4.20"},
                    "timestamp": "2026-01-15T15:00:00+03:00",
                    "type": "pre_match"
                }
            ]
        }
    }
}

# Канонический матч в процессе миграции live -> history
CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "liverpool__chelsea__20261001",
    "home_team": "Liverpool",
    "away_team": "Chelsea",
    "home_clean": "liverpool",
    "away_clean": "chelsea",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-10-01T19:00:00Z",
    "status": "completed",
    "score": {"home": 3, "away": 0},
    "version": 5,
    "schema_version": "v710",
    "sources": ["sharpapi", "odds_api", "bzzoiro"],
    "source_ids": {
        "sharpapi": "evt_456",
        "odds_api": "oa_789",
        "bzzoiro": "bz_012"
    },
    "predictions": {
        "source": "bzzoiro",
        "home_win": 0.65,
        "draw": 0.20,
        "away_win": 0.15
    },
    "stats": {
        "xg_home": 2.8,
        "xg_away": 0.3,
        "possession_home": 62,
        "possession_away": 38
    },
    "h2h": {
        "source": "bzzoiro",
        "total_meetings": 45,
        "home_wins": 20,
        "draws": 12,
        "away_wins": 13
    }
}


def validate_fixtures():
    """Валидация эталонных объектов. Используется в CI."""
    try:
        from gatekeeper_hub import validate_schema
    except ImportError:
        print("[FIXTURES] gatekeeper_hub not available, skipping validation")
        return True

    fixtures = [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
    ]

    all_valid = True
    for name, fixture in fixtures:
        if validate_schema(fixture):
            print(f"[FIXTURES] {name}: VALID")
        else:
            print(f"[FIXTURES] {name}: INVALID")
            all_valid = False

    return all_valid


if __name__ == "__main__":
    validate_fixtures()


__all__ = [
    "CANONICAL_LIVE_MATCH",
    "CANONICAL_HISTORY_MATCH",
    "CANONICAL_COMPLETED_MATCH",
    "validate_fixtures",
]
