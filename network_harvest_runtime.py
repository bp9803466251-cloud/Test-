#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: network_harvest_runtime.py
Version: 9.6.8-patched (Аудит: Гейт 28 whitelist, плоский мост v710, TLS-дефолт)
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §1.3 (Strict urllib/curl_cffi integration - NO requests library)
Description: Асинхронно-синхронный модуль сетевого сбора коэффициентов.
             Защищен от TLS-блокировок, дифференцирует штрафные баллы для Chaos Monkey.
             Гейт 28 с whitelist легальных вложенных узлов схемы v710 (odds_1x2, stats, odds).
             Мост отдаёт плоский паспорт v710 (§1.6: canonical_id добивает хаб).
"""

import json
import logging
import urllib.request
import urllib.error
import ssl
from typing import Dict, Any, Optional

# Настройка логирования под стандарты экосистемы Gatekeeper
logging.basicConfig(level=logging.INFO, format="[HARVEST] %(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("network_harvest")

__version__ = "9.6.8-patched"

# [FIX v9.6.8] Whitelist легальных вложенных узлов по Схеме v710.
# odds_1x2 — плоский словарь котировок ("1"/"X"/"2"), stats — объект метрик,
# odds — легаси-узел обратной совместимости моста. Всё остальное — нарушение Гейта 28.
GATE28_NESTED_WHITELIST = {"odds_1x2", "stats", "odds"}

# [FIX v9.6.8] Карта нормализации легаси-статусов (синхронно с hub и schema_validator)
LEGACY_STATUS_MAP = {
    "awarded": "completed", "forfeited": "completed",
    "ended": "completed", "finished": "completed",
    "notstarted": "scheduled",
}

# Валидные статусы для валидации котировок
_TERMINAL = {"completed", "postponed", "cancelled"}


def _local_clean_team_name(name: str) -> str:
    """Локальный fallback §19.2 на случай отсутствия search_module в корне."""
    if not name:
        return ""
    cleaned = str(name).strip().lower()
    cleaned = "".join(ch if (ch.isalnum() or ch in (" ", "_", "-")) else " " for ch in cleaned)
    return "_".join(cleaned.split())


def _get_clean_team_name():
    """§19.2: clean-нормализация через search_module с локальным fallback."""
    try:
        from search_module import clean_team_name
        return clean_team_name
    except Exception:
        return _local_clean_team_name


class GatekeeperNetworkHarvester:
    def __init__(self, config_path: str = "gatekeeper_config.yaml"):
        logger.info("Инициализация сетевого коллектора Gatekeeper-AI (v%s)...", __version__)
        self.config_path = config_path
        self.use_curl_cffi = True

        # Ленивый импорт Chaos Monkey для динамической отправки штрафных баллов
        self.chaos_monkey = None
        try:
            import chaos_monkey_runtime
            self.chaos_monkey = chaos_monkey_runtime
            logger.info("[INTEGRITY] Интеграция с модулем деградации chaos_monkey_runtime активна.")
        except ImportError:
            logger.warning("[WARNING] Модуль chaos_monkey_runtime не найден в корне. Метрики CB не будут отправлены.")

        # Предварительная проверка доступности curl_cffi
        try:
            from curl_cffi import requests as requests_cffi  # noqa: F401
            logger.info("[INTEGRITY] Модуль curl_cffi успешно импортирован. Защита TLS-Fingerprint active.")
        except ImportError:
            logger.warning("[WARNING] Модуль curl_cffi не найден. Автоматический переход на нативный urllib.request.")
            self.use_curl_cffi = False

    def _report_penalty(self, score: int) -> None:
        """Внутренний хелпер отправки дифференцированного веса ошибок в Circuit Breaker"""
        if self.chaos_monkey and hasattr(self.chaos_monkey, "register_failure_score"):
            try:
                self.chaos_monkey.register_failure_score(score)
                logger.info(f"[CIRCUIT BREAKER] Отправлен штрафной балл: {score}")
            except Exception as e:
                logger.error(f"[CIRCUIT BREAKER] Ошибка при логировании штрафа в Chaos Monkey: {e}")

    def fetch_odds_data(self, target_url: str) -> Optional[Dict[str, Any]]:
        """
        Запрос сырых данных коэффициентов с целевого эндпоинта.
        [COMPLIANCE] Строго контролирует отсутствие вызовов стандартной библиотеки requests.
        """
        logger.info(f"Запуск сбора данных с эндпоинта: {target_url}")

        if self.use_curl_cffi:
            return self._fetch_via_curl_cffi(target_url)
        return self._fetch_via_urllib(target_url, is_fallback=False)

    def _fetch_via_curl_cffi(self, url: str) -> Optional[Dict[str, Any]]:
        """Сбор данных с использованием curl_cffi для мимикрии под браузер (Chrome)"""
        try:
            from curl_cffi import requests as requests_cffi
        except Exception:
            logger.warning("[FAILOVER] curl_cffi недоступен на этапе вызова. Переход на urllib.")
            return self._fetch_via_urllib(url, is_fallback=True)

        try:
            logger.info("[TRANSPORT] Вызов curl_cffi (Сессия Chrome 110)...")
            response = requests_cffi.get(url, impersonate="chrome110", timeout=10)

            if response.status_code == 200:
                logger.info("[SUCCESS] Данные успешно получены через curl_cffi (TLS Impersonation).")
                return response.json()
            logger.error(f"[ERROR] Некорректный статус ответа curl_cffi: {response.status_code}")
            return self._fetch_via_urllib(url, is_fallback=True)

        except Exception as e:
            # [FIX v9.6.8] Устойчивый каскад: импорт исключений curl_cffi непортабелен
            # (RequestsError в разных версиях живёт в requests.errors / requests.exceptions),
            # поэтому перехватываем обобщающий класс — NameError в except невозможен.
            logger.warning(f"[FAILOVER] Перехвачен TLS-дроп/Таймаут curl_cffi: {e}. Каскадный спуск на urllib.")
            return self._fetch_via_urllib(url, is_fallback=True)

    def _fetch_via_urllib(self, url: str, is_fallback: bool = False) -> Optional[Dict[str, Any]]:
        """Резервный нативный транспорт строго в рамках регламента §1.3"""
        transport_type = "CASCADE FALLBACK" if is_fallback else "PRIMARY"
        logger.info(f"[TRANSPORT] Активация нативного urllib.request конвейера ({transport_type})...")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            # [FIX v9.6.8] Корректный Client Hints Chrome 110 (был битый 'Not A(Byte;Comprehensions')
            "Sec-Ch-Ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Connection": "keep-alive"
        }

        try:
            # [FIX v9.6.8] Безопасный дефолт TLS: проверка сертификатов включена.
            # CERT_NONE открывал канал MITM между коллектором и источником котировок.
            ctx = ssl.create_default_context()

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=7.0) as response:
                raw_data = response.read()
                logger.info(f"[SUCCESS] Данные успешно получены через нативный urllib.request ({transport_type}).")

                if is_fallback:
                    logger.warning("[METRIC] Сеть деградировала, но сессия спасена нативным транспортом.")
                    self._report_penalty(1)

                return json.loads(raw_data.decode("utf-8"))

        except Exception as e:
            # [FIX v9.6.8] Редундантный кортеж (URLError, JSONDecodeError, Exception) сведён к Exception
            if is_fallback:
                logger.error(f"[CRITICAL] Каскадный отказ: вслед за curl_cffi упал и urllib.request -> {e}")
                self._report_penalty(3)
            else:
                logger.error(f"[CRITICAL] Первичный сетевой сбой urllib транспорта -> {e}")
                self._report_penalty(3)

        return None

    # ── [NEW v9.6.8] Конвертация легаси-фида в плоский v710 ──────────────────
    def _normalize_legacy_odds(self, raw_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Легаси-фид odds.1x2.current {home/draw/away} -> плоский odds_1x2 {"1"/"X"/"2"}.
        Плюс нормализация легаси-статусов (синхронно с hub и schema_validator).
        """
        payload = dict(raw_payload)

        legacy_odds = (raw_payload.get("odds") or {}).get("1x2", {}).get("current", {})
        if legacy_odds:
            payload["odds_1x2"] = {
                "1": legacy_odds.get("home", legacy_odds.get("home_clean")),
                "X": legacy_odds.get("draw"),
                "2": legacy_odds.get("away", legacy_odds.get("away_clean")),
            }

        status = payload.get("status")
        if isinstance(status, str) and status in LEGACY_STATUS_MAP:
            payload["status"] = LEGACY_STATUS_MAP[status]

        return payload

    def _extract_1x2_line(self, payload: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """
        Извлечение валидной линии 1X2 (плоская v710 или легаси-вложенная).
        [FIX v9.6.8] Без валидной линии возвращает None — фейковые дефолты 1.0/1.0 убраны.
        """
        flat = payload.get("odds_1x2")
        if isinstance(flat, dict):
            try:
                line = {
                    "1": float(flat.get("1", flat.get("home"))),
                    "X": float(flat.get("X", flat.get("draw"))),
                    "2": float(flat.get("2", flat.get("away"))),
                }
            except (TypeError, ValueError):
                return None
            if all(v > 1.0 for v in line.values()):
                return line
            return None

        legacy = (payload.get("odds") or {}).get("1x2", {}).get("current", {})
        if isinstance(legacy, dict):
            try:
                line = {
                    "1": float(legacy.get("home", legacy.get("home_clean"))),
                    "X": float(legacy.get("draw")),
                    "2": float(legacy.get("away", legacy.get("away_clean"))),
                }
            except (TypeError, ValueError):
                return None
            if all(v > 1.0 for v in line.values()):
                return line
        return None

    # ── ГЕЙТ 28: контроль плоской структуры фида v710 ────────────────────────
    def _gate28_check(self, raw_payload: Dict[str, Any]) -> None:
        """
        [STRICT GATE 28 COMPLIANCE]: Валидирует плоскую структуру корневых данных.
        [FIX v9.6.8] Введён whitelist GATE28_NESTED_WHITELIST: корректный плоский фид
        v710 содержит легальные словари odds_1x2 и stats — раньше они штрафовались
        в 3 балла и валили мост (коллектор наказывал сам себя за честную работу).
        Вложенность вне whitelist по-прежнему штрафуется в 3 балла Circuit Breaker.
        """
        for key, value in raw_payload.items():
            if isinstance(value, (dict, list, tuple)) and key not in GATE28_NESTED_WHITELIST:
                logger.error(f"[GATE 28 FAILURE] Зафиксирована недопустимая вложенность в ключе '{key}'!")
                self._report_penalty(3)  # Нарушение плоской структуры фида v710 штрафуется в 3 балла
                raise ValueError(f"Gate 28 Violation: Nested structure detected at key '{key}'. Flat feed required.")

    def route_to_value_engine(self, raw_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Адаптивный дата-мост. Трансформирует сырые сетевые данные в плоский паспорт v710
        для передачи в value_engine.py.
        [FIX v9.6.8]:
          * мост больше НЕ строит вложенную обёртку meta/payload — отдаёт плоский паспорт;
          * clean-имена через search_module.clean_team_name (§19.2), а не котировки
            (раньше home_clean/away_clean наполнялись значениями odds.home/odds.away!);
          * canonical_id здесь НЕ генерируется — по §1.6 его добивает хаб;
          * без валидной линии 1X2 возвращается None (фейковые дефолты 1.0/1.0 убраны).
        """
        logger.info("Запуск адаптивного дата-моста: Трансформация Payload -> Схема v710...")
        if not isinstance(raw_payload, dict) or not raw_payload:
            logger.error("[BRIDGE] Пустой или несловарный фид — мост отклонён.")
            return None

        payload = self._normalize_legacy_odds(raw_payload)

        # Гейт 28 — на нормализованном фиде (после снятия легаси-обёртки odds)
        self._gate28_check(payload)

        line = self._extract_1x2_line(payload)
        if line is None:
            logger.warning("[BRIDGE] Валидная линия 1X2 отсутствует — матч пропущен без фейковых котировок.")
            return None

        # ── §19.2: clean-имена команд (НЕ котировки!) ──
        clean_fn = _get_clean_team_name()
        home_clean = clean_fn(payload.get("home") or payload.get("home_team") or payload.get("home_clean", ""))
        away_clean = clean_fn(payload.get("away") or payload.get("away_team") or payload.get("away_clean", ""))

        if not home_clean or not away_clean:
            logger.error("[BRIDGE] Отсутствуют имена команд — паспорт не собран.")
            return None

        # Плоский паспорт v710. canonical_id добивает хаб (§1.6).
        passport = {
            "schema_version": "v710",
            "home_team": payload.get("home") or payload.get("home_team") or home_clean.title(),
            "away_team": payload.get("away") or payload.get("away_team") or away_clean.title(),
            "home_clean": home_clean,
            "away_clean": away_clean,
            "date_utc": payload.get("date_utc", payload.get("match_date", "")),
            "status": payload.get("status", "scheduled"),
            "competition": payload.get("competition", payload.get("league", "")),
            "odds_1x2": line,
        }

        # Перенос метрик, если источник их прислал
        if isinstance(payload.get("stats"), dict):
            passport["stats"] = payload["stats"]

        logger.info(
            f"[BRIDGE] Паспорт v710 собран: {home_clean} vs {away_clean} | "
            f"1X2 {line['1']}/{line['X']}/{line['2']} | status={passport['status']}"
        )
        return passport


if __name__ == "__main__":
    harvester = GatekeeperNetworkHarvester()
    print("\n--- ТЕСТ СЕТЕВОГО СБОРЩИКА v9.6.8 (плоский фид v710) ---")

    # 1. Плоский фид v710 — корректный проход через Гейт 28
    flat_payload = {
        "home": "Manchester City", "away": "Arsenal",
        "date_utc": "2026-11-05T20:00:00Z", "status": "scheduled",
        "competition": "premier_league",
        "odds_1x2": {"1": 2.10, "X": 3.40, "2": 3.30},
        "stats": {"xg_home": 1.8, "xg_away": 1.2},
        "market_id": "match_17024",
    }
    print(json.dumps(harvester.route_to_value_engine(flat_payload), indent=2, ensure_ascii=False))

    # 2. Легаси-фид — конвертация в плоский вид
    legacy_payload = {"odds": {"1x2": {"current": {"home": 2.10, "draw": 3.40, "away": 3.30}}},
                      "status": "notstarted"}
    print(json.dumps(harvester.route_to_value_engine(legacy_payload), indent=2, ensure_ascii=False))

    # 3. Хаос-атака: недопустимая вложенность -> Gate 28 Violation
    try:
        harvester.route_to_value_engine({"bookmakers": {"b1": {"o": 2.0}}})
    except ValueError as e:
        print(f"[OK] Хаос перехвачен: {e}")
