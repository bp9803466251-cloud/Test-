"""
BaseCollector — базовый класс для всех коллекторов (§19.4, §23.3, §24.5)
========================================================================
Гарантирует единый 5-шаговый алгоритм:
  run_initialization() → fetch_events() → upsert_match() → patch_match() → save_meta()

Дополнительно:
  - Graceful shutdown (§23.3) — корректное завершение при SIGTERM
  - Metrics (§24.5) — автоматический сбор метрик
  - Idempotency keys (§23.2) — защита от дублей при ретраях CI

Использование:
    class SharpApiCollector(BaseCollector):
        def fetch_events(self):
            # парсинг API → список raw-матчей
            return raw_matches
"""

import os
import sys
import time
import signal
import json
from datetime import datetime, timezone, timedelta
from functools import wraps

# ── Глобальное состояние shutdown ────────────────────────────────────
# FIX-1: Делегируем в gatekeeper_hub.is_shutdown_requested()
# чтобы patch_match/upsert_match в хабе видели тот же флаг.
# Fallback — локальная переменная (если хаб недоступен).
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    sys.stderr.write(f"\n[BaseCollector] SIGTERM received, graceful shutdown...\n")
    # Делегируем в хаб — вызываем обработчик хаба, а не лезем в приватные поля
    try:
        import gatekeeper_hub
        gatekeeper_hub._handle_shutdown(signum, frame)
    except Exception:
        pass


def install_shutdown_handler():
    """Устанавливает обработчик SIGTERM (§23.3)."""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def is_shutdown_requested() -> bool:
    """Проверяет, был ли запрошен graceful shutdown (§23.3)."""
    # FIX-1: Сначала проверяем хабовский флаг
    try:
        import gatekeeper_hub
        if gatekeeper_hub.is_shutdown_requested():
            return True
    except Exception:
        pass
    # Fallback — локальный флаг
    return _shutdown_requested


def now_msk() -> str:
    """ISO timestamp в MSK. Делегирует в gatekeeper_config (единый источник)."""
    try:
        from gatekeeper_config import now_msk as _cfg_now_msk
        return _cfg_now_msk()
    except ImportError:
        # Fallback если config недоступен
        msk = timezone(timedelta(hours=3))
        return datetime.now(msk).isoformat()


class Metrics:
    """
    In-memory метрики за запуск (§24.5).
    Сбрасываются при каждом run_initialization().
    """
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.timers: dict[str, float] = {}

    def inc(self, name: str, n: int = 1):
        self.counters[name] = self.counters.get(name, 0) + n

    def time(self, name: str, seconds: float):
        self.timers[name] = self.timers.get(name, 0.0) + seconds

    def report(self) -> dict:
        return {"counters": self.counters, "timers": self.timers}

    def reset(self):
        self.counters = {}
        self.timers = {}


def timed(metrics: Metrics, name: str):
    """Декоратор для автоматического тайминга (§24.5)."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            result = func(*args, **kwargs)
            metrics.time(f"{name}_total", time.monotonic() - start)
            return result
        return wrapper
    return decorator


class BaseCollector:
    """
    Базовый класс для всех коллекторов (§19.4).

    Подклассы обязаны реализовать:
        fetch_events() → list[dict]  — парсинг API, возврат raw-матчей

    Опционально:
        enrich_match(cid, raw)       — enrichment после upsert
        collect_odds(cid, raw)       — сбор и patch коэффициентов
    """

    def __init__(self, source_name: str, rate_delay: float = 1.0, max_retries: int = 3):
        self.source = source_name
        self.rate_delay = rate_delay
        self.max_retries = max_retries
        self.metrics = Metrics()
        self.error_count = 0
        self.created = 0
        self.skipped = 0
        self.patched = 0

        # Lazy import (избегаем циклических зависимостей при импорте)
        self._hub = None

    def _get_hub(self):
        if self._hub is None:
            import gatekeeper_hub
            self._hub = gatekeeper_hub
        return self._hub

    # ── Шаг 1: Initialization ──────────────────────────────────────────
    def run_initialization(self):
        """
        Шаг 1: инициализация коллектора (§19.4).
        - Устанавливает graceful shutdown handler (§23.3)
        - Сбрасывает метрики (§24.5)
        - Вызывает run_initialization() хаба (cleanup, health, etc.)
        """
        install_shutdown_handler()
        self.metrics.reset()

        hub = self._get_hub()
        # Передаём имя коллектора для should_run_cleanup() (§24.7)
        try:
            hub.run_initialization(collector=self.source)
        except TypeError:
            # Fallback: если хаб не поддерживает collector= параметр
            hub.run_initialization()

        self._log("INFO", "Initialization complete", source=self.source)

    # ── Шаг 2: Fetch Events ────────────────────────────────────────────
    def fetch_events(self) -> list[dict]:
        """
        Шаг 2: парсинг API, возврат списка raw-матчей.
        Подклассы обязаны реализовать этот метод.
        """
        raise NotImplementedError(f"{self.source}: fetch_events() not implemented")

    # ── Шаг 3: Upsert Matches ─────────────────────────────────────────
    def upsert_matches(self, raw_matches: list[dict]) -> list[str]:
        """
        Шаг 3: создание/обновление матчей через upsert_match() (§19.4).
        Возвращает список canonical_id созданных матчей.
        """
        hub = self._get_hub()
        cids = []
        for raw in raw_matches:
            self.rate_limit_sleep()  # FIX-4: rate limiting в upsert цикле
            if is_shutdown_requested():
                self._log("WARN", "Shutdown requested, stopping upsert",
                          processed=len(cids), remaining=len(raw_matches) - len(cids))
                break
            try:
                start = time.monotonic()
                # Извлекаем обязательные поля
                home = raw.get("home_team", "")
                away = raw.get("away_team", "")
                date_utc = raw.get("date_utc", "")
                competition = raw.get("competition", "")
                country = raw.get("country", "")

                # FIX-2: нормализация команд через team_registry
                try:
                    from team_registry import normalize_team_name
                    home = normalize_team_name(home) or home
                    away = normalize_team_name(away) or away
                except ImportError:
                    pass

                # Extra поля (§1.13: **extra, не extra=extra)
                extra = {}
                for k in ("source_ids", "odds", "predictions", "h2h", "stats", "score"):
                    if k in raw:
                        extra[k] = raw[k]

                # FIX-3: idempotency_key для upsert (§23.2)
                date_key = (date_utc or "")[:10]
                idempotency_key = f"{self.source}:{home}:{away}:{date_key}"
                cid = hub.upsert_match(
                    home_team=home,
                    away_team=away,
                    date_utc=date_utc,
                    competition=competition,
                    country=country,
                    source=self.source,
                    idempotency_key=idempotency_key,
                    **extra
                )
                if cid:
                    cids.append(cid)
                    self.created += 1
                    self.metrics.inc("upsert_match")
                else:
                    self.skipped += 1
                    self.metrics.inc("skip_past_match")
                self.metrics.time("upsert_match_total", time.monotonic() - start)
            except Exception as e:
                self.error_count += 1
                self.metrics.inc("errors")
                self._log("WARN", "Upsert failed", error=str(e),
                          home=raw.get("home_team"), away=raw.get("away_team"))
        return cids

    # ── Шаг 4: Patch / Enrich ─────────────────────────────────────────
    def patch_matches(self, cids: list[str], raw_matches: list[dict]):
        """
        Шаг 4: enrichment через patch_match() (§19.4).
        Подклассы могут переопределить для кастомного enrichment.
        """
        hub = self._get_hub()
        for i, cid in enumerate(cids):
            self.rate_limit_sleep()  # FIX-4: rate limiting в patch цикле
            if is_shutdown_requested():
                self._log("WARN", "Shutdown requested, stopping patch",
                          processed=i, remaining=len(cids) - i)
                break
            if i >= len(raw_matches):
                break
            raw = raw_matches[i]
            try:
                # Patch odds (если есть)
                if "odds" in raw:
                    start = time.monotonic()
                    # §23.2: idempotency_key
                    idempotency_key = f"{self.source}:{cid}:odds:{now_msk()[:10]}"
                    hub.patch_match(
                        cid, "odds", raw["odds"],
                        source=self.source,
                        upstream=self._get_upstream(),
                        idempotency_key=idempotency_key,
                    )
                    self.patched += 1
                    self.metrics.inc("patch_match")
                    self.metrics.time("patch_match_total", time.monotonic() - start)

                # Patch stats / predictions / h2h — FIX-3: idempotency_key для всех
                for section in ("stats", "predictions", "h2h"):
                    if section in raw:
                        section_idem = f"{self.source}:{cid}:{section}:{now_msk()[:10]}"
                        hub.patch_match(cid, section, raw[section],
                                       source=self.source,
                                       idempotency_key=section_idem)
                        self.metrics.inc(f"patch_{section}")
            except Exception as e:
                self.error_count += 1
                self.metrics.inc("errors")
                self._log("WARN", "Patch failed", cid=cid, error=str(e))

    def _get_upstream(self) -> str:
        """Возвращает upstream для источника (§1.20). FIX-1: из UPSTREAM_MAP хаба."""
        try:
            import gatekeeper_hub
            upstream_map = getattr(gatekeeper_hub, "UPSTREAM_MAP", {})
            if upstream_map:
                return upstream_map.get(self.source, "unknown")
        except ImportError:
            pass
        # Fallback — синхронизирован с UPSTREAM_MAP в хабе
        upstream_map = {
            "sharpapi": "betradar",
            "odds_api": "betradar",
            "bzzoiro": "opta",
            "propline": "pinnacle",
            "football_data": "bet365",
        }
        return upstream_map.get(self.source, "unknown")

    # ── Шаг 5: Save Meta ──────────────────────────────────────────────
    def save_meta(self):
        """
        Шаг 5: сохранение мета (§19.4, §24.5).
        Автоматически включает метрики (§24.5).
        """
        hub = self._get_hub()
        meta = {
            "last_run": now_msk(),
            "error_count": self.error_count,
            "stored_matches": self.created,
            "skipped": self.skipped,
            "patched": self.patched,
            "metrics": self.metrics.report(),
        }
        # Дополнительные поля из подкласса
        extra_meta = self._extra_meta()
        meta.update(extra_meta)
        hub.save_meta(self.source, **meta)
        self._log("INFO", "Collection complete",
                  created=self.created, skipped=self.skipped,
                  patched=self.patched, errors=self.error_count)

    def _extra_meta(self) -> dict:
        """Дополнительные мета-поля (переопределяется подклассами)."""
        return {}

    # ── Главный цикл ──────────────────────────────────────────────────
    def run(self):
        """
        Главный цикл коллектора: 5 шагов.
        Подклассы вызывают collector.run() — весь алгоритм выполняется автоматически.
        FIX-5: shutdown-чеки между шагами — при SIGTERM сохраняет мета и выходит.
        """
        self._log("INFO", "Starting collection", source=self.source)
        self.run_initialization()

        if is_shutdown_requested():
            self._log("WARN", "Shutdown before fetch", source=self.source)
            self.save_meta()
            return

        raw_matches = self.fetch_events()
        self._log("INFO", "Fetched events", count=len(raw_matches))

        if is_shutdown_requested():
            self._log("WARN", "Shutdown before upsert", source=self.source)
            self.save_meta()
            return

        cids = self.upsert_matches(raw_matches)
        self._log("INFO", "Upserted matches", created=len(cids), skipped=self.skipped)

        if is_shutdown_requested():
            self._log("WARN", "Shutdown before patch, saving partial results",
                      source=self.source)
            self.save_meta()
            return

        self.patch_matches(cids, raw_matches)

        self.save_meta()
        self._log("INFO", "Done", source=self.source)

    # ── Утилиты ───────────────────────────────────────────────────────
    def _log(self, level: str, message: str, **context):
        """Структурный лог (§20.6)."""
        ts = now_msk()
        ctx = json.dumps(context, ensure_ascii=False, default=str) if context else ""
        print(f"[{ts}] [{self.source}] [{level}] {message} | {ctx}")

    def rate_limit_sleep(self):
        """Rate limiting (§1.8)."""
        if self.rate_delay > 0:
            time.sleep(self.rate_delay)

    def retry_with_backoff(self, func, *args, **kwargs):
        """Ретрай с экспоненциальной задержкой (§1.8)."""
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == self.max_retries - 1:
                    self._log("ERROR", "Max retries exceeded", error=str(e))
                    return None
                delay = 2 ** (attempt + 1)
                self._log("WARN", f"Retry {attempt+1}/{self.max_retries}",
                          delay=delay, error=str(e))
                time.sleep(delay)
        return None


# ── Версия ────────────────────────────────────────────────────────────
__version__ = "2.2"


# ── Публичный API ─────────────────────────────────────────────────────
__all__ = [
    "BaseCollector",
    "Metrics",
    "timed",
    "now_msk",
    "is_shutdown_requested",
    "install_shutdown_handler",
    "__version__",
]
