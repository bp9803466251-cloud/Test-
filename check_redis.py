# -*- coding: utf-8 -*-
"""
Диагностический скрипт check_redis.py для проверки Propline, включая spreads и totals (до 15 матчей).
Версия 9.6.8-patched.

[9.6.8 FIX] Лестница §1.10 дополнена средним звеном SHARED_REDIS_REST_*.
[9.6.8 FIX] Явная проверка конфигурации Redis: без URL/токена - понятное сообщение
            и осознанный exit 1 вместо TypeError в глубине urllib.
[9.6.8 FIX] Хардкод 'GatekeeperAI' заменен на HASH_NAME из redis_hub (с fallback на
            прежнее значение, если hub недоступен в контуре диагностики).
[9.6.8 FIX] Контракт v710: записи могут храниться и оберткой с payload, и плоским
            объектом (поля в корне). Прежняя версия на плоском хранении находила
            0 записей Propline - молчаливый ложный диагноз "чистой базы".
[9.6.8 FIX] urlopen с таймаутом 10с; молчаливые except: continue заменены на подсчет
            нечитаемых записей с предупреждениями (стандарт 9.6.8).
"""
import os
import sys
import json
import logging
import urllib.request

logger = logging.getLogger("check_redis")
logging.basicConfig(level=logging.INFO, format="[CHECK REDIS] %(message)s")

# Лестница приоритетов §1.10 (синхрон с redis_config.py)
url = (
    os.environ.get('SHARED_UPSTASH_REDIS_REST_URL')
    or os.environ.get('SHARED_REDIS_REST_URL')
    or os.environ.get('UPSTASH_REDIS_REST_URL')
)
token = (
    os.environ.get('SHARED_UPSTASH_REDIS_REST_TOKEN')
    or os.environ.get('SHARED_REDIS_REST_TOKEN')
    or os.environ.get('UPSTASH_REDIS_REST_TOKEN')
)

if not url or not token:
    print("❌ Redis не сконфигурирован: задайте SHARED_UPSTASH_REDIS_REST_URL/TOKEN "
          "(или альтернативные звенья лестницы §1.10)")
    sys.exit(1)

try:
    from redis_hub import HASH_NAME
except ImportError:
    HASH_NAME = "GatekeeperAI"  # Fallback, если hub недоступен в контуре диагностики


def call(cmd, timeout=10):
    req = urllib.request.Request(
        url,
        data=json.dumps(cmd).encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get('result')


print("=== ДИАГНОСТИКА ДАННЫХ PROPLINE В REDIS (ODDS, SPREADS, TOTALS) ===")

fields = call(['HKEYS', HASH_NAME])
found = 0
parse_errors = 0
limit_output = 15

if fields:
    for key in fields:
        if "history" in key.lower() or ":meta" in key:
            continue

        raw_val = call(['HGET', HASH_NAME, key])
        if not raw_val:
            continue

        try:
            wrapper = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
            if not isinstance(wrapper, dict):
                parse_errors += 1
                logger.warning("Запись %s: %s вместо dict - пропущена", key, type(wrapper).__name__)
                continue

            # [9.6.8 FIX] Контракт v710: поддержаны обе формы хранения -
            # обертка с payload и плоский объект (поля в корне)
            payload = wrapper.get("payload")
            if not isinstance(payload, dict):
                payload = wrapper

            source = payload.get("source", "")
            sources = payload.get("sources", [])
            source_ids = payload.get("source_ids", {})
            if not isinstance(source_ids, dict):
                # [9.6.8 FIX] source_ids: null раньше ронял .values() на AttributeError,
                # и запись молча пропускалась через except: continue
                source_ids = {}

            is_propline = (
                source == "propline"
                or "propline" in source_ids
                or "propline" in str(sources).lower()
                or any("propline" in str(v).lower() for v in source_ids.values())
            )

            if is_propline:
                found += 1
                print(f"\n{'='*60}")
                print(f"[PROPLINE RECORD #{found}] Ключ Redis: {key}")
                print(f"{'='*60}")

                print(f"🔹 Соперники: {payload.get('home_team')} vs {payload.get('away_team')}")
                print(f"🔹 Лига: {payload.get('competition')}")

                has_odds = "odds" in payload
                has_spreads = "spreads" in payload
                has_totals = "totals" in payload
                print(f"📊 Секции рынков: odds={has_odds}, spreads={has_spreads}, totals={has_totals}")

                print("\nПолный объект v710:")
                print(json.dumps(wrapper, indent=2, ensure_ascii=False))

                if found >= limit_output:
                    print(f"\n... (вывод ограничен первыми {limit_output} записями)")
                    break

        except (json.JSONDecodeError, ValueError) as e:
            parse_errors += 1
            logger.warning("Нечитаемый JSON записи %s: %s", key, e)
        except Exception as e:
            parse_errors += 1
            logger.warning("Ошибка разбора записи %s: %s", key, e)

print(f"\nВсего найдено и проверено записей Propline: {found}")
if parse_errors:
    print(f"⚠ Нечитаемых/ошибочных записей пропущено: {parse_errors}")
