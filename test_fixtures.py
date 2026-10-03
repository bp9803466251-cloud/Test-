"""
Test Fixtures — эталонные match-объекты для контрактных тестов (§20.8)
=====================================================================
4 эталонных объекта:
  - CANONICAL_LIVE_MATCH       — live-матч с odds
  - CANONICAL_HISTORY_MATCH    — исторический матч
  - CANONICAL_COMPLETED_MATCH  — матч в процессе миграции live → history
  - CANONICAL_EMPTY_MATCH      — матч с минимальным набором полей
"""

CANONICAL_LIVE_MATCH = {
    "canonical_id": "manchester_united__tottenham_hotspur__20261015",
    "home_team": "Manchester United",
    "away_team": "Tottenham Hotspur",
    "date_utc": "2026-10-15T20:00:00",
    "competition": "Premier League",
    "country": "England",
    "source": "sharpapi",
    "schema_version": "v710",
    "status": "scheduled",
    "sources": ["sharpapi"],
    "source_ids": {"sharpapi": "sa_12345"},
    "score": {"home": 0, "away": 0},
    "odds": {
        "1x2": {
            "current": {"home": "1.85", "draw": "3.60", "away": "2.10"},
            "opening": {"home": "1.80", "draw": "3.50", "away": "2.20"},
            "best":    {"home": "1.85", "draw": "3.60", "away": "2.20"},
            "sources": [
                {
                    "source": "sharpapi",
                    "upstream": "betradar",
                    "price": {"home": "1.85", "draw": "3.60", "away": "2.10"},
                    "timestamp": "2026-10-03T14:09:29+03:00",
                    "type": "prematch"
                }
            ]
        }
    },
    "predictions": {},
    "h2h": {},
    "stats": {},
    "updated_at": "2026-10-03T14:09:29+03:00",
    "section_history": {},
}

CANONICAL_HISTORY_MATCH = {
    "canonical_id": "liverpool__chelsea__20260920",
    "home_team": "Liverpool",
    "away_team": "Chelsea",
    "date_utc": "2026-09-20T17:30:00",
    "competition": "Premier League",
    "country": "England",
    "source": "football_data",
    "schema_version": "v710",
    "status": "completed",
    "sources": ["football_data", "sharpapi", "odds_api"],
    "source_ids": {"football_data": "fd_001"},
    "score": {"home": 2, "away": 1},
    "odds": {
        "1x2": {
            "current": {"home": "1.90", "draw": "3.40", "away": "2.05"},
            "opening": {"home": "1.85", "draw": "3.50", "away": "2.10"},
            "best":    {"home": "1.90", "draw": "3.40", "away": "2.10"},
            "sources": [
                {
                    "source": "football_data",
                    "upstream": "bet365",
                    "price": {"home": "1.90", "draw": "3.40", "away": "2.05"},
                    "timestamp": "2026-09-20T15:00:00+03:00",
                    "type": "closing"
                }
            ]
        }
    },
    "predictions": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20},
    "h2h": {"last_5_home": 3, "last_5_draw": 1, "last_5_away": 1},
    "stats": {"hs": 12, "as": 7, "hc": 5, "ac": 3, "possession_home": 58},
    "updated_at": "2026-09-20T19:30:00+03:00",
    "section_history": {},
}

CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "arsenal__manchester_city__20261001",
    "home_team": "Arsenal",
    "away_team": "Manchester City",
    "date_utc": "2026-10-01T21:00:00",
    "competition": "Premier League",
    "country": "England",
    "source": "sharpapi",
    "schema_version": "v710",
    "status": "completed",
    "sources": ["sharpapi", "odds_api", "bzzoiro"],
    "source_ids": {"sharpapi": "sa_999", "bzzoiro": "bz_456"},
    "score": {"home": 1, "away": 1},
    "odds": {
        "1x2": {
            "current": {"home": "2.00", "draw": "3.20", "away": "1.90"},
            "opening": {"home": "2.10", "draw": "3.30", "away": "1.80"},
            "best":    {"home": "2.00", "draw": "3.20", "away": "1.90"},
            "sources": [
                {
                    "source": "sharpapi",
                    "upstream": "betradar",
                    "price": {"home": "2.00", "draw": "3.20", "away": "1.90"},
                    "timestamp": "2026-10-01T20:55:00+03:00",
                    "type": "live"
                }
            ]
        }
    },
    "predictions": {},
    "h2h": {"last_5_home": 2, "last_5_draw": 2, "last_5_away": 1},
    "stats": {"hs": 15, "as": 10, "hc": 6, "ac": 4},
    "updated_at": "2026-10-01T23:00:00+03:00",
    "section_history": {
        "odds": [
            {"source": "sharpapi", "timestamp": "2026-10-01T20:55:00+03:00"},
            {"source": "odds_api", "timestamp": "2026-10-01T20:56:00+03:00"},
        ]
    },
}

CANONICAL_EMPTY_MATCH = {
    "canonical_id": "team_a__team_b__20261101",
    "home_team": "Team A",
    "away_team": "Team B",
    "date_utc": "",
    "competition": "Unknown",
    "country": "Unknown",
    "source": "sharpapi",
    "schema_version": "v710",
    "status": "scheduled",
    "sources": ["sharpapi"],
    "source_ids": {},
    "score": {"home": 0, "away": 0},
    "odds": {},
    "predictions": {},
    "h2h": {},
    "stats": {},
    "updated_at": "2026-10-03T10:00:00+03:00",
    "section_history": {},
}

# ── Утилиты для тестов ────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "canonical_id", "home_team", "away_team", "date_utc",
    "competition", "country", "source", "schema_version",
    "status", "sources",
]

OPTIONAL_FIELDS = [
    "source_ids", "score", "odds", "predictions", "h2h", "stats",
    "updated_at", "section_history", "_provenance", "_run_id", "_metrics",
]

VALID_STATUSES = [
    "scheduled", "live", "completed", "cancelled",
    "postponed", "interrupted", "archived",
]

VALID_SOURCES = [
    "sharpapi", "odds_api", "bzzoiro", "propline",
    "football_data", "import",
]


def assert_required_fields(match: dict) -> list[str]:
    """Возвращает список отсутствующих обязательных полей."""
    return [f for f in REQUIRED_FIELDS if f not in match]


def assert_valid_status(match: dict) -> bool:
    """Проверяет, что status — допустимое значение."""
    return match.get("status") in VALID_STATUSES


def assert_valid_source(match: dict) -> bool:
    """Проверяет, что source — допустимое значение."""
    return match.get("source") in VALID_SOURCES


def assert_odds_format(match: dict) -> bool:
    """Проверяет, что odds (если есть) в формате 1x2 (§1.21)."""
    odds = match.get("odds", {})
    if not odds:
        return True
    if "1x2" not in odds:
        return False
    current = odds["1x2"].get("current", {})
    return all(k in current for k in ("home", "draw", "away"))


if __name__ == "__main__":
    fixtures = [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
        ("CANONICAL_EMPTY_MATCH", CANONICAL_EMPTY_MATCH),
    ]
    failed = 0
    for name, fixture in fixtures:
        missing = assert_required_fields(fixture)
        status_ok = assert_valid_status(fixture)
        source_ok = assert_valid_source(fixture)
        odds_ok = assert_odds_format(fixture)
        all_ok = not missing and status_ok and source_ok and odds_ok
        if not all_ok:
            failed += 1
        print(f"  {'✅' if all_ok else '❌'} {name}: "
              f"missing={missing}, status={status_ok}, source={source_ok}, odds={odds_ok}")
    print(f"\n{'All passed ✅' if failed == 0 else f'{failed} FAILED ❌'}")
