#!/usr/bin/env python3
"""
test_fixtures.py — Эталонные матчи для CI тестирования (§20.8).
Используются в redis_diagnostics --validate и CI пайплайне.

Каждый эталон — полный match dict в формате schema v710.
"""

__version__ = "8.11-patched"
# FIX-AUDIT-v9.3: CANONICAL_* exports for test_contracts.py (§20.8)
__all__ = ["FIXTURES", "validate_fixture", "validate_fixtures",
           "CANONICAL_LIVE_MATCH", "CANONICAL_HISTORY_MATCH",
           "CANONICAL_COMPLETED_MATCH", "CANONICAL_MINIMAL_MATCH",
           "__version__"]

import json

FIXTURES = [
    {
        "canonical_id": "arsenal__chelsea__20261015",
        "home_clean": "arsenal",
        "away_clean": "chelsea",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "date_utc": "2026-10-15T19:00:00Z",
        "source": "sharpapi",
        "sources": ["sharpapi"],
        "upstream": "betradar",
        "status": "upcoming",
        "competition": "Premier League",
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
        "home_team": "Real Madrid",
        "away_team": "Barcelona",
        "date_utc": "2026-10-22T20:00:00Z",
        "source": "odds_api",
        "sources": ["odds_api"],
        "upstream": "betradar",
        "status": "upcoming",
        "competition": "La Liga",
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
        "home_team": "Bayern Munich",
        "away_team": "Borussia Dortmund",
        "date_utc": "2026-10-29T17:30:00Z",
        "source": "bzzoiro",
        "sources": ["bzzoiro"],
        "upstream": "opta",
        "status": "upcoming",
        "competition": "Bundesliga",
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



# ═══════════════════════════════════════════════════════════
# FIX-AUDIT-v9.3: CANONICAL_* match objects (§20.8)
# Full match dicts in schema v710 format for contract tests.
# ═══════════════════════════════════════════════════════════

CANONICAL_LIVE_MATCH = {
    "canonical_id": "arsenal__chelsea__20261015",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "home_clean": "arsenal",
    "away_clean": "chelsea",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-10-15T19:00:00Z",
    "status": "upcoming",
    "version": 1,
    "schema_version": "v710",
    "source": "sharpapi",
    "sources": ["sharpapi"],
    "source_ids": {"sharpapi": "ev_12345"},
    "upstream": "betradar",
    "created_at": "2026-10-06T12:00:00+03:00",
    "updated_at": "2026-10-06T12:00:00+03:00",
    "odds": {
        "1x2": {
            "opening": {"home": 1.85, "draw": 3.60, "away": 4.20},
            "current": {"home": 1.80, "draw": 3.70, "away": 4.30},
            "sources": ["sharpapi"],
            "upstream": "betradar",
        }
    },
    "score": {"home": None, "away": None},
    "stats": {},
    "section_history": [
        {"section": "base", "source": "sharpapi", "updated_at": "2026-10-06T12:00:00+03:00"}
    ],
}

CANONICAL_HISTORY_MATCH = {
    "canonical_id": "liverpool__man_city__20260920",
    "home_team": "Liverpool",
    "away_team": "Manchester City",
    "home_clean": "liverpool",
    "away_clean": "man_city",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-09-20T17:00:00Z",
    "status": "finished",
    "version": 3,
    "schema_version": "v710",
    "source": "football_data",
    "sources": ["football_data", "sharpapi"],
    "source_ids": {"football_data": "E0_123", "sharpapi": "ev_67890"},
    "upstream": "bet365",
    "created_at": "2026-09-15T10:00:00+03:00",
    "updated_at": "2026-09-21T10:00:00+03:00",
    "odds": {
        "1x2": {
            "opening": {"home": 2.10, "draw": 3.50, "away": 3.20},
            "closing": {"home": 2.00, "draw": 3.60, "away": 3.40},
            "sources": ["football_data", "sharpapi"],
        }
    },
    "score": {"home": 2, "away": 1},
    "stats": {"home_shots": 15, "away_shots": 8},
    "section_history": [
        {"section": "base", "source": "football_data", "updated_at": "2026-09-15T10:00:00+03:00"},
        {"section": "odds", "source": "sharpapi", "updated_at": "2026-09-20T09:00:00+03:00"},
        {"section": "score", "source": "football_data", "updated_at": "2026-09-21T10:00:00+03:00"},
    ],
}

CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "real_madrid__barcelona__20261002",
    "home_team": "Real Madrid",
    "away_team": "Barcelona",
    "home_clean": "real_madrid",
    "away_clean": "barcelona",
    "competition": "La Liga",
    "country": "Spain",
    "date_utc": "2026-10-02T20:00:00Z",
    "status": "finished",
    "version": 2,
    "schema_version": "v710",
    "source": "bzzoiro",
    "sources": ["bzzoiro"],
    "source_ids": {"bzzoiro": "m_54321"},
    "upstream": "opta",
    "created_at": "2026-09-28T08:00:00+03:00",
    "updated_at": "2026-10-03T08:00:00+03:00",
    "odds": {
        "1x2": {
            "opening": {"home": 1.90, "draw": 3.80, "away": 3.50},
            "closing": {"home": 1.95, "draw": 3.70, "away": 3.60},
            "sources": ["bzzoiro"],
        }
    },
    "score": {"home": 3, "away": 2},
    "stats": {"home_shots": 18, "away_shots": 11},
    "section_history": [
        {"section": "base", "source": "bzzoiro", "updated_at": "2026-09-28T08:00:00+03:00"},
        {"section": "score", "source": "bzzoiro", "updated_at": "2026-10-03T08:00:00+03:00"},
    ],
}

CANONICAL_MINIMAL_MATCH = {
    "canonical_id": "bayern_munich__dortmund__20261105",
    "home_team": "Bayern Munich",
    "away_team": "Borussia Dortmund",
    "home_clean": "bayern_munich",
    "away_clean": "dortmund",
    "competition": "Bundesliga",
    "country": "Germany",
    "date_utc": "2026-11-05T18:30:00Z",
    "status": "scheduled",
    "version": 1,
    "schema_version": "v710",
    "source": "odds_api",
    "sources": ["odds_api"],
    "source_ids": {"odds_api": "evt_99999"},
    "upstream": "betradar",
    "created_at": "2026-10-06T12:00:00+03:00",
    "updated_at": "2026-10-06T12:00:00+03:00",
    "odds": {
        "1x2": {
            "opening": {"home": 1.70, "draw": 4.00, "away": 4.50},
            "sources": ["odds_api"],
        }
    },
    "score": None,
    "stats": {},
    "section_history": [
        {"section": "base", "source": "odds_api", "updated_at": "2026-10-06T12:00:00+03:00"}
    ],
}


def validate_fixtures():
    """FIX-AUDIT-v9.3: Валидация всех CANONICAL_* фикстур (§20.8).
    Возвращает True если все фикстуры валидны."""
    from gatekeeper_hub import validate_schema
    all_fixtures = [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
        ("CANONICAL_MINIMAL_MATCH", CANONICAL_MINIMAL_MATCH),
    ]
    for name, fixture in all_fixtures:
        ok, msg = validate_schema(fixture)
        if not ok:
            print(f"FAIL: {name}: {msg}")
            return False
    return True


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
