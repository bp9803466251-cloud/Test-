#!/usr/bin/env python3
"""
diagnostic_matches.py — Полный дамп матчей из Redis.
Показывает все матчи, их источники, odds, прогнозы и склейку.

Запуск через GitHub Actions или локально с env:
  UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
"""
import os
import sys
import json
import urllib.request

# ── Redis REST API ─────────────────────────────────────────
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN", "")

if not REDIS_URL:
    print("❌ UPSTASH_REDIS_REST_URL не задан")
    sys.exit(1)

def redis_cmd(command, *args):
    """Выполняет команду через Upstash Redis REST API."""
    url = f"{REDIS_URL}/{command}"
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}
    body = json.dumps(list(args))
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("result")

def scan_keys(pattern, count=500):
    """Сканирует Redis по паттерну, возвращает список ключей."""
    keys = []
    cursor = "0"
    while True:
        result = redis_cmd("SCAN", cursor, "MATCH", pattern, "COUNT", str(count))
        if not result:
            break
        cursor, batch = result
        keys.extend(batch)
        if cursor == "0":
            break
    return keys

def get_match_data(key):
    """Получает JSON-данные матча по ключу."""
    raw = redis_cmd("GET", key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

def main():
    print("=" * 80)
    print("📋 ПОЛНЫЙ ДАМП МАТЧЕЙ ИЗ REDIS")
    print("=" * 80)

    # Сканируем все match:* ключи (исключаем meta и index)
    all_keys = scan_keys("match:*")
    match_keys = [k for k in all_keys if not k.startswith("match:index:") 
                  and not k.startswith("match:meta:")
                  and k != "match:index"
                  and k != "match:meta"]

    print(f"\nВсего ключей match:*: {len(all_keys)}")
    print(f"Из них матчей: {len(match_keys)}")

    # Загружаем данные
    matches = []
    for key in sorted(match_keys):
        data = get_match_data(key)
        if data:
            data["_key"] = key
            matches.append(data)

    print(f"Загружено: {len(matches)} матчей\n")

    # Категории
    glued = []        # 2+ источника
    single_pred = []   # 1 источник, но есть прогноз
    single_odds = []   # 1 источник, есть odds, нет прогноза
    no_odds = []       # нет odds

    for m in matches:
        sources = m.get("sources", [])
        odds = m.get("odds", {})
        odds_1x2 = odds.get("1x2", {}) if isinstance(odds, dict) else {}
        has_odds = bool(odds_1x2 and any(v for v in [odds_1x2.get("home"), odds_1x2.get("draw"), odds_1x2.get("away")]))
        pred = m.get("predictions", {})
        has_pred = bool(pred and (pred.get("home_win_prob") or pred.get("source")))

        if len(sources) >= 2:
            glued.append(m)
        elif has_pred and not has_odds:
            no_odds.append(m)
        elif has_pred:
            single_pred.append(m)
        elif has_odds:
            single_odds.append(m)
        else:
            no_odds.append(m)

    # ── Вывод: склеенные матчи ─────────────────────────────
    print("=" * 80)
    print(f"✅ СКЛЕЕННЫЕ МАТЧИ ({len(glued)}) — 2+ источника на одном canonical_id")
    print("=" * 80)
    print(f"{'#':>3}  {'canonical_id':<50} {'Источники':<30} {'Odds (1X2)':<25} {'Pred':<5}")
    print("-" * 120)
    for i, m in enumerate(glued, 1):
        cid = m.get("canonical_id", m.get("_key", "").replace("match:", ""))
        sources = ", ".join(m.get("sources", []))
        odds = m.get("odds", {})
        odds_1x2 = odds.get("1x2", {}) if isinstance(odds, dict) else {}
        h = odds_1x2.get("home", "?")
        d = odds_1x2.get("draw", "?")
        a = odds_1x2.get("away", "?")
        odds_str = f"{h} / {d} / {a}" if any(v and v != "?" for v in [h, d, a]) else "—"
        pred = m.get("predictions", {})
        pred_flag = "✅" if pred and pred.get("home_win_prob") else "—"
        print(f"{i:>3}  {cid:<50} {sources:<30} {odds_str:<25} {pred_flag:<5}")

    # ── Вывод: single-source с прогнозом ──────────────────
    print("\n" + "=" * 80)
    print(f"⚠️  ОДИНОЧНЫЕ С ПРОГНОЗОМ ({len(single_pred)}) — прогноз есть, но odds только от 1 источника")
    print("=" * 80)
    print(f"{'#':>3}  {'canonical_id':<50} {'Источник':<15} {'Odds (1X2)':<25} {'Pred':<5}")
    print("-" * 100)
    for i, m in enumerate(single_pred, 1):
        cid = m.get("canonical_id", m.get("_key", "").replace("match:", ""))
        sources = ", ".join(m.get("sources", []))
        odds = m.get("odds", {})
        odds_1x2 = odds.get("1x2", {}) if isinstance(odds, dict) else {}
        h = odds_1x2.get("home", "?")
        d = odds_1x2.get("draw", "?")
        a = odds_1x2.get("away", "?")
        odds_str = f"{h} / {d} / {a}" if any(v and v != "?" for v in [h, d, a]) else "—"
        print(f"{i:>3}  {cid:<50} {sources:<15} {odds_str:<25} ✅")

    # ── Вывод: single-source без прогноза ─────────────────
    print("\n" + "=" * 80)
    print(f"📋 ОДИНОЧНЫЕ БЕЗ ПРОГНОЗА ({len(single_odds)}) — odds есть, прогноза нет")
    print("=" * 80)
    print(f"{'#':>3}  {'canonical_id':<50} {'Источник':<15} {'Odds (1X2)':<25}")
    print("-" * 95)
    for i, m in enumerate(single_odds, 1):
        cid = m.get("canonical_id", m.get("_key", "").replace("match:", ""))
        sources = ", ".join(m.get("sources", []))
        odds = m.get("odds", {})
        odds_1x2 = odds.get("1x2", {}) if isinstance(odds, dict) else {}
        h = odds_1x2.get("home", "?")
        d = odds_1x2.get("draw", "?")
        a = odds_1x2.get("away", "?")
        odds_str = f"{h} / {d} / {a}" if any(v and v != "?" for v in [h, d, a]) else "—"
        print(f"{i:>3}  {cid:<50} {sources:<15} {odds_str:<25}")

    # ── Вывод: без odds ────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"❌ БЕЗ ODDS ({len(no_odds)}) — нет коэффициентов")
    print("=" * 80)
    for i, m in enumerate(no_odds, 1):
        cid = m.get("canonical_id", m.get("_key", "").replace("match:", ""))
        sources = ", ".join(m.get("sources", []))
        print(f"{i:>3}  {cid:<50} {sources:<15}")

    # ── Сводка для value engine ────────────────────────────
    print("\n" + "=" * 80)
    print("📊 СВОДКА ДЛЯ VALUE ENGINE")
    print("=" * 80)
    print(f"  Всего матчей:         {len(matches)}")
    print(f"  Склеенных (2+ src):   {len(glued)}")
    print(f"  С прогнозом:          {len(glued) + len(single_pred) - len([m for m in glued if not (m.get('predictions', {}).get('home_win_prob'))])}")
    print(f"  Склеено + прогноз:     {len([m for m in glued if m.get('predictions', {}).get('home_win_prob')])}")
    print(f"  Value candidates:     {len([m for m in glued if m.get('predictions', {}).get('home_win_prob')])}")
    print(f"  Без odds:             {len(no_odds)}")
    print()

    # ── Поиск потенциальных дубликатов ────────────────────
    # Группируем по дате + похожим именам команд
    print("=" * 80)
    print("🔍 ПОТЕНЦИАЛЬНЫЕ ДУБЛИКАТЫ (разные canonical_id, похожие матчи)")
    print("=" * 80)
    # Группируем по дате
    from collections import defaultdict
    by_date = defaultdict(list)
    for m in matches:
        date = m.get("date", m.get("commence_time", ""))
        if date:
            date_short = str(date)[:10]
            by_date[date_short].append(m)

    found_dupes = False
    for date, group in sorted(by_date.items()):
        if len(group) < 2:
            continue
        # Сравниваем попарно
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                m1, m2 = group[i], group[j]
                cid1 = m1.get("canonical_id", "")
                cid2 = m2.get("canonical_id", "")
                if cid1 == cid2:
                    continue
                # Проверяем похожесть команд
                teams1 = cid1.split("__")
                teams2 = cid2.split("__")
                if len(teams1) >= 2 and len(teams2) >= 2:
                    t1a, t1b = teams1[0], teams1[1]
                    t2a, t2b = teams2[0], teams2[1]
                    # Проверяем в обоих порядках
                    match_a = (t1a in t2a or t2a in t1a) or (t1a in t2b or t2b in t1a)
                    match_b = (t1b in t2a or t2a in t1b) or (t1b in t2b or t2b in t1b)
                    if match_a and match_b:
                        s1 = ", ".join(m1.get("sources", []))
                        s2 = ", ".join(m2.get("sources", []))
                        print(f"  {cid1} [{s1}]")
                        print(f"  {cid2} [{s2}]")
                        print()
                        found_dupes = True

    if not found_dupes:
        print("  ✅ Потенциальных дубликатов не обнаружено")

    print("\n" + "=" * 80)
    print("Дамп завершён.")
    print("=" * 80)


if __name__ == "__main__":
    main()
