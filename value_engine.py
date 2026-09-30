#!/usr/bin/env python3
"""
value_engine.py — Gatekeeper-AI v700-prod
Value-bet движок: читает odds через хаб, считает EV, классифицирует hot/warm.

V1.0.0 — создан как замена value-части SearchModule.
Все обращения к odds идут через gatekeeper_hub.get_all_odds() / get_current_odds().
Утилитные функции (predictions, competition, date, source display)
импортируются из search_module — без дублирования.
"""
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone, timedelta

from gatekeeper_hub import (
    get_all_odds,
    get_current_odds,
    get_match,
    get_history,
)
from search_module import (
    _get_predictions,
    _get_competition_code,
    _parse_date_msk,
    _dt_from_utc,
    _source_display,
)

MSK_TZ = timezone(timedelta(hours=3))


def _extract_odds_tuple(current: dict) -> Tuple[float, float, float]:
    """Преобразует {home, draw, away} в (float, float, float)."""
    def _val(key):
        v = current.get(key, "")
        if v is None or v == "-" or v == "":
            return 0.0
        try:
            f = float(v)
            return f if f > 1.0 else 0.0
        except (ValueError, TypeError):
            return 0.0
    return (_val("home"), _val("draw"), _val("away"))


def _build_verification_dict(all_odds: dict) -> dict:
    """
    Преобразует верификацию из хаба в формат, ожидаемый main.py.
    main.py._verification_badge ожидает dict с ключом 'level'.
    """
    verification = all_odds.get("verification", "UNVERIFIED")
    independent = all_odds.get("independent_sources", 0)
    consensus = all_odds.get("betradar_consensus", False)

    if verification == "VERIFIED" and consensus:
        level = "CONSENSUS"
    elif verification == "VERIFIED":
        level = "VERIFIED"
    elif verification == "WARNING":
        level = "SINGLE"
    else:
        level = ""

    return {
        "level": level,
        "independent_sources": independent,
        "betradar_consensus": consensus,
    }


def _extract_source(all_odds: dict, match: dict) -> str:
    """Извлекает имя источника из all_odds.sources или fallback на match.sources."""
    sources = all_odds.get("sources", [])
    if isinstance(sources, list) and sources:
        first = sources[0]
        if isinstance(first, dict):
            return first.get("source", "")
    # Fallback: top-level sources в match
    match_sources = match.get("sources", [])
    if isinstance(match_sources, list) and match_sources:
        return str(match_sources[0])
    return "unknown"


def evaluate_match(
    match: dict,
    cid: str,
    value_threshold: float = 0.03,
) -> Optional[Tuple[dict, bool]]:
    """
    Полный value-анализ одного матча.

    Возвращает (info_dict, is_hot) или None (если нет odds или матч вне окна).
    info_dict совместим с форматом, ожидаемым main.py.
    """
    if not isinstance(match, dict):
        return None

    # --- Получаем odds через хаб ---
    all_odds = get_all_odds(match)
    current = all_odds.get("current", {})
    o_h, o_d, o_a = _extract_odds_tuple(current)

    if o_h <= 1.0 or o_d <= 1.0 or o_a <= 1.0:
        return None

    # --- Окно времени: ±2h назад, +48h вперёд ---
    now = datetime.now(MSK_TZ)
    dt = _dt_from_utc(match.get("date_utc", ""))
    if dt is None:
        return None
    if dt < now - timedelta(hours=2):
        return None
    if dt > now + timedelta(hours=48):
        return None

    # --- Implied вероятности ---
    imp_h = 1.0 / o_h
    imp_d = 1.0 / o_d
    imp_a = 1.0 / o_a
    imp_total = imp_h + imp_d + imp_a
    p_h = imp_h / imp_total
    p_d = imp_d / imp_total
    p_a = imp_a / imp_total

    # --- Прогнозы ---
    pred = _get_predictions(match)
    has_pred = pred is not None
    if has_pred:
        ph_pred, pd_pred, pa_pred = pred

    # --- Value-расчёт ---
    value_side = None
    value_ev = 0.0
    value_odds = 0.0
    value_prob = 0.0

    if has_pred:
        ev_h = ph_pred * o_h - 1.0
        ev_d = pd_pred * o_d - 1.0
        ev_a = pa_pred * o_a - 1.0
        evs = [
            ("HOME", ev_h, o_h, ph_pred),
            ("DRAW", ev_d, o_d, pd_pred),
            ("AWAY", ev_a, o_a, pa_pred),
        ]
        best = max(evs, key=lambda x: x[1])
        if best[1] > value_threshold:
            value_side = best[0]
            value_ev = best[1]
            value_odds = best[2]
            value_prob = best[3]

    if value_side:
        v_side = value_side
        v_odds = value_odds
        v_prob = value_prob
        is_fire = True
    else:
        if has_pred:
            probs_list = [
                ("HOME", o_h, ph_pred),
                ("DRAW", o_d, pd_pred),
                ("AWAY", o_a, pa_pred),
            ]
        else:
            probs_list = [
                ("HOME", o_h, p_h),
                ("DRAW", o_d, p_d),
                ("AWAY", o_a, p_a),
            ]
        best = max(probs_list, key=lambda x: x[2])
        v_side = best[0]
        v_odds = best[1]
        v_prob = best[2]
        is_fire = False

    # --- Метаданные ---
    source = _extract_source(all_odds, match)
    odds_verification = _build_verification_dict(all_odds)
    independent_sources = all_odds.get("independent_sources", 0)

    # --- H2H / Stats ---
    h2h = match.get("h2h", {})
    has_h2h = isinstance(h2h, dict) and bool(h2h)
    stats = match.get("stats", {})
    has_stats = isinstance(stats, dict) and bool(stats)

    # --- Форматирование ---
    comp_code = _get_competition_code(
        match.get("competition", ""),
        match.get("country", ""),
    )
    date_str, time_str = _parse_date_msk(match.get("date_utc", ""))

    info = {
        "canonical_id": cid,
        "home_team": match.get("home_team", "?"),
        "away_team": match.get("away_team", "?"),
        "comp_code": comp_code,
        "date": date_str,
        "time": time_str,
        "dt": dt,
        "odds": (o_h, o_d, o_a),
        "probs": (p_h, p_d, p_a),
        "v_side": v_side,
        "v_odds": v_odds,
        "v_prob": v_prob,
        "value_ev": value_ev,
        "is_fire": is_fire,
        "source": source,
        "source_display": _source_display(source),
        "has_pred": has_pred,
        "has_h2h": has_h2h,
        "has_stats": has_stats,
        "odds_verification": odds_verification,
        "independent_sources": independent_sources,
    }

    # --- HOT = fire ИЛИ verified (2+ indep) ИЛИ sharpapi ---
    match_sources = match.get("sources", [])
    if not isinstance(match_sources, list):
        match_sources = [source] if source else ["unknown"]

    is_hot = (
        is_fire
        or odds_verification["level"] in ("VERIFIED", "CONSENSUS")
        or "sharpapi" in [s.lower() for s in match_sources]
    )

    return (info, is_hot)


def batch_evaluate(
    matches: Dict[str, dict],
    value_threshold: float = 0.03,
) -> dict:
    """
    Пакетный value-анализ. Совместим с SearchModule.process().

    Возвращает {hot, warm, stats} в формате, ожидаемом main.py.
    """
    hot: List[dict] = []
    warm: List[dict] = []
    with_odds = 0
    with_pred = 0
    with_h2h = 0
    with_stats = 0
    value_bets = 0

    for cid, match in matches.items():
        if not isinstance(match, dict):
            continue

        result = evaluate_match(match, cid, value_threshold)
        if result is None:
            # Считаем odds-покрытие даже для матчей вне окна
            all_odds = get_all_odds(match)
            current = all_odds.get("current", {})
            o_h, o_d, o_a = _extract_odds_tuple(current)
            if o_h > 1.0 and o_d > 1.0 and o_a > 1.0:
                with_odds += 1
            pred = _get_predictions(match)
            if pred is not None:
                with_pred += 1
            h2h = match.get("h2h", {})
            if isinstance(h2h, dict) and h2h:
                with_h2h += 1
            stats = match.get("stats", {})
            if isinstance(stats, dict) and stats:
                with_stats += 1
            continue

        info, is_hot = result
        with_odds += 1

        if info["has_pred"]:
            with_pred += 1
        if info["has_h2h"]:
            with_h2h += 1
        if info["has_stats"]:
            with_stats += 1
        if info["is_fire"]:
            value_bets += 1

        if is_hot:
            hot.append(info)
        else:
            warm.append(info)

    hot.sort(key=lambda x: (-int(x["is_fire"]), -x.get("value_ev", 0), x["dt"]))
    warm.sort(key=lambda x: x["dt"])
    hot = hot[:10]
    warm = warm[:10]

    return {
        "hot": hot,
        "warm": warm,
        "stats": {
            "total": len(matches),
            "with_odds": with_odds,
            "with_pred": with_pred,
            "with_h2h": with_h2h,
            "with_stats": with_stats,
            "value_bets": value_bets,
        },
    }


def evaluate_by_id(
    canonical_id: str,
    value_threshold: float = 0.03,
) -> Optional[dict]:
    """
    Анализ матча по canonical_id через get_match из хаба.
    Возвращает info_dict или None.
    """
    match = get_match(canonical_id)
    if not match:
        return None
    result = evaluate_match(match, canonical_id, value_threshold)
    if result is None:
        return None
    return result[0]
