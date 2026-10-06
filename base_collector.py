#!/usr/bin/env python3
"""
base_collector.py — Базовый класс коллектора GatekeeperAI (§19.4).
5-шаговый алгоритм: fetch → normalize → enrich → save → meta.

Новые коллекторы наследуют BaseCollector и реализуют fetch() + normalize().
"""

import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

__version__ = "9.3-audited"
__all__ = ["BaseCollector", "__version__"]

logger = logging.getLogger("base_collector")
logger.addHandler(logging.NullHandler__)

MSK_TZ = timezone(timedelta(hours=3))


class BaseCollector:
    """
    Базовый класс коллектора (§19.4).
    
    5-шаговый алгоритм:
      1. fetch()      — получить данные из API
      2. normalize()  — нормализовать имена команд, даты
      3. enrich()     — добавить odds, upstream, sources
      4. save()       — upsert_match через хаб
      5. meta()       — save_meta через хаб
    
    Подклассы реализуют:
      - fetch()        → raw API data
      - normalize(raw) → list of match dicts
    """

    def __init__(self, name: str, base_url: str = ""):
        self.name = name
        self.base_url = base_url
        self.matches_processed = 0
        self.errors = 0
        self.start_time = None
        
        # Lazy imports
        self._hub = None
        self._team_registry = None

    def _get_hub(self):
        if self._hub is None:
            try:
                import gatekeeper_hub as hub
                self._hub = hub
            except ImportError:
                logger.error("gatekeeper_hub not found")
                return None
        return self._hub

    def _get_team_registry(self):
        if self._team_registry is None:
            try:
                import team_registry
                self._team_registry = team_registry
            except ImportError:
                logger.warning("team_registry not found")
                return None
        return self._team_registry

    # ── Steps to override ────────────────────────────────

    def fetch(self, **kwargs) -> Any:
        """Шаг 1: Получить данные из API. Переопределить в подклассе."""
        raise NotImplementedError("fetch() must be implemented in subclass")

    def normalize(self, raw_data: Any) -> List[Dict[str, Any]]:
        """Шаг 2: Нормализовать raw данные в список матчей. Переопределить."""
        raise NotImplementedError("normalize() must be implemented in subclass")

    def enrich(self, match: Dict[str, Any]) -> Dict[str, Any]:
        """Шаг 3: Добавить odds, upstream, sources. Можно переопределить."""
        tr = self._get_team_registry()
        home_raw = match.get("home_team", match.get("home", ""))
        away_raw = match.get("away_team", match.get("away", ""))
        date_raw = match.get("date_utc", match.get("date", ""))
        if tr:
            match["home_clean"] = tr.clean_team_name(home_raw)
            match["away_clean"] = tr.clean_team_name(away_raw)
            match["canonical_id"] = tr.build_canonical_id(home_raw, away_raw, date_raw)
        match.setdefault("home_team", home_raw)
        match.setdefault("away_team", away_raw)
        match.setdefault("date_utc", date_raw)
        match.setdefault("source", self.name)
        match.setdefault("sources", [self.name])
        return match

    def save(self, match: Dict[str, Any]) -> bool:
        """Шаг 4: Сохранить матч через хаб (upsert_match — kwargs, не dict)."""
        hub = self._get_hub()
        if not hub:
            return False
        try:
            # §1.14: source — именованный параметр, не ключ в dict
            # §2.2: idempotency_key для защиты от дублей
            match_copy = {k: v for k, v in match.items() if k != "source"}
            idempotency_key = match_copy.pop("idempotency_key", None)
            hub.upsert_match(
                home_team=match_copy.get("home_team", match_copy.get("home", "")),
                away_team=match_copy.get("away_team", match_copy.get("away", "")),
                date_utc=match_copy.get("date_utc", match_copy.get("date", "")),
                competition=match_copy.get("competition", ""),
                country=match_copy.get("country", ""),
                status=match_copy.get("status", "scheduled"),
                source=self.name,
                source_ids=match_copy.get("source_ids", {}),
                odds=match_copy.get("odds"),
                idempotency_key=idempotency_key,
            )
            self.matches_processed += 1
            return True
        except TypeError:
            # Fallback: hub may accept dict (e.g. upsert_history_match)
            try:
                hub.upsert_match(match, source=self.name)
                self.matches_processed += 1
                return True
            except Exception as e:
                logger.error("save fallback error: %s", e)
                self.errors += 1
                return False
        except Exception as e:
            logger.error("save error: %s", e)
            self.errors += 1
            return False

    def meta(self) -> Dict[str, Any]:
        """Шаг 5: Сохранить метаданные коллектора (§1.27)."""
        hub = self._get_hub()
        duration = round(time.time() - self.start_time, 2) if self.start_time else 0
        # §1.27: обязательные поля — last_run, error_count, total_events, stored_matches
        meta_data = {
            "collector": self.name,
            "last_run": datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            "total_events": self.matches_processed,
            "stored_matches": self.matches_processed,
            "error_count": self.errors,
            "duration_seconds": duration,
        }
        if hub:
            try:
                hub.save_meta(self.name, **{
                    "last_run": meta_data["last_run"],
                    "total_events": self.matches_processed,
                    "stored_matches": self.matches_processed,
                    "error_count": self.errors,
                })
            except Exception as e:
                logger.error("save_meta error: %s", e)
        return meta_data

    # ── HTTP helper (urllib.request only — §1.3) ────────

    def _http_get(self, url: str, headers: dict = None, timeout: int = 30) -> Optional[dict]:
        """HTTP GET через urllib.request (§1.3 — нулевая зависимость)."""
        if headers is None:
            headers = {}
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode("utf-8"))
            return data
        except urllib.error.HTTPError as e:
            logger.error("HTTP %d: %s", e.code, url)
            return None
        except urllib.error.URLError as e:
            logger.error("URL error: %s", e)
            return None
        except json.JSONDecodeError as e:
            logger.error("JSON decode error: %s", e)
            return None

    # ── Graceful shutdown ────────────────────────────────

    def _is_shutdown(self) -> bool:
        hub = self._get_hub()
        if hub and hasattr(hub, "is_shutdown_requested"):
            return hub.is_shutdown_requested()
        return False

    # ── Main run ─────────────────────────────────────────

    def run(self, **kwargs) -> Dict[str, Any]:
        """
        Полный 5-шаговый цикл коллектора (§19.4).
        kwargs передаются в fetch().
        """
        self.start_time = time.time()
        
        # Initialization (§1.7a)
        hub = self._get_hub()
        if hub and hasattr(hub, "run_initialization"):
            hub.run_initialization(collector=self.name)

        # Step 1: Fetch
        logger.info("[%s] Step 1: Fetching data...", self.name)
        raw_data = self.fetch(**kwargs)
        if not raw_data:
            logger.warning("[%s] No data fetched", self.name)
            return self.meta()

        # Step 2: Normalize
        logger.info("[%s] Step 2: Normalizing...", self.name)
        matches = self.normalize(raw_data)
        if not matches:
            logger.warning("[%s] No matches after normalize", self.name)
            return self.meta()

        # Steps 3-4: Enrich + Save
        logger.info("[%s] Step 3-4: Enriching and saving %d matches...", self.name, len(matches))
        for match in matches:
            if self._is_shutdown():
                logger.info("[%s] Graceful shutdown requested", self.name)
                break
            match = self.enrich(match)
            self.save(match)

        # Step 5: Meta
        logger.info("[%s] Step 5: Saving metadata", self.name)
        result = self.meta()
        logger.info("[%s] Done: %d matches, %d errors", 
                     self.name, self.matches_processed, self.errors)
        return result
