#!/usr/bin/env python3
"""
Gatekeeper-AI v700-prod — Main Pipeline (Dashboard + Telegram)
V7.0.0
"""
import os
import sys
import json
import html
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

from gatekeeper_hub import (
    run_initialization,
    get_matches_by_date_range,
    save_search_results,
    save_analysis,
    get_current_odds,
    get_odds_metadata,
    get_from_cache,
)
from search_module import SearchModule, _parse_date_msk, _get_competition_code

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
        print("[TELEGRAM] Нет токена или chat_id — пропуск")
        return False
    text = text.replace('%', '&#37;')
    parts = _split_message(text, TG_MAX_CHARS)
    print(f"[TELEGRAM] Отправка {len(parts)} сообщений...")
    for i, part in enumerate(parts, 1):
        if not _send_one(part):
            print(f"[TELEGRAM] Ошибка отправки части {i}/{len(parts)}")
            return False
        print(f"[TELEGRAM] Часть {i}/{len(parts)} отправлена")
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
            print(f"[TELEGRAM] API error: {data.get('description', '?')}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            try:
                retry_after = json.loads(body).get("parameters", {}).get("retry_after", 3)
            except Exception:
                retry_after = 3
            print(f"[TELEGRAM] 429: ожидание {retry_after}с")
            import time
            time.sleep(retry_after)
            return _send_one(text)
        print(f"[TELEGRAM] HTTP {e.code}: {body[:200]}")
        return False
    except Exception as e:
        print(f"[TELEGRAM] Ошибка: {e}")
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
# Ecosystem markers
# ---------------------------------------------------------------------------
def _ecosystem_line() -> str:
    markers = ["Ecosystem+", "Redis+"]
    for name, meta_key in [
        ("Bzzoiro", "bzzoiro:meta"),
        ("Sharp", "sharpapi:meta"),
        ("PropLine", "propline:meta"),
        ("OddsAPI", "odds_api:meta"),
    ]:
        meta = get_from_cache(meta_key)
        if isinstance(meta, dict) and meta.get("last_run"):
            markers.append(f"{name}+")
        else:
            markers.append(f"{name}-")
    if os.environ.get("SHARPAPI_FLUSH_OLD") == "1":
        markers.append("Flush🧹")
    else:
        markers.append("Flush+")
    return " | ".join(markers)


# ---------------------------------------------------------------------------
# Last module
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
        meta = get_from_cache(meta_key)
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
# Форматирование
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
        return " ✅"
    elif level == "CONSENSUS":
        return " 🔒"
    elif level == "SINGLE":
        return " ⚠️"
    return ""


def _fmt_bet(info: dict, is_hot: bool) -> tuple:
    arrow = "➔"
    badge = _verification_badge(info)

    if is_hot and info.get("is_fire"):
        header = f"{arrow} {{n}} | 🔥{badge} | {html.escape(info['comp_code'])} | {info['date']} | {info['time']}"
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
        lines.append(f"🎯 HOT BETS ({len(hot)})")
        lines.append("")
        for i, info in enumerate(hot, 1):
            header, teams, data = _fmt_bet(info, is_hot=True)
            lines.append(header.format(n=i))
            lines.append(teams)
            lines.append(data)
            lines.append("")
    else:
        lines.append("🎯 HOT BETS (0)")
        lines.append("Нет кандидатов в топе.")
        lines.append("")

    if warm:
        lines.append(f"⚠️ WARM BETS ({len(warm)})")
        lines.append("")
        for i, info in enumerate(warm, 1):
            header, teams, data = _fmt_bet(info, is_hot=False)
            lines.append(header.format(n=i))
            lines.append(teams)
            lines.append(data)
            lines.append("")
    else:
        lines.append("⚠️ WARM BETS (0)")
        lines.append("Нет кандидатов.")
        lines.append("")

    now_str = datetime.now(MSK_TZ).strftime("%H:%M")
    module = _last_module()
    status_parts = [
        "🌐 Redis+",
        f"📦{stats['total']}",
        f"📊Odds:{stats['with_odds']}",
        f"🔥Value:{stats['value_bets']}",
        f"Pred:{stats['with_pred']}",
        f"H2H:{stats['with_h2h']}",
        f"Stats:{stats['with_stats']}",
        f"🕒{now_str}",
        f"⭐{module}",
    ]
    lines.append(" | ".join(status_parts))
    lines.append(_ecosystem_line())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("[DASH] Gatekeeper-AI Pipeline v700-prod")
    print(f"[DASH] Время: {datetime.now(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')}")
    print("=" * 60)

    print("[DASH] Инициализация Redis...")
    init = run_initialization()
    if not init or not init.get("redis_available"):
        print("[DASH] ❌ Redis недоступен")
        dashboard = (
            "🌐 Redis- | Пайплайн работает в graceful degradation | "
            "Данные не обновлены\n"
            + _ecosystem_line()
        )
        _send_telegram(dashboard)
        return

    latency = init.get("init_latency_ms", 0)
    print(f"[DASH] Redis init OK, latency={latency}ms")

    matches = get_matches_by_date_range()
    print(f"[DASH] get_matches_by_date_range → {len(matches)} матчей")

    if not matches:
        print("[DASH] Нет матчей для анализа")
        now_str = datetime.now(MSK_TZ).strftime("%H:%M")
        module = _last_module()
        dashboard = (
            f"📦 Нет матчей для анализа\n"
            f"🌐 Redis+ | 📦0 | 📊Odds:0 | 🕒{now_str} | ⭐{module}\n"
            + _ecosystem_line()
        )
        _send_telegram(dashboard)
        return

    search = SearchModule(value_threshold=VALUE_THRESHOLD)
    result = search.process(matches)

    print(f"[DASH] HOT: {len(result['hot'])}, WARM: {len(result['warm'])}, "
          f"Value: {result['stats']['value_bets']}")
    print(f"[DASH] Odds: {result['stats']['with_odds']}, "
          f"Pred: {result['stats']['with_pred']}, "
          f"H2H: {result['stats']['with_h2h']}, "
          f"Stats: {result['stats']['with_stats']}")

    # Сохранение analysis для HOT матчей
    for info in result["hot"]:
        cid = info.get("canonical_id", "")
        if not cid:
            continue
        save_analysis(cid, {
            "is_fire": info.get("is_fire", False),
            "value_ev": info.get("value_ev", 0),
            "v_side": info.get("v_side", ""),
            "v_odds": info.get("v_odds", 0),
            "v_prob": info.get("v_prob", 0),
            "odds_verification": info.get("odds_verification", {}),
            "timestamp": datetime.now(MSK_TZ).isoformat(),
        })

    dashboard = _format_dashboard(result)
    print()
    print(dashboard)

    save_search_results({
        "hot": len(result["hot"]),
        "warm": len(result["warm"]),
        "value_bets": result["stats"]["value_bets"],
        "total_matches": result["stats"]["total"],
        "timestamp": datetime.now(MSK_TZ).isoformat(),
    })
    print("[DASH] Результаты сохранены в Redis")

    _send_telegram(dashboard)
    print("[DASH] Готово")


if __name__ == "__main__":
    main()
