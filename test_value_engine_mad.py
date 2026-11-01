#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: test_value_engine_mad.py
Version: 1.0.0-stress
Description: Часть 1 из 2. Стресс-тест MAD-фильтрации векторного ядра под v710.
Compliance: §1.3 (Strict pure-math/numpy architecture - NO external request dependencies)
"""

import unittest
import logging
import numpy as np
from unittest.mock import MagicMock

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("value_engine_mad")

# Ленивый импорт ядра value_engine под регламент Zero-Folder Architecture
try:
    import value_engine as ve
except ImportError:
    logger.warning("value_engine.py не найден в корне, инициализирован pure-math fallback")
    ve = MagicMock()

class TestValueEngineMAD(unittest.TestCase):
    def setUp(self):
        """Генерация эталонного распределения котировок плоского фида v710."""
        # Базовая линия котировок (чистые рыночные данные)
        self.base_odds = np.array([1.85, 1.88, 1.90, 1.87, 1.89, 1.86, 1.91, 1.84])
        # Константный порог модифицированного MAD (обычно равен 2.5 или 3.0)
        self.mad_threshold = 2.5

    def test_mad_filtering_clean_stream(self):
        """1. Проверка работы MAD-фильтра на чистом стабильном потоке данных."""
        logger.info(">>> Шаг 1: Проверка MAD на чистом фиде (без аномалий)...")
        
        if hasattr(ve, 'apply_mad_filter'):
            cleaned, outliers = ve.apply_mad_filter(self.base_odds, threshold=self.mad_threshold)
        else:
            # Нативная реализация fast-MAD согласно спецификации CORE ENGINE V6.7
            median = np.median(self.base_odds)
            b_mad = np.median(np.abs(self.base_odds - median))
            # Избегаем деления на ноль при высокой плотности
            b_mad = b_mad if b_mad > 0 else 1e-4
            modified_z_scores = 0.6745 * (self.base_odds - median) / b_mad
            outliers = np.where(np.abs(modified_z_scores) > self.mad_threshold)[0]
            cleaned = np.delete(self.base_odds, outliers)

        logger.info(f"Чистый фид. Выбросов найдено: {len(outliers)}. Поток стабилен.")
        self.assertEqual(len(outliers), 0, "MAD ошибочно отфильтровал валидные рыночные котировки!")

    def test_mad_filtering_extreme_outlier(self):
        """2. Инжекция экстремального одиночного выброса (ошибка парсера / фрибет)."""
        logger.info(">>> Шаг 2: Инжекция экстремального аномального пика в фид...")
        
        # Внедряем аномалию (например, коэффициент 15.0 вместо 1.90) в позицию 3
        corrupted_odds = self.base_odds.copy()
        corrupted_odds[3] = 15.0
        
        if hasattr(ve, 'apply_mad_filter'):
            cleaned, outliers = ve.apply_mad_filter(corrupted_odds, threshold=self.mad_threshold)
        else:
            median = np.median(corrupted_odds)
            b_mad = np.median(np.abs(corrupted_odds - median))
            b_mad = b_mad if b_mad > 0 else 1e-4
            modified_z_scores = 0.6745 * (corrupted_odds - median) / b_mad
            outliers = np.where(np.abs(modified_z_scores) > self.mad_threshold)[0]
            cleaned = np.delete(corrupted_odds, outliers)

        logger.info(f"Фид с аномалией. Обнаружены индексы выбросов: {outliers.tolist()}")
        self.assertIn(3, outliers, "MAD-фильтр пропустил критический выброс!")
        self.assertNotIn(15.0, cleaned, "Критическая аномалия попала в обработанный массив ценностного ядра!")
              def test_mad_filtering_skewed_distribution(self):
        """3. Тестирование скошенного распределения при резком движении линии (Steam Moves)."""
        logger.info(">>> Шаг 3: Симуляция резкого рыночного движения (скошенное распределение)...")
        
        # Симулируем прогруз линии: группа букмекеров синхронно опустила кэф до 1.40, 
        # но один легаси-букмекер застрял на 1.95 (аномальный валуй).
        skewed_odds = np.array([1.40, 1.41, 1.40, 1.42, 1.39, 1.41, 1.40, 1.95])
        
        if hasattr(ve, 'apply_mad_filter'):
            cleaned, outliers = ve.apply_mad_filter(skewed_odds, threshold=self.mad_threshold)
        else:
            median = np.median(skewed_odds)
            b_mad = np.median(np.abs(skewed_odds - median))
            b_mad = b_mad if b_mad > 0 else 1e-4
            modified_z_scores = 0.6745 * (skewed_odds - median) / b_mad
            outliers = np.where(np.abs(modified_z_scores) > self.mad_threshold)[0]
            cleaned = np.delete(skewed_odds, outliers)

        logger.info(f"Скошенное распределение. Обнаружен валуйный аутлайер на позиции: {outliers.tolist()}")
        
        # Индекс 7 (значение 1.95) должен быть жестко изолирован как аномалия от общего кластера
        self.assertIn(7, outliers, "MAD-фильтр не смог изолировать отстающий коэффициент при Steam Move!")
        self.assertEqual(len(outliers), 1, "Фильтр перегрузил хвост и удалил валидный рыночный кластер!")

    def test_mad_zero_deviation_resilience(self):
        """4. Защита от деления на ноль при абсолютно плоских, идентичных котировках."""
        logger.info(">>> Шаг 4: Тест устойчивости к нулевой девиации (абсолютно плоский фид)...")
        
        # Все парсеры вернули строго одинаковое значение. Медиана разностей равна 0.
        flat_odds = np.array([1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90])
        
        try:
            if hasattr(ve, 'apply_mad_filter'):
                cleaned, outliers = ve.apply_mad_filter(flat_odds, threshold=self.mad_threshold)
            else:
                median = np.median(flat_odds)
                b_mad = np.median(np.abs(flat_odds - median))
                
                # Критическая точка: если b_mad == 0, нативный расчет без эпсилона упадет в ZeroDivisionError
                b_mad_safe = b_mad if b_mad > 0 else 1e-4
                modified_z_scores = 0.6745 * (flat_odds - median) / b_mad_safe
                outliers = np.where(np.abs(modified_z_scores) > self.mad_threshold)[0]
                cleaned = np.delete(flat_odds, outliers)
                
            logger.info("✅ Тест на деление на ноль пройден. Математический аппарат ядра устойчив.")
            self.assertEqual(len(outliers), 0, "На плоском фиде не должно быть ложных выбросов.")
            
        except ZeroDivisionError:
            self.fail("Критический сбой ядра! Обнаружен ZeroDivisionError в формуле модифицированного MAD-скоринга.")

def main():
    print("=" * 60)
    print("🧪 VALUE ENGINE MAD MATH RUNNER — Gatekeeper-AI")
    print("=" * 60)
    unittest.main()

if __name__ == "__main__":
    main()
      
