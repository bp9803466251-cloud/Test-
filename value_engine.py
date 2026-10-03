# value_engine.py
# Version: 3.0 — Фаза 3: real value calculation, closing odds, margin

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from gatekeeper_config import MSK_TZ
from gatekeeper_hub import is_shutdown_requested

__all__ = [
    "evaluate_match_value",
    "evaluate_match_full",
    "batch_evaluate",
    "calculate_margin",
    "extract_odds_pair",
    "ValueEngineError",
    "__version__",
]

__version__ = "3.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [VALUE] %(message)s",
)

logger = logging.getLogger(__name__)


class ValueEngineError(Exception):
    pass


# ---- Приоритеты источников (ранг 1 = лучший) ----
_PRIORITY_MAP = {
    "sharpapi": 1,
    "propline": 2,
    "odds_api": 3,
    "football_data": 4,
    "bzzoiro": 8,
}

# ---- Единый порог value (из gatekeeper_config → ENV → default) ----
try:
    from gatekeeper_config import VALUE_THRESHOLD as _CFG_THRESHOLD
except ImportError:
    _CFG_THRESHOLD = None

_DEFAULT_THRESHOLD = _CFG_THRESHOLD or float(
    os.environ.get("VALUE_BET_THRESHOLD", "0.03")
)


# ============================================================================
# Утилиты
# ============================================================================

def _to_float(v: Any) -> Optional[float]:
    """Конвертирует odds-значение в float. Принимает int, float, str."""
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if not s or s == "-":
            return None
        try:
            f = float(s)
            return f if f > 0 else None
        except ValueError:
            return None
    return None


def _extract_odds_tuple(odds: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    """Извлекает (home, draw, away) из dict odds. Все три обязательны."""
    if not isinstance(odds, dict):
        return None
    h = _to_float(odds.get("home"))
    d = _to_float(odds.get("draw"))
    a = _to_float(odds.get("away"))
    if h is None or d is None or a is None:
        return None
    return h, d, a


def _implied_probs(odds_tuple: Tuple[float, float, float]) -> Tuple[float, float, float, float]:
    """
    Возвращает (home_prob, draw_prob, away_prob, margin).
    implied_prob = 1 / odds. margin = sum(implied_probs) - 1.
    """
    h, d, a = odds_tuple
    hp = 1.0 / h
    dp = 1.0 / d
    ap = 1.0 / a
    total = hp + dp + ap
    margin = total - 1.0
    return hp, dp, ap, margin


def _normalize_probs(p: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Нормализует вероятности чтобы сумма = 1 (убирая margin)."""
    h, d, a = p
    total = h + d + a
    if total <= 0:
        return 0.0, 0.0, 0.0
    return h / total, d / total, a / total


# ============================================================================
# Публичные функции
# ============================================================================

def calculate_margin(odds: Dict[str, Any]) -> Optional[float]:
    """
    Считает bookmaker margin (overround) для блока odds.
    Возвращает margin как долю (0.05 = 5%).
    """
    t = _extract_odds_tuple(odds)
    if not t:
        return None
    _, _, _, margin = _implied_probs(t)
    return margin


def extract_odds_pair(all_odds: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float, float]], Optional[Tuple[float, float, float]]]:
    """
    Извлекает current и closing odds как два независимых тензора.
    Возвращает (current_tuple, closing_tuple). Любой может быть None.

    Closing — «истинная» цена рынка (PropLine/Pinnacle).
    Current — коэффициент, на который можно поставить сейчас.
    """
    current_tuple = None
    closing_tuple = None

    current_block = all_odds.get("current")
    if isinstance(current_block, dict):
        current_tuple = _extract_odds_tuple(current_block)

    closing_block = all_odds.get("closing")
    if isinstance(closing_block, dict):
        closing_tuple = _extract_odds_tuple(closing_block)

    return current_tuple, closing_tuple


def _extract_source(sources: List[Dict[str, Any]]) -> Optional[str]:
    """Выбор лучшего источника по рангу приоритета."""
    if not sources:
        return None

    def priority(src: Dict[str, Any]) -> int:
        name = str(src.get("source", "")).lower()
        return _PRIORITY_MAP.get(name, 99)

    best = min(sources, key=priority)
    return str(best.get("source"))


def evaluate_match_full(
    match_data: Dict[str, Any],
    value_threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Полная оценка value. Возвращает dict с деталями или None.

    Логика value:
    1. Если есть current И closing — value = разница нормализованных
       implied probs (closing = «истина», current = где ставим).
    2. Если есть только current — value = отклонение max prob от 1/3
       (дисбаланс рынка), fallback без closing.
    3. Если есть только closing — value не считается (нечего сравнивать).

    Возвращает:
        {
            "value": float,           # значение value
            "direction": "home"|"draw"|"away",  # где максимальное value
            "margin": float,           # bookmaker margin на current odds
            "closing_margin": float|None,  # margin на closing odds
            "current_odds": (h, d, a),
            "closing_odds": (h, d, a)|None,
            "best_source": str|None,
        }
    """
    if value_threshold is None:
        value_threshold = _DEFAULT_THRESHOLD

    all_odds = match_data.get("odds", {})
    if not isinstance(all_odds, dict):
        return None

    current_tuple, closing_tuple = extract_odds_pair(all_odds)

    # Нет ни current, ни closing — нечего оценивать
    if not current_tuple and not closing_tuple:
        return None

    # Только closing — нечего сравнивать
    if not current_tuple and closing_tuple:
        logger.debug("Только closing odds — value не вычисляется")
        return None

    # Fallback: если current есть, но нет closing — используем current
    if current_tuple and not closing_tuple:
        return _evaluate_single(current_tuple, value_threshold, match_data, has_closing=False)

    # Оба есть — считаем реальный value: current vs closing
    return _evaluate_dual(current_tuple, closing_tuple, value_threshold, match_data)


def _evaluate_single(
    current: Tuple[float, float, float],
    threshold: float,
    match_data: Dict[str, Any],
    has_closing: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Fallback-оценка без closing odds.
    Value = отклонение максимальной normalised prob от 1/3 (равномерного распределения).
    """
    hp, dp, ap, margin = _implied_probs(current)
    h_norm, d_norm, a_norm = _normalize_probs((hp, dp, ap))

    fair = 1.0 / 3.0
    value = max(
        abs(h_norm - fair),
        abs(d_norm - fair),
        abs(a_norm - fair),
    )

    if value < threshold:
        return None

    direction = max(
        ("home", h_norm - fair),
        ("draw", d_norm - fair),
        ("away", a_norm - fair),
        key=lambda x: abs(x[1]),
    )[0]

    sources = match_data.get("sources", [])
    best_source = _extract_source(sources) if isinstance(sources, list) else None

    return {
        "value": round(value, 5),
        "direction": direction,
        "margin": round(margin, 5),
        "closing_margin": None,
        "current_odds": current,
        "closing_odds": None,
        "best_source": best_source,
    }


def _evaluate_dual(
    current: Tuple[float, float, float],
    closing: Tuple[float, float, float],
    threshold: float,
    match_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Реальный value: сравнение current (где ставим) vs closing (истина рынка).

    implied_prob = 1 / odds.
    fair_prob = normalised implied prob (без margin).
    value_home = fair_prob_home(closing) - fair_prob_home(current)
    ...
    value = max(abs(value_home), abs(value_draw), abs(value_away))
    """
    ch, cd, ca = current
    kh, kd, ka = closing

    # Implied probs
    c_hp, c_dp, c_ap, c_margin = _implied_probs(current)
    k_hp, k_dp, k_ap, k_margin = _implied_probs(closing)

    # Normalised probs (убираем margin)
    c_hn, c_dn, c_an = _normalize_probs((c_hp, c_dp, c_ap))
    k_hn, k_dn, k_an = _normalize_probs((k_hp, k_dp, k_ap))

    # Value = разница normalised probs (closing = истина, current = где ставим)
    v_home = k_hn - c_hn
    v_draw = k_dn - c_dn
    v_away = k_an - c_an

    value = max(abs(v_home), abs(v_draw), abs(v_away))

    if value < threshold:
        return None

    direction = max(
        ("home", v_home),
        ("draw", v_draw),
        ("away", v_away),
        key=lambda x: abs(x[1]),
    )[0]

    sources = match_data.get("sources", [])
    best_source = _extract_source(sources) if isinstance(sources, list) else None

    return {
        "value": round(value, 5),
        "direction": direction,
        "margin": round(c_margin, 5),
        "closing_margin": round(k_margin, 5),
        "current_odds": current,
        "closing_odds": closing,
        "best_source": best_source,
    }


def evaluate_match_value(
    match_data: Dict[str, Any],
    value_threshold: Optional[float] = None,
) -> Optional[float]:
    """
    Упрощённая оценка — возвращает только значение value (float) или None.
    Для детального результата используйте evaluate_match_full().
    """
    result = evaluate_match_full(match_data, value_threshold)
    return result["value"] if result else None


def batch_evaluate(
    matches: Dict[str, Dict[str, Any]],
    value_threshold: Optional[float] = None,
) -> Dict[str, Optional[float]]:
    """
    Пакетная оценка. Возвращает {canonical_id: value | None}.
    Graceful shutdown: проверка is_shutdown_requested() в цикле.
    """
    results: Dict[str, Optional[float]] = {}

    for cid, match in matches.items():
        if is_shutdown_requested():
            logger.info("Graceful shutdown — batch_evaluate прерван")
            break
        results[cid] = evaluate_match_value(match, value_threshold)

    return results


def batch_evaluate_full(
    matches: Dict[str, Dict[str, Any]],
    value_threshold: Optional[float] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Пакетная оценка с детальным результатом.
    Возвращает {canonical_id: {value, direction, margin, ...} | None}.
    """
    results: Dict[str, Optional[Dict[str, Any]]] = {}

    for cid, match in matches.items():
        if is_shutdown_requested():
            logger.info("Graceful shutdown — batch_evaluate_full прерван")
            break
        results[cid] = evaluate_match_full(match, value_threshold)

    return results
