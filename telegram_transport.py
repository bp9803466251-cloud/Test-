"""
Транспорт для отправки Telegram-дашбордов в Gatekeeper-AI v600-prod.
Гарантирует:
- корректную разбивку HTML по частям <= 4000 символов без разрыва тегов;
- балансировку HTML-тегов между чанками;
- жесткую нормализацию маркеров: все стрелки → ➔ (U+2794, монохром);
- добавление пробелов после маркера и вокруг | для читаемости;
- точечное оборачивание O: и P: в <code> для предотвращения синей подсветки;
- retry для transient-ошибок API Telegram;
- явные уровни логирования ошибок.
"""
import os
import re
import time
import urllib.request
import urllib.error
import json
from typing import List, Dict, Any

TELEGRAM_TIMEOUT = 10
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_RETRY_DELAY = 2
TELEGRAM_CHUNK_LIMIT = 4000

# HTML-теги, которые Telegram поддерживает и которые нужно балансировать
_BALANCEABLE_TAGS = {"b", "i", "u", "s", "code", "pre"}


def _scan_open_tags(text: str) -> List[str]:
    """Сканирует текст и возвращает список незакрытых тегов в порядке открытия."""
    open_tags = []
    for match in re.finditer(r"</?(\w+)[^>]*?>", text):
        tag = match.group(1).lower()
        if tag not in _BALANCEABLE_TAGS:
            continue
        is_closing = match.group(0).startswith("</")
        if is_closing:
            if tag in open_tags:
                open_tags.remove(tag)
        else:
            if not match.group(0).endswith("/>"):
                open_tags.append(tag)
    return open_tags


def _close_tags(tags: List[str]) -> str:
    """Закрывает теги в обратном порядке."""
    return "".join(f"</{t}>" for t in reversed(tags))


def _reopen_tags(tags: List[str]) -> str:
    """Открывает теги заново в исходном порядке."""
    return "".join(f"<{t}>" for t in tags)


def split_html_safe(text: str, max_chars: int = TELEGRAM_CHUNK_LIMIT) -> List[str]:
    """
    Безопасно разбивает HTML-текст на части <= max_chars.
    Балансирует HTML-теги между чанками.
    """
    lines = text.splitlines(keepends=True)
    parts = []
    current = ""

    for line in lines:
        if len(current) + len(line) <= max_chars:
            current += line
            continue

        if current:
            open_tags = _scan_open_tags(current)
            if open_tags:
                close_str = _close_tags(open_tags)
                reopen_str = _reopen_tags(open_tags)
                current += close_str
                parts.append(current)
                current = reopen_str + line
            else:
                parts.append(current)
                current = line
        else:
            if len(line) > max_chars:
                search_start = max(0, max_chars - 200)
                search_end = max_chars
                cut_pos = line.rfind(" ", search_start, search_end)
                if cut_pos <= 0:
                    cut_pos = max_chars - 1
                chunk = line[:cut_pos]
                open_tags = _scan_open_tags(chunk)
                if open_tags:
                    chunk += _close_tags(open_tags)
                    parts.append(chunk)
                    current = _reopen_tags(open_tags) + line[cut_pos:]
                else:
                    parts.append(chunk)
                    current = line[cut_pos:]
            else:
                current = line

    if current:
        parts.append(current)

    return parts


def send_telegram_dashboard(dashboard_text: str) -> Dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_GROUP_ID")

    if not token:
        print("[TELEGRAM FAIL] TELEGRAM_BOT_TOKEN не задан в окружении!")
        return {"success": False, "sent_parts": 0, "total_parts": 0}
    if not chat_id:
        print("[TELEGRAM FAIL] TELEGRAM_GROUP_ID не задан в окружении!")
        return {"success": False, "sent_parts": 0, "total_parts": 0}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    headers = {"Content-Type": "application/json"}

    # Жесткая нормализация: все стрелки и треугольники → ➔ (U+2794, монохром)
    dashboard_text = re.sub(
        r'[\u25B6\u25B8\u25BA\u23F5\u2192\u21A6\u21D2\u21E2\u2794\u27A4\u27AF\u27B8][\uFE00-\uFE0F\u200D]?',
        '\u2794',
        dashboard_text
    )
    dashboard_text = re.sub(
        r'[\u25B7\u25B9][\uFE00-\uFE0F\u200D]?',
        '\u2794',
        dashboard_text
    )

    # Добавление пробелов: после маркера перед номером и вокруг всех |
    dashboard_text = re.sub(r'➔(\d)', r'➔ \1', dashboard_text)
    dashboard_text = re.sub(r'\s*\|\s*', ' | ', dashboard_text)

    # Точечно оборачиваем значения O: и P: до | — strip убирает лишние пробелы
    def protect_values(match):
        prefix = match.group(1)  # 'O' или 'P'
        val = match.group(2).strip()  # значение без пробелов по краям
        return f"{prefix}: <code>{val}</code>"

    processed_text = re.sub(r'\b(O|P):([^|\n]+)', protect_values, dashboard_text)

    parts = split_html_safe(processed_text, TELEGRAM_CHUNK_LIMIT)
    total_parts = len(parts)
    sent_parts = 0

    if total_parts == 0:
        return {"success": True, "sent_parts": 0, "total_parts": 0}

    for i, part in enumerate(parts):
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")

        retry_count = 0
        while retry_count <= TELEGRAM_MAX_RETRIES:
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT) as response:
                    res = json.loads(response.read().decode("utf-8"))
                    if res.get("ok"):
                        sent_parts += 1
                        break
                    else:
                        error_desc = res.get("description", "unknown error")
                        print(f"[TELEGRAM API ERROR] Часть {i+1}/{total_parts}: {error_desc}")
                        if res.get("error_code") in (400, 403, 404):
                            break
                        retry_count += 1
                        if retry_count <= TELEGRAM_MAX_RETRIES:
                            print(f"[TELEGRAM RETRY] {retry_count}/{TELEGRAM_MAX_RETRIES} через {TELEGRAM_RETRY_DELAY}с...")
                            time.sleep(TELEGRAM_RETRY_DELAY)
                        continue
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = TELEGRAM_RETRY_DELAY
                    try:
                        body = json.loads(e.read().decode("utf-8"))
                        retry_after = body.get("parameters", {}).get("retry_after")
                        if retry_after:
                            wait = int(retry_after)
                    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
                        pass
                    print(f"[TELEGRAM 429] Rate limited. Жду {wait}с...")
                    time.sleep(wait)
                    retry_count += 1
                    continue
                else:
                    print(f"[TELEGRAM HTTP {e.code}] Часть {i+1}/{total_parts}: {e.reason}")
                    retry_count += 1
                    if retry_count <= TELEGRAM_MAX_RETRIES:
                        print(f"[TELEGRAM RETRY] {retry_count}/{TELEGRAM_MAX_RETRIES} через {TELEGRAM_RETRY_DELAY}с...")
                        time.sleep(TELEGRAM_RETRY_DELAY)
                    continue
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                print(f"[TELEGRAM SEND ERROR] Часть {i+1}/{total_parts}: {e}")
                retry_count += 1
                if retry_count <= TELEGRAM_MAX_RETRIES:
                    print(f"[TELEGRAM RETRY] {retry_count}/{TELEGRAM_MAX_RETRIES} через {TELEGRAM_RETRY_DELAY}с...")
                    time.sleep(TELEGRAM_RETRY_DELAY)
                    continue
            except Exception as e:
                print(f"[TELEGRAM UNEXPECTED ERROR] Часть {i+1}/{total_parts}: {e}")
                break

    success = sent_parts == total_parts
    print(f"[TELEGRAM STATUS] Отправлено {sent_parts}/{total_parts} частей. success={success}")
    return {"success": success, "sent_parts": sent_parts, "total_parts": total_parts}
