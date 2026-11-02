# -*- coding: utf-8 -*-
"""
Диагностический скрипт check_redis.py для проверки Propline, включая spreads и totals (до 15 матчей).
"""
import os
import json
import urllib.request

url = os.environ.get('SHARED_UPSTASH_REDIS_REST_URL') or os.environ.get('UPSTASH_REDIS_REST_URL')
token = os.environ.get('SHARED_UPSTASH_REDIS_REST_TOKEN') or os.environ.get('UPSTASH_REDIS_REST_TOKEN')

def call(cmd):
    req = urllib.request.Request(
        url,
        data=json.dumps(cmd).encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get('result')

print("=== ДИАГНОСТИКА ДАННЫХ PROPLINE В REDIS (ODDS, SPREADS, TOTALS) ===")

fields = call(['HKEYS', 'GatekeeperAI'])
found = 0
limit_output = 15

if fields:
    for key in fields:
        if "history" in key.lower() or ":meta" in key:
            continue
            
        raw_val = call(['HGET', 'GatekeeperAI', key])
        if not raw_val:
            continue
            
        try:
            wrapper = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
            if not isinstance(wrapper, dict):
                continue
                
            payload = wrapper.get("payload", {})
            if not isinstance(payload, dict):
                continue
                
            source = payload.get("source", "")
            sources = payload.get("sources", [])
            source_ids = payload.get("source_ids", {})
            
            # Проверяем принадлежность к Propline
            is_propline = (
                source == "propline" or 
                "propline" in source_ids or 
                "propline" in str(sources).lower() or
                any("propline" in str(v).lower() for v in source_ids.values())
            )
            
            if is_propline:
                found += 1
                print(f"\n{'='*60}")
                print(f"[PROPLINE RECORD #{found}] Ключ Redis: {key}")
                print(f"{'='*60}")
                
                # Выводим базовые данные и наличие секций
                print(f"🔹 Соперники: {payload.get('home_team')} vs {payload.get('away_team')}")
                print(f"🔹 Лига: {payload.get('competition')}")
                
                # Проверяем наличие котировок
                has_odds = "odds" in payload
                has_spreads = "spreads" in payload
                has_totals = "totals" in payload
                print(f"📊 Секции рынков: odds={has_odds}, spreads={has_spreads}, totals={has_totals}")
                
                # Полный JSON объекта для детального изучения
                print("\nПолный объект v710:")
                print(json.dumps(wrapper, indent=2, ensure_ascii=False))
                
                if found >= limit_output:
                    print(f"\n... (вывод ограничен первыми {limit_output} записями)")
                    break
                    
        except Exception as e:
            continue

print(f"\nВсего найдено и проверено записей Propline: {found}")
          
