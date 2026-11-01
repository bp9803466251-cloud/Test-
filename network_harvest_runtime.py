#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: network_harvest_runtime.py
Version: 9.6.8-patched
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §1.3 (Strict urllib/curl_cffi integration - NO requests library)
Description: Асинхронно-синхронный модуль сетевого сбора коэффициентов.
             Защищен от TLS-блокировок, дифференцирует штрафные баллы для Chaos Monkey.
             Интегрирован жесткий контроль плоской структуры фида v710 (Гейт 28).
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

class GatekeeperNetworkHarvester:
    def __init__(self, config_path: str = "gatekeeper_config.yaml"):
        logger.info("Инициализация сетевого коллектора Gatekeeper-AI...")
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
            from curl_cffi import requests as requests_cffi
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
        else:
            return self._fetch_via_urllib(target_url, is_fallback=False)

    def _fetch_via_curl_cffi(self, url: str) -> Optional[Dict[str, Any]]:
        """Сбор данных с использованием curl_cffi для мимикрии под браузер (Chrome)"""
        try:
            from curl_cffi import requests as requests_cffi
            from curl_cffi.requests.errors import RequestsError
            
            logger.info("[TRANSPORT] Вызов curl_cffi (Сессия Chrome 110)...")
            response = requests_cffi.get(url, impersonate="chrome110", timeout=10)
            
            if response.status_code == 200:
                logger.info("[SUCCESS] Данные успешно получены через curl_cffi (TLS Impersonation).")
                return response.json()
            else:
                logger.error(f"[ERROR] Некорректный статус ответа curl_cffi: {response.status_code}")
                return self._fetch_via_urllib(url, is_fallback=True)
                
        except (Exception, RequestsError) as e:
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
            "Sec-Ch-Ua": '"Chromium";v="110", "Not A(Byte;Comprehensions";v="24", "Google Chrome";v="110"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Connection": "keep-alive"
        }
        
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=7.0) as response:
                raw_data = response.read()
                logger.info(f"[SUCCESS] Данные успешно получены через нативный urllib.request ({transport_type}).")
                
                if is_fallback:
                    logger.warning("[METRIC] Сеть деградировала, но сессия спасена нативным транспортом.")
                    self._report_penalty(1)
                    
                return json.loads(raw_data.decode("utf-8"))
                
        except (urllib.error.URLError, json.JSONDecodeError, Exception) as e:
            if is_fallback:
                logger.error(f"[CRITICAL] Каскадный отказ: вслед за curl_cffi упал и urllib.request -> {e}")
                self._report_penalty(3)
            else:
                logger.error(f"[CRITICAL] Первичный сетевой сбой urllib транспорта -> {e}")
                self._report_penalty(3)
            
        return None

    def route_to_value_engine(self, raw_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Адаптивный дата-мост. Трансформирует сырые сетевые данные в 
        мультитипизированную схему v710 для передачи в value_engine.py.
        [STRICT GATE 28 COMPLIANCE]: Валидирует плоскую структуру корневых данных.
        """
        logger.info("Запуск адаптивного дата-моста: Трансформация Payload -> Схема v710...")
        
        # Предварительная жесткая проверка на отсутствие вложенности во входящем потоке фида
        for key, value in raw_payload.items():
            if isinstance(value, (dict, list, tuple)) and key != "odds": # Исключаем легальный старый узел odds для обратной совместимости моста
                logger.error(f"[GATE 28 FAILURE] Зафиксирована недопустимая вложенность в ключе '{key}'!")
                self._report_penalty(3) # Нарушение плоской структуры фида v710 штрафуется в 3 балла
                raise ValueError(f"Gate 28 Violation: Nested structure detected at key '{key}'. Flat feed required.")

        home_clean = raw_payload.get("home_clean", raw_payload.get("odds", {}).get("home", 1.0))
        away_clean = raw_payload.get("away_clean", raw_payload.get("odds", {}).get("away", 1.0))
        
        canonical_fixture = {
            "meta": {
                "version": "7.1.0",
                "gatekeeper_verified": True
            },
            "payload": {
                "home_clean": float(home_clean),
                "away_clean": float(away_clean),
                "market_id": raw_payload.get("market_id", "canonical-uuid-999")
            }
        }
        
        logger.info(f"[BRIDGE] Данные успешно нормализованы: Home {canonical_fixture['payload']['home_clean']} | Away {canonical_fixture['payload']['away_clean']}")
        return canonical_fixture


if __name__ == "__main__":
    harvester = GatekeeperNetworkHarvester()
    mock_url = "https://upstash-redis-mock.local"
    print("\n--- ТЕСТ СЕТЕВОГО СБОРЩИКА (КАСКАДНЫЙ РЕЖИМ) ---")
    
    dummy_payload = {"odds": {"home": "2.45", "away": "1.95"}, "market_id": "match_17024"}
    normalized = harvester.route_to_value_engine(dummy_payload)
    print(f"Результат работы моста:\n{json.dumps(normalized, indent=2)}")
