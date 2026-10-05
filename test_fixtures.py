#!/usr/bin/env python3
"""
test_fixtures.py — Эталонные матчи для CI тестирования (§20.8).
Используются в redis_diagnostics --validate и CI пайплайне.

Каждый эталон — полный match dict в формате schema v710.
"""

__version__ = "8.11-patched"
__all__ = ["FIXTURES", "validate_fixture", "__version__"]

import json

FIXTURES = [
    {
        "canonical_id": "arsenal__chelsea__20261015",
        "home_clean": "arsenal",
        "away_clean": "chelsea",
        "home": "Arsenal",
        "away": "Chelsea",
        "date_utc": "2026-10-15T19:00:00Z",
        "source": "sharpapi",
        "sources": ["sharpapi"],
        "upstream": "betradar",
        "status": "upcoming",
        "competition": {
            "name": "Premier League",
            "country": "England",
            "code": "EPL"
        },
        "odds": {
            "1x2": {
                "home": 1.85,
                "draw": 3.60,
                "away": 4.20,
                "sources": ["sharpapi"],
                "upstream": "betradar"
            },
            "ou25": {
                "over": 1.95,
                "under": 1.95,
                "sources": ["sharpapi"]
            },
            "ah": {
                "home": 1.90,
                "away": 2.00,
                "handicap": -0.5,
                "sources": ["sharpapi"]
            }
        },
        "score": {
            "home": None,
            "away": None
        },
        "stats": {}
    },
    {
        "canonical_id": "real_madrid__barcelona__20261022",
        "home_clean": "real_madrid",
        "away_clean": "barcelona",
        "home": "Real Madrid",
        "away": "Barcelona",
        "date_utc": "2026-10-22T20:00:00Z",
        "source": "odds_api",
        "sources": ["odds_api"],
        "upstream": "betradar",
        "status": "upcoming",
        "competition": {
            "name": "La Liga",
            "country": "Spain",
            "code": "SP1"
        },
        "odds": {
            "1x2": {
                "home": 2.10,
                "draw": 3.40,
                "away": 3.20,
                "sources": ["odds_api"],
                "upstream": "betradar"
            },
            "ou25": {
                "over": 1.75,
                "under": 2.10,
                "sources": ["odds_api"]
            }
        },
        "score": {
            "home": None,
            "away": None
        },
        "stats": {}
    },
    {
        "canonical_id": "bayern_munich__borussia_dortmund__20261029",
        "home_clean": "bayern_munich",
        "away_clean": "borussia_dortmund",
        "home": "Bayern Munich",
        "away": "Borussia Dortmund",
        "date_utc": "2026-10-29T17:30:00Z",
        "source": "bzzoiro",
        "sources": ["bzzoiro"],
        "upstream": "opta",
        "status": "upcoming",
        "competition": {
            "name": "Bundesliga",
            "country": "Germany",
            "code": "BL"
        },
        "odds": {
            "1x2": {
                "home": 1.65,
                "draw": 4.00,
                "away": 5.00,
                "sources": ["bzzoiro"],
                "upstream": "opta"
            },
            "ou25": {
                "over": 1.50,
                "under": 2.60,
                "sources": ["bzzoiro"]
            },
            "ah": {
                "home": 1.85,
                "away": 2.05,
                "handicap": -1.0,
                "sources": ["bzzoiro"]
            }
        },
        "score": {
            "home": None,
            "away": None
        },
        "stats": {}
    }
]


def validate_fixture(fixture: dict) -> tuple:
    """
    Валидация эталонного матча (§20.8).
    Возвращает (is_valid, errors_list).
    """
    errors = []
    
    required_fields = ["canonical_id", "home_clean", "away_clean", "date_utc", "source"]
    for field in required_fields:
        if not fixture.get(field):
            errors.append(f"Missing required field: {field}")
    
    # canonical_id format: home__away__YYYYMMDD
    cid = fixture.get("canonical_id", "")
    if cid and "__" not in cid:
        errors.append(f"canonical_id format invalid: {cid}")
    
    # home_clean and away_clean must be non-empty
    if not fixture.get("home_clean"):
        errors.append("home_clean is empty")
    if not fixture.get("away_clean"):
        errors.append("away_clean is empty")
    
    # odds 1x2 must have numeric values
    odds_1x2 = fixture.get("odds", {}).get("1x2", {})
    for key in ["home", "draw", "away"]:
        val = odds_1x2.get(key)
        if val is not None and not isinstance(val, (int, float)):
            errors.append(f"odds.1x2.{key} must be numeric, got {type(val)}")
    
    return (len(errors) == 0, errors)


if __name__ == "__main__":
    for i, f in enumerate(FIXTURES):
        valid, errors = validate_fixture(f)
        status = "✅" if valid else "❌"
        print(f"{status} Fixture {i+1}: {f['canonical_id']}")
        for e in errors:
            print(f"  - {e}")
