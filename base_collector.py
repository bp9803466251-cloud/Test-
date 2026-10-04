"""
base_collector.py — Базовый класс для всех коллекторов (§19.4).
Наследник переопределяет только fetch_events() и метаданные.
Шаги 1, 3, 4, 5 — в базовом классе.
"""

import time

from gatekeeper_hub import (
    run_initialization, upsert_match, patch_match, save_meta,
    normalize_date, is_future_match, now_msk,
)
from team_registry import clean_team_name


class BaseCollector:
    """
    Базовый класс коллектора GatekeeperAI.
    Единый 5-шаговый алгоритм (правило 1.11).
    
    Наследник переопределяет:
        COLLECTOR_NAME — имя коллектора (для meta)
        SOURCE_NAME — имя источника (для source_ids)
        fetch_events() — получение событий из API
    
    Опционально:
        enrich_events() — enrichment после создания матчей
    """

    COLLECTOR_NAME = "base"
    SOURCE_NAME = "base"
    TIMEOUT = 30  # секунд для внешних API

    def run(self):
        """Единый 5-шаговый алгоритм."""
        # === ШАГ 1: Инициализация ===
        init = run_initialization(self.COLLECTOR_NAME)
        if not init.get("redis_available"):
            save_meta(self.COLLECTOR_NAME, stored_matches=0, error_count=1)
            return

        # === ШАГ 2: Получение событий ===
        events = self.fetch_events()
        if not events:
            save_meta(self.COLLECTOR_NAME,
                      total_events=0, stored_matches=0, error_count=0)
            return

        # === ШАГ 3: Создание матчей ===
        created, updated, skipped = 0, 0, 0
        for event in events:
            result = self.process_event(event)
            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1

        # === ШАГ 4: Enrichment (опционально) ===
        self.enrich_events(events)

        # === ШАГ 5: Сохранение мета ===
        save_meta(self.COLLECTOR_NAME,
                  total_events=len(events),
                  stored_matches=created + updated,
                  error_count=0,
                  created=created,
                  updated=updated,
                  skipped_past=skipped)

    def process_event(self, event: dict) -> str:
        """Создание матча. Не переопределять."""
        date = normalize_date(event.get("date_utc", ""))

        if not is_future_match(date):
            return "skipped"

        extra = {}
        if "source_ids" in event:
            extra["source_ids"] = event["source_ids"]
        if "odds" in event:
            extra["odds"] = event["odds"]

        cid = upsert_match(
            home_team=event.get("home_team", ""),
            away_team=event.get("away_team", ""),
            date_utc=date,
            competition=event.get("competition", ""),
            country=event.get("country", ""),
            source=self.SOURCE_NAME,
            status=event.get("status", "scheduled"),
            **extra,
        )
        return "created" if cid else "skipped"

    def fetch_events(self) -> list:
        """Переопределить в наследнике. Возвращает список событий."""
        raise NotImplementedError

    def enrich_events(self, events: list):
        """Переопределить при необходимости. Enrichment после создания."""
        pass


__all__ = ["BaseCollector"]
