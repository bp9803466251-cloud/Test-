#!/usr/bin/env python3
"""
diagnostic_matches.py — Полный дамп матчей из Redis.
Использует gatekeeper_hub для доступа к Redis (как коллекторы).
"""
import os
import sys
import json
import logging

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

    # Фильтруем только match:* ключи
    match_keys = {k: v for k, v in all_fields.items() if k.startswith("match:") and k != "match:index"}

    if not match_keys:
        print("❌ Нет матчей в Redis")
        sys.exit(0)

    # Парсим матчи
    matches = []
    for key, value in match_keys.items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            matches.append((key, value))

    print(f"\nВсего матчей: {len(matches)}\n")

    # Категории
    merged = []        # 2+ источника на одном canonical_id
    single_with_pred = []  # прогноз есть, но 1 источник
    single_no_pred = []    # прогноза нет, 1 источник
    no_odds = []           # нет коэффициентов

    for key, data in matches:
        sources = data.get("sources", [])
        if isinstance(sources, str):
            try:
                sources = json.loads(sources)
            except:
                sources = [sources]

        odds = data.get("odds", {})
        if isinstance(odds, str):
            try:
                odds = json.loads(odds)
            except:
                odds = {}

        has_odds = bool(odds.get("current"))
        predictions = data.get("predictions", {})
        if isinstance(predictions, str):
            try:
                predictions = json.loads(predictions)
            except:
                predictions = {}
        has_pred = bool(predictions)

        home = data.get("home_team", "?")
        away = data.get("away_team", "?")
        date = data.get("date_utc", "?")
        league = data.get("competition", "?")
        status = data.get("status", "?")

        # Extract odds values
        odds_cur = odds.get("current", {}) if isinstance(odds, dict) else {}
        oh = odds_cur.get("home", "?")
        od = odds_cur.get("draw", "?")
        oa = odds_cur.get("away", "?")

        entry = {
            "key": key,
            "home": home,
            "away": away,
            "date": date,
            "league": league,
            "status": status,
            "sources": sources,
            "has_odds": has_odds,
            "has_pred": has_pred,
            "oh": oh, "od": od, "oa": oa,
        }

        if len(sources) >= 2:
            merged.append(entry)
        elif has_pred:
            single_with_pred.append(entry)
        elif has_odds:
            single_no_pred.append(entry)
        else:
            no_odds.append(entry)

    # ── Склеенные (2+ источника) ──
    print("=" * 80)
    print(f"✅ СКЛЕЕННЫЕ (2+ источника): {len(merged)}")
    print("=" * 80)
    for m in merged:
        src_list = ", ".join(m["sources"]) if m["sources"] else "?"
        pred = " + прогноз" if m["has_pred"] else ""
        print(f"  {m['home']} vs {m['away']}")
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
        print(f"    Дата: {m['date']}, лига: {m['league']}")
        print(f"    1X2: {m['oh']} / {m['od']} / {m['oa']}")
        print(f"    Источник: {src_list} + прогноз")
        print()

    # ── Одиночные без прогноза ──
    print("=" * 80)
    print(f"📋 ОДИНОЧНЫЕ БЕЗ ПРОГНОЗА: {len(single_no_pred)}")
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

    # Группируем по дате
    from collections import defaultdict
    by_date = defaultdict(list)
    for key, data in matches:
        date_str = data.get("date_utc", "")
        date_day = date_str[:10] if date_str else "?"
        by_date[date_day].append((key, data))

    dupes_found = 0
    for date_day, day_matches in by_date.items():
        if len(day_matches) < 2:
            continue
        # Сравниваем попарно
        for i in range(len(day_matches)):
            for j in range(i + 1, len(day_matches)):
                k1, d1 = day_matches[i]
                k2, d2 = day_matches[j]
                h1 = d1.get("home_team", "")
                a1 = d1.get("away_team", "")
                h2 = d2.get("home_team", "")
                a2 = d2.get("away_team", "")
                # Проверяем похожесть команд
                h1c = clean_team_name(h1)
                a1c = clean_team_name(a1)
                h2c = clean_team_name(h2)
                a2c = clean_team_name(a2)
                if k1 == k2:
                    continue
                # Проверяем, есть ли частичное совпадение
                h_match = (h1c == h2c) or (h1c in h2c) or (h2c in h1c) or                           (h1c.split("_")[0] == h2c.split("_")[0] and h1c.split("_")[0])
                a_match = (a1c == a2c) or (a1c in a2c) or (a2c in a1c) or                           (a1c.split("_")[0] == a2c.split("_")[0] and a1c.split("_")[0])
                if h_match and a_match:
                    dupes_found += 1
                    print(f"  ⚠️ {h1} vs {a1} ({k1})")
                    print(f"     {h2} vs {a2} ({k2})")
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
    print(f"  Всего матчей: {len(matches)}")
    print(f"  Склеенные (2+ источника): {len(merged)}")
    print(f"  Одиночные с прогнозом: {len(single_with_pred)}")
    print(f"  Одиночные без прогноза: {len(single_no_pred)}")
    print(f"  Без коэффициентов: {len(no_odds)}")
    print(f"  Потенциальные дубликаты: {dupes_found}")


if __name__ == "__main__":
    main()
