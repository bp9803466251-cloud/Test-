#!/usr/bin/env python3
"""
web_dashboard.py — Веб-панель мониторинга GatekeeperAI.
Легковесный HTTP-сервер на стандартной библиотеке Python.

§19.5 этап 4: веб-панель для мониторинга.
§20.7: авто-сбор через MODULE_REGISTRY вместо хардкода.
§12: формат дашборда.

Запуск:
  python web_dashboard.py                  # порт 8080
  python web_dashboard.py --port 3000      # порт 3000
  python web_dashboard.py --host 0.0.0.0   # все интерфейсы

API endpoints:
  GET /               — HTML-дашборд
  GET /api/health     — состояние Redis, circuit breaker, cleanup
  GET /api/matches    — live-матчи (с пагинацией)
  GET /api/collectors — статусы коллекторов из MODULE_REGISTRY
  GET /api/quality    — сводка качества данных
  GET /api/registry   — реестр модулей
  GET /api/system     — system:health из Redis
"""

import os
import sys
import json
import time
import argparse
import logging
import html as html_module
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("web_dashboard")
logger.addHandler(logging.NullHandler())

__version__ = "8.11-patched"
__all__ = ["run_server", "DashboardHandler", "__version__"]

MSK_TZ = timezone(timedelta(hours=3))


def _now_msk() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")

def _esc(value):
    """HTML-escape динамических значений — защита от XSS."""
    if value is None:
        return ""
    return html_module.escape(str(value))



# ── Lazy-импорты ─────────────────────────────────────────────

def _get_hub():
    try:
        import gatekeeper_hub
        return gatekeeper_hub
    except ImportError as e:
        logger.error(f"gatekeeper_hub не найден: {e}")
        return None


def _get_redis_hub():
    try:
        import redis_hub
        return redis_hub
    except ImportError as e:
        logger.error(f"redis_hub не найден: {e}")
        return None


def _get_config():
    try:
        import redis_config
        return redis_config
    except ImportError:
        return None


# ── Сбор данных ──────────────────────────────────────────────

_health_cache = None
_health_cache_ts = 0
_HEALTH_CACHE_TTL = 60  # seconds

def _collect_health() -> dict:
    """Сбор состояния системы — §12 формат. Кеширование с TTL 60s."""
    global _health_cache, _health_cache_ts
    now = time.time()
    if _health_cache is not None and (now - _health_cache_ts) < _HEALTH_CACHE_TTL:
        return _health_cache
    hub = _get_hub()
    rdb = _get_redis_hub()
    cfg = _get_config()

    health = {
        "timestamp": _now_msk(),
        "redis_available": False,
        "circuit_breaker": "unknown",
        "matches_total": 0,
        "matches_with_odds": 0,
        "matches_with_predictions": 0,
        "matches_with_h2h": 0,
        "matches_with_stats": 0,
        "matches_with_value": 0,
        "collectors": {},
        "cleanup": {},
        "errors": [],
    }

    if cfg:
        try:
            health["redis_available"] = cfg.is_redis_configured()
        except Exception:
            pass

    if rdb:
        try:
            cb = rdb.get_circuit_breaker_status()
            health["circuit_breaker"] = cb
        except Exception as e:
            health["errors"].append(f"circuit_breaker: {e}")

    if hub:
        try:
            init = hub.run_initialization(collector="web_dashboard")
            health["redis_available"] = init.get("redis_available", False)
            health["cleanup"] = {
                "migration_count": init.get("migration_count", 0),
                "cleanup_count": init.get("cleanup_count", 0),
                "init_latency_ms": init.get("init_latency_ms", 0),
            }
        except Exception as e:
            health["errors"].append(f"init: {e}")

        try:
            # FIX-AUDIT-v9.3: Defensive dict conversion
            _raw = hub.get_matches_by_date_range()
            matches = _raw if isinstance(_raw, dict) else {m.get("canonical_id", ""): m for m in (_raw or [])}
            health["matches_total"] = len(matches)
            for m in matches.values():
                if isinstance(m, dict):
                    if m.get("odds") and m["odds"].get("current"):
                        health["matches_with_odds"] += 1
                    if m.get("predictions"):
                        health["matches_with_predictions"] += 1
                    if m.get("h2h"):
                        health["matches_with_h2h"] += 1
                    if m.get("stats"):
                        health["matches_with_stats"] += 1
                    va = m.get("value_analysis", {})
                    if va and va.get("has_value"):
                        health["matches_with_value"] += 1
        except Exception as e:
            health["errors"].append(f"matches: {e}")

    # ── §20.7: авто-сбор статусов коллекторов через MODULE_REGISTRY ──
    if hub and hasattr(hub, "MODULE_REGISTRY"):
        registry = hub.MODULE_REGISTRY
    else:
        registry = {}

    collector_names = list(registry.keys())
    if not collector_names:
        # Fallback: хардкод из §5
        collector_names = ["sharpapi", "odds_api", "bzzoiro", "propline", "football_data"]

    rh = _get_redis_hub()
    for name in collector_names:
        meta = None
        if rh:
            try:
                meta = rh.get_key(f"{name}:meta")
                if isinstance(meta, str):
                    meta = json.loads(meta)
            except Exception:
                meta = None

        reg_info = registry.get(name, {})
        health["collectors"][name] = {
            "role": reg_info.get("role", "unknown"),
            "writes": reg_info.get("writes", []),
            "reads": reg_info.get("reads", []),
            "last_run": meta.get("last_run", "") if meta else "",
            "error_count": meta.get("error_count", 0) if meta else 0,
            "total_events": meta.get("total_events", 0) if meta else 0,
            "stored_matches": meta.get("stored_matches", 0) if meta else 0,
            "status": "+" if meta and meta.get("last_run") else "-",
        }

    _health_cache = health
    _health_cache_ts = now
    return health


def _collect_matches(limit=50, offset=0) -> dict:
    """Список live-матчей с пагинацией."""
    hub = _get_hub()
    if not hub:
        return {"matches": [], "total": 0, "error": "hub unavailable"}

    try:
        # FIX-AUDIT-v9.3: Defensive dict conversion
        _raw = hub.get_matches_by_date_range()
        all_matches = _raw if isinstance(_raw, dict) else {m.get("canonical_id", ""): m for m in (_raw or [])}
        total = len(all_matches)
        items = []
        for i, (cid, m) in enumerate(all_matches.items()):
            if i < offset:
                continue
            if i >= offset + limit:
                break
            if not isinstance(m, dict):
                continue
            items.append({
                "canonical_id": cid,
                "home": m.get("home_clean", ""),
                "away": m.get("away_clean", ""),
                "date": m.get("date_utc", ""),
                "competition": m.get("competition", ""),
                "status": m.get("status", ""),
                "has_odds": bool(m.get("odds") and m["odds"].get("current")),
                "has_predictions": bool(m.get("predictions")),
                "has_h2h": bool(m.get("h2h")),
                "has_stats": bool(m.get("stats")),
                "has_value": bool(m.get("value_analysis", {}).get("has_value")),
                "sources": m.get("sources", []),
                "updated_at": m.get("updated_at", ""),
            })
        return {"matches": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        return {"matches": [], "total": 0, "error": str(e)}


def _collect_quality() -> dict:
    """Сводка качества данных — §20.4."""
    hub = _get_hub()
    if not hub:
        return {"error": "hub unavailable"}

    try:
        if not hasattr(hub, "validate_match_quality"):
            return {"error": "validate_match_quality not implemented"}
        # FIX-AUDIT-v9.3: Defensive dict conversion
        _raw = hub.get_matches_by_date_range()
        matches = _raw if isinstance(_raw, dict) else {m.get("canonical_id", ""): m for m in (_raw or [])}
        scores = []
        issues_count = 0
        warnings_count = 0
        worst = []
        for cid, m in matches.items():
            if not isinstance(m, dict):
                continue
            result = hub.validate_match_quality(m)
            scores.append(result["score"])
            if result["issues"]:
                issues_count += 1
            if result["warnings"]:
                warnings_count += 1
            if result["score"] < 70:
                worst.append({
                    "canonical_id": cid,
                    "score": result["score"],
                    "issues": result["issues"],
                    "warnings": result["warnings"],
                })
        avg = sum(scores) / len(scores) if scores else 0
        return {
            "total_checked": len(scores),
            "avg_score": round(avg, 1),
            "valid": sum(1 for s in scores if s >= 70),
            "invalid": sum(1 for s in scores if s < 70),
            "issues_count": issues_count,
            "warnings_count": warnings_count,
            "worst_matches": sorted(worst, key=lambda x: x["score"])[:10],
        }
    except Exception as e:
        return {"error": str(e)}


def _collect_registry() -> dict:
    """Реестр модулей — §20.7."""
    hub = _get_hub()
    if not hub:
        return {"modules": {}, "error": "hub unavailable"}

    try:
        if hasattr(hub, "get_module_registry"):
            return hub.get_module_registry()
        elif hasattr(hub, "MODULE_REGISTRY"):
            return {"modules": hub.MODULE_REGISTRY}
        return {"modules": {}, "error": "registry not implemented"}
    except Exception as e:
        return {"modules": {}, "error": str(e)}


def _collect_system() -> dict:
    """system:health из Redis."""
    rh = _get_redis_hub()
    if not rh:
        return {"error": "redis_hub unavailable"}

    try:
        raw = rh.get_key("system:health")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw or {}
    except Exception as e:
        return {"error": str(e)}


# ── HTML-дашборд ─────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GatekeeperAI Monitor</title>
<meta http-equiv="refresh" content="30">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #0d1117; color: #c9d1d9; padding: 20px;
  }
  h1 { color: #58a6ff; font-size: 1.4em; margin-bottom: 16px; }
  h2 { color: #8b949e; font-size: 1.1em; margin: 20px 0 10px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; }
  .card .label { color: #8b949e; font-size: 0.8em; text-transform: uppercase; margin-bottom: 4px; }
  .card .value { font-size: 1.6em; font-weight: 600; color: #58a6ff; }
  .card .value.green { color: #3fb950; }
  .card .value.red { color: #f85149; }
  .card .value.yellow { color: #d29922; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.85em; }
  th { color: #8b949e; font-weight: 600; text-transform: uppercase; font-size: 0.75em; }
  tr:hover { background: #161b22; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
  .badge.ok { background: #1a3417; color: #3fb950; }
  .badge.err { background: #3d1114; color: #f85149; }
  .badge.warn { background: #3d2a00; color: #d29922; }
  .ts { color: #484f58; font-size: 0.8em; text-align: right; margin-bottom: 16px; }
  .err-list { color: #f85149; font-size: 0.85em; margin-top: 8px; }
  .err-list li { margin-left: 20px; }
</style>
</head>
<body>
<h1>GatekeeperAI Monitor <span style="font-size:0.6em;color:#484f58">v{version}</span></h1>
<div class="ts">Updated: {timestamp} (auto-refresh 30s)</div>

<h2>System</h2>
<div class="grid">
  <div class="card"><div class="label">Redis</div><div class="value {redis_class}">{redis_status}</div></div>
  <div class="card"><div class="label">Circuit Breaker</div><div class="value {cb_class}">{cb_status}</div></div>
  <div class="card"><div class="label">Cleanup (last)</div><div class="value">{cleanup_count}</div></div>
  <div class="card"><div class="label">Init latency</div><div class="value">{init_latency}ms</div></div>
</div>

<h2>Matches</h2>
<div class="grid">
  <div class="card"><div class="label">Total</div><div class="value">{matches_total}</div></div>
  <div class="card"><div class="label">With Odds</div><div class="value green">{matches_odds}</div></div>
  <div class="card"><div class="label">Predictions</div><div class="value">{matches_pred}</div></div>
  <div class="card"><div class="label">H2H</div><div class="value">{matches_h2h}</div></div>
  <div class="card"><div class="label">Stats</div><div class="value">{matches_stats}</div></div>
  <div class="card"><div class="label">Value Bets</div><div class="value yellow">{matches_value}</div></div>
</div>

<h2>Collectors</h2>
<table>
  <tr><th>Name</th><th>Role</th><th>Status</th><th>Last Run</th><th>Events</th><th>Stored</th><th>Errors</th></tr>
  {collectors_rows}
</table>

{errors_section}

<p style="color:#484f58;font-size:0.8em;margin-top:30px">
  API: <a href="/api/health" style="color:#58a6ff">/api/health</a> ·
  <a href="/api/matches" style="color:#58a6ff">/api/matches</a> ·
  <a href="/api/collectors" style="color:#58a6ff">/api/collectors</a> ·
  <a href="/api/quality" style="color:#58a6ff">/api/quality</a> ·
  <a href="/api/registry" style="color:#58a6ff">/api/registry</a> ·
  <a href="/api/system" style="color:#58a6ff">/api/system</a>
</p>
</body>
</html>"""


def _render_html(health: dict) -> str:
    """Рендер HTML-дашборда из собранных данных."""
    redis_ok = health.get("redis_available", False)
    cb = health.get("circuit_breaker", "unknown")
    cleanup = health.get("cleanup", {})

    rows = []
    for name, info in sorted(health.get("collectors", {}).items()):
        status = info.get("status", "-")
        badge_cls = "ok" if status == "+" else "err"
        rows.append(
            f"<tr>"
            f"<td>{_esc(name)}</td>"
            f"<td>{info.get('role', '')}</td>"
            f"<td><span class='badge {badge_cls}'>{_esc(status)}</span></td>"
            f"<td>{info.get('last_run', '—')}</td>"
            f"<td>{info.get('total_events', 0)}</td>"
            f"<td>{info.get('stored_matches', 0)}</td>"
            f"<td>{info.get('error_count', 0)}</td>"
            f"</tr>"
        )

    errors = health.get("errors", [])
    errors_html = ""
    if errors:
        items = "".join(f"<li>{e}</li>" for e in errors)
        errors_html = f"<h2>Errors</h2><ul class='err-list'>{items}</ul>"

    return HTML_TEMPLATE.format(
        version=__version__,
        timestamp=health.get("timestamp", _now_msk()),
        redis_status="ONLINE" if redis_ok else "OFFLINE",
        redis_class="green" if redis_ok else "red",
        cb_status=cb,
        cb_class="green" if cb == "closed" else ("yellow" if cb == "half-open" else "red"),
        cleanup_count=cleanup.get("cleanup_count", 0),
        init_latency=cleanup.get("init_latency_ms", 0),
        matches_total=health.get("matches_total", 0),
        matches_odds=health.get("matches_with_odds", 0),
        matches_pred=health.get("matches_with_predictions", 0),
        matches_h2h=health.get("matches_with_h2h", 0),
        matches_stats=health.get("matches_with_stats", 0),
        matches_value=health.get("matches_with_value", 0),
        collectors_rows="\n".join(rows),
        errors_section=errors_html,
    )


# ── HTTP Handler ─────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler для веб-панели."""

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/dashboard":
            health = _collect_health()
            html = _render_html(health)
            self._send_html(html)

        elif path == "/api/health":
            self._send_json(_collect_health())

        elif path == "/api/matches":
            limit = int(qs.get("limit", ["50"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            self._send_json(_collect_matches(limit, offset))

        elif path == "/api/collectors":
            health = _collect_health()
            self._send_json({"collectors": health.get("collectors", {})})

        elif path == "/api/quality":
            self._send_json(_collect_quality())

        elif path == "/api/registry":
            self._send_json(_collect_registry())

        elif path == "/api/system":
            self._send_json(_collect_system())

        else:
            self._send_json({"error": "not found", "path": path}, 404)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()


# ── Entry point ──────────────────────────────────────────────

def run_server(host="0.0.0.0", port=8080):
    """Запуск HTTP-сервера."""
    server = HTTPServer((host, port), DashboardHandler)
    logger.info(f"GatekeeperAI Monitor → http://{host}:{port}")
    logger.info(f"Version: {__version__}")
    print(f"\n  GatekeeperAI Monitor")
    print(f"  → http://{host}:{port}")
    print(f"  → API: /api/health, /api/matches, /api/collectors, /api/quality, /api/registry, /api/system")
    print(f"  → Auto-refresh: 30s\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="GatekeeperAI Web Monitor")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
