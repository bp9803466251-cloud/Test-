"""
telegram_transport.py — Транспорт для отправки Telegram-дашбордов.
GatekeeperAI v8.10-patched.

Гарантирует:
- корректную разбивку HTML по частям <= 4000 символов без разрыва тегов;
- балансировку HTML-тегов между чанками;
- жесткую нормализацию маркеров: все стрелки → ➔ (U+2794, монохром);
- добавление пробелов после маркера и вокруг | для читаемости;
- точечное оборачивание O: и P: в <code> для предотвращения подсветки;
- retry для transient-ошибок API Telegram (с exponential backoff);
- обработку HTTP 429 с retry_after из ответа;
- структурированное логирование через logging.
"""

import os
import re
import time
import logging
import urllib.request
import urllib.error
import json
from typing import List, Dict, Any

__version__ = "8.10-patched"

__all__ = [
    "split_html_safe",
    "send_telegram_dashboard",
    "normalize_dashboard_text",
    "__version__",
]

logger = logging.getLogger("telegram_transport")

# ── Константы ──────────────────────────────────────────────
TELEGRAM_TIMEOUT = 10
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_RETRY_DELAY = 2
TELEGRAM_CHUNK_LIMIT = 4000

# HTML-теги, которые Telegram поддерживает и которые нужно балансировать
_BALANCEABLE_TAGS = {"b", "i", "u", "s", "code", "pre"}


# ═══════════════════════════════════════════════════════════
# HTML-балансировка
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
# Нормализация текста дашборда
# ═══════════════════════════════════════════════════════════

def normalize_dashboard_text(dashboard_text: str) -> str:
    """
    Нормализует маркеры и оборачивает значения в <code>.
    Вынесено в отдельную функцию для тестируемости.
    """
    # Жесткая нормализация: все стрелки и треугольники → ➔ (U+2794, монохром)
    dashboard_text = re.sub(
        r'[\u25B6\u25B8\u25BA\u23F5\u2192\u21A6\u21D2\u21E2\u2794\u27A4\u27AF\u27B8][\uFE00-\uFE0F\u200D]?',
        "\u2794",
        dashboard_text
    )
    dashboard_text = re.sub(
        r'[\u25B7\u25B9][\uFE00-\uFE0F\u200D]?',
        "\u2794",
        dashboard_text
    )

    # Добавление пробелов: после маркера перед номером и вокруг всех |
    dashboard_text = re.sub(r"\u2794(\d)", r"\u2794 \1", dashboard_text)
    dashboard_text = re.sub(r"\s*\|\s*", " | ", dashboard_text)

    # Точечно оборачиваем значения O: и P: до | — strip убирает лишние пробелы
    def protect_values(match):
        prefix = match.group(1)  # 'O' или 'P'
        val = match.group(2).strip()  # значение без пробелов по краям
        return f"{prefix}: <code>{val}</code>"

    processed_text = re.sub(r"\b(O|P):([^|\n]+)", protect_values, dashboard_text)

    return processed_text


# ═══════════════════════════════════════════════════════════
# Отправка
# ═══════════════════════════════════════════════════════════

def send_telegram_dashboard(dashboard_text: str) -> Dict[str, Any]:
    """
    Отправляет дашборд в Telegram.
    Возвращает {"success": bool, "sent_parts": int, "total_parts": int}.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_GROUP_ID")

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не задан в окружении")
        return {"success": False, "sent_parts": 0, "total_parts": 0}
    if not chat_id:
        logger.error("TELEGRAM_GROUP_ID не задан в окружении")
        return {"success": False, "sent_parts": 0, "total_parts": 0}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    headers = {"Content-Type": "application/json"}

    # Нормализация текста
    processed_text = normalize_dashboard_text(dashboard_text)

    parts = split_html_safe(processed_text, TELEGRAM_CHUNK_LIMIT)
    total_parts = len(parts)
    sent_parts = 0

    if total_parts == 0:
        logger.info("Пустой дашборд — ничего не отправлено")
        return {"success": True, "sent_parts": 0, "total_parts": 0}

    logger.info(f"Отправка {total_parts} частей в Telegram...")

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
                        error_code = res.get("error_code", 0)
                        logger.warning(f"Часть {i+1}/{total_parts}: API error — {error_desc}")
                        # 400, 403, 404 — не повторяем (клиентская ошибка)
                        if error_code in (400, 403, 404):
                            break
                        retry_count += 1
                        if retry_count <= TELEGRAM_MAX_RETRIES:
                            delay = TELEGRAM_RETRY_DELAY * (2 ** (retry_count - 1))
                            logger.info(f"Retry {retry_count}/{TELEGRAM_MAX_RETRIES} через {delay}с...")
                            time.sleep(delay)
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
                    logger.warning(f"429 Rate limited. Жду {wait}с...")
                    time.sleep(wait)
                    retry_count += 1
                    continue
                else:
                    logger.warning(f"Часть {i+1}/{total_parts}: HTTP {e.code} — {e.reason}")
                    retry_count += 1
                    if retry_count <= TELEGRAM_MAX_RETRIES:
                        delay = TELEGRAM_RETRY_DELAY * (2 ** (retry_count - 1))
                        logger.info(f"Retry {retry_count}/{TELEGRAM_MAX_RETRIES} через {delay}с...")
                        time.sleep(delay)
                    continue
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                logger.warning(f"Часть {i+1}/{total_parts}: сетевая ошибка — {e}")
                retry_count += 1
                if retry_count <= TELEGRAM_MAX_RETRIES:
                    delay = TELEGRAM_RETRY_DELAY * (2 ** (retry_count - 1))
                    logger.info(f"Retry {retry_count}/{TELEGRAM_MAX_RETRIES} через {delay}с...")
                    time.sleep(delay)
                    continue
            except Exception as e:
                logger.error(f"Часть {i+1}/{total_parts}: непредвиденная ошибка — {e}", exc_info=True)
                break

    success = sent_parts == total_parts
    if success:
        logger.info(f"Отправлено {sent_parts}/{total_parts} частей. success={success}")
    else:
        logger.warning(f"Отправлено {sent_parts}/{total_parts} частей. success={success}")
    return {"success": success, "sent_parts": sent_parts, "total_parts": total_parts}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [TG] %(message)s")
    # Smoke test
    result = send_telegram_dashboard("<b>Test</b> | O: 1.85 | P: 2.10")
    print(json.dumps(result, indent=2))
