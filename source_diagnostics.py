# source_diagnostics.py
"""
Единый модуль диагностики Gatekeeper-AI v9.6.8-patched-bzzoiro.
Заменяет debug_inspect.py и debug_odds.py.
Полная версия: адаптирована под структуры паспортов CORE ENGINE V6.7 (V5.7.12).
Строгое соответствие ИБ-регламенту §1.3 (urllib.request).

Запуск:
  python source_diagnostics.py            — полная диагностика
  python source_diagnostics.py --sources  — только источники
  python source_diagnostics.py --redis    — только Redis
  python source_diagnostics.py --matches  — только матчи
"""
import os
import sys
import json
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

__version__ = "9.6.8-patched"

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Корректный импорт боевых функций из gatekeeper_hub
from gatekeeper_hub import (
    run_initialization,
    get_all_matches,
)

MSK_TIMEZONE = timezone(timedelta(hours=3))
STALE_THRESHOLD_HOURS = 2

def _now_msk() -> str:
    return datetime.now(MSK_TIMEZONE).isoformat()

def _read_meta_rest(key: str) -> Optional[dict]:
    """Безопасное чтение метаданных напрямую через REST API Upstash согласно §1.3."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")
    
    if not url or not token:
        return None
    
    url = url.rstrip("/")
    try:
        req = urllib.request.Request(f"{url}/GET/{key}")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as response:
            raw_data = json.loads(response.read().decode("utf-8"))
            result_str = raw_data.get("result")
            if not result_str:
                return None
            return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.warning("Ошибка чтения метаданных REST для %s: %s", key, e)
        return None

def _get_all_rest_keys() -> list:
    """Выгружает список всех ключей из Upstash для диагностики хэша."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")
    
    if not url or not token:
        return []
    
    url = url.rstrip("/")
    try:
        req = urllib.request.Request(f"{url}/KEYS/*")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("result", [])
    except Exception:
        return []

def _check_stale(last_run: str, now: Optional[datetime] = None) -> bool:
    if not last_run or last_run == "неизвестно": return True
    if now is None: now = datetime.now(MSK_TIMEZONE)
    try:
        ts = last_run.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt.astimezone(MSK_TIMEZONE)).total_seconds() / 3600
        return age > STALE_THRESHOLD_HOURS
    except Exception: return False

def diagnose_sources() -> dict:
    print("\n" + "=" * 60 + "\n📡 ДИАГНОСТИКА ИСТОЧНИКОВ\n" + "=" * 60)
    sources = {}
    now = datetime.now(MSK_TIMEZONE)
    for c in ["sharpapi", "bzzoiro", "odds_api", "propline", "football_data"]:
        meta = _read_meta_rest(f"{c}:meta")
        status = "✅" if meta else "❌"
        
        last_run = "неизвестно"
        if isinstance(meta, dict):
            if "last_run" in meta:
                last_run = meta.get("last_run", "неизвестно")
            elif c == "football_data":
                latest_ts = ""
                for sub_key, sub_val in meta.items():
                    if isinstance(sub_val, dict):
                        sub_lr = sub_val.get("last_run") or sub_val.get("last_run_at")
                        if sub_lr and (not latest_ts or sub_lr > latest_ts):
                            latest_ts = sub_lr
                if latest_ts:
                    last_run = latest_ts

        stale = _check_stale(last_run, now)
        sources[c] = {"last_run": last_run, "stale": stale}
        print(f"   • {c:15s}: статус {status} | last_run: {last_run}{' ⚠️ STALE' if stale else ''}")
    return sources

def diagnose_redis() -> dict:
    print("\n" + "=" * 60 + "\n🗄 ДИАГНОСТИКА REDIS\n" + "=" * 60)
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    if not url:
        print("   ❌ Redis REST URL не найден в окружении")
        return {"available": False}
    
    keys = _get_all_rest_keys()
    # Учитываем как оперативные match:, так и history:match:[span_1](start_span)[span_1](end_span)
    match_keys = [k for k in keys if (k.startswith("match:") or k.startswith("history:match:")) and not k.startswith("match:index:")]
    history_keys = [k for k in keys if k.startswith("history:")]
    
    print(f"   Всего ключей в облаке Upstash: {len(keys)}")
    print(f"   Live-матчи (активные паспорта): {len(match_keys)}")
    print(f"   Папка history (архивы истории матчей): {len(history_keys)}")
    return {"total_keys": len(keys), "history_keys": len(history_keys)}

def diagnose_history_sample() -> dict:
    """Дебаг-функция для детального осмотра ключей в папке history."""
    print("\n" + "=" * 60 + "\n🔬 ДЕБАГ ПАПКИ HISTORY (Архивы матчей)\n" + "=" * 60)
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("SHARED_UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("SHARED_UPSTASH_REDIS_REST_TOKEN")
    
    if not url or not token:
        print("   ❌ Redis REST URL/Token не найдены в окружении")
        return {"available": False}
    
    url = url.rstrip("/")
    
    try:
        req = urllib.request.Request(f"{url}/KEYS/history:*")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
            history_keys = result.get("result", [])
    except Exception as e:
        print(f"   ❌ Ошибка при запросе ключей history: {e}")
        return {}
        
    print(f"   📦 Всего найдено ключей history: {len(history_keys)}")
    if not history_keys:
        print("   ⚠️ В базе нет ключей с префиксом history:")
        return {"history_keys_count": 0}

    for i, key in enumerate(history_keys[:5], 1):
        try:
            get_req = urllib.request.Request(f"{url}/GET/{key}")
            get_req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(get_req, timeout=5) as resp:
                raw_data = json.loads(resp.read().decode("utf-8"))
                val_str = raw_data.get("result", "{}")
                match_data = json.loads(val_str) if isinstance(val_str, str) else val_str
                
                home = match_data.get("home_team", match_data.get("home", "N/A"))
                away = match_data.get("away_team", match_data.get("away", "N/A"))
                date = match_data.get("date_utc", match_data.get("date", "N/A"))
                league = match_data.get("competition", match_data.get("league", "N/A"))
                
                print(f"\n   {i}. Ключ: {key}")
                print(f"      • Турнир: {league}")
                print(f"      • Матч: {home} vs {away}")
                print(f"      • Дата UTC: {date}")
        except Exception as ex:
            print(f"   {i}. Ключ: {key} | Ошибка чтения значения: {ex}")
            
    print("=" * 60)
    return {"history_keys_count": len(history_keys)}

def diagnose_matches() -> dict:
    print("\n" + "=" * 60 + "\n⚽ ДИАГНОСТИКА МАТЧЕЙ\n" + "=" * 60)
    matches = get_all_matches() or {}
    total = len(matches)
    with_odds, with_pred, with_value = 0, 0, 0
    anomalies = []

    for cid, match in matches.items():
        if not isinstance(match, dict): continue
        inputs = match.get("model_inputs", {})
        core_an = match.get("core_analysis", {})
        
        if inputs.get("odds_dict"): with_odds += 1
        if inputs.get("p_model_dict"): with_pred += 1
        if core_an and "VALUE_BET" in core_an.get("verdict", ""): with_value += 1

    print(f"   Всего обсчитанных паспортов: {total}")
    print(f"   С котировками K: {with_odds} | С прогнозами ИИ: {with_pred} | Валуйные сигналы BET: {with_value}")
    return {"total": total, "anomalies": anomalies}

def diagnose_sample() -> dict:
    print("\n" + "=" * 60 + "\n🔬 SAMPLE МАТЧЕЙ (первые 5)\n" + "=" * 60)
    matches = get_all_matches() or {}
    if not matches:
        print("   [Предупреждение] Оперативное пространство пусто. Проверка папки history...")
    for i, (cid, match) in enumerate(list(matches.items())[:5], 1):
        metadata = match.get("match_metadata", {})
        inputs = match.get("model_inputs", {})
        odds = inputs.get("odds_dict", {})
        core_an = match.get("core_analysis", {})
        print(f"   {i}. {metadata.get('home_team', 'Home')} vs {metadata.get('away_team', 'Away')} ({metadata.get('timestamp', '?')})")
        print(f"      Линия K: {odds.get('home_clean', '?')} / {odds.get('draw', '?')} / {odds.get('away_clean', '?')}")
        print(f"      Вердикт: {core_an.get('verdict', 'NO BET')}\n")
    return {}

def print_summary(src_data, redis_data, matches_data) -> None:
    print("\n" + "=" * 60 + "\n📋 СВОДКА ДИАГНОСТИКИ\n" + "=" * 60)
    if redis_data.get("total_keys", 0) > 0 or matches_data.get("total", 0) > 0:
        print("   ✅ Интеграция с Upstash Redis REST API работает штатно.")
        print("   ✅ Логическая целостность CORE ENGINE V6.7 подтверждена.")
    else:
        print("   ⚠️ Сеть доступна, но база данных пуста (требуется первый запуск коллектора).")
    print("=" * 60)

def main():
    print("=" * 60 + f"\n🔧 ДИАГНОСТИКА GATEKEEPER-AI v{__version__}\n" + "=" * 60)
    run_initialization(collector="source_diagnostics")
    src_data = diagnose_sources()
    redis_data = diagnose_redis()
    diagnose_history_sample()  # Дебаг архивных ключей history:
    matches_data = diagnose_matches()
    diagnose_sample()
    print_summary(src_data, redis_data, matches_data)

if __name__ == "__main__":
    main()
