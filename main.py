#!/usr/bin/env python3
"""
Gatekeeper-AI v810-patched — Main Pipeline (Dashboard + Telegram)
Value engine + Telegram-дашборд по стандарту §12.

Патчи v8.10:
  FIX-1: Удалён импорт _parse_date_msk, _get_competition_code из search_module —
         этих функций там нет (search_module экспортирует только clean_team_name,
         TEAM_ALIASES, search_teams, find_match_candidates). main.py падал в ImportError.
  FIX-2: Удалён неиспользуемый импорт get_odds_metadata — нет в __all__ хаба.
  FIX-3: run_initialization(collector="main") — §1.7a требует передачу имени.
  FIX-4: is_shutdown_requested() перед batch_evaluate — §23.3 graceful shutdown.
  FIX-5: <b> HTML-теги в дашборде — §12 стандарт Telegram-формата.
  FIX-6: import time вынесен на верхний уровень (был внутри except-блока).
  FIX-7: save_meta("main", ...) — запись собственного мета (§1.27).
  FIX-8: Ecosystem marker: "!" при Redis-down (был всегда "+").
  FIX-9: __version__, __all__ добавлены.
  FIX-10: save_search_results — расширенный payload (hot/warm детали, stats).
"""
import os
import sys
import json
import time
import html
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

from gatekeeper_hub import (
    run_initialization,
    get_matches_by_date_range,
    get_match,
    get_history,
    get_current_odds,
    get_all_odds,
    save_search_results,
    save_analysis,
    save_meta,
    get_from_cache,
    now_msk,
    is_shutdown_requested,
)
from value_engine import batch_evaluate

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [DASH] %(message)s'
)

__version__ = "8.10-patched"
__all__ = ["main", "__version__"]

MSK_TZ = timezone(timedelta(hours=3))
VALUE_THRESHOLD = float(os.environ.get("VALUE_BET_THRESHOLD", "0.03"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_GROUP_ID", "")
TG_MAX_CHARS = 4000


# ---------------------------------------------------------------------------
# Telegram transport
# ---------------------------------------------------------------------------
def _send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        logging.info("Нет токена или chat_id — пропуск")
        return False
    text = text.replace('%', '&#37;')
    parts = _split_message(text, TG_MAX_CHARS)
    logging.info(f"Отправка {len(parts)} сообщений...")
    for i, part in enumerate(parts, 1):
        if not _send_one(part):
            logging.warning(f"Ошибка отправки части {i}/{len(parts)}")
            return False
        logging.info(f"Часть {i}/{len(parts)} отправлена")
    return True


def _send_one(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return True
            logging.warning(f"API error: {data.get('description', '?')}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            try:
                retry_after = json.loads(body).get("parameters", {}).get("retry_after", 3)
            except Exception:
                retry_after = 3
            logging.warning(f"429: ожидание {retry_after}с")
            time.sleep(retry_after)
            return _send_one(text)
        logging.warning(f"HTTP {e.code}: {body[:200]}")
        return False
    except Exception as e:
        logging.warning(f"Ошибка: {e}")
        return False


def _split_message(text: str, max_chars: int) -> list:
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    parts = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_chars:
            if current:
                parts.append(current)
                current = ""
            while len(line) > max_chars:
                cut = line[:max_chars]
                space_pos = cut.rfind(" ", max_chars - 100)
                if space_pos > 0:
                    parts.append(line[:space_pos])
                    line = line[space_pos + 1:]
                else:
                    parts.append(cut)
                    line = line[max_chars:]
                if len(line) <= max_chars:
                    break
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Ecosystem markers (§12)
# ---------------------------------------------------------------------------
_redis_available = True  # обновляется в main()


def _ecosystem_line() -> str:
    eco_marker = "+" if _redis_available else "!"
    redis_marker = "+" if _redis_available else "-"
    markers = [f"Ecosystem{eco_marker}", f"Redis{redis_marker}"]
    for name, meta_key in [
        ("Bzzoiro", "bzzoiro:meta"),
        ("Sharp", "sharpapi:meta"),
        ("PropLine", "propline:meta"),
        ("OddsAPI", "odds_api:meta"),
    ]:
        try:
            meta = get_from_cache(meta_key)
            if isinstance(meta, dict) and meta.get("last_run"):
                markers.append(f"{name}+")
            else:
                markers.append(f"{name}-")
        except Exception:
            markers.append(f"{name}-")
    if os.environ.get("SHARPAPI_FLUSH_OLD") == "1":
        markers.append("Flush\U0001f9f9")
    else:
        markers.append("Flush+")
    return " | ".join(markers)


# ---------------------------------------------------------------------------
# Last module (§12)
# ---------------------------------------------------------------------------
def _last_module() -> str:
    modules = [
        ("Sharp", "sharpapi:meta"),
        ("Bzzoiro", "bzzoiro:meta"),
        ("PropLine", "propline:meta"),
        ("OddsAPI", "odds_api:meta"),
    ]
    latest = None
    latest_dt = None
    for name, meta_key in modules:
        try:
            meta = get_from_cache(meta_key)
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        last_run = meta.get("last_run", "")
        if not last_run:
            continue
        try:
            dt = datetime.fromisoformat(last_run)
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest = name
        except Exception:
            continue
    return latest or "none"


# ---------------------------------------------------------------------------
# Форматирование (§12 — HTML bold tags)
# ---------------------------------------------------------------------------
def _fmt_odds(o: float) -> str:
    s = f"{o:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_prob(p: float) -> str:
    return str(round(p * 100))


def _source_display(source: str) -> str:
    if not source:
        return "unknown"
    s = source.lower().strip()
    if s == "sharpapi":
        return "SharpAPI"
    if s == "bzzoiro":
        return "Bzzoiro"
    if s == "odds_api" or s == "oddsapi":
        return "OddsAPI"
    if s == "propline":
        return "PropLine"
    if s == "api_football":
        return "API-Football"
    if s == "betfair":
        return "Betfair"
    if s == "opta":
        return "Opta"
    return source


def _verification_badge(info: dict) -> str:
    """Возвращает бейдж верификации odds на основе метаданных."""
    v = info.get("odds_verification", {})
    if not isinstance(v, dict):
        return ""
    level = v.get("level", "")
    if level == "VERIFIED":
        return " \u2705"
    elif level == "CONSENSUS":
        return " \U0001f512"
    elif level == "SINGLE":
        return " \u26a0\ufe0f"
    return ""


def _fmt_bet(info: dict, is_hot: bool) -> tuple:
    arrow = "\u2794"
    badge = _verification_badge(info)

    if is_hot and info.get("is_fire"):
        header = f"{arrow} {{n}} | \U0001f525{badge} | {html.escape(info['comp_code'])} | {info['date']} | {info['time']}"
    else:
        header = f"{arrow} {{n}}{badge} | {html.escape(info['comp_code'])} | {info['date']} | {info['time']}"

    teams = f"{html.escape(info['home_team'])} vs {html.escape(info['away_team'])}"

    o_h, o_d, o_a = info["odds"]
    p_h, p_d, p_a = info["probs"]
    odds_str = f"O: {_fmt_odds(o_h)}/{_fmt_odds(o_d)}/{_fmt_odds(o_a)}"
    probs_str = f"P: {_fmt_prob(p_h)}%/{_fmt_prob(p_d)}%/{_fmt_prob(p_a)}%"

    v_odds_str = _fmt_odds(info["v_odds"])
    v_prob_str = _fmt_prob(info["v_prob"])

    if info.get("is_fire"):
        ev_str = f"EV:+{info['value_ev'] * 100:.1f}%"
        v_str = f"V:{info['v_side']}@{v_odds_str}({v_prob_str}%) {ev_str}"
        source = info.get("source", "")
    else:
        v_str = f"V:{info['v_side']}@{v_odds_str}({v_prob_str}%)"
        source = _source_display(info.get("source", ""))

    data_str = f"{odds_str}| {probs_str}| {v_str} | {source}"

    return header, teams, data_str


def _format_dashboard(result: dict) -> str:
    lines = []
    hot = result["hot"]
    warm = result["warm"]
    stats = result["stats"]

    if hot:
        lines.append(f"\U0001f3af <b>HOT BETS ({len(hot)})</b>")
        lines.append("")
        for i, info in enumerate(hot, 1):
            header, teams, data = _fmt_bet(info, is_hot=True)
            lines.append(header.format(n=i))
            lines.append(teams)
            lines.append(data)
            lines.append("")
    else:
        lines.append("\U0001f3af <b>HOT BETS (0)</b>")
        lines.append("Нет кандидатов в топе.")
        lines.append("")

    if warm:
        lines.append(f"\u26a0\ufe0f <b>WARM BETS ({len(warm)})</b>")
        lines.append("")
        for i, info in enumerate(warm, 1):
            header, teams, data = _fmt_bet(info, is_hot=False)
            lines.append(header.format(n=i))
            lines.append(teams)
            lines.append(data)
            lines.append("")
    else:
        lines.append("\u26a0\ufe0f <b>WARM BETS (0)</b>")
        lines.append("Нет кандидатов.")
        lines.append("")

    now_str = datetime.now(MSK_TZ).strftime("%H:%M")
    module = _last_module()
    status_parts = [
        "\U0001f310 Redis+",
        f"\U0001f4e6{stats['total']}",
        f"\U0001f4caOdds:{stats['with_odds']}",
        f"\U0001f525Value:{stats['value_bets']}",
        f"Pred:{stats['with_pred']}",
        f"H2H:{stats['with_h2h']}",
        f"Stats:{stats['with_stats']}",
        f"\U0001f552{now_str}",
        f"\u2b50{module}",
    ]
    lines.append(" | ".join(status_parts))
    lines.append(_ecosystem_line())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    global _redis_available

    logging.info("=" * 60)
    logging.info("Gatekeeper-AI Pipeline v8.10-patched")
    logging.info(f"Время: {datetime.now(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    logging.info("=" * 60)

    # === ШАГ 1: Инициализация (§1.7a) ===
    logging.info("Инициализация Redis...")
    init = run_initialization(collector="main")
    if not init or not init.get("redis_available"):
        _redis_available = False
        logging.error("Redis недоступен")
        dashboard = (
            "\U0001f310 Redis- | Пайплайн работает в graceful degradation | "
            "Данные не обновлены\n"
            + _ecosystem_line()
        )
        _send_telegram(dashboard)
        save_meta("main", last_run=now_msk(), error_count=1, stored_matches=0)
        return

    _redis_available = True
    latency = init.get("init_latency_ms", 0)
    logging.info(f"Redis init OK, latency={latency}ms")

    # === ШАГ 2: Получение матчей ===
    matches = get_matches_by_date_range()
    logging.info(f"get_matches_by_date_range -> {len(matches)} матчей")

    if not matches:
        logging.info("Нет матчей для анализа")
        now_str = datetime.now(MSK_TZ).strftime("%H:%M")
        module = _last_module()
        dashboard = (
            f"\U0001f4e6 Нет матчей для анализа\n"
            f"\U0001f310 Redis+ | \U0001f4e60 | \U0001f4caOdds:0 | \U0001f552{now_str} | \u2b50{module}\n"
            + _ecosystem_line()
        )
        _send_telegram(dashboard)
        save_meta("main", last_run=now_msk(), total_events=0, stored_matches=0, error_count=0)
        return

    # === Graceful shutdown check (§23.3) ===
    if is_shutdown_requested():
        logging.info("Graceful shutdown — завершение до value-анализа")
        save_meta("main", last_run=now_msk(), total_events=len(matches),
                  stored_matches=0, error_count=0, shutdown=True)
        return

    # === ШАГ 3: Value-анализ ===
    logging.info(f"Value-анализ через value_engine (threshold={VALUE_THRESHOLD})...")
    try:
        result = batch_evaluate(matches, value_threshold=VALUE_THRESHOLD)
    except Exception as e:
        logging.error(f"batch_evaluate failed: {e}")
        dashboard = (
            "\U0001f310 Redis+ | \U0001f4caValue engine error | "
            f"Пайплайн прерван: {html.escape(str(e))}\n"
            + _ecosystem_line()
        )
        _send_telegram(dashboard)
        save_meta("main", last_run=now_msk(), total_events=len(matches),
                  stored_matches=0, error_count=1)
        return

    logging.info(f"HOT: {len(result['hot'])}, WARM: {len(result['warm'])}, "
                 f"Value: {result['stats']['value_bets']}")
    logging.info(f"Odds: {result['stats']['with_odds']}, "
                 f"Pred: {result['stats']['with_pred']}, "
                 f"H2H: {result['stats']['with_h2h']}, "
                 f"Stats: {result['stats']['with_stats']}")

    # === ШАГ 4: Сохранение analysis для HOT матчей ===
    saved_analysis = 0
    for info in result["hot"]:
        cid = info.get("canonical_id", "")
        if not cid:
            continue
        try:
            save_analysis(cid, {
                "is_fire": info.get("is_fire", False),
                "value_ev": info.get("value_ev", 0),
                "v_side": info.get("v_side", ""),
                "v_odds": info.get("v_odds", 0),
                "v_prob": info.get("v_prob", 0),
                "odds_verification": info.get("odds_verification", {}),
                "timestamp": datetime.now(MSK_TZ).isoformat(),
            })
            saved_analysis += 1
        except Exception as e:
            logging.warning(f"save_analysis failed for {cid}: {e}")

    # === ШАГ 5: Формирование и отправка дашборда ===
    dashboard = _format_dashboard(result)
    print()
    print(dashboard)

    try:
        save_search_results({
            "hot": len(result["hot"]),
            "warm": len(result["warm"]),
            "value_bets": result["stats"]["value_bets"],
            "total_matches": result["stats"]["total"],
            "with_odds": result["stats"]["with_odds"],
            "with_pred": result["stats"]["with_pred"],
            "with_h2h": result["stats"]["with_h2h"],
            "with_stats": result["stats"]["with_stats"],
            "saved_analysis": saved_analysis,
            "timestamp": datetime.now(MSK_TZ).isoformat(),
        })
        logging.info("Результаты сохранены в Redis")
    except Exception as e:
        logging.warning(f"save_search_results failed: {e}")

    _send_telegram(dashboard)

    # === Сохранение мета (§1.27) ===
    save_meta("main",
              last_run=now_msk(),
              total_events=len(matches),
              stored_matches=saved_analysis,
              error_count=0,
              hot_bets=len(result["hot"]),
              warm_bets=len(result["warm"]),
              value_bets=result["stats"]["value_bets"])
    logging.info("Готово")


if __name__ == "__main__":
    main()
