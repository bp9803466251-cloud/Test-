"""
test_fixtures.py — Канонические тестовые фикстуры GatekeeperAI (§20.8).
Эталонные объекты матчей для контрактных тестов и валидации схемы.

v9.3-audited:
  FIX-1: status enum — "scheduled"/"completed" (not "upcoming"/"finished")
  FIX-2: canonical_id соответствует home/away командам
  FIX-3: section_history — upstream поле добавлено (§3)
  FIX-4: Все 4 фикстуры валидируются против schema_v710.json
"""

__version__ = "9.3-audited"

__all__ = [
    "CANONICAL_LIVE_MATCH",
    "CANONICAL_HISTORY_MATCH",
    "CANONICAL_COMPLETED_MATCH",
    "CANONICAL_MINIMAL_MATCH",
    "validate_fixtures",
    "__version__",
]


# ── CANONICAL_LIVE_MATCH — матч до начала (scheduled) ──
CANONICAL_LIVE_MATCH = {
    "canonical_id": "man__fulham__20260921",
    "home_team": "Manchester United",
    "away_team": "Fulham",
    "home_clean": "man",
    "away_clean": "fulham",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-09-21T18:00:00Z",
    "time_utc": "18:00",
    "status": "scheduled",
    "score": None,
    "version": 1,
    "schema_version": "v710",
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
                    "timestamp": "2026-09-21T17:55:00+03:00",
                    "type": "prematch"
                }
            ]
        }
    },
    "source_map": {
        "odds": {
            "source": "sharpapi",
            "upstream": "betradar",
            "sharp_benchmark": "pinnacle",
            "soft_bookmakers": ["bet365", "bwin", "betfair"],
            "types": ["opening"]
        }
    },
    "source_ids": {"sharpapi": "evt_123"},
    "sources": ["sharpapi"],
    "section_history": [
        {"section": "odds", "source": "sharpapi", "upstream": "betradar", "updated_at": "2026-09-21T17:55:00+03:00"}
    ],
    "value_analysis": {},
    "flags": {"extreme_result": False, "abnormal_score": False, "red_card_driven": False},
    "predictions": {"source": "bzzoiro", "home_win": 45, "draw": 30, "away_win": 25},
    "stats": {"xg_home": 1.8, "xg_away": 0.9, "possession_home": 62, "possession_away": 38},
    "h2h": {"source": "bzzoiro", "total_meetings": 10, "home_wins": 5, "draws": 3, "away_wins": 2},
    "created_at": "2026-09-21T17:50:00+03:00",
    "updated_at": "2026-09-21T17:55:00+03:00",
}


# ── CANONICAL_HISTORY_MATCH — завершённый матч из CSV ──
CANONICAL_HISTORY_MATCH = {
    "canonical_id": "man__arsenal__20260115",
    "home_team": "Manchester United",
    "away_team": "Arsenal",
    "home_clean": "man",
    "away_clean": "arsenal",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-01-15T20:00:00Z",
    "time_utc": "20:00",
    "status": "completed",
    "score": {"home": 2, "away": 1},
    "half_time_score": {"home": 1, "away": 0},
    "full_time_result": "H",
    "referee": "M. Oliver",
    "version": 1,
    "schema_version": "v710",
    "season": "2526",
    "league_code": "E0",
    "csv_raw": {
        "B365H": "1.85", "B365D": "3.60", "B365A": "2.10",
        "Max>2.5": "1.97", "Avg>2.5": "1.95",
        "MaxCAHH": "-0.5", "AHCh": "1.92"
    },
    "odds": {
        "1x2": {
            "current": {"home": "1.85", "draw": "3.60", "away": "2.10"},
            "opening": {"home": "1.80", "draw": "3.50", "away": "2.20"},
            "sources": [
                {
                    "source": "football_data",
                    "upstream": "multi_bookmaker",
                    "price": {"home": "1.85", "draw": "3.60", "away": "2.10"},
                    "timestamp": "2026-01-15T20:00:00+03:00",
                    "type": "opening"
                }
            ]
        }
    },
    "source_map": {
        "odds": {
            "source": "football_data",
            "upstream": "multi_bookmaker",
            "sharp_benchmark": "pinnacle",
            "soft_bookmakers": ["bet365", "bwin", "betfair"],
            "types": ["opening", "closing"]
        }
    },
    "source_ids": {"football_data": "csv_E0_2526"},
    "sources": ["football_data"],
    "section_history": [
        {"section": "odds", "source": "football_data", "upstream": "multi_bookmaker", "updated_at": "2026-01-15T20:00:00+03:00"}
    ],
    "value_analysis": {},
    "flags": {"extreme_result": False, "abnormal_score": False, "red_card_driven": False},
    "created_at": "2026-01-15T20:00:00+03:00",
    "updated_at": "2026-01-15T20:00:00+03:00",
}


# ── CANONICAL_COMPLETED_MATCH — матч в процессе миграции live→history ──
CANONICAL_COMPLETED_MATCH = {
    "canonical_id": "chelsea__liverpool__20261004",
    "home_team": "Chelsea",
    "away_team": "Liverpool",
    "home_clean": "chelsea",
    "away_clean": "liverpool",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-10-04T17:30:00Z",
    "time_utc": "17:30",
    "status": "completed",
    "score": {"home": 1, "away": 1},
    "version": 3,
    "schema_version": "v710",
    "odds": {
        "1x2": {
            "current": {"home": "2.50", "draw": "3.20", "away": "2.80"},
            "opening": {"home": "2.40", "draw": "3.30", "away": "2.90"},
            "best":    {"home": "2.50", "draw": "3.20", "away": "2.90"},
            "sources": [
                {"source": "sharpapi", "upstream": "betradar", "price": {"home": "2.50", "draw": "3.20", "away": "2.80"}, "timestamp": "2026-10-04T17:25:00+03:00", "type": "live"},
                {"source": "propline", "upstream": "pinnacle", "price": {"home": "2.55", "draw": "3.20", "away": "2.85"}, "timestamp": "2026-10-04T17:26:00+03:00", "type": "live"}
            ]
        }
    },
    "source_map": {
        "odds": {"source": "sharpapi", "upstream": "betradar"},
        "stats": {"source": "bzzoiro", "upstream": "opta"},
        "h2h": {"source": "bzzoiro", "upstream": "opta"},
        "pred": {"source": "bzzoiro", "upstream": "opta"}
    },
    "source_ids": {"sharpapi": "evt_456", "bzzoiro": "bz_789", "odds_api": "oa_012", "propline": "pp_345"},
    "sources": ["sharpapi", "odds_api", "bzzoiro", "propline"],
    "section_history": [
        {"section": "odds", "source": "sharpapi", "upstream": "betradar", "updated_at": "2026-10-04T17:25:00+03:00"},
        {"section": "stats", "source": "bzzoiro", "upstream": "opta", "updated_at": "2026-10-04T17:26:00+03:00"}
    ],
    "value_analysis": {"best_side": "draw", "value_pct": 5.2, "classification": "HOT"},
    "stats": {"xg_home": 1.2, "xg_away": 1.1, "possession_home": 51, "possession_away": 49},
    "h2h": {"source": "bzzoiro", "total_meetings": 15, "home_wins": 6, "draws": 5, "away_wins": 4},
    "predictions": {"source": "bzzoiro", "home_win": 35, "draw": 35, "away_win": 30},
    "flags": {"extreme_result": False, "abnormal_score": False, "red_card_driven": False},
    "created_at": "2026-10-04T17:00:00+03:00",
    "updated_at": "2026-10-04T17:30:00+03:00",
}


# ── CANONICAL_MINIMAL_MATCH — минимальный валидный объект ──
CANONICAL_MINIMAL_MATCH = {
    "canonical_id": "arsenal__chelsea__20261015",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "home_clean": "arsenal",
    "away_clean": "chelsea",
    "competition": "Premier League",
    "country": "England",
    "date_utc": "2026-10-15T19:00:00Z",
    "status": "scheduled",
    "score": None,
    "version": 1,
    "schema_version": "v710",
    "sources": [],
    "section_history": [],
    "value_analysis": {},
    "created_at": "2026-10-15T18:00:00+03:00",
    "updated_at": "2026-10-15T18:00:00+03:00",
}


def validate_fixtures():
    """
    Валидация всех канонических фикстур.
    Возвращает (True, []) если все фикстуры корректны,
    иначе (False, [list_of_errors]).
    """
    import os
    import json

    errors = []

    # Загружаем схему
    schema_path = os.path.join(os.path.dirname(__file__) or ".", "schema_v710.json")
    if not os.path.exists(schema_path):
        # Fallback — проверяем базовые поля
        for name, fixture in [
            ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
            ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
            ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
            ("CANONICAL_MINIMAL_MATCH", CANONICAL_MINIMAL_MATCH),
        ]:
            for field in ["canonical_id", "home_team", "away_team", "home_clean",
                          "away_clean", "competition", "country", "date_utc",
                          "status", "version", "schema_version"]:
                if field not in fixture:
                    errors.append(f"{name}: missing required field '{field}'")
            if fixture.get("status") not in ("scheduled", "live", "completed"):
                errors.append(f"{name}: invalid status '{fixture.get('status')}'")
            if fixture.get("schema_version") != "v710":
                errors.append(f"{name}: wrong schema_version '{fixture.get('schema_version')}'")
        return (len(errors) == 0, errors)

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        errors.append(f"Cannot load schema_v710.json: {e}")
        return (False, errors)

    required_fields = schema.get("required", [])
    status_enum = schema.get("properties", {}).get("status", {}).get("enum", [])
    version_check = schema.get("version", "")

    for name, fixture in [
        ("CANONICAL_LIVE_MATCH", CANONICAL_LIVE_MATCH),
        ("CANONICAL_HISTORY_MATCH", CANONICAL_HISTORY_MATCH),
        ("CANONICAL_COMPLETED_MATCH", CANONICAL_COMPLETED_MATCH),
        ("CANONICAL_MINIMAL_MATCH", CANONICAL_MINIMAL_MATCH),
    ]:
        # Проверка обязательных полей
        for field in required_fields:
            if field not in fixture:
                errors.append(f"{name}: missing required field '{field}'")

        # Проверка status enum
        status = fixture.get("status")
        if status_enum and status not in status_enum:
            errors.append(f"{name}: status '{status}' not in enum {status_enum}")

        # Проверка schema_version
        sv = fixture.get("schema_version")
        if sv != "v710":
            errors.append(f"{name}: schema_version '{sv}' != 'v710'")

        # Проверка version — целое число >= 1
        v = fixture.get("version")
        if not isinstance(v, int) or v < 1:
            errors.append(f"{name}: version must be int >= 1, got {v}")

        # Проверка canonical_id — непустая строка
        cid = fixture.get("canonical_id", "")
        if not isinstance(cid, str) or len(cid) < 1:
            errors.append(f"{name}: canonical_id must be non-empty string")

        # Проверка score — None или dict с home/away
        score = fixture.get("score")
        if score is not None:
            if not isinstance(score, dict):
                errors.append(f"{name}: score must be dict or None")
            elif "home" not in score or "away" not in score:
                errors.append(f"{name}: score must have home and away")

    if errors:
        return (False, errors)
    return (True, [])
