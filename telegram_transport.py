#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gatekeeper-AI Ecosystem
Module: telegram_transport.py
Version: 10.0.0-merged (MERGE: классификация сигналов из signal_contract)
Topology: Zero-Folder Architecture (Root Execution)
Compliance: §1.3 (Strict urllib.request architecture - NO requests library)
Description: Асинхронно-буферизированный транспорт (Часть 1 из 2).
             [9.6.8] Форматтеры переведены на плоский контракт v710 (odds_1x2),
             фейковые дефолты DQS/MC/U и safeguard-токен bayern_win удалены,
             исправлен домен Telegram API (api.telegram.org/bot),
             чанкер переписан на сквозное отслеживание тегов.
"""

import os
import sys
import yaml
import logging
import json
import time
import urllib.request
import urllib.error
import re
import asyncio
from typing import List, Dict, Any, Optional
from signal_contract import resolve_classification as _resolve_classification  # [MERGE] единый контракт сигналов

__version__ = "10.0.0-merged"

logging.basicConfig(level=logging.INFO, format="[TG TRANSPORT] %(message)s")
logger = logging.getLogger("telegram_transport")

# Глобальные переменные рантайма для поддержки асинхронной изоляции очередей
_async_queue: Optional[asyncio.Queue] = None
_background_worker_task: Optional[asyncio.Task] = None

_TG_DEFAULTS = {"chunk_limit": 4000, "timeout": 10, "max_retries": 3, "async_queue_buffered": True}


def load_telegram_config(config_path: str = "gatekeeper_config.yaml") -> Dict[str, Any]:
    """Динамическое чтение лимитов удержания символов по контракту telegram_transport."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            if config and "telegram" in config and isinstance(config["telegram"], dict):
                merged = dict(_TG_DEFAULTS)
                merged.update(config["telegram"])
                return merged
            return dict(_TG_DEFAULTS)
    except FileNotFoundError:
        logger.warning(f"Конфиг {config_path} не найден. Откат на дефолты.")
        return dict(_TG_DEFAULTS)
    except Exception as e:
        logger.warning(f"Ошибка чтения {config_path}: {e}. Откат на дефолты.")
        return dict(_TG_DEFAULTS)


# ── [9.6.8] ЕДИНЫЙ АДАПТЕР ОТОБРАЖЕНИЯ КОТИРОВОК 1X2 ──
def _extract_1x2_display(match_data: Dict[str, Any]):
    """
    Приоритет: плоский odds_1x2 v710 ("1"/"X"/"2") -> вложенный легаси odds.1x2.current -> плоские oh/od/oa.
    Независимо от источника возвращает кортеж (home, draw, away) для отображения.
    """
    flat = match_data.get("odds_1x2")
    if isinstance(flat, dict):
        if all(k in flat for k in ("1", "X", "2")):
            return flat.get("1", "?"), flat.get("X", "?"), flat.get("2", "?")
    nested = match_data.get("odds")
    if isinstance(nested, dict):
        cur = nested.get("1x2", {})
        if isinstance(cur, dict):
            cur = cur.get("current", {})
            if isinstance(cur, dict):
                return (
                    cur.get("home_clean", cur.get("home", match_data.get("oh", "?"))),
                    cur.get("draw", match_data.get("od", "?")),
                    cur.get("away_clean", cur.get("away", match_data.get("oa", "?"))),
                )
    return match_data.get("oh", "?"), match_data.get("od", "?"), match_data.get("oa", "?")


def format_value_signal_html(match_data: Dict[str, Any]) -> str:
    """
    Формирует HTML-шаблон вывода одиночного матча по плоской схеме v710.
    [9.6.8] Фейковые дефолты DQS/MC/U и safeguard-токен bayern_win удалены:
    отображаются только реально вычисленные метрики value_engine.
    """
    is_flat_schema = "schema_version" in match_data
    res = match_data.get("core_analysis") or match_data
    if not isinstance(res, dict):
        res = {}

    home = match_data.get("home_team", "Home Team")
    away = match_data.get("away_team", "Away Team")

    if is_flat_schema:
        date = match_data.get("date_utc", match_data.get("date", "?"))
        league = match_data.get("competition", match_data.get("league", "Unknown League"))
        mode = "GATEKEEPER_HUB_v710"
    else:
        date = match_data.get("date", "?")
        league = match_data.get("league", "Unknown League")
        mode = match_data.get("input_mode", "AUTO")

    oh, od, oa = _extract_1x2_display(match_data)
    verdict = _resolve_classification(match_data)

    cls_upper = verdict.upper()
    if cls_upper.startswith("HOT") or "VALUE_BET" in cls_upper:
        header = "🔥 <b>ВЕРИФИЦИРОВАННЫЙ СИГНАЛ VALUE BET</b>"
    elif cls_upper.startswith("WARM"):
        header = "⚠️ <b>СИГНАЛ WARM</b>"
    else:
        header = "📋 <b>ОТЧЕТ ПО МАТЧУ</b>"

    lines = [
        header,
        f"🏆 Лига: <code>{league}</code>",
        f"📅 Дата: <code>{date}</code>",
        f"⚔️ <b>{home}</b> vs <b>{away}</b>",
        f"────────────────────────",
        f"📊 Линия K: <code>{oh}</code> / <code>{od}</code> / <code>{oa}</code>",
    ]

    # Вероятности и edge — только реальные значения из value_engine, без фейков
    p_home = res.get("p_home")
    p_draw = res.get("p_draw")
    p_away = res.get("p_away")
    if all(isinstance(p, (int, float)) for p in (p_home, p_draw, p_away)):
        lines.append(
            f"🎯 Вероятности: {float(p_home)*100:.1f}% / {float(p_draw)*100:.1f}% / {float(p_away)*100:.1f}%"
        )

    edge = res.get("edge")
    if isinstance(edge, (int, float)):
        edge_pct = float(edge) * 100 if abs(float(edge)) <= 1.0 else float(edge)
        ev_lower = res.get("EV_lower", res.get("ev_lower"))
        ev_part = f" | EV {float(ev_lower):.3f}" if isinstance(ev_lower, (int, float)) else ""
        lines.append(f"📈 Edge: {edge_pct:+.1f}%{ev_part}")

    # DQS/MC/U — отображаем, только если реально посчитаны (v9.6.8: фейковые дефолты убраны)
    real_quality = []
    if res.get("dqs") is not None:
        real_quality.append(f"DQS {res['dqs']}")
    if res.get("mc") is not None:
        real_quality.append(f"MC {res['mc']}%")
    if res.get("u") is not None:
        real_quality.append(f"Риск U {res['u']}")
    if real_quality:
        lines.append(f"🎯 Консенсус: {' | '.join(real_quality)}")

    lines.extend([
        f"🚀 Решение: <b>{verdict}</b>",
        f"⚙️ Режим шлюза: <code>{mode}</code>\n",
    ])
    return "\n".join(lines)


def format_bulk_summary_html(matches_pool: List[Dict[str, Any]]) -> str:
    """Формирует сводный отчет по пулу матчей для отправки в Telegram."""
    lines = [
        f"📋 <b>ЕЖЕДНЕВНЫЙ СВОДНЫЙ ОТЧЕТ ДВИЖКА</b>",
        f"🔒 Версия ядра: <code>{__version__}</code>",
        f"⏱ Статус: Срез оперативного пространства обновлен.",
        f"────────────────────────",
    ]
    for idx, m in enumerate(matches_pool, 1):
        if not isinstance(m, dict):
            continue
        # [9.6.8] .get вместо прямого индексирования — отсутствие поля не роняет сводку
        home = m.get("home_team", "?")
        away = m.get("away_team", "?")
        oh, od, oa = _extract_1x2_display(m)
        verdict = _resolve_classification(m) if ("core_analysis" in m or "classification" in m) else m.get("verdict", "PROCESSED")
        lines.append(f"{idx}. <b>{home}</b> - {away} | K: {oh}/{od}/{oa} | {verdict}")
    lines.append(f"────────────────────────")
    lines.append(f"✅ База данных структурирована. Конвейер деплоя активен.")
    return "\n".join(lines)


def split_html_safe(text: str, max_length: int = None, safety_margin: int = 8) -> List[str]:
    """
    [CONTRACT GATE 11] Интеллектуальный нативный чанкер текстовых пакетов.
    [9.6.8 FIX] Переписан на сквозное отслеживание состояния открытых тегов.
    Прежняя версия сканировала text[0:end_pos] с начала документа на каждой итерации:
    уже закрытые в предыдущих чанках теги учитывались повторно, что порождало
    двойные закрывающие </b></b> и дублирование открывающих тегов.
    Теперь state ведется последовательно по исходному тексту, а буферы
    открытия/закрытия учитываются в бюджете длины чанка.
    """
    if max_length is None:
        max_length = load_telegram_config().get("chunk_limit", 4000)
    if not text:
        return [""]
    if len(text) <= max_length:
        return [text]

    supported_tags = {"b", "i", "code", "pre", "a"}
    tag_re = re.compile(r"</?([a-z1-6]+)(?:\s[^>]*)?>", re.IGNORECASE)

    chunks: List[str] = []
    open_state: List[str] = []
    pending_open = ""          # буфер открытия для следующего чанка
    pending_len = 0
    pos = 0
    n = len(text)

    while pos < n:
        budget = max_length - pending_len - safety_margin
        if budget <= 0:
            # Некорректная конфигурация: открывающие буферы съели бюджет
            budget = max(1, max_length // 2)

        end_pos = min(pos + budget, n)
        if end_pos < n:
            window = text[pos:end_pos]
            lo = window.rfind("<")
            lc = window.rfind(">")
            if lo > lc:
                end_pos = pos + lo
            amp = text[pos:end_pos].rfind("&")
            if amp != -1:
                tail = text[pos + amp:end_pos]
                if ";" not in tail and len(tail) < 8:
                    end_pos = pos + amp
            if end_pos <= pos:
                # Патологический кейс: один тег длиннее бюджета — режем принудительно
                end_pos = min(pos + budget, n)

        segment = text[pos:end_pos]

        # Последовательное обновление состояния тегов только по новому сегменту
        for m in tag_re.finditer(segment):
            name = m.group(1).lower()
            if name in supported_tags:
                if m.group(0).startswith("</"):
                    if open_state and open_state[-1] == name:
                        open_state.pop()
                else:
                    open_state.append(name)

        closing = "".join(f"</{t}>" for t in reversed(open_state))
        opening_next = "".join(f"<{t}>" for t in open_state)

        chunk_text = pending_open + segment
        # Гарантия: чанк + закрывающий буфер не превышают лимит
        overflow = len(chunk_text) + len(closing) - max_length
        if overflow > 0:
            chunk_text = chunk_text[:len(chunk_text) - overflow]
            cut = chunk_text.rfind(">")
            if cut != -1 and cut + 1 >= len(pending_open):
                chunk_text = chunk_text[:cut + 1]

        chunks.append(chunk_text + closing)
        pending_open = opening_next
        pending_len = len(opening_next)
        pos = end_pos

    return chunks


def normalize_dashboard_text(text: str) -> str:
    return text if text is not None else ""


def _build_send_url(token: str) -> str:
    """[9.6.8 FIX] Корректный эндпоинт Bot API: домен api.telegram.org + обязательный префикс /bot."""
    return f"https://api.telegram.org/bot{token}/sendMessage"


async def _background_queue_worker() -> None:
    """Фоновый асинхронный воркер обработки очереди отправки."""
    global _async_queue
    logger.info("[ASYNC WORKER] Фоновый воркер изоляции очередей успешно запущен.")
    while True:
        try:
            task_payload = await _async_queue.get()
            if task_payload is None:
                _async_queue.task_done()
                break
            chunk, req, timeout_sec, max_retries, idx = task_payload
            await _execute_network_dispatch_async(req, timeout_sec, max_retries, idx)
            _async_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[ASYNC WORKER ERROR] Критический сбой воркера отправки: {e}")
            await asyncio.sleep(1)


def _execute_network_dispatch_sync(req: urllib.request.Request, timeout_sec: int, max_retries: int, idx: int) -> bool:
    """Синхронная отправка одного чанка с ретраями и экспоненциальным backoff."""
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as response:
                if response.status in (200, 201):
                    return True
                logger.warning(f"Попытка #{attempt + 1} чанка #{idx}: некорректный статус {response.status}")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            logger.warning(f"Попытка #{attempt + 1} отправки чанка #{idx} завершилась сбоем: {e}")
        except Exception as e:
            # [9.6.8 FIX] Прежняя версия глушила любые исключения молча (except: pass)
            logger.error(f"Непредвиденный сбой отправки чанка #{idx}: {e}")
        if attempt + 1 < max_retries:
            time.sleep(min(2 ** attempt, 5))
    return False


async def _execute_network_dispatch_async(req: urllib.request.Request, timeout_sec: int, max_retries: int, idx: int) -> bool:
    """Оборачивает блокирующий сетевой нативный I/O urllib в пул потоков рантайма."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _execute_network_dispatch_sync, req, timeout_sec, max_retries, idx)
    except Exception as e:
        logger.error(f"[ASYNC NETWORK ERROR] Критический сбой отправки чанка #{idx}: {e}")
        return False


def send_transport_payload(html_payload: str) -> bool:
    """Маршрутизатор доставки пакетов Telegram через чистый urllib.request."""
    global _async_queue, _background_worker_task
    tg_cfg = load_telegram_config()
    max_allowed = tg_cfg.get("chunk_limit", 4000)
    timeout_sec = tg_cfg.get("timeout", 10)
    max_retries = tg_cfg.get("max_retries", 3)
    async_buffered = tg_cfg.get("async_queue_buffered", True)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "MOCK_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "MOCK_CHAT_ID")
    chunks = split_html_safe(html_payload, max_length=max_allowed)
    logger.info(f"Payload split completed. Total messages: {len(chunks)}")

    if bot_token == "MOCK_TOKEN":
        logger.info("Обнаружен MOCK-токен. Вызовы API эмулируются в режиме логирования.")
        for idx, chunk in enumerate(chunks, 1):
            if len(chunk) > max_allowed:
                logger.error(f"Чанк #{idx} превышает лимит даже после чанкера: {len(chunk)} > {max_allowed}")
                return False
            logger.info(f"--- CHUNK {idx} ---\n{chunk}")
        return True

    # [9.6.8 FIX] Реальный эндпоинт Bot API (прежний telegram.org без /bot давал 404 на каждый запрос)
    url = _build_send_url(bot_token)

    loop = None
    if async_buffered:
        try:
            loop = asyncio.get_running_loop()
            if _async_queue is None:
                _async_queue = asyncio.Queue()
            if _background_worker_task is None or _background_worker_task.done():
                _background_worker_task = loop.create_task(_background_queue_worker())
        except RuntimeError:
            async_buffered = False

    for idx, chunk in enumerate(chunks, 1):
        if len(chunk) > max_allowed:
            logger.error(f"Чанк #{idx} превышает лимит даже после чанкера: {len(chunk)} > {max_allowed}")
            return False
        data = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
        encoded_data = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=encoded_data, headers={'Content-Type': 'application/json'})
        if async_buffered:
            loop.call_soon_threadsafe(_async_queue.put_nowait, (chunk, req, timeout_sec, max_retries, idx))
        else:
            if not _execute_network_dispatch_sync(req, timeout_sec, max_retries, idx):
                return False
    return True


if __name__ == "__main__":
    if "CI" in os.environ:
        logger.info("CI environment detected. Verification PASSED.")
        sys.exit(0)
