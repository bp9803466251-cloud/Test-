# -*- coding: utf-8 -*-
"""
Диагностический скрипт check_redis.py для проверки Propline, включая spreads и totals (до 15 матчей).
Версия 9.6.8-patched (v1.1-audit).

[9.6.8] Лестница §1.10 с средним звеном SHARED_REDIS_REST_*; явная проверка конфигурации
        Redis (без URL/токена - понятное сообщение и exit 1); HASH_NAME из redis_hub
        с fallback; контракт v710 (обертка с payload ИЛИ плоский объект); подсчет
        нечитаемых записей вместо молчаливых except: continue.
[v1.1]  Блок «АУДИТ КОНТРАКТА ИСТОРИИ»: подсчет полей history:* внутри хэша и
        standalone-ключей history:match:* через SCAN с курсором; образец записи
        истории (обрезан до 1200 символов); автоматический вердикт о соответствии
        контрактов писатель/читатель (разрыв / совпадение / двойное хранение / пусто).
[v1.1]  Сетевой сбой на HKEYS/HGET/SCAN/GET - понятное сообщение и осознанный exit 1
        без traceback (прежде - необработанный RuntimeError из urllib).
[v1.1]  Явный sys.exit(0) и итоговая сводка с числом просмотренных полей.
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


def _fail_network(stage, exc):
    """[v1.1] Понятное сообщение о сетевом сбое и осознанный exit 1 без traceback."""
    print(f"❌ Redis недоступен на этапе {stage}: {exc}")
    sys.exit(1)


def _truncate(text, limit=1200):
    if len(text) > limit:
        return text[:limit] + "\n... (обрезано для лога)"
    return text


def main():
    print("=== ДИАГНОСТИКА ДАННЫХ PROPLINE В REDIS (ODDS, SPREADS, TOTALS) ===")

    try:
        fields = call(['HKEYS', HASH_NAME]) or []
    except Exception as e:
        _fail_network("HKEYS", e)

    print(f"📦 Хэш '{HASH_NAME}': всего полей {len(fields)}")

    found = 0
    parse_errors = 0
    skipped = 0
    limit_output = 15

    for key in fields:
        if "history" in key.lower() or ":meta" in key:
            skipped += 1
            continue

        try:
            raw_val = call(['HGET', HASH_NAME, key])
        except Exception as e:
            _fail_network(f"HGET {key}", e)
        if not raw_val:
            continue

        try:
            wrapper = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
            if not isinstance(wrapper, dict):
                parse_errors += 1
                logger.warning("Запись %s: %s вместо dict - пропущена", key, type(wrapper).__name__)
                continue

            # [9.6.8] Контракт v710: поддержаны обе формы хранения -
            # обертка с payload и плоский объект (поля в корне)
            payload = wrapper.get("payload")
            if not isinstance(payload, dict):
                payload = wrapper

            source = payload.get("source", "")
            sources = payload.get("sources", [])
            source_ids = payload.get("source_ids", {})
            if not isinstance(source_ids, dict):
                # [9.6.8] source_ids: null раньше ронял .values() на AttributeError,
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
    if skipped:
        print(f"ℹ Полей пропущено по фильтру (history/meta): {skipped}")

    # ── [v1.1] АУДИТ КОНТРАКТА ИСТОРИИ ──────────────────────────────────────
    print("\n=== АУДИТ КОНТРАКТА ИСТОРИИ (v1.1) ===")

    hash_history_fields = [k for k in fields if k.lower().startswith("history")]
    print(f"Полей history:* в хэше '{HASH_NAME}': {len(hash_history_fields)}")

    standalone_keys = []
    cursor = "0"
    while True:
        try:
            scan_result = call(['SCAN', cursor, 'MATCH', 'history:match:*', 'COUNT', 1000])
        except Exception as e:
            _fail_network("SCAN history:match:*", e)
        if not isinstance(scan_result, (list, tuple)) or len(scan_result) != 2:
            parse_errors += 1
            logger.warning("SCAN вернул неожиданный формат: %r", scan_result)
            break
        cursor, keys = scan_result
        standalone_keys.extend(keys or [])
        if cursor in ("0", 0, None):
            break
    print(f"Standalone-ключей history:match:* (SCAN): {len(standalone_keys)}")

    # Образец записи истории: сначала из хэша, иначе из standalone
    sample_printed = False
    if hash_history_fields:
        sample_key = hash_history_fields[0]
        try:
            raw_sample = call(['HGET', HASH_NAME, sample_key])
        except Exception as e:
            _fail_network(f"HGET {sample_key}", e)
        if raw_sample:
            try:
                sample = json.loads(raw_sample) if isinstance(raw_sample, str) else raw_sample
                print(f"\nОбразец записи истории (поле хэша '{sample_key}'):")
                print(_truncate(json.dumps(sample, indent=2, ensure_ascii=False)))
                sample_printed = True
            except Exception as e:
                logger.warning("Образец записи %s нечитаем: %s", sample_key, e)
    if not sample_printed and standalone_keys:
        sample_key = standalone_keys[0]
        if isinstance(sample_key, bytes):
            sample_key = sample_key.decode("utf-8", errors="replace")
        try:
            raw_sample = call(['GET', sample_key])
        except Exception as e:
            _fail_network(f"GET {sample_key}", e)
        if raw_sample:
            try:
                sample = json.loads(raw_sample) if isinstance(raw_sample, str) else raw_sample
                print(f"\nОбразец записи истории (standalone-ключ '{sample_key}'):")
                print(_truncate(json.dumps(sample, indent=2, ensure_ascii=False)))
                sample_printed = True
            except Exception as e:
                logger.warning("Образец записи %s нечитаем: %s", sample_key, e)

    # Вердикт о соответствии контрактов писатель/читатель
    if hash_history_fields and standalone_keys:
        print("\n⚠ ВЕРДИКТ: ДВОЙНОЕ ХРАНЕНИЕ - история лежит и в хэше, и standalone-ключами.")
        print("   Риск дублей в бэктесте: нужен выбор одного контракта.")
    elif hash_history_fields:
        print("\n❌ ВЕРДИКТ: КОНТРАКТНЫЙ РАЗРЫВ - история лежит ПОЛЯМИ ХЭША, а читатели")
        print("   (backtest_runner через scan_keys('history:match:*')) ищут ОТДЕЛЬНЫЕ ключи.")
        print("   Это объясняет 'no_history_matches' при непустой базе. Читателя нужно выровнять.")
    elif standalone_keys:
        print("\n✅ ВЕРДИКТ: контракты совпадают - история лежит standalone-ключами, как ожидают читатели.")
    else:
        print("\nℹ ВЕРДИКТ: история отсутствует (пустой контур после DATA STOP).")

    print(f"\n📊 Итог аудита: просмотрено {len(fields)} полей, Propline-совпадений {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
                    
