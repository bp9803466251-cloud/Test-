#!/usr/bin/env python3
"""
base_collector.py — Базовый абстрактный класс коллектора GatekeeperAI (§1.11, §24).
"""

import os
import sys
import time
import logging
from typing import Any, Dict, List, Optional

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    logging.error("[BASE_COLLECTOR] Библиотека curl_cffi не найдена.")
    sys.exit(1)

logger = logging.getLogger("base_collector")


class BaseCollector:
    COLLECTOR_NAME: str = "base"
    SOURCE_NAME: str = "base"

    def __init__(self):
        self.matches_processed = 0
        self.errors = 0
        self.total_events = 0
        self.skipped_past = 0
        self.start_time = None
        self._hub = None

    def _get_hub(self):
        if self._hub is None:
            try:
                import gatekeeper_hub as hub
                self._hub = hub
            except ImportError:
                return None
        return self._hub

    def fetch_events(self) -> list:
        raise NotImplementedError()

    def _http_get(self, url: str, headers: dict = None, timeout: int = 15) -> Optional[dict]:
        if headers is None:
            headers = {}
        try:
            resp = curl_requests.get(url, headers=headers, timeout=timeout, impersonate="chrome")
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"[HTTP ERROR] Статус {resp.status_code} на маршруте: {url}")
            return None
        except Exception as e:
            logger.error(f"[SOCKET ERROR] Сбой сокета API: {e}")
            return None

    def run(self) -> None:
        self.start_time = time.time()
        hub = self._get_hub()
        if not hub:
            sys.exit(1)
            
        if hasattr(hub, "run_initialization"):
            try: hub.run_initialization(collector=self.COLLECTOR_NAME)
            except Exception: pass

        events = self.fetch_events()
        if not events:
            print(f"[{self.COLLECTOR_NAME.upper()}] Провайдер вернул пустой фид. Фиксация мета-состояния...")
            if hasattr(hub, "save_meta"):
                try: hub.save_meta(self.COLLECTOR_NAME, total_events=0, stored_matches=0, error_count=0)
                except Exception: pass
            return
        
        self.total_events = len(events)

        if hasattr(self, "process_events_pipeline"):
            self.process_events_pipeline(events)

        # Безопасный Шаг 5 с проверкой hasattr (§1.11, §1.26)
        if hasattr(hub, "save_meta"):
            try:
                hub.save_meta(
                    self.COLLECTOR_NAME,
                    last_run=hub.now_msk() if hasattr(hub, "now_msk") else str(time.time()),
                    total_events=self.total_events,
                    stored_matches=self.matches_processed,
                    error_count=self.errors,
                    skipped_past=self.skipped_past
                )
            except Exception as e:
                print(f"[{self.COLLECTOR_NAME.upper()}] Предупреждение save_meta: {e}")
        else:
            print(f"[{self.COLLECTOR_NAME.upper()}] Метод save_meta отсутствует в gatekeeper_hub. Пропуск.")
        
        print(f"[{self.COLLECTOR_NAME.upper()}] Сквозной сбор завершен. Записано: {self.matches_processed}")
        
