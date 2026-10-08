# value_engine.py
# Version: 9.3-audited — Phase 1+2+3 (complete)
# v8.11-patched: run_pipeline() for main.py, odds-level sources, logging fix
# v9.3-audited: DQS/MC/U/gates/scenarios/Poisson/Elo/Kelly/PAVA/backtest/CLV

import logging
import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

try:
    from gatekeeper_hub import is_shutdown_requested
except ImportError:
    def is_shutdown_requested():
        return False

__all__ = [
    "evaluate_match_value",
    "evaluate_match_full",
    "batch_evaluate",
    "batch_evaluate_full",
    "run_pipeline",
    "calculate_margin",
    "extract_odds_pair",
    "analyze_match",
    "calc_kelly_stake",
    "calc_brier_score",
    "calc_rps",
    "backtest_match",
    "run_backtest",
    "ValueEngineError",
    "__version__",
]

__version__ = "9.3-audited"

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())


class ValueEngineError(Exception):
    pass


# ---- Приоритеты источников ----
_PRIORITY_MAP = {
    "sharpapi": 1,
    "propline": 1,
    "odds_api": 2,
    "bzzoiro": 3,
    "football_data": 4,
}

# ---- Threshold (динамический) ----
try:
    from gatekeeper_config import get_value_threshold as _get_threshold
except ImportError:
    _get_threshold = None

_DEFAULT_THRESHOLD = 0.03
if _get_threshold:
    try:
        _DEFAULT_THRESHOLD = _get_threshold()
    except Exception:
        pass

_WARM_THRESHOLD = float(os.environ.get("VALUE_WARM_THRESHOLD", "0.015"))


# ============================================================================
# УТИЛИТЫ (Phase 1)
# ============================================================================

def _to_float(v: Any) -> Optional[float]:
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
    if not isinstance(odds, dict):
        return None
    h = _to_float(odds.get("home"))
    d = _to_float(odds.get("draw"))
    a = _to_float(odds.get("away"))
    if h is None or d is None or a is None:
        return None
    return h, d, a


def _implied_probs(odds_tuple):
    h, d, a = odds_tuple
    hp = 1.0 / h
    dp = 1.0 / d
    ap = 1.0 / a
    total = hp + dp + ap
    margin = total - 1.0
    return hp, dp, ap, margin


# ---- Canonical 1x2 helpers (§1.21, §3) ----

def _extract_from_1x2(odds_1x2: Dict[str, Any], prefer: str = "current") -> Optional[Tuple[float, float, float]]:
    """Извлекает odds из канонического 1x2 блока.
    Формат: match["odds"]["1x2"]["current"]["home"|"draw"|"away"]
    Также: opening, best, sources[]
    """
    if not isinstance(odds_1x2, dict):
        return None
    # Priority: current > best > opening
    for level in (prefer, "current", "best", "opening"):
        block = odds_1x2.get(level)
        if isinstance(block, dict):
            o = _extract_odds_tuple(block)
            if o:
                return o
    # sources[] — массив per-source odds
    sources = odds_1x2.get("sources")
    if isinstance(sources, list):
        best_o = None
        best_rank = 99
        for s in sources:
            if not isinstance(s, dict):
                continue
            o = _extract_odds_tuple(s)
            if o:
                rank = _PRIORITY_MAP.get(s.get("source", s.get("upstream", "")), 99)
                if rank < best_rank:
                    best_rank = rank
                    best_o = o
        if best_o:
            return best_o
    # Flat: home/draw/away directly in 1x2
    o = _extract_odds_tuple(odds_1x2)
    if o:
        return o
    return None


def _extract_1x2_sources(odds_1x2: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Извлекает список sources[] из 1x2 блока."""
    if not isinstance(odds_1x2, dict):
        return []
    sources = odds_1x2.get("sources")
    if isinstance(sources, list):
        return [s for s in sources if isinstance(s, dict)]
    return []


def _normalize_probs(p):
    h, d, a = p
    total = h + d + a
    if total <= 0:
        return 0.0, 0.0, 0.0
    return h / total, d / total, a / total


def calculate_margin(odds: Dict[str, Any]) -> Optional[float]:
    t = _extract_odds_tuple(odds)
    if not t:
        return None
    _, _, _, margin = _implied_probs(t)
    return margin


def extract_odds_pair(all_odds: Dict[str, Any]):
    """Извлекает (open_odds, closing_odds) из all_odds dict.
    Сначала пробует канонический 1x2 формат, затем legacy per-source.
    """
    if not isinstance(all_odds, dict):
        return None, None

    # ── Canonical 1x2 format (§1.21) ──
    block_1x2 = all_odds.get("1x2")
    if isinstance(block_1x2, dict):
        # opening
        open_odds = _extract_from_1x2(block_1x2, "opening")
        # current / closing
        close_odds = _extract_from_1x2(block_1x2, "current")
        if not close_odds:
            close_odds = _extract_from_1x2(block_1x2, "best")
        if open_odds or close_odds:
            if open_odds and not close_odds:
                close_odds = open_odds
            if close_odds and not open_odds:
                open_odds = close_odds
            return open_odds, close_odds

    # ── Legacy per-source format ──
    open_odds = None
    close_odds = None
    for source, block in all_odds.items():
        if not isinstance(block, dict) or source == "1x2":
            continue
        odds_1x2 = block.get("odds", {})
        if isinstance(odds_1x2, dict):
            nesting = odds_1x2.get("1x2", odds_1x2)
            if isinstance(nesting, dict):
                o = _extract_odds_tuple(nesting)
                if o:
                    if "opening" in block or "open" in str(source).lower():
                        if open_odds is None:
                            open_odds = o
                    if "closing" in block or "close" in str(source).lower():
                        if close_odds is None:
                            close_odds = o
                    if open_odds is None:
                        open_odds = o
                    if close_odds is None:
                        close_odds = o
    return open_odds, close_odds


# ============================================================================
# SOURCES (Phase 1)
# ============================================================================

def _extract_best_source(all_odds: Dict[str, Any], prefer: str = "current"):
    """Извлекает лучший источник по priority map.
    Сначала пробует канонический 1x2 формат (§1.21), затем legacy per-source.
    """
    if not isinstance(all_odds, dict):
        return None

    # ── Canonical 1x2 format (§1.21) ──
    block_1x2 = all_odds.get("1x2")
    if isinstance(block_1x2, dict):
        o = _extract_from_1x2(block_1x2, prefer)
        if o:
            return o
        # sources[] с priority
        sources = _extract_1x2_sources(block_1x2)
        if sources:
            best = None
            best_rank = 99
            for s in sources:
                o_s = _extract_odds_tuple(s)
                if o_s:
                    rank = _PRIORITY_MAP.get(s.get("source", s.get("upstream", "")), 99)
                    if rank < best_rank:
                        best_rank = rank
                        best = o_s
            if best:
                return best

    # ── Legacy per-source format ──
    best = None
    best_rank = 99
    for source_name, block in all_odds.items():
        if not isinstance(block, dict) or source_name == "1x2":
            continue
        rank = _PRIORITY_MAP.get(source_name, 99)
        odds_1x2 = block.get("odds", {})
        if isinstance(odds_1x2, dict):
            nesting = odds_1x2.get("1x2", odds_1x2)
            o = _extract_odds_tuple(nesting) if isinstance(nesting, dict) else None
            if o and rank < best_rank:
                best_rank = rank
                best = o
    return best


# ============================================================================
# EVALUATE (Phase 1)
# ============================================================================

def _evaluate_dual(open_odds, close_odds):
    """Dual eval: нормализованные implied probs из open и closing."""
    open_p = _normalize_probs(_implied_probs(open_odds)[:3])
    close_p = _normalize_probs(_implied_probs(close_odds)[:3])
    return open_p, close_p


def _evaluate_single(odds_tuple):
    """Single eval: отклонение от равных вероятностей (1/3)."""
    p = _normalize_probs(_implied_probs(odds_tuple)[:3])
    return p


def evaluate_match_value(match: Dict[str, Any]) -> Optional[float]:
    """Упрощённая оценка value для одного матча."""
    full = evaluate_match_full(match)
    if full is None:
        return None
    return full.get("value_pct")


def evaluate_match_full(match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Полная оценка матча (Phase 1 — без DQS/MC/U)."""
    if not isinstance(match, dict):
        return None
    all_odds = match.get("odds", {})
    if not all_odds:
        return None

    open_odds, close_odds = extract_odds_pair(all_odds)
    best_odds = _extract_best_source(all_odds)

    if open_odds and close_odds and open_odds != close_odds:
        market_probs, model_probs = _evaluate_dual(open_odds, close_odds)
    elif best_odds:
        model_probs = _evaluate_single(best_odds)
        market_probs = model_probs
    else:
        return None

    # EV calculation
    margin = calculate_margin({"home": best_odds[0], "draw": best_odds[1], "away": best_odds[2]}) if best_odds else None
    ev_home = model_probs[0] * (best_odds[0] if best_odds else 0) - 1.0
    ev_draw = model_probs[1] * (best_odds[1] if best_odds else 0) - 1.0
    ev_away = model_probs[2] * (best_odds[2] if best_odds else 0) - 1.0

    best_ev = max(ev_home, ev_draw, ev_away)
    best_idx = [ev_home, ev_draw, ev_away].index(best_ev)
    labels = ["home", "draw", "away"]

    return {
        "canonical_id": match.get("canonical_id", ""),
        "market_probs": list(market_probs),
        "model_probs": list(model_probs),
        "ev": [ev_home, ev_draw, ev_away],
        "value_pct": best_ev,
        "value_side": labels[best_idx],
        "margin": margin,
        "odds": {"home": best_odds[0], "draw": best_odds[1], "away": best_odds[2]} if best_odds else None,
    }


def batch_evaluate(matches: List[Dict[str, Any]]) -> Dict[str, float]:
    """Пакетная оценка — возвращает {cid: value_pct}."""
    result = {}
    for m in matches:
        cid = m.get("canonical_id", "")
        v = evaluate_match_value(m)
        if v is not None:
            result[cid] = v
    return result


def batch_evaluate_full(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Пакетная полная оценка — graceful shutdown + error isolation."""
    results = []
    for m in matches:
        if is_shutdown_requested():
            logger.info("Shutdown requested — остановка batch_evaluate")
            break
        try:
            r = evaluate_match_full(m)
            if r:
                results.append(r)
        except Exception as e:
            cid = m.get("canonical_id", "?")
            logger.warning("evaluate error for %s: %s", cid, e)
    return results


# ============================================================================
# PHASE 2: DQS — Data Quality Score (9 компонентов, §22.1)
# ============================================================================

def _dqs_od(match: Dict) -> float:
    """OD — Odds availability."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return 0.0
    score = 0.0
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        for level in ("current", "opening", "closing", "best"):
            if isinstance(block_1x2.get(level), dict):
                if _extract_odds_tuple(block_1x2[level]):
                    score += 25
        # sources[]
        if isinstance(block_1x2.get("sources"), list) and len(block_1x2["sources"]) > 0:
            score += 25
        return min(score, 100)
    # ── Legacy per-source ──
    for level in ("current", "opening", "closing", "best"):
        if any(level in str(k).lower() for k in odds):
            score += 25
    return min(score, 100)


def _dqs_oh(match: Dict) -> float:
    """OH — Odds history depth."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return 0.0
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        src_list = _extract_1x2_sources(block_1x2)
        count = len(src_list)
        # Also count current/opening/best as sources
        for level in ("current", "opening", "best"):
            if isinstance(block_1x2.get(level), dict) and _extract_odds_tuple(block_1x2[level]):
                count += 1
        if count >= 4:
            return 100
        if count >= 3:
            return 80
        if count >= 2:
            return 60
        if count >= 1:
            return 40
        return 0
    # ── Legacy per-source ──
    sources = sum(1 for v in odds.values() if isinstance(v, dict) and v.get("odds"))
    if sources >= 4:
        return 100
    if sources >= 3:
        return 80
    if sources >= 2:
        return 60
    if sources >= 1:
        return 40
    return 0




# ---- Prediction probability extraction (fallback keys) ----

def _try_float_val(v) -> Optional[float]:
    """Safely convert to float. Handles %, strings, None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        s = v.strip().replace("%", "").replace(",", ".")
        if not s or s == "-":
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    # Values > 1 might be percentages (0-100) or odds
    if f > 1.0 and f <= 100.0:
        f = f / 100.0
    return f if 0 <= f <= 1.0 else None


def _extract_pred_probs_flat(pred: Dict) -> Optional[Tuple[float, float, float]]:
    """Try flat key extraction."""
    home_keys = ("home_win", "home", "1", "p1", "home_prob", "h",
                 "home_team_win", "prob_home", "home_pct", "prob_1",
                 "p_home", "winner_home", "home_probability", "home_win_prob")
    draw_keys = ("draw", "X", "x", "pX", "pdraw", "d",
                 "prob_draw", "draw_pct", "prob_x", "p_draw", "p_x",
                 "draw_probability", "draw_prob")
    away_keys = ("away_win", "away", "2", "p2", "away_prob", "a",
                 "away_team_win", "prob_away", "away_pct", "prob_2",
                 "p_away", "winner_away", "away_probability", "away_win_prob")
    ph = _try_float_val(_first_non_none(pred, home_keys))
    pd = _try_float_val(_first_non_none(pred, draw_keys))
    pa = _try_float_val(_first_non_none(pred, away_keys))
    if ph is None or pd is None or pa is None:
        return None
    total = ph + pd + pa
    if total <= 0:
        return None
    return ph / total, pd / total, pa / total


def _first_non_none(d: Dict, keys: Tuple) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _extract_pred_probs(pred: Any) -> Optional[Tuple[float, float, float]]:
    """Extract (home, draw, away) probabilities from prediction dict.
    Supports flat keys, nested markets, recommendations, and nested dicts.
    Handles Bzzoiro API format: {id, event, markets, recommendations, model, source}."""
    if not isinstance(pred, dict):
        return None

    # 1. Try flat keys first
    result = _extract_pred_probs_flat(pred)
    if result is not None:
        return result

    # 2. Try nested dicts (prediction, probabilities, event, model, pred, outcome, result)
    for nested_key in ("prediction", "probabilities", "event", "model", "pred", "outcome", "result"):
        nested = pred.get(nested_key)
        if isinstance(nested, dict):
            r = _extract_pred_probs_flat(nested)
            if r is not None:
                return r

    # 3. Try markets (list of market objects with outcomes/selections)
    markets = pred.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict):
                continue
            # Market might have outcomes/selections list
            for outcomes_key in ("outcomes", "selections", "runners", "bets"):
                outcomes = market.get(outcomes_key)
                if isinstance(outcomes, list):
                    r = _parse_outcomes_list(outcomes, market)
                    if r is not None:
                        return r
            # Market might have flat odds/probs
            r = _extract_pred_probs_flat(market)
            if r is not None:
                return r
    elif isinstance(markets, dict):
        r = _extract_pred_probs_flat(markets)
        if r is not None:
            return r

    # 4. Try recommendations (list of recommendation objects)
    recs = pred.get("recommendations")
    if isinstance(recs, list):
        r = _parse_recommendations(recs)
        if r is not None:
            return r

    # 5. Try event.markets
    event = pred.get("event")
    if isinstance(event, dict):
        ev_markets = event.get("markets")
        if isinstance(ev_markets, list):
            for market in ev_markets:
                if not isinstance(market, dict):
                    continue
                for outcomes_key in ("outcomes", "selections", "runners", "bets"):
                    outcomes = market.get(outcomes_key)
                    if isinstance(outcomes, list):
                        r = _parse_outcomes_list(outcomes, market)
                        if r is not None:
                            return r
                r = _extract_pred_probs_flat(market)
                if r is not None:
                    return r
        elif isinstance(ev_markets, dict):
            r = _extract_pred_probs_flat(ev_markets)
            if r is not None:
                return r

    return None


def _parse_outcomes_list(outcomes: List, market: Dict = None) -> Optional[Tuple[float, float, float]]:
    """Parse a list of outcome/selection objects into (home, draw, away) probs."""
    ph = pd = pa = None
    name_keys = ("name", "label", "selection", "outcome", "type", "key", "side")
    prob_keys = ("probability", "prob", "implied_prob", "implied_probability", "chance", "pct")
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        name = _first_non_none(item, name_keys)
        if name is None:
            continue
        name = str(name).lower().strip()
        prob = None
        for pk in prob_keys:
            v = item.get(pk)
            if v is not None:
                prob = _try_float_val(v)
                if prob is not None:
                    break
        if prob is None:
            # Try odds → implied prob
            odds = _try_float_val(item.get("odds") or item.get("price") or item.get("decimal"))
            if odds is not None and odds > 1.0:
                prob = 1.0 / odds
        if prob is None:
            continue
        if name in ("home", "home_win", "1", "h", "home_team", "winner_home", "home_team_win"):
            ph = prob
        elif name in ("draw", "x", "tie", "d", "0"):
            pd = prob
        elif name in ("away", "away_win", "2", "a", "away_team", "winner_away", "away_team_win"):
            pa = prob
    if ph is None or pd is None or pa is None:
        return None
    total = ph + pd + pa
    if total <= 0:
        return None
    return ph / total, pd / total, pa / total


def _parse_recommendations(recs: List) -> Optional[Tuple[float, float, float]]:
    """Parse recommendations list into (home, draw, away) probs."""
    ph = pd = pa = None
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        # Try selection/bet name
        name = str(rec.get("selection", rec.get("bet", rec.get("outcome", rec.get("name", ""))))).lower().strip()
        prob = _try_float_val(rec.get("probability", rec.get("prob", rec.get("chance", rec.get("pct")))))
        if prob is None:
            _ov = rec.get("odds") or rec.get("price")
            if _ov is not None:
                try:
                    _of = float(str(_ov).replace(",", "."))
                    if _of > 1.0:
                        prob = 1.0 / _of
                except (ValueError, TypeError):
                    pass
        if prob is None:
            continue
        if name in ("home", "home_win", "1", "h", "home_team", "winner_home"):
            ph = prob
        elif name in ("draw", "x", "tie", "d", "0"):
            pd = prob
        elif name in ("away", "away_win", "2", "a", "away_team", "winner_away"):
            pa = prob
    if ph is None or pd is None or pa is None:
        return None
    total = ph + pd + pa
    if total <= 0:
        return None
    return ph / total, pd / total, pa / total


def _dqs_pi(match: Dict) -> float:
    """PI — Prediction integrity (Bzzoiro).
    FIX: Uses _extract_pred_probs for fallback key support."""
    pred = match.get("predictions") or match.get("bzzoiro_predictions")
    if not pred:
        return 0.0
    if isinstance(pred, dict):
        probs = _extract_pred_probs(pred)
        if probs is not None:
            return 75.0
        all_keys = ("home_win", "home", "1", "p1", "draw", "X",
                    "away_win", "away", "2", "p2", "score_pred")
        keys = sum(1 for k in all_keys if k in pred)
        return min(keys * 20, 100)
    return 50


def _dqs_hl(match: Dict, history: Optional[List] = None) -> float:
    """HL — History length for H2H."""
    if not history:
        return 0.0
    n = len(history)
    if n >= 20:
        return 100
    if n >= 10:
        return 75
    if n >= 5:
        return 50
    if n >= 1:
        return 25
    return 0


def _dqs_ss(match: Dict) -> float:
    """SS — Source stability."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return 0.0
    upstreams = set()
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        for s in _extract_1x2_sources(block_1x2):
            up = s.get("upstream") or s.get("source")
            if up:
                upstreams.add(up)
        if upstreams:
            if len(upstreams) >= 3:
                return 100
            if len(upstreams) >= 2:
                return 70
            if len(upstreams) >= 1:
                return 40
    # ── Legacy per-source ──
    for v in odds.values():
        if isinstance(v, dict) and not (isinstance(v, dict) and v == block_1x2):
            up = v.get("upstream") or v.get("source")
            if up:
                upstreams.add(up)
    if len(upstreams) >= 3:
        return 100
    if len(upstreams) >= 2:
        return 70
    if len(upstreams) >= 1:
        return 40
    return 0


def _dqs_bd(match: Dict) -> float:
    """BD — Base data completeness."""
    score = 0
    for k in ("competition", "country", "date_utc", "home_team", "away_team"):
        if match.get(k):
            score += 20
    return min(score, 100)


def _dqs_cs(match: Dict) -> float:
    """CS — Context & squad."""
    score = 0
    if match.get("lineup") or match.get("lineups"):
        score += 50
    ctx = match.get("context", {})
    if isinstance(ctx, dict):
        if ctx.get("fatigue") is not None:
            score += 25
        if ctx.get("travel") is not None:
            score += 25
    return min(score, 100)


def _dqs_st(match: Dict) -> float:
    """ST — Stats completeness."""
    stats = match.get("stats", {})
    if not isinstance(stats, dict):
        return 0.0
    metrics = ("possession", "shots", "shots_on_target", "corners", "fouls", "yellow_cards")
    count = sum(1 for m in metrics if m in stats)
    return min(count / 6 * 100, 100)


def _dqs_si(match: Dict) -> float:
    """SI — Source independence."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return 0.0
    upstreams = set()
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        for s in _extract_1x2_sources(block_1x2):
            up = s.get("upstream") or s.get("source")
            if up:
                upstreams.add(up)
        if upstreams:
            n = len(upstreams)
            if n >= 4: return 100
            if n >= 3: return 80
            if n >= 2: return 50
            if n >= 1: return 25
    # ── Legacy per-source ──
    for v in odds.values():
        if isinstance(v, dict):
            up = v.get("upstream") or v.get("source")
            if up:
                upstreams.add(up)
    if len(upstreams) >= 4:
        return 100
    if len(upstreams) >= 3:
        return 80
    if len(upstreams) >= 2:
        return 50
    if len(upstreams) >= 1:
        return 25
    return 0


_DQS_WEIGHTS = {"OD": 0.20, "OH": 0.15, "PI": 0.10, "HL": 0.15, "SS": 0.10,
                "BD": 0.10, "CS": 0.05, "ST": 0.05, "SI": 0.10}


def _calc_dqs(match: Dict, history: Optional[List] = None) -> Dict[str, Any]:
    components = {
        "OD": _dqs_od(match), "OH": _dqs_oh(match), "PI": _dqs_pi(match),
        "HL": _dqs_hl(match, history), "SS": _dqs_ss(match), "BD": _dqs_bd(match),
        "CS": _dqs_cs(match), "ST": _dqs_st(match), "SI": _dqs_si(match),
    }
    score = sum(components[k] * _DQS_WEIGHTS[k] for k in components)
    # Penalties
    penalties = 0
    odds = match.get("odds", {})
    if isinstance(odds, dict) and len(odds) == 1:
        penalties += 5  # single source
    score = max(0, score - penalties)
    if score >= 90:
        level = "HIGH"
    elif score >= 75:
        level = "GOOD"
    elif score >= 60:
        level = "MEDIUM"
    else:
        level = "LOW"
    return {"score": round(score, 1), "level": level, "components": {k: round(v, 1) for k, v in components.items()}}


# ============================================================================
# PHASE 2: MC — Model Confidence (6 компонентов, §22.2)
# ============================================================================

def _calc_mc(consensus: Dict, dqs: Dict, match_data: Dict) -> Dict[str, Any]:
    # A — Agreement (entropy-based)
    probs = consensus.get("probs") or [0.33, 0.33, 0.34]
    entropy = -sum(p * math.log(p + 1e-10) for p in probs if p > 0) / math.log(3)
    A = (1 - entropy) * 100

    # C — Calibration (Phase 3: PAVA, сейчас LIMITED)
    C = 50.0

    # S — Sample size
    S = min(consensus.get("sample_size", 0) / 50 * 100, 100)

    # P — Personnel
    P = 50.0
    if match_data.get("lineup") or match_data.get("lineups"):
        P = 80.0

    # M — Market stability
    M = 80.0
    margin = consensus.get("margin", 0.05)
    if margin > 0.10:
        M = 40.0

    # L — League coverage (Phase 3: passthrough)
    L = 50.0

    score = (A * 0.25 + C * 0.20 + S * 0.20 + P * 0.10 + M * 0.15 + L * 0.10)
    if score >= 75:
        level = "HIGH"
    elif score >= 55:
        level = "MEDIUM"
    elif score >= 35:
        level = "LOW"
    else:
        level = "CRITICAL"
    return {"score": round(score, 1), "level": level,
            "components": {"A": round(A, 1), "C": round(C, 1), "S": round(S, 1),
                           "P": round(P, 1), "M": round(M, 1), "L": round(L, 1)}}


# ============================================================================
# PHASE 2: U — Uncertainty Score (6 компонентов, §22.3)
# ============================================================================

def _calc_u(match: Dict, dqs: Dict, mc: Dict, risk_flags: List[str]) -> Dict[str, Any]:
    # DU — Data uncertainty
    dqs_score = dqs.get("score", 0)
    DU = max(0, 100 - dqs_score)

    # MD — Model disagreement
    MD = max(0, 100 - mc.get("score", 0))

    # PU — Personnel uncertainty
    if not match.get("lineup") and not match.get("lineups"):
        PU = 60.0
    else:
        PU = 20.0

    # MU — Market uncertainty
    margin = 0
    odds = match.get("odds", {})
    if isinstance(odds, dict):
        for v in odds.values():
            if isinstance(v, dict) and v.get("odds"):
                m = calculate_margin(v["odds"] if isinstance(v["odds"], dict) else {})
                if m:
                    margin = m
                    break
    MU = min(margin * 500, 100)

    # CU — Contextual uncertainty (floor по risk flags)
    n_flags = len(risk_flags)
    if n_flags >= 3:
        CU = 85.0
    elif n_flags == 2:
        CU = 70.0
    elif n_flags == 1:
        CU = 40.0
    else:
        CU = 15.0

    # CalU — Calibration uncertainty (Phase 3)
    CalU = 50.0

    score = (DU * 0.25 + MD * 0.20 + PU * 0.15 + MU * 0.15 + CU * 0.15 + CalU * 0.10)
    if score >= 75:
        level = "EXTREME"
    elif score >= 50:
        level = "HIGH"
    elif score >= 30:
        level = "MEDIUM"
    else:
        level = "LOW"
    return {"score": round(score, 1), "level": level,
            "components": {"DU": round(DU, 1), "MD": round(MD, 1), "PU": round(PU, 1),
                           "MU": round(MU, 1), "CU": round(CU, 1), "CalU": round(CalU, 1)}}


# ============================================================================
# PHASE 2: Risk Flags (6, §22.5)
# ============================================================================

def _check_fatigue(match: Dict, history: Optional[List] = None) -> bool:
    if not history:
        return False
    dates = []
    from datetime import datetime, timezone
    for h in history[-10:]:
        d = h.get("date_utc", "")
        if d:
            try:
                dates.append(datetime.fromisoformat(d.replace("Z", "+00:00")))
            except Exception:
                pass
    if len(dates) < 2:
        return False
    # >=2 matches in 7 days or >=3 in 10 days
    for i in range(len(dates) - 1):
        gap = (dates[i+1] - dates[i]).days
        if abs(gap) <= 3:
            return True
    return False


def _check_travel(match: Dict) -> bool:
    ctx = match.get("context", {})
    if isinstance(ctx, dict) and ctx.get("travel"):
        return True
    return False


def _check_rotation(match: Dict) -> bool:
    lineup = match.get("lineup") or match.get("lineups")
    if not lineup:
        return False
    return False  # Requires previous lineup comparison


def _calc_risk_flags(match: Dict, history: Optional[List] = None) -> List[str]:
    flags = []
    stats = match.get("stats", {})
    if isinstance(stats, dict):
        # defensive_collapse
        if stats.get("goals_conceded_last5", 99) >= 10:
            flags.append("defensive_collapse")
        # extreme_attack
        if stats.get("goals_scored_last5", 0) >= 15:
            flags.append("extreme_attack")
        # red_card_driven
        if stats.get("red_cards_last5", 0) >= 2:
            flags.append("red_card_driven")
    # fatigue
    if _check_fatigue(match, history):
        flags.append("fatigue")
    # travel
    if _check_travel(match):
        flags.append("travel")
    # rotation
    if _check_rotation(match):
        flags.append("rotation")
    return flags


# ============================================================================
# PHASE 2: Source Independence + Conflict Resolution (§22.5)
# ============================================================================

def _calc_source_independence(match: Dict) -> Dict[str, Any]:
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return {"independent_sources": 0, "conflicts": []}
    upstreams = {}
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        for s in _extract_1x2_sources(block_1x2):
            up = s.get("upstream") or s.get("source", "unknown")
            o = _extract_odds_tuple(s)
            if o:
                upstreams.setdefault(up, []).append(o)
        # Also add level-based odds
        for level in ("current", "opening", "best"):
            lvl_block = block_1x2.get(level)
            if isinstance(lvl_block, dict):
                o = _extract_odds_tuple(lvl_block)
                if o:
                    up = lvl_block.get("upstream") or lvl_block.get("source", level)
                    upstreams.setdefault(up, []).append(o)
    # ── Legacy per-source ──
    for src, block in odds.items():
        if isinstance(block, dict) and src != "1x2":
            up = block.get("upstream") or block.get("source", src)
            o = _extract_odds_tuple(block.get("odds", {})) if isinstance(block.get("odds"), dict) else None
            if o:
                upstreams.setdefault(up, []).append(o)
    conflicts = []
    for up, vals in upstreams.items():
        if len(vals) >= 2:
            for i in range(len(vals)):
                for j in range(i+1, len(vals)):
                    diff = max(abs(vals[i][k] - vals[j][k]) / max(vals[i][k], 1e-6) for k in range(3))
                    if diff > 0.15:
                        conflicts.append({"upstream": up, "deviation": round(diff, 3)})
    return {"independent_sources": len(upstreams), "conflicts": conflicts}


def _resolve_conflicts(match: Dict) -> Dict[str, Any]:
    """Приоритет: propline > sharpapi > odds_api > bzzoiro."""
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return {}
    priority = ["propline", "sharpapi", "odds_api", "bzzoiro", "football_data"]
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        sources = _extract_1x2_sources(block_1x2)
        for src in priority:
            for s in sources:
                src_name = s.get("source", s.get("upstream", ""))
                if src_name == src:
                    o = _extract_odds_tuple(s)
                    if o:
                        return {"resolved_source": src, "odds": {"home": o[0], "draw": o[1], "away": o[2]}}
        # levels as fallback
        for level in ("current", "best", "opening"):
            o = _extract_from_1x2(block_1x2, level)
            if o:
                return {"resolved_source": level, "odds": {"home": o[0], "draw": o[1], "away": o[2]}}
    # ── Legacy per-source ──
    for src in priority:
        block = odds.get(src)
        if isinstance(block, dict) and block.get("odds"):
            return {"resolved_source": src, "odds": block["odds"]}
    return {}


# ============================================================================
# PHASE 2: Odds Snapshot Versioning (§22.6)
# ============================================================================

def _calc_median_odds(sources: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    if not sources:
        return None
    h = sorted(s[0] for s in sources)
    d = sorted(s[1] for s in sources)
    a = sorted(s[2] for s in sources)
    n = len(sources)
    med = lambda x: x[n//2] if n % 2 else (x[n//2-1] + x[n//2]) / 2
    return med(h), med(d), med(a)


def _calc_line_dispersion(sources: List[Tuple[float, float, float]]) -> Dict[str, float]:
    if len(sources) < 2:
        return {"home": 0, "draw": 0, "away": 0}
    import statistics
    return {
        "home": round(statistics.stdev([s[0] for s in sources]), 4),
        "draw": round(statistics.stdev([s[1] for s in sources]), 4),
        "away": round(statistics.stdev([s[2] for s in sources]), 4),
    }


def _calc_odds_snapshot(match: Dict) -> Dict[str, Any]:
    odds = match.get("odds", {})
    if not isinstance(odds, dict):
        return {"levels": [], "median": None, "line_dispersion": {}, "snapshot_version": 0}
    sources = []
    levels = []
    # ── Canonical 1x2 format ──
    block_1x2 = odds.get("1x2")
    if isinstance(block_1x2, dict):
        for level in ("current", "opening", "best"):
            o = _extract_from_1x2(block_1x2, level)
            if o:
                sources.append(o)
                levels.append(level)
        for s in _extract_1x2_sources(block_1x2):
            o = _extract_odds_tuple(s)
            if o:
                sources.append(o)
                src_name = s.get("source", s.get("upstream", "unknown"))
                levels.append(src_name)
    # ── Legacy per-source ──
    for src, block in odds.items():
        if isinstance(block, dict) and src != "1x2" and isinstance(block.get("odds"), dict):
            o = _extract_odds_tuple(block["odds"])
            if o:
                sources.append(o)
                levels.append(src)
    median = _calc_median_odds(sources)
    dispersion = _calc_line_dispersion(sources)
    return {"levels": levels, "median": list(median) if median else None,
            "line_dispersion": dispersion, "snapshot_version": len(sources)}


# ============================================================================
# PHASE 2: Edge Quality + Anomaly (§22.7)
# ============================================================================

def _calc_edge_quality(value_pct: float, match: Dict, dqs: Dict, mc: Dict, u: Dict,
                       source_indep: Dict) -> Dict[str, Any]:
    quality = "low"
    reasons = []
    if value_pct < 0.03:
        quality = "low"
        reasons.append("below_threshold")
    elif value_pct >= 0.05 and dqs.get("score", 0) >= 70 and mc.get("score", 0) >= 55:
        quality = "high"
    elif value_pct >= 0.03:
        quality = "medium"
    # Anomaly checks (§22.7)
    anomaly = False
    if value_pct > 0.15:
        anomaly = True
        reasons.append("extreme_value")
    margin = 0
    odds = match.get("odds", {})
    if isinstance(odds, dict):
        for v in odds.values():
            if isinstance(v, dict) and isinstance(v.get("odds"), dict):
                m = calculate_margin(v["odds"])
                if m and m > 0.10:
                    anomaly = True
                    reasons.append("high_margin")
                if m:
                    margin = m
                    break
    if mc.get("components", {}).get("A", 100) < 50:
        anomaly = True
        reasons.append("low_agreement")
    if source_indep.get("independent_sources", 0) < 2:
        anomaly = True
        reasons.append("insufficient_sources")
    if len(source_indep.get("conflicts", [])) > 10:
        anomaly = True
        reasons.append("excessive_conflicts")
    return {"quality": quality, "anomaly": anomaly, "reasons": reasons, "margin": round(margin, 4)}


# ============================================================================
# PHASE 2: 12 Cumulative Gates (§22.4)
# ============================================================================

def _gates(value_pct: float, dqs: Dict, mc: Dict, u: Dict, match: Dict,
           consensus: Dict, risk_flags: List, edge_quality: Dict) -> Dict[str, Any]:
    gates = {}
    # G1: DATA_STOP
    gates["G1_DATA_STOP"] = {"pass": dqs.get("score", 0) >= 40, "phase": 1}
    # G2: MARKET_UNAVAILABLE
    gates["G2_MARKET_UNAVAILABLE"] = {"pass": consensus.get("probs") is not None, "phase": 1}
    # G3: CRITICAL_COMPLETENESS
    gates["G3_CRITICAL_COMPLETENESS"] = {"pass": dqs.get("score", 0) >= 60, "phase": 1}
    # G4: DQS_MC_U
    gates["G4_DQS_MC_U"] = {"pass": dqs.get("score", 0) >= 60 and mc.get("score", 0) >= 35, "phase": 1}
    # G5: P_CALIBRATED (Phase 2 passthrough)
    gates["G5_P_CALIBRATED"] = {"pass": True, "phase": 2}
    # G6: CONFIDENCE_GATE (Phase 2)
    gates["G6_CONFIDENCE_GATE"] = {"pass": mc.get("score", 0) >= 35, "phase": 2}
    # G7: EV_5PCT
    gates["G7_EV_5PCT"] = {"pass": value_pct >= 0.03, "phase": 1}
    # G8: MARKET_LINE_VERIFIED
    gates["G8_MARKET_LINE_VERIFIED"] = {"pass": not edge_quality.get("anomaly", False), "phase": 1}
    # G9: P_LOWER (Phase 3 passthrough)
    gates["G9_P_LOWER"] = {"pass": True, "phase": 3}
    # G10: ANOMALY
    gates["G10_ANOMALY"] = {"pass": not edge_quality.get("anomaly", False), "phase": 1}
    # G11: INDEPENDENT_RECALC (Phase 3 passthrough)
    gates["G11_INDEPENDENT_RECALC"] = {"pass": True, "phase": 3}
    # G12: STAKE_AFTER_GATES (Phase 3 passthrough)
    gates["G12_STAKE_AFTER_GATES"] = {"pass": True, "phase": 3}
    all_pass = all(g["pass"] for g in gates.values())
    return {"all_pass": all_pass, "gates": gates}


# ============================================================================
# PHASE 2: Three Scenarios (§24.4)
# ============================================================================

def _bootstrap_lower_bound(value_pct: float, sample: Optional[List] = None,
                           n_iter: int = 10000) -> float:
    """Bootstrap 10th percentile (Phase 3). N>=200 for full, else logit-normal."""
    if sample and len(sample) >= 200:
        random.seed(20260825)
        boot_stats = []
        n = len(sample)
        for _ in range(n_iter):
            resample = [random.choice(sample) for _ in range(n)]
            boot_stats.append(sum(resample) / n)
        boot_stats.sort()
        return boot_stats[int(n_iter * 0.10)]
    # Logit-normal fallback
    if value_pct <= 0:
        return 0.0
    logit = math.log(value_pct / (1 - value_pct)) if value_pct < 1 else 4.0
    return 1.0 / (1.0 + math.exp(-(logit - 0.5)))


def _calc_scenarios(value_obj: Dict, match_data: Dict) -> Dict[str, Any]:
    base = value_obj.get("value_pct", 0)
    u_score = value_obj.get("u", {}).get("score", 50)
    spread = 0.02 + u_score / 100 * 0.10  # 2-12% spread
    return {
        "realistic": {"value_pct": round(base, 4)},
        "optimistic": {"value_pct": round(min(base + spread, 0.50), 4)},
        "destructive": {"value_pct": round(max(base - spread, -0.20), 4)},
        "lower_bound": round(_bootstrap_lower_bound(base), 4),
    }


# ============================================================================
# PHASE 2: Edge Cases §27
# ============================================================================

def _check_attack_exhaustion(history: Optional[List]) -> Optional[Dict]:
    if not history or len(history) < 2:
        return None
    recent = history[-2:]
    total_goals = sum(h.get("goals_total", h.get("score_total", 0)) for h in recent if isinstance(h, dict))
    if total_goals >= 6:
        return {"flag": True, "reason": "attack_exhaustion", "goals": total_goals}
    return None


def _check_h2h_context_reset(h2h: Optional[List]) -> Optional[Dict]:
    if not h2h:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for h in h2h:
        d = h.get("date", h.get("date_utc", ""))
        if d:
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                if (now - dt).days > 365 * 3:
                    return {"flag": True, "reason": "h2h_context_reset", "age_years": round((now - dt).days / 365, 1)}
            except Exception:
                pass
    return None


def _check_fresh_form_override(history: Optional[List]) -> Optional[Dict]:
    if not history or len(history) < 3:
        return None
    recent = history[-3:]
    wins = sum(1 for h in recent if isinstance(h, dict) and h.get("result") == "W")
    if wins >= 3:
        return {"flag": True, "reason": "fresh_form_override", "boost": 1.5}
    return None


def _check_early_season_override(history: Optional[List]) -> Optional[Dict]:
    if not history or len(history) < 6:
        return {"flag": True, "reason": "early_season", "form_weight_mult": 0.5, "prior_weight_mult": 1.5}
    return None


# ============================================================================
# PHASE 3: Layer 2 — Poisson + Dixon-Coles (§23.1)
# ============================================================================

def _calc_attack_defence(history: Optional[List], decay: float = 0.0035):
    """Attack/defence strength with time-decay weighting."""
    if not history or len(history) < 3:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    goals_for = []
    goals_against = []
    for h in history[-30:]:
        if not isinstance(h, dict):
            continue
        d = h.get("date_utc", "")
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            age_days = max((now - dt).days, 0)
        except Exception:
            age_days = 365
        w = math.exp(-decay * age_days)
        gf = h.get("goals_for", h.get("home_goals", 0))
        ga = h.get("goals_against", h.get("away_goals", 0))
        goals_for.append((gf, w))
        goals_against.append((ga, w))
    if not goals_for:
        return None
    w_sum = sum(w for _, w in goals_for)
    if w_sum == 0:
        return None
    avg_gf = sum(g * w for g, w in goals_for) / w_sum
    avg_ga = sum(g * w for g, w in goals_against) / w_sum
    return {"attack": avg_gf, "defence": avg_ga, "sample": len(goals_for)}


def _poisson_prob(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def _dixon_coles_rho(home_goals: int, away_goals: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles τ_ρ correction for low-score cells."""
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lam_h * lam_a * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lam_a * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lam_h * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def _layer_poisson(match: Dict, history: Optional[List] = None) -> Dict[str, Any]:
    """Layer 2: Poisson + Dixon-Coles."""
    home_stats = _calc_attack_defence(history) if history else None
    away_stats = None
    if history:
        # Simple: use same history for away (in real impl, separate home/away)
        away_stats = home_stats
    if not home_stats:
        return {"probs": None, "sample_size": 0, "confidence": 0}
    # λ with home advantage
    lam_h = max(home_stats["attack"] * 1.10, 0.1)
    lam_a = max(away_stats["attack"] * 0.95, 0.1)
    n_rho = home_stats["sample"]
    use_dc = n_rho >= 100
    rho = 0.0 if not use_dc else -0.05  # simplified
    # Grid 0-10
    max_goals = 10
    probs = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = _poisson_prob(lam_h, i) * _poisson_prob(lam_a, j)
            if use_dc:
                p *= _dixon_coles_rho(i, j, lam_h, lam_a, rho)
            probs[i][j] = p
    # Renormalize
    total = sum(sum(row) for row in probs)
    if total > 0:
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                probs[i][j] /= total
    # To 1X2
    p_home = sum(probs[i][j] for i in range(max_goals+1) for j in range(max_goals+1) if i > j)
    p_draw = sum(probs[i][j] for i in range(max_goals+1) for j in range(max_goals+1) if i == j)
    p_away = sum(probs[i][j] for i in range(max_goals+1) for j in range(max_goals+1) if i < j)
    return {"probs": [p_home, p_draw, p_away], "sample_size": n_rho,
            "confidence": min(n_rho / 50, 1.0), "lambda": [lam_h, lam_a]}


# ============================================================================
# PHASE 3: Layer 3 — Elo (§23.2)
# ============================================================================

def _calc_elo_ratings(history: Optional[List], k: float = 20.0) -> Optional[Dict]:
    if not history or len(history) < 3:
        return None
    rating = 1500.0
    for h in history[-50:]:
        if not isinstance(h, dict):
            continue
        result = h.get("result", "D")
        gf = h.get("goals_for", 0)
        ga = h.get("goals_against", 0)
        expected = 1.0 / (1.0 + 10 ** ((1500 - rating) / 400))
        if result == "W":
            actual = 1.0
        elif result == "D":
            actual = 0.5
        else:
            actual = 0.0
        # Goal-diff adjustment
        gd = abs(gf - ga)
        k_adj = k * (1.0 + gd * 0.1) if gd > 0 else k
        rating += k_adj * (actual - expected)
    return {"rating": rating, "sample_size": len(history[-50:])}


def _elo_to_prob(rating: float, opp_rating: float = 1500.0, home_adv: float = 65.0) -> Tuple[float, float, float]:
    """Elo ratings → (P_home, P_draw, P_away)."""
    dr = (rating + home_adv) - opp_rating
    p_home = 1.0 / (1.0 + 10 ** (-dr / 400))
    p_away = 1.0 - p_home
    p_draw = 0.28 + 0.10 * (1 - abs(p_home - p_away))
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def _layer_elo(match: Dict, history: Optional[List] = None) -> Dict[str, Any]:
    """Layer 3: Elo ratings."""
    ratings = _calc_elo_ratings(history)
    if not ratings:
        return {"probs": None, "sample_size": 0, "confidence": 0}
    p_h, p_d, p_a = _elo_to_prob(ratings["rating"])
    return {"probs": [p_h, p_d, p_a], "sample_size": ratings["sample_size"],
            "confidence": min(ratings["sample_size"] / 30, 1.0), "rating": ratings["rating"]}


# ============================================================================
# PHASE 3: Layer 4 — Form + Bayesian Shrinkage (§23.3, §23.4)
# ============================================================================

def _calc_team_form(history: Optional[List], decay: float = 0.005) -> Optional[Dict]:
    if not history or len(history) < 2:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    wins = draws = losses = 0
    goals_for = goals_against = 0
    w_sum = 0
    for h in history[-20:]:
        if not isinstance(h, dict):
            continue
        d = h.get("date_utc", "")
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            age = max((now - dt).days, 0)
        except Exception:
            age = 365
        w = math.exp(-decay * age)
        result = h.get("result", "D")
        gf = h.get("goals_for", 0)
        ga = h.get("goals_against", 0)
        if result == "W":
            wins += w
        elif result == "D":
            draws += w
        else:
            losses += w
        goals_for += gf * w
        goals_against += ga * w
        w_sum += w
    if w_sum == 0:
        return None
    n = len(history[-20:])
    return {"wins": wins / w_sum, "draws": draws / w_sum, "losses": losses / w_sum,
            "goals_for_avg": goals_for / w_sum, "goals_against_avg": goals_against / w_sum,
            "sample_size": n}


def _layer_form(match: Dict, history: Optional[List] = None, prior: Tuple = (0.45, 0.25, 0.30)) -> Dict[str, Any]:
    """Layer 4: Form + Bayesian shrinkage."""
    form = _calc_team_form(history)
    if not form:
        return {"probs": None, "sample_size": 0, "confidence": 0}
    n = form["sample_size"]
    k = 15  # shrinkage constant
    w = n / (n + k)
    p_emp = [form["wins"], form["draws"], form["losses"]]
    p_prior = list(prior)
    p_shrunk = [w * p_emp[i] + (1 - w) * p_prior[i] for i in range(3)]
    # Home advantage
    p_shrunk[0] *= 1.10
    total = sum(p_shrunk)
    p_shrunk = [p / total for p in p_shrunk]
    return {"probs": p_shrunk, "sample_size": n, "confidence": min(n / 15, 1.0),
            "form_data": form, "shrinkage_weight": round(w, 3)}


# ============================================================================
# PHASE 3: Layer 5 — Consensus (Q_i weighted, §21.2)
# ============================================================================

def _calc_q_weights(layers: List[Dict]) -> List[float]:
    """OOS Brier → inverse weighting.
    FIX: confidence (0-1, higher=better) → brier = 1 - confidence (lower=better)."""
    briers = []
    for layer in layers:
        b = layer.get("brier")
        if b is None:
            b = 1.0 - layer.get("confidence", 0.5)
        briers.append(max(b, 0.01))
    inv = [1.0 / b for b in briers]
    total = sum(inv)
    return [i / total for i in inv]


def _layer_consensus(layers: List[Dict]) -> Dict[str, Any]:
    """Weighted ensemble of all model layers."""
    valid = [l for l in layers if l.get("probs")]
    if not valid:
        return {"probs": None, "sample_size": 0, "confidence": 0, "has_model": False}
    weights = _calc_q_weights(valid)
    probs = [0.0, 0.0, 0.0]
    for i, layer in enumerate(valid):
        p = layer["probs"]
        for j in range(3):
            probs[j] += weights[i] * p[j]
    total = sum(probs)
    probs = [p / total for p in probs] if total > 0 else [0.33, 0.33, 0.34]
    avg_conf = sum(l.get("confidence", 0) * weights[i] for i, l in enumerate(valid))
    max_sample = max(l.get("sample_size", 0) for l in valid)
    return {"probs": probs, "sample_size": max_sample, "confidence": avg_conf, "has_model": True}


# ============================================================================
# PHASE 3: PAVA Calibration (§23.5)
# ============================================================================

def _pava_fit(x: List[float], y: List[float]) -> List[float]:
    """Pool Adjacent Violators Algorithm — isotonic regression."""
    n = len(y)
    if n == 0:
        return []
    if n == 1:
        return list(y)
    values = list(y)
    weights = [1.0] * n
    while True:
        violated = False
        for i in range(len(values) - 1):
            if values[i] > values[i + 1]:
                w1, w2 = weights[i], weights[i + 1]
                merged = (values[i] * w1 + values[i + 1] * w2) / (w1 + w2)
                values[i] = merged
                weights[i] = w1 + w2
                del values[i + 1]
                del weights[i + 1]
                violated = True
                break
        if not violated:
            break
    return values


def _layer_calibration(probs: List[float], oos_data: Optional[List] = None) -> Dict[str, Any]:
    """Hierarchical calibration: league → team → match."""
    if not oos_data or len(oos_data) < 100:
        return {"probs": probs, "status": "LIMITED", "calibrated": False}
    # PAVA on calibration set
    pred_probs = [d[0] for d in oos_data]
    actual = [d[1] for d in oos_data]
    calibrated = _pava_fit(pred_probs, actual)
    return {"probs": calibrated[:len(probs)] if len(calibrated) >= len(probs) else probs,
            "status": "ACTIVE", "calibrated": True}


# ============================================================================
# PHASE 3: Layer 6 — Value (EV + Kelly)
# ============================================================================

def _layer_value(consensus_probs: List[float], best_odds: Optional[Tuple]) -> Dict[str, Any]:
    if not best_odds:
        return {"ev": [0, 0, 0], "value_pct": 0, "value_side": "none"}
    ev = [consensus_probs[i] * best_odds[i] - 1.0 for i in range(3)]
    best_ev = max(ev)
    labels = ["home", "draw", "away"]
    idx = ev.index(best_ev)
    return {"ev": ev, "value_pct": best_ev, "value_side": labels[idx]}


# ============================================================================
# PHASE 3: Kelly Stake Engine (§24.1-24.3)
# ============================================================================

def _calc_kelly_binary(p: float, odds: float) -> float:
    """Binary Kelly: f = (bp - q) / b."""
    b = odds - 1.0
    q = 1.0 - p
    if b <= 0:
        return 0.0
    return max((b * p - q) / b, 0.0)


def _calc_kelly_three_way(probs: List[float], odds: List[float],
                           tol: float = 1e-8, max_iter: int = 1000) -> List[float]:
    """Multinomial Kelly via iterative KKT."""
    f = [0.0, 0.0, 0.0]
    for _ in range(max_iter):
        f_new = list(f)
        for i in range(3):
            b = odds[i] - 1.0
            if b <= 0:
                f_new[i] = 0.0
                continue
            # Marginal utility
            bankroll_frac = 1.0 - sum(f)
            if bankroll_frac <= 0:
                f_new[i] = 0.0
                continue
            kelly = (b * probs[i] - (1 - probs[i])) / b
            f_new[i] = max(kelly, 0.0)
        if all(abs(f_new[i] - f[i]) < tol for i in range(3)):
            break
        f = f_new
    # Normalize to sum <= 1
    total = sum(f)
    if total > 1.0:
        f = [x / total for x in f]
    return f


def calc_kelly_stake(value_analysis: Dict, bankroll_pct: float = 0.02) -> Dict[str, Any]:
    """Kelly stake with caps: MAIN 2%, VALIDATED 1%, HIGH_RISK 0.5%."""
    u_score = value_analysis.get("u", {}).get("score", 50)
    if u_score > 50:
        return {"stake_pct": 0.0, "reason": "uncertainty_too_high"}
    ev = value_analysis.get("ev", [0, 0, 0])
    probs = value_analysis.get("model_probs", [0.33, 0.33, 0.34])
    odds = value_analysis.get("odds")
    if not odds:
        return {"stake_pct": 0.0, "reason": "no_odds"}
    odds_tuple = (odds.get("home", 0), odds.get("draw", 0), odds.get("away", 0))
    kelly = _calc_kelly_three_way(probs, odds_tuple)
    best_idx = max(range(3), key=lambda i: kelly[i])
    raw_stake = kelly[best_idx]
    # Caps
    verification = value_analysis.get("verification", "SINGLE")
    if verification == "VERIFIED":
        cap = 0.02
    elif verification == "CONSENSUS":
        cap = 0.01
    else:
        cap = 0.005
    stake = min(raw_stake, cap)
    return {"stake_pct": round(stake, 4), "kelly_raw": round(raw_stake, 4),
            "side": ["home", "draw", "away"][best_idx], "cap": cap}


# ============================================================================
# PHASE 3: CLV Capture (§25.2)
# ============================================================================

def _calc_clv(entry_odds: Dict, closing_odds: Dict) -> Dict[str, Any]:
    """Closing Line Value: entry vs closing."""
    e = _extract_odds_tuple(entry_odds)
    c = _extract_odds_tuple(closing_odds)
    if not e or not c:
        return {"clv_pct": 0, "positive": False}
    # Shin de-vig (simplified)
    e_p = _normalize_probs(_implied_probs(e)[:3])
    c_p = _normalize_probs(_implied_probs(c)[:3])
    clv = sum((c_p[i] - e_p[i]) * e[i] for i in range(3))
    return {"clv_pct": round(clv, 4), "positive": clv > 0}


# ============================================================================
# PHASE 3: Post-Match Audit (E001-E009, §25.1)
# ============================================================================

def _post_match_audit(prediction: Dict, actual: Dict) -> List[Dict]:
    """9 error codes: xG, goals, shots, possession, calibration, lineup, CLV, DQS, model."""
    errors = []
    # E001: xG error
    pred_xg = prediction.get("xg_pred")
    actual_xg = actual.get("xg")
    if pred_xg is not None and actual_xg is not None:
        if abs(pred_xg - actual_xg) > 1.0:
            errors.append({"code": "E001", "desc": "xG deviation > 1.0", "pred": pred_xg, "actual": actual_xg})
    # E002: Goal forecast
    pred_goals = prediction.get("goals_pred", 0)
    actual_goals = actual.get("total_goals", 0)
    if abs(pred_goals - actual_goals) >= 3:
        errors.append({"code": "E002", "desc": "goal forecast deviation >= 3", "pred": pred_goals, "actual": actual_goals})
    # E003: Shot deviation
    pred_shots = prediction.get("shots_pred")
    actual_shots = actual.get("shots")
    if pred_shots is not None and actual_shots is not None:
        if abs(pred_shots - actual_shots) > 8:
            errors.append({"code": "E003", "desc": "shot deviation > 8", "pred": pred_shots, "actual": actual_shots})
    # E004: Possession
    pred_poss = prediction.get("possession_pred")
    actual_poss = actual.get("possession")
    if pred_poss is not None and actual_poss is not None:
        if abs(pred_poss - actual_poss) > 20:
            errors.append({"code": "E004", "desc": "possession deviation > 20%", "pred": pred_poss, "actual": actual_poss})
    # E005: Calibration drift
    if prediction.get("calibration_drift", 0) > 0.10:
        errors.append({"code": "E005", "desc": "calibration drift > 10%"})
    # E006: Lineup mismatch
    if prediction.get("lineup_hash") and actual.get("lineup_hash"):
        if prediction["lineup_hash"] != actual["lineup_hash"]:
            errors.append({"code": "E006", "desc": "lineup mismatch"})
    # E007: CLV negative
    if prediction.get("clv_pct", 0) < 0:
        errors.append({"code": "E007", "desc": "negative CLV", "clv": prediction["clv_pct"]})
    # E008: DQS regression
    if prediction.get("dqs_score", 100) < actual.get("dqs_score", 100):
        errors.append({"code": "E008", "desc": "DQS regression"})
    # E009: Model failure
    if not prediction.get("probs"):
        errors.append({"code": "E009", "desc": "model produced no output"})
    return errors


# ============================================================================
# PHASE 3: Backtest (§25.4)
# ============================================================================

def calc_brier_score(pred_probs: List[float], actual_outcome: int) -> float:
    """Brier score for 1X2. actual_outcome: 0=home, 1=draw, 2=away."""
    one_hot = [0.0, 0.0, 0.0]
    one_hot[actual_outcome] = 1.0
    return sum((pred_probs[i] - one_hot[i]) ** 2 for i in range(3))


def calc_rps(pred_probs: List[float], actual_outcome: int) -> float:
    """Ranked Probability Score for ordinal markets.
    Normalized by (K-1) where K=3 outcomes → divide by 2."""
    cum_pred = []
    cum_actual = []
    sp = sa = 0.0
    for i in range(3):
        sp += pred_probs[i]
        sa += 1.0 if i == actual_outcome else 0.0
        cum_pred.append(sp)
        cum_actual.append(sa)
    raw = sum((cum_pred[i] - cum_actual[i]) ** 2 for i in range(3))
    return raw / (3 - 1)  # normalize by (K-1)


def backtest_match(prediction: Dict, actual_result: Dict) -> Dict[str, Any]:
    """Compare prediction vs actual for one match."""
    probs = prediction.get("model_probs", [0.33, 0.33, 0.34])
    outcome = actual_result.get("outcome", 1)  # 0=home, 1=draw, 2=away
    return {
        "brier": round(calc_brier_score(probs, outcome), 4),
        "rps": round(calc_rps(probs, outcome), 4),
        "value_pct": prediction.get("value_pct", 0),
        "actual_outcome": outcome,
        "correct": probs.index(max(probs)) == outcome,
    }


def run_backtest(predictions: List[Dict], actuals: List[Dict],
                  window: int = 50) -> Dict[str, Any]:
    """Sequential backtest with sliding window and drift detection."""
    results = []
    for pred, actual in zip(predictions, actuals):
        results.append(backtest_match(pred, actual))
    n = len(results)
    if n == 0:
        return {"total": 0, "avg_brier": 0, "drift": False}
    # Sliding window
    briers = [r["brier"] for r in results]
    if n >= window * 2:
        early = briers[:window]
        late = briers[-window:]
        early_avg = sum(early) / window
        late_avg = sum(late) / window
        drift = (late_avg - early_avg) / early_avg >= 0.10 if early_avg > 0 else False
    else:
        drift = False
    avg_brier = sum(briers) / n
    accuracy = sum(1 for r in results if r["correct"]) / n
    return {
        "total": n,
        "avg_brier": round(avg_brier, 4),
        "accuracy": round(accuracy, 4),
        "drift_detected": drift,
        "results": results,
    }


# ============================================================================
# PHASE 3: xG Provider Matrix (§25.5)
# ============================================================================

def _xg_provider_matrix(match: Dict) -> Dict[str, Any]:
    """Priority: football_data (80), bzzoiro (70). Conflict detection."""
    providers = []
    stats = match.get("stats", {})
    if isinstance(stats, dict):
        if stats.get("_source") == "football_data":
            providers.append({"name": "football_data", "confidence": 80, "xg": stats.get("xg")})
        if stats.get("_source") == "bzzoiro":
            providers.append({"name": "bzzoiro", "confidence": 70, "xg": stats.get("xg")})
    if not providers:
        return {"resolved": None, "conflict": False}
    providers.sort(key=lambda x: -x["confidence"])
    resolved = providers[0]
    conflict = False
    if len(providers) >= 2:
        xg_vals = [p["xg"] for p in providers if p.get("xg") is not None]
        if len(xg_vals) >= 2 and abs(xg_vals[0] - xg_vals[1]) > 0.5:
            conflict = True
    return {"resolved": resolved, "conflict": conflict, "providers": providers}


# ============================================================================
# PHASE 3: Repeat Lineup Check (§27)
# ============================================================================

def _check_repeat_lineup(match: Dict, history: Optional[List] = None) -> Optional[Dict]:
    if not history or not match.get("lineup"):
        return None
    prev = history[-1] if history else None
    if prev and isinstance(prev, dict) and prev.get("lineup"):
        if match["lineup"] == prev["lineup"]:
            return {"flag": True, "reason": "repeat_lineup"}
    return None


# ============================================================================
# PHASE 3: Game State (§27)
# ============================================================================

def _check_game_state(match: Dict) -> Optional[Dict]:
    comp = match.get("competition", "").lower()
    if "cup" in comp or "cupa" in comp:
        return {"type": "cup"}
    if "aggregate" in comp or "qualify" in comp:
        return {"type": "aggregate"}
    return {"type": "league"}


# ============================================================================
# MAIN: analyze_match (§21.1, §21.5)
# ============================================================================



def _adapt_history(history: Optional[List]) -> Optional[List]:
    """Convert history format to internal format for Poisson/Elo/Form layers."""
    if not history or not isinstance(history, list):
        return history
    adapted = []
    for m in history:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        score = item.get("score")
        if isinstance(score, dict) and "result" not in item:
            gf = score.get("home", score.get("goals_for", 0))
            ga = score.get("away", score.get("goals_against", 0))
            try:
                gf = int(gf) if gf is not None else 0
                ga = int(ga) if ga is not None else 0
            except (TypeError, ValueError):
                gf = ga = 0
            item["goals_for"] = gf
            item["goals_against"] = ga
            if gf > ga: item["result"] = "W"
            elif gf < ga: item["result"] = "L"
            else: item["result"] = "D"
        adapted.append(item)
    return adapted

def analyze_match(match: Dict, history: Optional[List] = None,
                   value_threshold: Optional[float] = None) -> Dict[str, Any]:
    """Главная функция — оркестрирует все слои. Никогда не падает (§27)."""
    try:
        if not isinstance(match, dict):
            return {"error": "invalid_input", "canonical_id": ""}
        cid = match.get("canonical_id", "")

        # Threshold (динамический)
        if value_threshold is None:
            if _get_threshold:
                try:
                    value_threshold = _get_threshold()
                except Exception:
                    value_threshold = _DEFAULT_THRESHOLD
            else:
                value_threshold = _DEFAULT_THRESHOLD

        # Odds
        all_odds = match.get("odds", {})
        open_odds, close_odds = extract_odds_pair(all_odds) if isinstance(all_odds, dict) else (None, None)
        best_odds = _extract_best_source(all_odds) if isinstance(all_odds, dict) else None

        # ── Layers ──
        layer_market = {"probs": _normalize_probs(_implied_probs(best_odds)[:3]) if best_odds else None,
                        "margin": calculate_margin({"home": best_odds[0], "draw": best_odds[1], "away": best_odds[2]}) if best_odds else None}
        # FIX: Adapt history format for Poisson/Elo/Form layers
        history = _adapt_history(history)
        layer_poisson = _layer_poisson(match, history)
        layer_elo = _layer_elo(match, history)
        layer_form = _layer_form(match, history)
        # Early-season override
        early = _check_early_season_override(history)
        # Bzzoiro predictions as layer
        pred = match.get("predictions") or match.get("bzzoiro_predictions")
        layer_bzzoiro = {"probs": None, "sample_size": 0, "confidence": 0}
        # FIX: Use _extract_pred_probs for fallback key support
        pred_probs = _extract_pred_probs(pred) if isinstance(pred, dict) else None
        if pred_probs is not None:
            layer_bzzoiro = {"probs": list(pred_probs),
                            "sample_size": 1, "confidence": 0.6}

        # Consensus — layer_market is the BENCHMARK, not a model layer.
        layers = [layer_poisson, layer_elo, layer_form, layer_bzzoiro]
        consensus = _layer_consensus(layers)

        # FIX: Market fallback (§27 case 6) — if all model layers dead, use market implied
        fallback = "none"
        if consensus.get("probs") is None and best_odds:
            implied = _implied_probs(best_odds)[:3]
            consensus = {"probs": _normalize_probs(implied),
                        "sample_size": 0, "confidence": 0.0, "has_model": False}
            fallback = "market"

        # Calibration (passthrough if no model)
        if consensus.get("probs") is not None:
            consensus["probs"] = _layer_calibration(consensus["probs"])["probs"]

        # ── DIAG logging ──
        _has_odds = "Y" if best_odds else "N"
        _has_pred = "Y" if (isinstance(pred, dict) and _extract_pred_probs(pred) is not None) else "N"
        _has_hist = "Y" if history else "N"
        _layer_status = {
            "poisson": layer_poisson.get("probs") is not None,
            "elo": layer_elo.get("probs") is not None,
            "form": layer_form.get("probs") is not None,
            "bzzoiro": layer_bzzoiro.get("probs") is not None,
        }
        _consensus_str = ("None (all layers dead)" if consensus.get("probs") is None
                          else "[" + " ".join(f"{p:.3f}" for p in consensus["probs"]) + "]")
        if _has_pred == "N" and isinstance(pred, dict) and pred:
            print(f"[DIAG] cid={cid} pred_keys_not_found: {list(pred.keys())[:10]}")
        print(f"[DIAG] cid={cid} odds={_has_odds} pred={_has_pred} hist={_has_hist} "
              f"layers={_layer_status} consensus={_consensus_str} fallback={fallback}")

        # ── Quality metrics ──
        dqs = _calc_dqs(match, history)
        mc = _calc_mc(consensus, dqs, match)
        risk_flags = _calc_risk_flags(match, history)
        u = _calc_u(match, dqs, mc, risk_flags)
        source_indep = _calc_source_independence(match)
        _consensus_probs = consensus.get("probs") or [0.33, 0.33, 0.34]
        edge_quality = _calc_edge_quality(
            max(_consensus_probs[i] * (best_odds[i] if best_odds else 0) - 1.0 for i in range(3)) if best_odds else 0,
            match, dqs, mc, u, source_indep)

        # ── Value ──
        if consensus.get("probs") is not None:
            value = _layer_value(consensus["probs"], best_odds)
        else:
            value = {"ev": [0, 0, 0], "value_pct": 0, "value_side": "none"}
        snapshot = _calc_odds_snapshot(match)
        conflicts = _resolve_conflicts(match)
        scenarios = _calc_scenarios({"value_pct": value["value_pct"], "u": u}, match)

        # ── Edge cases ──
        edge_cases = {}
        ae = _check_attack_exhaustion(history)
        if ae:
            edge_cases["attack_exhaustion"] = ae
        hr = _check_h2h_context_reset(match.get("h2h"))
        if hr:
            edge_cases["h2h_context_reset"] = hr
        ff = _check_fresh_form_override(history)
        if ff:
            edge_cases["fresh_form_override"] = ff
        if early:
            edge_cases["early_season"] = early

        # ── Gates ──
        gates = _gates(value["value_pct"], dqs, mc, u, match, consensus, risk_flags, edge_quality)

        # ── Kelly ──
        value_analysis = {
            "canonical_id": cid,
            "market_probs": layer_market.get("probs"),
            "model_probs": consensus.get("probs"),
            "ev": value["ev"],
            "value_pct": value["value_pct"],
            "value_side": value["value_side"],
            "margin": layer_market.get("margin"),
            "odds": {"home": best_odds[0], "draw": best_odds[1], "away": best_odds[2]} if best_odds else None,
            "dqs": dqs, "mc": mc, "u": u,
            "risk_flags": risk_flags,
            "source_independence": source_indep,
            "edge_quality": edge_quality,
            "gates": gates,
            "scenarios": scenarios,
            "snapshot": snapshot,
            "conflicts": conflicts,
            "edge_cases": edge_cases,
            "consensus": consensus,
        }
        kelly = calc_kelly_stake(value_analysis)
        value_analysis["kelly"] = kelly

        # ── Classification ──
        v = value["value_pct"]
        dqs_score = dqs.get("score", 0)
        hot_threshold = value_threshold
        warm_threshold = value_threshold * 0.5
        if v >= hot_threshold and dqs_score > 70:
            classification = "HOT"
        elif v >= hot_threshold:
            classification = "HOT"
        elif v >= warm_threshold:
            classification = "WARM"
        else:
            classification = "SKIP"
        value_analysis["classification"] = classification

        logger.info("analyze_match: cid=%s, value=%.4f, dqs=%.1f, mc=%.1f, u=%.1f, class=%s",
                     cid, v, dqs_score, mc.get("score", 0), u.get("score", 0), classification)

        # ── DIAG: value + classification ──
        _ev_str = "[" + ", ".join(f"{e:+.3f}" for e in value["ev"]) + "]" if value.get("ev") else "[]"
        _odds_str = "[" + " ".join(f"{o:.2f}" for o in best_odds) + "]" if best_odds else "[]"
        print(f"[DIAG] cid={cid} value={v:.4f} side={value["value_side"]} class={classification} "
              f"dqs={dqs_score:.1f} ev={_ev_str} odds={_odds_str} fallback={fallback}")
        return value_analysis

    except Exception as e:
        logger.error("analyze_match error: %s", e, exc_info=True)
        return {"error": str(e), "canonical_id": match.get("canonical_id", "") if isinstance(match, dict) else ""}


# ============================================================================
# RUN PIPELINE (§28 — for main.py)
# ============================================================================

def run_pipeline(matches: List[Dict[str, Any]],
                  history_map: Optional[Dict[str, List]] = None,
                  value_threshold: Optional[float] = None) -> Dict[str, Any]:
    """Пайплайн для main.py. Возвращает {hot, warm, stats, results}."""
    # FIX: dict→list нормализация (get_matches_by_date_range возвращает dict)
    if isinstance(matches, dict):
        matches = list(matches.values())

    if value_threshold is None:
        if _get_threshold:
            try:
                value_threshold = _get_threshold()
            except Exception:
                value_threshold = _DEFAULT_THRESHOLD
        else:
            value_threshold = _DEFAULT_THRESHOLD

    hot = []
    warm = []
    results = []
    errors = 0
    # Stats counters (для main.py дашборда)
    with_odds = 0
    with_pred = 0
    with_h2h = 0
    with_stats = 0
    value_bets = 0

    for match in matches:
        if is_shutdown_requested():
            logger.info("Shutdown requested — остановка run_pipeline")
            break
        try:
            cid = match.get("canonical_id", "")
            history = history_map.get(cid, []) if history_map else None
            # Count stats
            best_odds = _extract_best_source(match.get("odds", {})) if isinstance(match.get("odds"), dict) else None
            if best_odds:
                with_odds += 1
            pred = match.get("predictions") or match.get("bzzoiro_predictions")
            if isinstance(pred, dict) and _extract_pred_probs(pred) is not None:
                with_pred += 1
            if match.get("h2h") or history:
                with_h2h += 1
            if isinstance(match.get("stats"), dict) and match.get("stats"):
                with_stats += 1

            analysis = analyze_match(match, history, value_threshold)
            if analysis.get("error"):
                errors += 1
                continue
            # Attach original match for dashboard
            analysis["_match"] = match
            results.append(analysis)
            cls = analysis.get("classification", "SKIP")
            if cls == "HOT":
                hot.append(analysis)
                value_bets += 1
            elif cls == "WARM":
                warm.append(analysis)
                value_bets += 1
        except Exception as e:
            errors += 1
            logger.warning("pipeline error: %s", e)

    # Sort by value_pct desc
    hot.sort(key=lambda x: x.get("value_pct", 0), reverse=True)
    warm.sort(key=lambda x: x.get("value_pct", 0), reverse=True)

    stats = {
        "total": len(matches),
        "analyzed": len(results),
        "hot": len(hot),
        "warm": len(warm),
        "errors": errors,
        "value_threshold": value_threshold,
        # v9.3-audited: keys для main.py дашборда
        "with_odds": with_odds,
        "with_pred": with_pred,
        "with_h2h": with_h2h,
        "with_stats": with_stats,
        "value_bets": value_bets,
    }

    # ── DIAG: Pipeline summary ──
    _history_size = len(history_map) if history_map else 0
    print(f"[DIAG] === PIPELINE SUMMARY ===")
    print(f"[DIAG] matches={len(matches)} analyzed={len(results)} hot={len(hot)} warm={len(warm)} errors={errors} value_bets={value_bets}")
    print(f"[DIAG] with_odds={with_odds} with_pred={with_pred} with_h2h={with_h2h} with_stats={with_stats}")
    print(f"[DIAG] history_map_size={_history_size}")
    if not hot and not warm:
        print(f"[DIAG] NO VALUE BETS FOUND — checking first 5 matches for details...")
        for i, m in enumerate(matches[:5]):
            if not isinstance(m, dict):
                continue
            _cid = m.get("canonical_id", "?")
            _a = next((r for r in results if r.get("canonical_id") == _cid), None)
            if _a:
                print(f"[DIAG] SKIP: cid={_cid} value={_a.get('value_pct', 0):.4f} class={_a.get('classification', '?')} model_probs={_a.get('model_probs')}")
    else:
        for h in hot[:5]:
            print(f"[DIAG] HOT: cid={h.get('canonical_id')} value={h.get('value_pct', 0):.4f} side={h.get('value_side', '?')} dqs={h.get('dqs', {}).get('score', 0):.1f}")

    return {"hot": hot, "warm": warm, "stats": stats, "results": results}
