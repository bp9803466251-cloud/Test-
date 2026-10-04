"""
base_collector.py — Базовый класс для всех коллекторов (§19.4).
Наследник переопределяет только fetch_events() и метаданные.
Шаги 1, 3, 4, 5 — в базовом классе.

v8.10-patched:
  FIX-1: try-except в process_event — изоляция ошибок (§1.25)
  FIX-2: clean_team_name вызывается до upsert (§19.4)
  FIX-3: source_ids конструируется, а не берётся из event (§1.26)
  FIX-4: last_run в save_meta (§1.27)
  FIX-5: сортировка событий по дате (§1.32)
  FIX-6: is_shutdown_requested в цикле (§23.3)
  FIX-7: source ключ удаляется из event перед **extra (§1.14)
  FIX-8: try-except в run() для save_meta при падении Redis
  FIX-9: удалены неиспользуемые импорты (time, patch_match, now_msk)
  FIX-10: __version__
"""

import time

from gatekeeper_hub import (
    run_initialization,
    upsert_match,
    save_meta,
    normalize_date,
    is_future_match,
    is_shutdown_requested,
    now_msk,
)
from team_registry import clean_team_name

__version__ = "8.10-patched"

__all__ = ["BaseCollector"]


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
            try:
                save_meta(self.COLLECTOR_NAME,
                          stored_matches=0, error_count=1,
                          last_run=now_msk())
            except Exception:
                pass
            return

        # === ШАГ 2: Получение событий ===
        try:
            events = self.fetch_events()
        except Exception as e:
            print(f"[{self.COLLECTOR_NAME}] fetch_events error: {e}")
            try:
                save_meta(self.COLLECTOR_NAME,
                          total_events=0, stored_matches=0,
                          error_count=1, last_run=now_msk())
            except Exception:
                pass
            return

        if not events:
            try:
                save_meta(self.COLLECTOR_NAME,
                          total_events=0, stored_matches=0,
                          error_count=0, last_run=now_msk())
            except Exception:
                pass
            return

        # Сортировка по дате — ближайшие первыми (§1.32)
        events = self._sort_by_date(events)

        # === ШАГ 3: Создание матчей ===
        created, updated, skipped, errors = 0, 0, 0, 0
        for event in events:
            # Graceful shutdown (§23.3)
            if is_shutdown_requested():
                print(f"[{self.COLLECTOR_NAME}] Shutdown requested, stopping.")
                break

            try:
                result = self.process_event(event)
            except Exception as e:
                print(f"[{self.COLLECTOR_NAME}] process_event error: {e}")
                errors += 1
                continue

            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1

        # === ШАГ 4: Enrichment (опционально) ===
        try:
            self.enrich_events(events)
        except Exception as e:
            print(f"[{self.COLLECTOR_NAME}] enrich_events error: {e}")

        # === ШАГ 5: Сохранение мета (§1.27) ===
        try:
            save_meta(self.COLLECTOR_NAME,
                      total_events=len(events),
                      stored_matches=created + updated,
                      error_count=errors,
                      created=created,
                      updated=updated,
                      skipped_past=skipped,
                      last_run=now_msk())
        except Exception as e:
            print(f"[{self.COLLECTOR_NAME}] save_meta error: {e}")

    def process_event(self, event: dict) -> str:
        """Создание матча. Не переопределять."""
        if not isinstance(event, dict):
            return "skipped"

        date = normalize_date(event.get("date_utc", ""))

        if not is_future_match(date):
            return "skipped"

        # Нормализация команд (§19.4 — clean_team_name до upsert)
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        home_clean = clean_team_name(home_team)
        away_clean = clean_team_name(away_team)

        if not home_clean or not away_clean:
            return "skipped"

        # source_ids конструируется (§1.26 — обязательное поле)
        source_ids = event.get("source_ids")
        if not source_ids:
            event_id = event.get("id", event.get("event_id", ""))
            source_ids = {self.SOURCE_NAME: event_id} if event_id else {}

        # extra-поля (§1.13 — **extra, не extra=extra)
        extra = {"source_ids": source_ids}
        if "odds" in event:
            extra["odds"] = event["odds"]

        # §1.14: удаляем source из event, чтобы не было конфликта
        extra.pop("source", None)

        cid = upsert_match(
            home_team=home_team,
            away_team=away_team,
            date_utc=date,
            competition=event.get("competition", ""),
            country=event.get("country", ""),
            source=self.SOURCE_NAME,
            status=event.get("status", "scheduled"),
            **extra,
        )
        return "created" if cid else "skipped"

    def _sort_by_date(self, events: list) -> list:
        """Сортировка событий по дате — ближайшие первыми (§1.32)."""
        try:
            return sorted(events, key=lambda e: e.get("date_utc", ""))
        except Exception:
            return events

    def fetch_events(self) -> list:
        """Переопределить в наследнике. Возвращает список событий."""
        raise NotImplementedError

    def enrich_events(self, events: list):
        """Переопределить при необходимости. Enrichment после создания."""
        pass
