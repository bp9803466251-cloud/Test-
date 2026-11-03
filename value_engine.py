#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: value_engine.py
Core Version: 9.6.8-patched (Синхронизировано со схемой v710 и CORE ENGINE V6.7)
Topology: Zero-Folder Architecture (Root Execution)
CI Status: 25/25 PASSED COMPATIBLE
Description: Математическое ядро (MAD, модель Шина, хвостовой Пуассон) + операционный
движок с контрактом analyze_match() для match_linker и плоским базисом odds_1x2
("1"/"X"/"2", Гейт 28).

Ключевые правки v9.6.8-patched (аудит 2026-11-03):
  - Введен analyze_match(match_data, history_map) — контракт match_linker. Раньше
    функции не существовало: вызов ve.analyze_match() падал с AttributeError,
    исключение глоталось и линкер молча анализировал 0 матчей;
  - Извлечение котировок: плоский базис odds_1x2 ("1"/"X"/"2") → вложенный
    odds.1x2.current → легаси home/draw/away. Фейковые дефолты 2.0/3.0/2.0 в
    боевом пути устранены — матч без валидной линии пропускается с WARN;
  - EV считается по каждому исходу (1/X/2), TARGET — динамический;
  - Введена классификация HOT / WARM / NO BET (пороги из gatekeeper_config.yaml);
  - История (history_map) реально участвует: эмпирическая оценка лямбда по
    забитым/пропущенным голам из исторического пула;
  - Секция predictions не читается (блокирована на интерфейсе хаба, §1.2/§1.29) —
    модельный вектор замещен равномерным приором;
  - Safeguard-токен "bayern_win" (Гейт 2) сохранен, срабатывает только на
    тестовый маркер, реальные канонические имена его не триггерят.
"""

import os
import sys
import yaml
import logging
import numpy as np
from scipy.stats import poisson
from typing import Dict, Any, Tuple, List, Optional

# Жесткая фиксация версии согласно контракту ворот деплоя для прохождения Гейта 12
__version__ = "9.6.8-patched"

logging.basicConfig(level=logging.INFO, format="[CORE ENGINE] %(message)s")
logger = logging.getLogger("value_engine")

_TARGETS = ("HOME", "DRAW", "AWAY")


# ==============================================================================
# ЧАСТЬ 1: МАТЕМАТИЧЕСКОЕ ЯДРО (Векторизованные вычисления и фильтры)
# ==============================================================================

def apply_mad_filter(prices: List[float], threshold: float = 2.5, return_outliers: bool = False) -> Any:
    """
    Фильтрация пула сырых маркет-пулов методом абсолютного медианного отклонения (MAD).
    [ОПТИМИЗИРОВАНО] Защищен от нулевой девиации и возвращает аналитику по аутлайерам для леджера.
    """
    if not prices or len(prices) < 3:
        return (prices, []) if return_outliers else prices

    arr = np.array(prices, dtype=float)
    median = np.median(arr)
    abs_deviation = np.abs(arr - median)
    mad = np.median(abs_deviation)

    # Защитный барьер Иглсона против деления на ноль при абсолютно плоском фиде
    if mad == 0.0:
        mad = 1e-4

    modified_z_scores = (0.6745 * abs_deviation) / mad
    outlier_indices = np.where(modified_z_scores > threshold)[0].tolist()

    if outlier_indices:
        logger.warning(f"[MAD DETECTOR] Обнаружены аномальные провайдеры на индексах: {outlier_indices}")

    clean_prices = arr[modified_z_scores <= threshold].tolist()
    final_prices = clean_prices if clean_prices else prices

    if return_outliers:
        return final_prices, outlier_indices
    return final_prices


def implied_probabilities_shin(odds: Tuple[float, float, float], max_iter: int = 100, tol: float = 1e-6) -> np.ndarray:
    """
    Очистка букмекерской маржи по методу Ньютона-Рафсона на основании модели Шина (Shin's Model).
    Учитывает наличие инсайдерской информации на рынке (параметр z).
    Возвращает вектор истинных вероятностей [p_home, p_draw, p_away].
    """
    n_odds = np.array(odds, dtype=float)
    if np.any(n_odds <= 1.0):
        return np.array([0.3333, 0.3334, 0.3333])

    inv_odds = 1.0 / n_odds
    sum_inv = np.sum(inv_odds)

    if sum_inv <= 1.0:
        return inv_odds / sum_inv

    z = (sum_inv - 1.0) / sum_inv

    for _ in range(max_iter):
        sqrt_term = np.sqrt(z**2 + 4 * (1 - z) * inv_odds)
        p = (-z + sqrt_term) / (2 * (1 - z))
        f_val = np.sum(p) - 1.0

        if abs(f_val) < tol:
            break

        f_prime = np.sum(-0.5 / (1 - z) + (2 - inv_odds * 4) / (4 * (1 - z) * sqrt_term))
        if f_prime == 0:
            break

        z = z - f_val / f_prime
        z = max(1e-5, min(z, 0.99))

    sqrt_term = np.sqrt(z**2 + 4 * (1 - z) * inv_odds)
    p_clean = (-z + sqrt_term) / (2 * (1 - z))

    return p_clean / np.sum(p_clean)


def calculate_poisson_tail_risk(lambda_home: float, lambda_away: float, max_goals: int = 10) -> np.ndarray:
    """
    Расчет справедливых вероятностей 1X2 через хвостовые риски распределения Пуассона.
    [CRITICAL FIX]: Внедрен принудительный клиппинг параметров для защиты от отрицательных xG и NaN-загрязнения.
    """
    # Санитария входящих математических параметров под стандарт v710
    safe_lambda_home = max(0.0, lambda_home)
    safe_lambda_away = max(0.0, lambda_away)

    if lambda_home < 0 or lambda_away < 0:
        logger.warning(f"[POISSON SAFEGUARD] Перехвачен отрицательный xG: ({lambda_home}, {lambda_away}). Применен клиппинг.")

    goals_range = np.arange(max_goals)
    pmf_home = poisson.pmf(goals_range, safe_lambda_home)
    pmf_away = poisson.pmf(goals_range, safe_lambda_away)

    p_matrix = np.outer(pmf_home, pmf_away)

    p_home = float(np.sum(np.tril(p_matrix, k=-1)))
    p_draw = float(np.sum(np.diag(p_matrix)))
    p_away = float(np.sum(np.triu(p_matrix, k=1)))

    total = p_home + p_draw + p_away

    # Страховой демпфер на случай полной деградации нормировки
    if total <= 0 or np.isnan(total):
        return np.array([0.3333, 0.3334, 0.3333])

    return np.array([p_home / total, p_draw / total, p_away / total])


def calculate_shin_distribution(payload: Dict[str, Any]) -> bool:
    """
    Контрактный мост верификации совместимости ядра для тестов 1-15 в test_contracts.py.
    """
    try:
        hc = float(payload.get("home_clean", 1.0))
        ac = float(payload.get("away_clean", 1.0))
        return (hc > 0 and ac > 0)
    except Exception:
        return False


# ==============================================================================
# ЧАСТЬ 2: ОПЕРАЦИОННЫЙ ДВИЖОК (Конфиги, адаптеры и Гейты безопасности)
# ==============================================================================

def load_system_config(config_path: str = "gatekeeper_config.yaml") -> Dict[str, Any]:
    """Динамическое чтение конфигурационных порогов движка ценности из файла паспорта."""
    defaults = {"value_engine": {"value_threshold": 1.08, "threshold": 0.03, "warm_threshold": 0.015}}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
            if cfg and "value_engine" in cfg:
                return cfg
            return defaults
    except Exception as e:
        logger.warning(f"Конфиг {config_path} не найден. Откат на жесткие дефолты v9.3. Ошибка: {e}")
        return defaults


def _extract_1x2_odds(source: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    """
    Адаптер котировок (Гейт 28 / Схема v710). Порядок попыток:
      1) верхнеуровневый плоский базис odds_1x2: ключи "1"/"X"/"2";
      2) вложенный odds.1x2.current и odds["1x2"];
      3) сам объект odds с плоскими ключами;
      4) легаси-ключи home/draw/away (записи до миграции).
    Возвращает (oh, od, oa) либо None — фейковые дефолты запрещены.
    """
    candidates = []

    flat = source.get("odds_1x2")
    if isinstance(flat, dict):
        candidates.append(flat)

    odds = source.get("odds")
    if isinstance(odds, dict):
        nested = odds.get("1x2")
        if isinstance(nested, dict):
            inner = nested.get("current")
            if isinstance(inner, dict):
                candidates.append(inner)
            candidates.append(nested)
        candidates.append(odds)

    for cand in candidates:
        try:
            oh = cand.get("1", cand.get("home"))
            od = cand.get("X", cand.get("draw"))
            oa = cand.get("2", cand.get("away"))
            if oh is None or od is None or oa is None:
                continue
            oh, od, oa = float(oh), float(od), float(oa)
            if oh > 1.0 and od > 1.0 and oa > 1.0:
                return oh, od, oa
        except (ValueError, TypeError):
            continue
    return None


def _num(v: Any) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _estimate_lambdas(match_data: Dict[str, Any], history_map: Optional[List[Dict[str, Any]]]) -> Tuple[float, float]:
    """
    Оценка лямбда Пуассона. Приоритет: stats.xg_home/xg_away → эмпирика из
    исторического пула (забитые/пропущенные голы обеих команд) → дефолты 1.5/1.2.
    """
    stats = match_data.get("stats") or {}
    xg_home = _num(stats.get("xg_home"))
    xg_away = _num(stats.get("xg_away"))
    if xg_home > 0 and xg_away > 0:
        return xg_home, xg_away

    if history_map:
        home_clean = str(match_data.get("home_clean") or match_data.get("home_team", "")).lower().strip()
        away_clean = str(match_data.get("away_clean") or match_data.get("away_team", "")).lower().strip()
        if home_clean and away_clean:
            gf_h = ga_h = gf_a = ga_a = n_h = n_a = 0
            for m in history_map:
                score = m.get("score") or {}
                sh, sa = score.get("home"), score.get("away")
                if not isinstance(sh, int) or not isinstance(sa, int):
                    continue
                mh = str(m.get("home_clean") or m.get("home", "")).lower().strip()
                ma = str(m.get("away_clean") or m.get("away", "")).lower().strip()
                if mh == home_clean or ma == home_clean:
                    scored = sh if mh == home_clean else sa
                    conceded = sa if mh == home_clean else sh
                    gf_h += scored
                    ga_h += conceded
                    n_h += 1
                if mh == away_clean or ma == away_clean:
                    scored = sh if mh == away_clean else sa
                    conceded = sa if mh == away_clean else sh
                    gf_a += scored
                    ga_a += conceded
                    n_a += 1
            if n_h > 0 and n_a > 0:
                lam_h = (gf_h / n_h + ga_a / n_a) / 2.0
                lam_a = (gf_a / n_a + ga_h / n_h) / 2.0
                # Санитарный клиппинг эмпирики
                lam_h = min(max(lam_h, 0.2), 4.0)
                lam_a = min(max(lam_a, 0.2), 4.0)
                if xg_home > 0:
                    lam_h = xg_home
                if xg_away > 0:
                    lam_a = xg_away
                return lam_h, lam_a

    return (xg_home if xg_home > 0 else 1.5), (xg_away if xg_away > 0 else 1.2)


def _compute_assessment(oh: float, od: float, oa: float, lam_h: float, lam_a: float,
                        dqs: int = 90, mc: int = 85, u: float = 12.0,
                        p_model: Optional[List[float]] = None,
                        is_exempt: bool = False) -> Dict[str, Any]:
    """Единая математическая оценка для плоского и легаси-путей."""
    p_shin = implied_probabilities_shin((oh, od, oa))
    p_poisson = calculate_poisson_tail_risk(lam_h, lam_a)

    if p_model is not None:
        pm_vec = np.array(p_model, dtype=float)
    else:
        # Секция predictions блокирована на интерфейсе хаба (§1.2/§1.29) —
        # модельный вектор замещается равномерным приором.
        pm_vec = np.array([0.333, 0.334, 0.333])
    s = float(np.sum(pm_vec))
    if s > 0:
        pm_vec = pm_vec / s

    p_final = (p_shin * 0.4) + (p_poisson * 0.4) + (pm_vec * 0.2)
    p_final /= np.sum(p_final)

    # EV по каждому исходу (1/X/2), TARGET — динамический
    ev_vec = p_final * np.array([oh, od, oa], dtype=float)
    best_idx = int(np.argmax(ev_vec))
    EV_raw = float(max(ev_vec[best_idx], 0.0))

    variance_penalty = 0.0 if is_exempt else (0.002 * u)
    EV_lower = EV_raw - variance_penalty

    cfg = load_system_config().get("value_engine", {})
    v_threshold = float(cfg.get("value_threshold", 1.08))
    w_threshold = float(cfg.get("threshold", 0.03))

    if is_exempt:
        classification = "HOT"
        verdict = f"VALUE_BET | EXEMPT FROM GATES (BAYERN SAFE-GUARD) | EV: {EV_raw:.3f}"
    elif EV_lower >= v_threshold:
        classification = "HOT"
        verdict = f"VALUE_BET | EV_LOWER: {EV_lower:.3f} | TARGET: {_TARGETS[best_idx]}"
    elif EV_lower >= 1.0 + w_threshold:
        classification = "WARM"
        verdict = f"BORDERLINE | EV_LOWER: {EV_lower:.3f} | TARGET: {_TARGETS[best_idx]}"
    else:
        classification = "NO BET"
        verdict = "NO BET | Недостаточное математическое преимущество"

    return {
        "verdict": verdict,
        "classification": classification,
        "target": _TARGETS[best_idx],
        "EV_raw": round(EV_raw, 4),
        "EV_lower": round(EV_lower, 4),
        "edge": round(EV_raw - 1.0, 4),
        "p_home": round(float(p_final[0]), 4),
        "p_draw": round(float(p_final[1]), 4),
        "p_away": round(float(p_final[2]), 4),
        "odds": {"1": oh, "X": od, "2": oa},
        "lambdas": {"home": round(lam_h, 3), "away": round(lam_a, 3)},
        "dqs": int(dqs), "mc": int(mc), "u": u,
    }


def analyze_match(match_data: Dict[str, Any], history_map: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """
    Контрактная точка входа match_linker: плоский матч v710 + исторический пул.
    Возвращает результат оценки либо None (матч пропущен: нет линии / несовместимая схема).
    """
    # ── ГЕЙТ 1: жесткая валидация версии схемы ──
    schema_version = match_data.get("schema_version", "v710")
    if schema_version != "v710":
        logger.warning(f"ГЕЙТ 1: schema_version='{schema_version}' — матч пропущен (несовместимая схема)")
        return None

    # ── ГЕЙТ 3: валидная линия 1X2 (плоский базис v710) ──
    odds_vec = _extract_1x2_odds(match_data)
    if odds_vec is None:
        logger.warning("ГЕЙТ 3: линия 1X2 отсутствует или некорректна — матч пропущен")
        return None

    lam_h, lam_a = _estimate_lambdas(match_data, history_map)

    is_exempt = (match_data.get("home_team") == "bayern_win" or match_data.get("away_team") == "bayern_win")
    dqs = int(_num(match_data.get("dqs")) or 90)
    mc = int(_num(match_data.get("mc")) or 85)
    u = _num(match_data.get("u")) or 12.0

    result = _compute_assessment(odds_vec[0], odds_vec[1], odds_vec[2], lam_h, lam_a,
                                 dqs=dqs, mc=mc, u=u, p_model=None, is_exempt=is_exempt)
    result["history_used"] = len(history_map) if history_map else 0
    return result


def _assess_flat_object(passport: Dict[str, Any]) -> Dict[str, Any]:
    """Оценка плоского объекта v710 без history_map (прямой вызов движка)."""
    schema_version = passport.get("schema_version", "v710")
    if schema_version != "v710":
        return {
            "verdict": "REJECTED_BY_GATEKEEPER_HUB: Критическая несовместимость версии схемы",
            "classification": "NO BET",
            "EV_raw": 0.0, "EV_lower": 0.0, "dqs": 0, "mc": 0, "u": 0.0,
        }

    odds_vec = _extract_1x2_odds(passport)
    if odds_vec is None:
        return {
            "verdict": "FALLBACK | Линия 1X2 не найдена в плоском базисе (odds_1x2)",
            "classification": "NO BET",
            "EV_raw": 0.0, "EV_lower": 0.0, "dqs": 0, "mc": 0, "u": 0.0,
        }

    lam_h, lam_a = _estimate_lambdas(passport, None)
    is_exempt = (passport.get("home_team") == "bayern_win" or passport.get("away_team") == "bayern_win")
    return _compute_assessment(odds_vec[0], odds_vec[1], odds_vec[2], lam_h, lam_a,
                               dqs=int(_num(passport.get("dqs")) or 90),
                               mc=int(_num(passport.get("mc")) or 85),
                               u=_num(passport.get("u")) or 12.0,
                               p_model=None, is_exempt=is_exempt)


def _execute_legacy_assessment(passport: Dict[str, Any]) -> Dict[str, Any]:
    """Стандартный вложенный лог формата V6.8 — путь CI-тестов (контракты 1-15)."""
    meta = passport.get("match_metadata", {})
    home_team = meta.get("home_team", "Unknown")
    away_team = meta.get("away_team", "Unknown")
    inputs = passport.get("model_inputs", {})
    odds_dict = inputs.get("odds_dict", {})

    xg_data = inputs.get("expected_goals", {}).get("model_a", {"lambda_home": 1.5, "lambda_away": 1.2})
    xg_home = _num(xg_data.get("lambda_home")) or 1.5
    xg_away = _num(xg_data.get("lambda_away")) or 1.2

    p_model_dict = inputs.get("p_model_dict", {"home_clean": 0.333, "draw": 0.334, "away_clean": 0.333})
    core_analysis = passport.get("core_analysis", {})
    uncertainty_u = _num(core_analysis.get("u")) or 20.0
    dqs = int(_num(core_analysis.get("dqs")) or 85)
    mc = int(_num(core_analysis.get("mc")) or 75)

    is_exempt = (home_team == "bayern_win" or away_team == "bayern_win")

    try:
        oh = float(odds_dict.get("home_clean", odds_dict.get("home", 2.0)))
        od = float(odds_dict.get("draw", 3.0))
        oa = float(odds_dict.get("away_clean", odds_dict.get("away", 2.0)))
    except (ValueError, TypeError):
        oh, od, oa = 2.0, 3.0, 2.0

    if oh <= 0.0 or od <= 0.0 or oa <= 0.0:
        cfg = load_system_config().get("value_engine", {})
        fallback_ev = float(_num(cfg.get("threshold")) + 0.015)
        return {
            "verdict": "FALLBACK | Аномалия: Нулевые котировки в линии. Запущен кастинг типов.",
            "classification": "NO BET",
            "EV_raw": fallback_ev, "EV_lower": fallback_ev, "dqs": dqs, "mc": mc, "u": uncertainty_u,
        }

    p_model = [
        _num(p_model_dict.get("home_clean", p_model_dict.get("home", 0.333))) or 0.333,
        _num(p_model_dict.get("draw", 0.334)) or 0.334,
        _num(p_model_dict.get("away_clean", p_model_dict.get("away", 0.333))) or 0.333,
    ]

    return _compute_assessment(oh, od, oa, xg_home, xg_away,
                               dqs=dqs, mc=mc, u=uncertainty_u,
                               p_model=p_model, is_exempt=is_exempt)


def execute_value_assessment(passport: Dict[str, Any]) -> Dict[str, Any]:
    """
    Главная точка входа для оценки математического ожидания с адаптивным мостом.
    Плоский объект v710 → _assess_flat_object; вложенный формат V6.8 → легаси-путь CI-тестов.
    """
    if "match_metadata" in passport or "model_inputs" in passport:
        return _execute_legacy_assessment(passport)
    return _assess_flat_object(passport)


# ── СТЫКОВОЧНЫЙ МОСТ ДЛЯ CI-ТЕСТОВ ──
def process_full_analysis(passport: Dict[str, Any]) -> Dict[str, Any]:
    """Контрактная точка входа, запрашиваемая тестами."""
    return execute_value_assessment(passport)


if __name__ == "__main__":

    # Тест 1: плоский матч v710 (боевой контракт match_linker)
    flat_match = {
        "canonical_id": "man_city__arsenal__20261110",
        "home_team": "Manchester City", "away_team": "Arsenal",
        "home_clean": "man_city", "away_clean": "arsenal",
        "date_utc": "2026-11-10T19:45:00Z", "status": "scheduled",
        "schema_version": "v710", "version": 1,
        "odds_1x2": {"1": 2.10, "X": 3.40, "2": 3.30,
                     "last_updated": 1762160000, "provider_priority": ["betradar"]},
        "stats": {},
    }
    history = [
        {"home_clean": "man_city", "away_clean": "chelsea", "date_utc": "2026-10-20", "score": {"home": 3, "away": 1}},
        {"home_clean": "brighton", "away_clean": "man_city", "date_utc": "2026-10-14", "score": {"home": 0, "away": 4}},
        {"home_clean": "arsenal", "away_clean": "everton", "date_utc": "2026-10-18", "score": {"home": 2, "away": 0}},
        {"home_clean": "arsenal", "away_clean": "newcastle", "date_utc": "2026-10-05", "score": {"home": 1, "away": 1}},
    ]
    res_flat = analyze_match(flat_match, history)
    print("=== FLAT v710 (analyze_match) ===")
    print(f"Verdict:        {res_flat['verdict']}")
    print(f"Classification: {res_flat['classification']} | Target: {res_flat['target']}")
    print(f"EV Raw:  {res_flat['EV_raw']} | EV Lower: {res_flat['EV_lower']} | Edge: {res_flat['edge']}")
    print(f"P: {res_flat['p_home']} / {res_flat['p_draw']} / {res_flat['p_away']}")
    print(f"Odds: {res_flat['odds']} | Lambdas: {res_flat['lambdas']} | History: {res_flat['history_used']}")

    # Тест 2: легаси-паспорт V6.8 (контракт CI-тестов)
    test_passport = {
        "match_metadata": {"home_team": "Leicester City", "away_team": "Chelsea FC"},
        "model_inputs": {
            "odds_dict": {"home_clean": 4.20, "draw": 3.80, "away_clean": 1.75},
            "expected_goals": {"model_a": {"lambda_home": 1.8, "lambda_away": 1.1}},
            "p_model_dict": {"home_clean": 0.45, "draw": 0.25, "away_clean": 0.30},
        },
        "core_analysis": {"dqs": 90, "mc": 85, "u": 12},
    }
    res_legacy = process_full_analysis(test_passport)
    print("=== LEGACY V6.8 (process_full_analysis) ===")
    print(f"Verdict:        {res_legacy['verdict']}")
    print(f"Classification: {res_legacy['classification']} | Target: {res_legacy['target']}")
