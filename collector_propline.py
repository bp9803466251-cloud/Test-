import os
import time
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- Конфигурация и зависимости ---
REDIS_URL = os.getenv("SHARED_UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("SHARED_UPSTASH_REDIS_REST_TOKEN")
API_KEY = os.getenv("PROPLINE_API_KEY")

# Настройки Rate Limiting (PropLine API)
RATE_DELAY = float(os.getenv("PROPLINE_RATE_DELAY", "0.2"))
MAX_RETRIES = int(os.getenv("PROPLINE_MAX_RETRIES", "3"))
TIMEOUT = int(os.getenv("PROPLINE_API_TIMEOUT", "15"))

BASE_URL = "https://api.prop-line.com/v1"
SOCCER_LEAGUES = [
    "soccer_epl", "soccer_la_liga", "soccer_serie_a",
    "soccer_bundesliga", "soccer_ligue_1", "soccer_mls"
]

def get_redis_headers():
    return {
        "Authorization": f"Bearer {REDIS_TOKEN}",
        "Content-Type": "application/json"
    }

def call_redis(endpoint, payload=None, method="GET"):
    url = f"{REDIS_URL}/{endpoint}"
    req = urllib.request.Request(url, headers=get_redis_headers(), method=method)
    if payload:
        req.data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"[ERROR] Redis call failed: {e}")
        return None

def american_to_decimal(odds_val):
    if odds_val > 0:
        return (odds_val / 100) + 1
    elif odds_val < 0:
        return (100 / abs(odds_val)) + 1
    return 1.0

def fetch_odds(sport_key, markets="h2h,spreads,totals"):
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    full_url = f"{url}?apiKey={API_KEY}&markets={markets}"
    
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "GatekeeperAI"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                data = json.loads(response.read())
                # Проверка заголовков квоты
                remaining = response.headers.get('X-Daily-Remaining')
                if remaining:
                    print(f"[QUOTA] Remaining: {remaining}")
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get('Retry-After', 5))
                print(f"[RATE LIMIT] Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            print(f"[API ERROR] {e.code}: {e.reason}")
            break
        except Exception as e:
            print(f"[NETWORK ERROR] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    return None

def process_match(match_data, sport_key):
    # Формирование уникального ID
    event_id = match_data.get("id")
    if not event_id:
        return
    
    canonical_id = f"propline:{event_id}"
    
    # Нормализация данных под общую схему
    odds_data = {
        "source": "propline",
        "sport": sport_key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets": {}
    }
    
    # Парсинг 1x2 (h2h)
    if "h2h" in match_data.get("odds", {}):
        h2h = match_data["odds"]["h2h"]
        odds_data["markets"]["1x2"] = {
            "home": american_to_decimal(h2h.get("home")),
            "draw": american_to_decimal(h2h.get("draw")),
            "away": american_to_decimal(h2h.get("away"))
        }
        
    # Парсинг Spreads (Asian Handicap)
    if "spreads" in match_data.get("odds", {}):
        spreads = match_data["odds"]["spreads"]
        # Упрощенно берем первый вариант или усредняем, зависит от требований схемы
        odds_data["markets"]["spreads"] = spreads 

    # Парсинг Totals (O/U)
    if "totals" in match_data.get("odds", {}):
        totals = match_data["odds"]["totals"]
        odds_data["markets"]["totals"] = totals

    # Сохранение в Redis
    # Предполагаем, что у вас есть ключ для хранения сырых данных или нормализованных
    redis_key = f"odds:raw:{canonical_id}"
    call_redis("set", payload={redis_key: json.dumps(odds_data)})
    
    print(f"[OK] Processed {canonical_id}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Не писать в Redis")
    parser.add_argument("--sports", nargs="+", default=SOCCER_LEAGUES)
    parser.add_argument("--markets", default="h2h,spreads,totals")
    args = parser.parse_args()

    if not API_KEY:
        print("[CRITICAL] PROPLINE_API_KEY not set in secrets!")
        return 1

    print(f"[START] Collector Propline. Sports: {args.sports}")
    
    for sport in args.sports:
        print(f"[FETCH] Fetching odds for {sport}...")
        data = fetch_odds(sport, args.markets)
        
        if not data or "events" not in data:
            print(f"[WARN] No events found for {sport}")
            continue
            
        for event in data["events"]:
            if args.dry_run:
                print(f"[DRY] Would process {event.get('id')}")
            else:
                process_match(event, sport)
            
            # Rate limiting между событиями
            time.sleep(RATE_DELAY)
            
    print("[DONE] Collection finished.")
    return 0

if __name__ == "__main__":
    exit(main())
  
