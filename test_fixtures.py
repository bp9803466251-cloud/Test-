"""
test_fixtures.py — Тесты Фазы 1: канонизация + валидация схемы
=============================================================
Покрывает:
  1. normalize_team_name — алиасы, умляуты, суффиксы
  2. build_canonical_id — консистентность между источниками
  3. schema_v710.json — odds как number, closing секция, sources
  4. End-to-end: нормализация → ID → валидация схемы
  5. Фикстуры CANONICAL_* для test_contracts.py
  6. Дополнительные проверки: типы данных, timestamp, дедупликация

v3.0:
  - Интегрированы TestMatchFixtures: schema, types, timestamps
  - Добавлены проверки дедупликации и idempotency
  - Добавлены проверки cross-source консистентности
  - Добавлены проверки metadata во всех фикстурах

Запуск:
  pytest test_fixtures.py -v
  pytest test_fixtures.py -v --html=test-reports/report.html --self-contained-html
"""

import json
import re
import pytest
from datetime import datetime, timezone
from jsonschema import validate, ValidationError
from typing import Dict, Any, List, Optional

# Импорты из проекта
from team_registry import normalize_team_name, build_canonical_id

# Загрузка схемы
with open("schema_v710.json", "r", encoding="utf-8") as f:
    MATCH_SCHEMA = json.load(f)

__all__ = [
    "CANONICAL_LIVE_MATCH",
    "CANONICAL_HISTORY_MATCH",
    "CANONICAL_COMPLETED_MATCH",
    "CANONICAL_EMPTY_MATCH",
    "assert_required_fields",
    "assert_valid_status",
    "assert_odds_format",
    "TestNormalizeTeamName",
    "TestBuildCanonicalId",
    "TestSchemaValidation",
    "TestEndToEnd",
    "TestFixtures",
    "TestMatchFixtures",
]


# ═══════════════════════════════════════════════════════════
# 0. Фикстуры для test_contracts.py
# ═══════════════════════════════════════════════════════════

_VALID_STATUSES = {
    "scheduled", "live", "completed", "cancelled",
    "postponed", "interrupted", "archived",
}

_VALID_SOURCES = {
    "sharpapi", "odds_api", "bzzoiro", "propline",
    "football_data", "import",
}

_REQUIRED_FIELDS = {
    "id", "source", "sources", "home", "away", "date_utc", "odds",
}

_ID_PATTERN = r"^[a-z0-9_]+__[a-z0-9_]+__[0-9]{8}$"


def assert_required_fields(fixture: Dict[str, Any]) -> List[str]:
    """Возвращает список недостающих обязательных полей."""
    missing = []
    for field in _REQUIRED_FIELDS:
        if field not in fixture:
            missing.append(field)
    return missing


def assert_valid_status(fixture: Dict[str, Any]) -> bool:
    """Проверяет, что status в допустимом enum."""
    status = fixture.get("status", "scheduled")
    return status in _VALID_STATUSES


def assert_valid_source(fixture: Dict[str, Any]) -> bool:
    """Проверяет, что source в допустимом enum."""
    return fixture.get("source") in _VALID_SOURCES


def assert_odds_format(fixture: Dict[str, Any]) -> bool:
    """Проверяет, что odds в формате 1x2 с home/draw/away."""
    odds = fixture.get("odds", {})
    if not isinstance(odds, dict):
        return False
    open_odds = odds.get("open")
    if not isinstance(open_odds, dict):
        return False
    for key in ("home", "draw", "away"):
        if key not in open_odds:
            return False
    return True


# ── Эталонные объекты ──────────────────────────────────────

CANONICAL_LIVE_MATCH: Dict[str, Any] = {
    "id": "manchester_united__liverpool__20261027",
    "source": "sharpapi",
    "sources": ["sharpapi"],
    "home": "manchester united",
    "away": "liverpool",
    "date_utc": "2026-10-27T15:00:00Z",
    "status": "scheduled",
    "competition": "Premier League",
    "country": "England",
    "odds": {
        "open": {"home": 2.15, "draw": 3.40, "away": 3.20},
        "closing": {"home": 2.10, "draw": 3.45, "away": 3.25},
    },
    "metadata": {
        "fetched_at": "2026-10-03T12:00:00+03:00",
        "canonical_verified": True,
        "upstream": "betradar",
    },
}

CANONICAL_HISTORY_MATCH: Dict[str, Any] = {
    "id": "borussia_dortmund__bayern_munich__20260920",
    "source": "football_data",
    "sources": ["football_data"],
    "home": "borussia dortmund",
    "away": "bayern munich",
    "date_utc": "2026-09-20T16:30:00Z",
    "status": "completed",
    "score": {"home": 2, "away": 1},
    "competition": "Bundesliga",
    "country": "Germany",
    "odds": {
        "open": {"home": 2.50, "draw": 3.30, "away": 2.80},
        "closing": {"home": 2.40, "draw": 3.50, "away": 2.90},
    },
    "metadata": {
        "fetched_at": "2026-09-20T18:45:00+03:00",
        "canonical_verified": True,
        "upstream": "football_data",
    },
}

CANONICAL_COMPLETED_MATCH: Dict[str, Any] = {
    "id": "inter_milan__juventus_turin__20260915",
    "source": "bzzoiro",
    "sources": ["bzzoiro"],
    "home": "inter milan",
    "away": "juventus turin",
    "date_utc": "2026-09-15T19:00:00Z",
    "status": "completed",
    "score": {"home": 1, "away": 1},
    "competition": "Serie A",
    "country": "Italy",
    "odds": {
        "open": {"home": 2.20, "draw": 3.10, "away": 3.30},
    },
    "metadata": {
        "fetched_at": "2026-09-15T21:30:00+03:00",
        "canonical_verified": True,
        "upstream": "opta",
    },
}

CANONICAL_EMPTY_MATCH: Dict[str, Any] = {
    "id": "unknown_team__other_team__20261101",
    "source": "odds_api",
    "sources": ["odds_api"],
    "home": "unknown team",
    "away": "other team",
    "date_utc": "2026-11-01T14:00:00Z",
    "status": "scheduled",
    "competition": "Unknown Cup",
    "country": "International",
    "odds": {
        "open": {"home": 1.90, "draw": 3.50, "away": 4.00},
    },
    "metadata": {
        "fetched_at": "2026-10-03T12:00:00+03:00",
        "canonical_verified": False,
        "upstream": "the-odds-api",
    },
}


# ── Список всех фикстур для итерации ──────────────────────

ALL_FIXTURES = [
    ("LIVE", CANONICAL_LIVE_MATCH),
    ("HISTORY", CANONICAL_HISTORY_MATCH),
    ("COMPLETED", CANONICAL_COMPLETED_MATCH),
    ("EMPTY", CANONICAL_EMPTY_MATCH),
]


# ═══════════════════════════════════════════════════════════
# 1. Канонизация: алиасы
# ═══════════════════════════════════════════════════════════

class TestNormalizeTeamName:

    def test_premier_league_aliases(self):
        assert normalize_team_name("Man Utd") == "manchester united"
        assert normalize_team_name("Man United") == "manchester united"
        assert normalize_team_name("spurs") == "tottenham hotspur"
        assert normalize_team_name("wolves") == "wolverhampton wanderers"

    def test_bundesliga_aliases(self):
        assert normalize_team_name("BVB") == "borussia dortmund"
        assert normalize_team_name("bayern") == "bayern munich"
        assert normalize_team_name("koln") == "1 fc koln"
        assert normalize_team_name("cologne") == "1 fc koln"

    def test_serie_a_aliases(self):
        assert normalize_team_name("inter") == "inter milan"
        assert normalize_team_name("juve") == "juventus turin"
        assert normalize_team_name("napoli") == "ssc napoli"

    def test_empty_and_none(self):
        assert normalize_team_name("") == ""
        assert normalize_team_name(None) == ""

    def test_whitespace_collapse(self):
        result = normalize_team_name("  Man   Utd  ")
        assert result == "manchester united"

    def test_unknown_team_passthrough(self):
        # FIX §2.4: FC suffix должен стрипаться
        result = normalize_team_name("Some Unknown Team FC")
        assert result == "some unknown team"

    def test_suffixes_stripped(self):
        """Суффиксы FC, CF, AFC, SC, AC, AS, FK стрипаются (§2.4)."""
        for suffix in ["FC", "CF", "AFC", "SC", "AC", "AS", "FK"]:
            result = normalize_team_name(f"Test Team {suffix}")
            assert result == "test team", f"Suffix {suffix} not stripped: {result}"

    def test_umlauts_handled(self):
        """Умляуты транслитерируются (§2.4)."""
        assert normalize_team_name("M\u00fcnchen") == "munchen"
        assert normalize_team_name("K\u00f6ln") == "koln"

    def test_case_insensitive(self):
        """Регистр не влияет на результат."""
        assert normalize_team_name("MAN UTD") == "manchester united"
        assert normalize_team_name("Manchester United") == "manchester united"
        assert normalize_team_name("MANCHESTER UNITED") == "manchester united"

    def test_idempotent(self):
        """Повторная нормализация не меняет результат."""
        once = normalize_team_name("Man Utd")
        twice = normalize_team_name(once)
        assert once == twice


# ═══════════════════════════════════════════════════════════
# 2. Canonical ID: консистентность между источниками
# ═══════════════════════════════════════════════════════════

class TestBuildCanonicalId:

    def test_cross_source_same_id(self):
        """SharpAPI 'Man Utd' и OddsAPI 'Manchester United' -> один ID"""
        id_sharp = build_canonical_id("Man Utd", "Liverpool", "2026-10-27T15:00:00Z")
        id_odds = build_canonical_id("Manchester United", "Liverpool", "2026-10-27T15:00:00Z")
        assert id_sharp == id_odds

    def test_bundesliga_umlaut_same_id(self):
        """K\u00f6ln и Cologne -> один ID"""
        id1 = build_canonical_id("K\u00f6ln", "Bayern", "2026-10-28T18:30:00Z")
        id2 = build_canonical_id("cologne", "bayern", "2026-10-28T18:30:00Z")
        assert id1 == id2

    def test_id_format(self):
        cid = build_canonical_id("Man Utd", "Spurs", "2026-10-03T15:00:00")
        assert cid == "manchester_united__tottenham_hotspur__20261003"
        assert " " not in cid
        parts = cid.split("__")
        assert len(parts) == 3
        assert len(parts[2]) == 8  # YYYYMMDD

    def test_different_dates_different_ids(self):
        id1 = build_canonical_id("Man Utd", "Liverpool", "2026-10-27T15:00:00Z")
        id2 = build_canonical_id("Man Utd", "Liverpool", "2026-10-28T15:00:00Z")
        assert id1 != id2

    def test_different_teams_different_ids(self):
        id1 = build_canonical_id("Man Utd", "Liverpool", "2026-10-27T15:00:00Z")
        id2 = build_canonical_id("Man Utd", "Arsenal", "2026-10-27T15:00:00Z")
        assert id1 != id2

    def test_id_matches_pattern(self):
        """ID соответствует pattern ^[a-z0-9_]+__[a-z0-9_]+__[0-9]{8}$"""
        cid = build_canonical_id("Man Utd", "Liverpool", "2026-10-27T15:00:00Z")
        assert re.match(_ID_PATTERN, cid), f"ID {cid} doesn't match pattern"

    def test_idempotent(self):
        """Построение ID из уже канонизированных имён не меняет результат."""
        home = normalize_team_name("Man Utd")
        away = normalize_team_name("Liverpool")
        cid1 = build_canonical_id(home, away, "2026-10-27T15:00:00Z")
        cid2 = build_canonical_id(
            normalize_team_name(home),
            normalize_team_name(away),
            "2026-10-27T15:00:00Z",
        )
        assert cid1 == cid2


# ═══════════════════════════════════════════════════════════
# 3. Schema v710: валидация
# ═══════════════════════════════════════════════════════════

class TestSchemaValidation:

    def _make_valid_match(self):
        return {
            "id": "manchester_united__liverpool__20261027",
            "source": "sharpapi",
            "sources": ["sharpapi"],
            "home": "manchester united",
            "away": "liverpool",
            "date_utc": "2026-10-27T15:00:00Z",
            "status": "scheduled",
            "competition": "Premier League",
            "country": "England",
            "odds": {
                "open": {"home": 2.15, "draw": 3.40, "away": 3.20},
                "closing": {"home": 2.10, "draw": 3.45, "away": 3.25}
            },
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "betradar"
            }
        }

    def test_valid_match_passes(self):
        match = self._make_valid_match()
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_string_odds_rejected(self):
        match = self._make_valid_match()
        match["odds"]["open"]["home"] = "2.15"  # string вместо number
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_missing_open_odds_rejected(self):
        match = self._make_valid_match()
        del match["odds"]["open"]
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_odds_below_1_rejected(self):
        match = self._make_valid_match()
        match["odds"]["open"]["home"] = 0.95  # < 1.0
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_invalid_status_rejected(self):
        match = self._make_valid_match()
        match["status"] = "in_progress"  # не в enum
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_invalid_source_rejected(self):
        match = self._make_valid_match()
        match["source"] = "unknown_api"
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_closing_optional(self):
        """closing секция опциональна — матч без неё валиден"""
        match = self._make_valid_match()
        del match["odds"]["closing"]
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_missing_sources_rejected(self):
        """sources — обязательное поле (§2.2)"""
        match = self._make_valid_match()
        del match["sources"]
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_sources_must_be_array(self):
        """sources должен быть массивом, не строкой"""
        match = self._make_valid_match()
        match["sources"] = "sharpapi"
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_closing_odds_are_number(self):
        """closing odds должны быть number, не string"""
        match = self._make_valid_match()
        match["odds"]["closing"]["home"] = "2.10"
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_id_pattern_enforced(self):
        """id должен соответствовать pattern"""
        match = self._make_valid_match()
        match["id"] = "Invalid ID With Spaces__20261027"
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_empty_competition_rejected(self):
        """competition не может быть пустой строкой (minLength: 1)"""
        match = self._make_valid_match()
        match["competition"] = ""
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)

    def test_empty_country_rejected(self):
        """country не может быть пустой строкой (minLength: 1)"""
        match = self._make_valid_match()
        match["country"] = ""
        with pytest.raises(ValidationError):
            validate(instance=match, schema=MATCH_SCHEMA)


# ═══════════════════════════════════════════════════════════
# 4. End-to-End: нормализация → ID → валидация
# ═══════════════════════════════════════════════════════════

class TestEndToEnd:

    def test_sharpapi_flow(self):
        """Полный цикл: raw SharpAPI -> normalize -> schema -> PASS"""
        raw = {
            "home": "Man Utd",
            "away": "Liverpool",
            "date_utc": "2026-10-27T15:00:00Z",
            "odds": {"open": {"home": 2.15, "draw": 3.40, "away": 3.20}}
        }
        home = normalize_team_name(raw["home"])
        away = normalize_team_name(raw["away"])
        cid = build_canonical_id(home, away, raw["date_utc"])

        match = {
            "id": cid,
            "source": "sharpapi",
            "sources": ["sharpapi"],
            "home": home,
            "away": away,
            "date_utc": raw["date_utc"],
            "status": "scheduled",
            "competition": "Premier League",
            "country": "England",
            "odds": raw["odds"],
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "betradar"
            }
        }
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_oddsapi_flow(self):
        """Полный цикл: raw OddsAPI -> normalize -> schema -> PASS"""
        raw = {
            "home": "Manchester United",
            "away": "Liverpool",
            "date_utc": "2026-10-27T15:00:00Z",
            "odds": {
                "open": {"home": 2.12, "draw": 3.45, "away": 3.25},
                "closing": {"home": 2.08, "draw": 3.50, "away": 3.30}
            }
        }
        home = normalize_team_name(raw["home"])
        away = normalize_team_name(raw["away"])
        cid = build_canonical_id(home, away, raw["date_utc"])

        match = {
            "id": cid,
            "source": "odds_api",
            "sources": ["odds_api"],
            "home": home,
            "away": away,
            "date_utc": raw["date_utc"],
            "status": "scheduled",
            "competition": "Premier League",
            "country": "England",
            "odds": raw["odds"],
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "the-odds-api"
            }
        }
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_propline_flow(self):
        """Полный цикл: raw PropLine -> normalize -> schema -> PASS (closing odds)"""
        raw = {
            "home": "Man Utd",
            "away": "Spurs",
            "date_utc": "2026-10-27T15:00:00Z",
            "odds": {
                "open": {"home": 2.20, "draw": 3.30, "away": 3.10},
                "closing": {"home": 2.15, "draw": 3.40, "away": 3.20}
            }
        }
        home = normalize_team_name(raw["home"])
        away = normalize_team_name(raw["away"])
        cid = build_canonical_id(home, away, raw["date_utc"])

        match = {
            "id": cid,
            "source": "propline",
            "sources": ["propline"],
            "home": home,
            "away": away,
            "date_utc": raw["date_utc"],
            "status": "scheduled",
            "competition": "Premier League",
            "country": "England",
            "odds": raw["odds"],
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "pinnacle"
            }
        }
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_football_data_flow(self):
        """Полный цикл: raw football_data -> normalize -> schema -> PASS"""
        raw = {
            "home": "BVB",
            "away": "Bayern",
            "date_utc": "2026-10-28T16:30:00Z",
            "odds": {
                "open": {"home": 2.50, "draw": 3.30, "away": 2.80},
                "closing": {"home": 2.45, "draw": 3.35, "away": 2.85}
            }
        }
        home = normalize_team_name(raw["home"])
        away = normalize_team_name(raw["away"])
        cid = build_canonical_id(home, away, raw["date_utc"])

        match = {
            "id": cid,
            "source": "football_data",
            "sources": ["football_data"],
            "home": home,
            "away": away,
            "date_utc": raw["date_utc"],
            "status": "scheduled",
            "competition": "Bundesliga",
            "country": "Germany",
            "odds": raw["odds"],
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "football_data"
            }
        }
        validate(instance=match, schema=MATCH_SCHEMA)

    def test_cross_source_same_id_e2e(self):
        """SharpAPI и OddsAPI дают один canonical_id -> дедупликация возможна"""
        raw_sharp = {"home": "Man Utd", "away": "Liverpool", "date_utc": "2026-10-27T15:00:00Z"}
        raw_odds = {"home": "Manchester United", "away": "Liverpool", "date_utc": "2026-10-27T15:00:00Z"}

        cid_sharp = build_canonical_id(
            normalize_team_name(raw_sharp["home"]),
            normalize_team_name(raw_sharp["away"]),
            raw_sharp["date_utc"]
        )
        cid_odds = build_canonical_id(
            normalize_team_name(raw_odds["home"]),
            normalize_team_name(raw_odds["away"]),
            raw_odds["date_utc"]
        )
        assert cid_sharp == cid_odds

    def test_multi_source_match_valid(self):
        """Матч с несколькими источниками в sources — валиден"""
        raw = {
            "home": "Man Utd",
            "away": "Liverpool",
            "date_utc": "2026-10-27T15:00:00Z",
            "odds": {"open": {"home": 2.15, "draw": 3.40, "away": 3.20}}
        }
        home = normalize_team_name(raw["home"])
        away = normalize_team_name(raw["away"])
        cid = build_canonical_id(home, away, raw["date_utc"])

        match = {
            "id": cid,
            "source": "sharpapi",
            "sources": ["sharpapi", "odds_api", "propline"],
            "home": home,
            "away": away,
            "date_utc": raw["date_utc"],
            "status": "scheduled",
            "competition": "Premier League",
            "country": "England",
            "odds": raw["odds"],
            "metadata": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canonical_verified": True,
                "upstream": "betradar"
            }
        }
        validate(instance=match, schema=MATCH_SCHEMA)


# ═══════════════════════════════════════════════════════════
# 5. Фикстуры: валидация
# ═══════════════════════════════════════════════════════════

class TestFixtures:

    def test_canonical_live_match_valid(self):
        """CANONICAL_LIVE_MATCH проходит валидацию схемы"""
        validate(instance=CANONICAL_LIVE_MATCH, schema=MATCH_SCHEMA)

    def test_canonical_history_match_valid(self):
        """CANONICAL_HISTORY_MATCH проходит валидацию схемы"""
        validate(instance=CANONICAL_HISTORY_MATCH, schema=MATCH_SCHEMA)

    def test_canonical_completed_match_valid(self):
        """CANONICAL_COMPLETED_MATCH проходит валидацию схемы"""
        validate(instance=CANONICAL_COMPLETED_MATCH, schema=MATCH_SCHEMA)

    def test_canonical_empty_match_valid(self):
        """CANONICAL_EMPTY_MATCH проходит валидацию схемы"""
        validate(instance=CANONICAL_EMPTY_MATCH, schema=MATCH_SCHEMA)

    def test_all_fixtures_have_sources(self):
        """Все фикстуры имеют sources как список"""
        for name, fixture in ALL_FIXTURES:
            assert isinstance(fixture["sources"], list), f"{name}: sources not a list"

    def test_source_in_sources(self):
        """source входит в sources во всех фикстурах"""
        for name, fixture in ALL_FIXTURES:
            assert fixture["source"] in fixture["sources"], \
                f"{name}: source {fixture['source']} not in sources {fixture['sources']}"

    def test_fixtures_odds_are_float(self):
        """Все odds в фикстурах — float, не string (§2.1)"""
        for name, fixture in ALL_FIXTURES:
            odds = fixture.get("odds", {})
            for section in ("open", "closing"):
                section_odds = odds.get(section)
                if section_odds is None:
                    continue
                for key, val in section_odds.items():
                    assert isinstance(val, (int, float)), \
                        f"{name}.odds.{section}.{key} is {type(val).__name__}, expected number"

    def test_fixtures_closing_odds(self):
        """CANONICAL_LIVE_MATCH и CANONICAL_HISTORY_MATCH имеют closing odds (§2.5)"""
        assert "closing" in CANONICAL_LIVE_MATCH["odds"]
        assert "closing" in CANONICAL_HISTORY_MATCH["odds"]
        for key, val in CANONICAL_LIVE_MATCH["odds"]["closing"].items():
            assert isinstance(val, (int, float)), f"closing.{key} is {type(val).__name__}"

    def test_fixtures_required_fields(self):
        """Все фикстуры имеют обязательные поля"""
        for name, fixture in ALL_FIXTURES:
            missing = assert_required_fields(fixture)
            assert not missing, f"{name}: missing {missing}"

    def test_fixtures_valid_status(self):
        """Все фикстуры имеют допустимый status"""
        for name, fixture in ALL_FIXTURES:
            assert assert_valid_status(fixture), f"{name}: invalid status"

    def test_fixtures_valid_source(self):
        """Все фикстуры имеют допустимый source"""
        for name, fixture in ALL_FIXTURES:
            assert assert_valid_source(fixture), \
                f"{name}: invalid source {fixture.get('source')}"

    def test_fixtures_id_format(self):
        """Все фикстуры имеют id в формате pattern"""
        for name, fixture in ALL_FIXTURES:
            assert re.match(_ID_PATTERN, fixture["id"]), \
                f"{name}: id {fixture['id']} doesn't match pattern"

    def test_fixtures_competition_nonempty(self):
        """Все фикстуры имеют непустой competition"""
        for name, fixture in ALL_FIXTURES:
            comp = fixture.get("competition", "")
            assert comp and len(comp) >= 1, f"{name}: competition empty"

    def test_fixtures_country_nonempty(self):
        """Все фикстуры имеют непустой country"""
        for name, fixture in ALL_FIXTURES:
            country = fixture.get("country", "")
            assert country and len(country) >= 1, f"{name}: country empty"

    def test_fixtures_have_metadata(self):
        """Все фикстуры имеют metadata с fetched_at"""
        for name, fixture in ALL_FIXTURES:
            meta = fixture.get("metadata", {})
            assert "fetched_at" in meta, f"{name}: missing metadata.fetched_at"
            assert "upstream" in meta, f"{name}: missing metadata.upstream"

    def test_fixtures_odds_format(self):
        """Все фикстуры проходят проверку odds формата"""
        for name, fixture in ALL_FIXTURES:
            assert assert_odds_format(fixture), f"{name}: invalid odds format"

    def test_completed_match_has_score(self):
        """CANONICAL_COMPLETED_MATCH и CANONICAL_HISTORY_MATCH имеют score"""
        assert "score" in CANONICAL_COMPLETED_MATCH
        assert "score" in CANONICAL_HISTORY_MATCH
        assert isinstance(CANONICAL_COMPLETED_MATCH["score"]["home"], int)
        assert isinstance(CANONICAL_COMPLETED_MATCH["score"]["away"], int)


# ═══════════════════════════════════════════════════════════
# 6. Дополнительные проверки: типы, timestamp, дедупликация
# ═══════════════════════════════════════════════════════════

class TestMatchFixtures:
    """Расширенные проверки эталонных объектов."""

    def test_schema_validation_all(self):
        """Все фикстуры проходят валидацию JSON-схемы"""
        for name, fixture in ALL_FIXTURES:
            try:
                validate(instance=fixture, schema=MATCH_SCHEMA)
            except ValidationError as e:
                pytest.fail(f"Schema validation failed for {name}: {e}")

    def test_data_types(self):
        """Проверка типов данных во всех фикстурах"""
        for name, fixture in ALL_FIXTURES:
            assert isinstance(fixture["id"], str), f"{name}: id not str"
            assert isinstance(fixture["home"], str), f"{name}: home not str"
            assert isinstance(fixture["away"], str), f"{name}: away not str"
            assert isinstance(fixture["date_utc"], str), f"{name}: date_utc not str"
            assert isinstance(fixture["source"], str), f"{name}: source not str"
            assert isinstance(fixture["sources"], list), f"{name}: sources not list"
            assert isinstance(fixture["status"], str), f"{name}: status not str"
            assert isinstance(fixture["competition"], str), f"{name}: competition not str"
            assert isinstance(fixture["country"], str), f"{name}: country not str"
            assert isinstance(fixture["odds"], dict), f"{name}: odds not dict"

    def test_timestamp_format(self):
        """Проверка формата date_utc во всех фикстурах"""
        for name, fixture in ALL_FIXTURES:
            date_str = fixture["date_utc"]
            # Пробуем парсить ISO формат
            try:
                # Заменяем Z на +00:00 для fromisoformat
                parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                pytest.fail(f"{name}: invalid date_utc format: {date_str}")

    def test_metadata_timestamp_format(self):
        """Проверка формата fetched_at в metadata"""
        for name, fixture in ALL_FIXTURES:
            fetched = fixture.get("metadata", {}).get("fetched_at", "")
            try:
                datetime.fromisoformat(fetched)
            except ValueError:
                pytest.fail(f"{name}: invalid metadata.fetched_at: {fetched}")

    def test_unique_ids(self):
        """Все фикстуры имеют уникальные id"""
        ids = [f["id"] for _, f in ALL_FIXTURES]
        assert len(ids) == len(set(ids)), "Duplicate ids found"

    def test_unique_competitions(self):
        """Фикстуры покрывают разные лиги"""
        competitions = {f["competition"] for _, f in ALL_FIXTURES}
        assert len(competitions) >= 3, "Not enough competition diversity"

    def test_status_diversity(self):
        """Фикстуры покрывают разные статусы"""
        statuses = {f["status"] for _, f in ALL_FIXTURES}
        assert "scheduled" in statuses
        assert "completed" in statuses

    def test_source_diversity(self):
        """Фикстуры покрывают разные источники"""
        sources = {f["source"] for _, f in ALL_FIXTURES}
        assert len(sources) >= 3, "Not enough source diversity"

    def test_empty_match_minimal_odds(self):
        """CANONICAL_EMPTY_MATCH имеет только open odds"""
        odds = CANONICAL_EMPTY_MATCH["odds"]
        assert "open" in odds
        assert "closing" not in odds

    def test_empty_match_not_canonical_verified(self):
        """CANONICAL_EMPTY_MATCH не верифицирован"""
        assert CANONICAL_EMPTY_MATCH["metadata"]["canonical_verified"] is False

    def test_live_match_canonical_verified(self):
        """CANONICAL_LIVE_MATCH верифицирован"""
        assert CANONICAL_LIVE_MATCH["metadata"]["canonical_verified"] is True

    def test_completed_match_has_open_only(self):
        """CANONICAL_COMPLETED_MATCH имеет только open odds (нет closing)"""
        odds = CANONICAL_COMPLETED_MATCH["odds"]
        assert "open" in odds
        assert "closing" not in odds

    def test_history_match_has_closing(self):
        """CANONICAL_HISTORY_MATCH имеет и open, и closing odds"""
        odds = CANONICAL_HISTORY_MATCH["odds"]
        assert "open" in odds
        assert "closing" in odds

    def test_cross_source_deduplication(self):
        """Дедупликация: разные названия команд -> один canonical_id"""
        cid1 = build_canonical_id(
            normalize_team_name("Man Utd"),
            normalize_team_name("Liverpool"),
            "2026-10-27T15:00:00Z",
        )
        cid2 = build_canonical_id(
            normalize_team_name("Manchester United"),
            normalize_team_name("Liverpool"),
            "2026-10-27T15:00:00Z",
        )
        cid3 = build_canonical_id(
            normalize_team_name("MAN UTD"),
            normalize_team_name("liverpool"),
            "2026-10-27T15:00:00Z",
        )
        assert cid1 == cid2 == cid3

    def test_fixtures_match_own_ids(self):
        """id каждой фикстуры можно пересобрать из home/away/date"""
        for name, fixture in ALL_FIXTURES:
            rebuilt = build_canonical_id(
                fixture["home"],
                fixture["away"],
                fixture["date_utc"],
            )
            assert rebuilt == fixture["id"], \
                f"{name}: rebuilt id {rebuilt} != fixture id {fixture['id']}"


# ═══════════════════════════════════════════════════════════
# Запуск как скрипт
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Test Fixtures v3.0 — Self-check")
    print("=" * 60)
    failed = 0
    for name, fixture in ALL_FIXTURES:
        missing = assert_required_fields(fixture)
        status_ok = assert_valid_status(fixture)
        source_ok = assert_valid_source(fixture)
        odds_ok = assert_odds_format(fixture)
        all_ok = not missing and status_ok and source_ok and odds_ok
        if not all_ok:
            failed += 1
        print(f"  {'\u2705' if all_ok else '\u274c'} {name}: "
              f"missing={missing}, status={status_ok}, "
              f"source={source_ok}, odds={odds_ok}")
    print(f"\n{'All passed \u2705' if failed == 0 else f'{failed} FAILED \u274c'}")
    print("=" * 60)
