"""
SharpApiCollector — рефакторинг на BaseCollector (§19.4)
========================================================
Образец для рефакторинга остальных коллекторов.

До (без BaseCollector):
    run_initialization()
    matches = fetch_sharpapi_events()
    for m in matches:
        cid = upsert_match(...)
        patch_match(cid, "odds", ...)
    save_meta("sharpapi", ...)

После (с BaseCollector):
    class SharpApiCollector(BaseCollector):
        def fetch_events(self):
            return parse_sharpapi_api()

    SharpApiCollector("sharpapi").run()
"""

import os
import time
from base_collector import BaseCollector, is_shutdown_requested


class SharpApiCollector(BaseCollector):
    """Коллектор SharpAPI (§1.11, §19.4)."""

    def __init__(self):
        super().__init__(
            source_name="sharpapi",
            rate_delay=float(os.environ.get("SHARPAPI_RATE_LIMIT_DELAY", "1.0")),
            max_retries=3,
        )
        self.api_key = os.environ.get("SHARPAPI_API_KEY", "")
        self.max_pages = int(os.environ.get("SHARPAPI_MAX_PAGES", "5"))
        self.limit = int(os.environ.get("SHARPAPI_LIMIT", "100"))

    def fetch_events(self) -> list[dict]:
        """
        Шаг 2: парсинг SharpAPI → список raw-матчей.
        Возвращает список словарей в едином формате (§1.26).
        """
        raw_matches = []
        try:
            events = self._fetch_sharpapi_pages()
            for event in events:
                if is_shutdown_requested():
                    self._log("WARN", "Shutdown during fetch")
                    break
                raw = self._normalize_event(event)
                if raw:
                    raw_matches.append(raw)
                self.rate_limit_sleep()
        except Exception as e:
            self.error_count += 1
            self._log("ERROR", "Fetch failed", error=str(e))
            return []

        self._log("INFO", "Fetch complete", count=len(raw_matches))
        return raw_matches

    def _fetch_sharpapi_pages(self) -> list:
        """Запрос к SharpAPI с пагинацией."""
        # Здесь — реальный HTTP-запрос через curl_cffi (§1.24)
        # Заглушка для образца:
        return []

    def _normalize_event(self, event: dict) -> dict | None:
        """
        Нормализация события SharpAPI к единому формату (§1.26).
        """
        try:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            if not home or not away:
                return None

            return {
                "home_team": home,
                "away_team": away,
                "date_utc": event.get("date_utc", ""),
                "competition": event.get("competition", ""),
                "country": event.get("country", ""),
                "source_ids": {"sharpapi": str(event.get("id", ""))},
                "odds": event.get("odds", {}),
                "stats": event.get("stats", {}),
            }
        except Exception as e:
            self.error_count += 1
            self._log("WARN", "Normalize failed", error=str(e))
            return None

    def _extra_meta(self) -> dict:
        """Дополнительные мета-поля для SharpAPI."""
        return {
            "quota_remaining": getattr(self, "_quota_remaining", -1),
            "pages_fetched": getattr(self, "_pages_fetched", 0),
        }


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    SharpApiCollector().run()
