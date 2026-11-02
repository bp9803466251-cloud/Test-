#!/usr/bin/env python3
"""
diagnostic_matches.py — Полный дамп матчей из Redis.
Использует gatekeeper_hub для доступа к Redis (как коллекторы).
v2: исправлено чтение odds.1x2, фильтр :meta/idem/canary, canonical_id, value readiness.
"""
import os
import sys
import json
import logging
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="[DIAG] %(message)s")
logger = logging.getLogger("diagnostic")

# ── gatekeeper_hub ─────────────────────────────────────────
try:
    from gatekeeper_hub import run_initialization, get_all_fields
except ImportError:
    logger.error("gatekeeper_hub не найден")
    sys.exit(1)

try:
    from team_registry import clean_team_name
except ImportError:
    clean_team_name = lambda n: n.lower().strip().replace(" ", "_") if n else ""


def _safe_json(value, default):
    """Парсит JSON-строку или возвращает как есть."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return default
    return value if isinstance(value, type(default)) else default


def _extract_odds_1x2(odds_raw):
    """Извлекает 1X2 из odds.1x2 (как в gatekeeper_hub.get_all_odds)."""
    if not isinstance(odds_raw, dict):
        return {}
    sec = odds_raw.get("1x2", {})
    if not isinstance(sec, dict):
        return {}
    # Приоритет: opening → open → current (как в get_all_odds)
    return sec.get("opening", sec.get("open", sec.get("current", {})))


def _extract_odds_sources(odds_raw, match_sources):
    """Извлекает sources из odds.1x2.sources, fallback на match.sources."""
    if not isinstance(odds_raw, dict):
        return match_sources if isinstance(match_sources, list) else []
    sec = odds_raw.get("1x2", {})
    if isinstance(sec, dict):
        srcs = sec.get("sources", [])
        if srcs:
            return srcs if isinstance(srcs, list) else [srcs]
    return match_sources if isinstance(match_sources, list) else []


def _is_real_match(key):
    """Фильтрует мусорные ключи (как в cleanup_expired / get_all_matches)."""
    if not key.startswith("match:"):
        return False
    if ":meta" in key:
        return False
    if "idem" in key or "canary" in key:
        return False
    if key == "match:index":
        return False
    return True


def main():
    print("=" * 80)
    print("📋 ПОЛНЫЙ ДАМП МАТЧЕЙ ИЗ REDIS")
    print("=" * 80)

    init = run_initialization(collector="diagnostic")
    if not init.get("redis_available", False):
        print("❌ Redis недоступен")
        sys.exit(1)

    all_fields = get_all_fields()
    if not all_fields:
        print("❌ Нет данных в Redis")
        sys.exit(0)

    # Фильтруем только реальные матчи (как в get_all_matches)
    match_keys = {k: v for k, v in all_fields.items() if _is_real_match(k)}

    if not match_keys:
        print("❌ Нет матчей в Redis")
        sys.exit(0)

    # Парсим матчи
    matches = []
    for key, value in match_keys.items():
        data = _safe_json(value, {})
        if isinstance(data, dict):
            matches.append((key, data))

    print(f"\nВсего матчей: {len(matches)}\n")

    # Категории
    merged = []            # 2+ источника на одном canonical_id
    single_with_pred = []  # прогноз есть, но 1 источник
    single_no_pred = []     # прогноза нет, 1 источник, но есть odds
    no_odds = []            # нет коэффициентов
    value_ready = []       # 2+ sources + odds + predictions → готов к value engine

    for key, data in matches:
        # Sources (match-level)
        sources = _safe_json(data.get("sources", []), [])
        if isinstance(sources, str):
            sources = [sources]

        # Odds (вложенность odds.1x2.opening/current — FIX)
        odds_raw = _safe_json(data.get("odds", {}), {})
        odds_1x2 = _extract_odds_1x2(odds_raw)
        has_odds = bool(odds_1x2)
        odds_sources = _extract_odds_sources(odds_raw, sources)

        # Predictions
        predictions = _safe_json(data.get("predictions", {}), {})
        has_pred = bool(predictions)

        # Поля матча (home_team/away_team — КОРРЕКТНО, хаб хранит их)
        home = data.get("home_team", data.get("home_clean", "?"))
        away = data.get("away_team", data.get("away_clean", "?"))
        date = data.get("date_utc", "?")
        league = data.get("competition", "?")
        status = data.get("status", "?")
        cid = data.get("canonical_id", key.replace("match:", ""))

        # Odds values
        oh = odds_1x2.get("home", "?") if isinstance(odds_1x2, dict) else "?"
        od = odds_1x2.get("draw", "?") if isinstance(odds_1x2, dict) else "?"
        oa = odds_1x2.get("away", "?") if isinstance(odds_1x2, dict) else "?"

        entry = {
            "key": key,
            "cid": cid,
            "home": home,
            "away": away,
            "date": date,
            "league": league,
            "status": status,
            "sources": sources,
            "odds_sources": odds_sources,
            "has_odds": has_odds,
            "has_pred": has_pred,
            "oh": oh, "od": od, "oa": oa,
        }

        # Категоризация
        total_sources = max(len(sources), len(odds_sources))
        if total_sources >= 2 and has_odds and has_pred:
            value_ready.append(entry)
        if total_sources >= 2:
            merged.append(entry)
        elif has_pred:
            single_with_pred.append(entry)
        elif has_odds:
            single_no_pred.append(entry)
        else:
            no_odds.append(entry)

    # ── Value Engine Ready ──
    print("=" * 80)
    print(f"🔥 VALUE ENGINE READY (2+ sources + odds + прогноз): {len(value_ready)}")
    print("=" * 80)
    for m in value_ready:
        all_src = sorted(set(m["sources"] + m["odds_sources"]))
        src_list = ", ".join(all_src) if all_src else "?"
        print(f"  ✅ {m['home']} vs {m['away']}")
        print(f"     CID: {m['cid']}")
        print(f"     Дата: {m['date']}, лига: {m['league']}, статус: {m['status']}")
        print(f"     1X2: {m['oh']} / {m['od']} / {m['oa']}")
        print(f"     Источники: {src_list} + прогноз")
        print()

    # ── Склеенные (2+ источника) ──
    print("=" * 80)
    print(f"✅ СКЛЕЕННЫЕ (2+ источника): {len(merged)}")
    print("=" * 80)
    for m in merged:
        all_src = sorted(set(m["sources"] + m["odds_sources"]))
        src_list = ", ".join(all_src) if all_src else "?"
        pred = " + прогноз" if m["has_pred"] else ""
        print(f"  {m['home']} vs {m['away']}")
        print(f"    CID: {m['cid']}")
        print(f"    Дата: {m['date']}, статус: {m['status']}, лига: {m['league']}")
        print(f"    1X2: {m['oh']} / {m['od']} / {m['oa']}")
        print(f"    Источники: {src_list}{pred}")
        print()

    # ── Одиночные с прогнозом ──
    print("=" * 80)
    print(f"⚠️ ОДИНОЧНЫЕ С ПРОГНОЗОМ (нужны odds от другого источника): {len(single_with_pred)}")
    print("=" * 80)
    for m in single_with_pred:
        src_list = ", ".join(m["sources"]) if m["sources"] else "?"
        print(f"  {m['home']} vs {m['away']}")
        print(f"    CID: {m['cid']}")
        print(f"    Дата: {m['date']}, лига: {m['league']}")
        print(f"    1X2: {m['oh']} / {m['od']} / {m['oa']}")
        print(f"    Источник: {src_list} + прогноз")
        print()

    # ── Одиночные без прогноза ──
    print("=" * 80)
    print(f"📋 ОДИНОЧНЫЕ БЕЗ ПРОГНОЗА (есть odds): {len(single_no_pred)}")
    print("=" * 80)
    for m in single_no_pred:
        src_list = ", ".join(m["sources"]) if m["sources"] else "?"
        print(f"  {m['home']} vs {m['away']} | {m['date']} | {src_list} | 1X2: {m['oh']}/{m['od']}/{m['oa']}")

    # ── Без odds ──
    print()
    print("=" * 80)
    print(f"❌ БЕЗ КОЭФФИЦИЕНТОВ: {len(no_odds)}")
    print("=" * 80)
    for m in no_odds:
        print(f"  {m['home']} vs {m['away']} | {m['date']} | sources: {m['sources']}")

    # ── Потенциальные дубликаты ──
    print()
    print("=" * 80)
    print("🔍 ПОТЕНЦИАЛЬНЫЕ ДУБЛИКАТЫ (разные canonical_id, похожие команды)")
    print("=" * 80)

    by_date = defaultdict(list)
    for key, data in matches:
        date_str = data.get("date_utc", "")
        date_day = date_str[:10] if date_str else "?"
        by_date[date_day].append((key, data))

    dupes_found = 0
    for date_day, day_matches in by_date.items():
        if len(day_matches) < 2:
            continue
        for i in range(len(day_matches)):
            for j in range(i + 1, len(day_matches)):
                k1, d1 = day_matches[i]
                k2, d2 = day_matches[j]
                h1 = d1.get("home_team", d1.get("home_clean", ""))
                a1 = d1.get("away_team", d1.get("away_clean", ""))
                h2 = d2.get("home_team", d2.get("home_clean", ""))
                a2 = d2.get("away_team", d2.get("away_clean", ""))
                h1c = clean_team_name(h1)
                a1c = clean_team_name(a1)
                h2c = clean_team_name(h2)
                a2c = clean_team_name(a2)
                if k1 == k2:
                    continue
                h_match = (h1c == h2c) or (h1c in h2c) or (h2c in h1c) or \
                          (h1c.split("_")[0] == h2c.split("_")[0] and h1c.split("_")[0])
                a_match = (a1c == a2c) or (a1c in a2c) or (a2c in a1c) or \
                          (a1c.split("_")[0] == a2c.split("_")[0] and a1c.split("_")[0])
                if h_match and a_match:
                    dupes_found += 1
                    cid1 = d1.get("canonical_id", k1)
                    cid2 = d2.get("canonical_id", k2)
                    print(f"  ⚠️ {h1} vs {a1}")
                    print(f"     CID: {cid1}")
                    print(f"     {h2} vs {a2}")
                    print(f"     CID: {cid2}")
                    print(f"     Clean: {h1c}__{a1c} vs {h2c}__{a2c}")
                    print()

    if dupes_found == 0:
        print("  ✅ Дубликатов не найдено")
    else:
        print(f"  Найдено {dupes_found} потенциальных дубликатов")

    # ── Сводка ──
    print()
    print("=" * 80)
    print("📋 СВОДКА")
    print("=" * 80)
    print(f"  Всего матчей:                  {len(matches)}")
    print(f"  🔥 Value Engine Ready:          {len(value_ready)}")
    print(f"  ✅ Склеенные (2+ источника):    {len(merged)}")
    print(f"  ⚠️ Одиночные с прогнозом:       {len(single_with_pred)}")
    print(f"  📋 Одиночные без прогноза:      {len(single_no_pred)}")
    print(f"  ❌ Без коэффициентов:           {len(no_odds)}")
    print(f"  🔍 Потенциальные дубликаты:     {dupes_found}")
    print()
    if len(value_ready) == 0:
        print("  ⛔ Нет матчей, готовых к value engine")
        print("     Нужно: 2+ источника + odds + прогноз (Bzzoiro)")
        sys.exit(1)
    else:
        print(f"  ✅ {len(value_ready)} матчей готовы к value engine — можно запускать пайплайн")


if __name__ == "__main__":
    main()
