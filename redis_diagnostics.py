#!/usr/bin/env python3
"""redis_diagnostics.py — единая диагностическая утилита GatekeeperAI.
Все флаги из §18.4 гида."""

import sys, json, argparse

def main():
    p = argparse.ArgumentParser(description="GatekeeperAI Diagnostics")
    
    # ── Существующие флаги ──
    p.add_argument("--validate", action="store_true", help="Схемная валидация (§19.1)")
    p.add_argument("--migrate-schema", metavar="VERSION", help="Миграция схемы (§20.2)")
    p.add_argument("--quality", action="store_true", help="Проверка качества (§20.4)")
    p.add_argument("--health", action="store_true", help="Health monitoring (§21.3)")
    p.add_argument("--reconcile", action="store_true", help="Реконсиляция (§21.5)")
    p.add_argument("--fix", action="store_true", help="Авто-исправление при --reconcile (§21.5)")
    p.add_argument("--audit", action="store_true", help="Аудит-лог (§22.1)")
    p.add_argument("--dlq", action="store_true", help="Dead Letter Queue (§22.4)")
    p.add_argument("--dlq-replay", action="store_true", help="Переобработка DLQ (§22.4)")
    p.add_argument("--batch-stats", action="store_true", help="Статистика батчинга (§22.6)")
    p.add_argument("--diff", metavar="CID", help="История изменений матча (§22.6)")
    p.add_argument("--retention", action="store_true", help="Статус retention (§22.7)")
    p.add_argument("--hub-version", action="store_true", help="Версия API хаба (§22.8)")
    
    # ── Флаги §23 ──
    p.add_argument("--odds-conflicts", metavar="CID", help="Конфликты коэффициентов (§23.1)")
    p.add_argument("--canary", metavar="COLLECTOR", help="Теневой запуск (§23.8)")
    p.add_argument("--canary-cleanup", action="store_true", help="Очистка canary (§23.8)")
    p.add_argument("--lineage", action="store_true", help="Граф зависимостей (§23.5)")
    p.add_argument("--dashboard", action="store_true", help="Текстовый дашборд (§23.6)")
    
    # ── Флаги §24 ──
    p.add_argument("--state-stats", action="store_true", help="Гистограмма статусов (§24.1)")
    p.add_argument("--config-check", action="store_true", help="Валидация конфигурации (§24.3)")
    p.add_argument("--env", action="store_true", help="Текущее окружение (§24.4)")
    p.add_argument("--metrics", action="store_true", help="Сводка метрик (§24.5)")
    p.add_argument("--export", action="store_true", help="Экспорт данных (§24.6)")
    p.add_argument("--import", metavar="FILE", help="Импорт данных (§24.6)")
    p.add_argument("--orchestration", action="store_true", help="Граф запуска (§24.7)")
    p.add_argument("--drift", action="store_true", help="Schema drift (§24.8)")
    p.add_argument("--full", action="store_true", help="Полное сканирование (для --drift)")
    
    # ── Фильтры ──
    p.add_argument("--source", metavar="NAME", help="Фильтр по источнику")
    p.add_argument("--action", metavar="TYPE", help="Фильтр по действию")
    p.add_argument("--namespace", metavar="NS", default="live", help="Namespace")
    p.add_argument("--league", metavar="NAME", help="Фильтр по лиге")
    
    args = p.parse_args()
    
    # ── Обработка ──
    if args.hub_version:
        from gatekeeper_hub import __version__
        print(f"GatekeeperAI Hub version: {__version__}")
        return
    
    if args.health:
        from gatekeeper_ops import check_health
        h = check_health()
        print(json.dumps(h, indent=2, ensure_ascii=False))
        return
    
    if args.audit:
        from gatekeeper_lifecycle import query_audit_log
        events = query_audit_log(source=args.source, action=args.action, limit=100)
        for e in events:
            print(f"[{e['timestamp']}] {e['action']} {e.get('canonical_id','')} by {e['source']}")
        return
    
    if args.dlq:
        from gatekeeper_lifecycle import get_dlq
        items = get_dlq()
        print(f"DLQ: {len(items)} items")
        for item in items:
            print(f"  {item['canonical_id']} — {item['error']}")
        return
    
    if args.dlq_replay:
        from gatekeeper_lifecycle import replay_dlq
        def fix_cb(cid, section, data, source):
            print(f"  Replaying {cid}...")
        r = replay_dlq(fix_cb)
        print(f"Replayed: {r['replayed']}, Failed: {r['failed']}, Remaining: {r['remaining']}")
        return
    
    if args.quality:
        from gatekeeper_ops import validate_match_quality
        from test_fixtures import CANONICAL_LIVE_MATCH
        r = validate_match_quality(CANONICAL_LIVE_MATCH)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    
    if args.reconcile:
        from gatekeeper_ops import reconcile
        r = reconcile(dry_run=not args.fix)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    
    if args.retention:
        from gatekeeper_lifecycle import get_retention_status
        s = get_retention_status()
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return
    
    if args.dashboard:
        from gatekeeper_diagnostics import generate_dashboard, format_dashboard_text
        dash = generate_dashboard()
        print(format_dashboard_text(dash))
        return
    
    if args.config_check:
        from gatekeeper_config import validate_config, load_config
        cfg = load_config()
        errors = validate_config(cfg)
        if errors:
            print(f"Config errors: {len(errors)}")
            for e in errors:
                print(f"  ❌ {e}")
        else:
            print("✅ Configuration valid")
        return
    
    if args.env:
        from gatekeeper_config import get_env, get_env_config
        env = get_env()
        cfg = get_env_config()
        print(f"Environment: {env}")
        print(f"Namespace prefix: {cfg.get('namespace_prefix','')}")
        print(f"Redis URL env: {cfg.get('redis_url_env','?')}")
        return
    
    if args.metrics:
        from gatekeeper_hub import METRICS
        r = METRICS.report()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    
    if args.export:
        from gatekeeper_diagnostics import export_matches
        from test_fixtures import CANONICAL_LIVE_MATCH
        data = export_matches([CANONICAL_LIVE_MATCH], namespace=args.namespace)
        filename = f"export_{args.namespace}_{now_msk()[:10]}.json"
        with open(filename, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Exported {data['count']} matches to {filename}")
        return
    
    if getattr(args, 'import', None):
        from gatekeeper_diagnostics import import_matches
        with open(getattr(args, 'import'), "r") as f:
            data = json.load(f)
        r = import_matches(data, dry_run=True)
        print(f"Import (dry-run): {r['imported']} matches, {r['skipped']} skipped")
        return
    
    if args.orchestration:
        from gatekeeper_config import get_config
        cfg = get_config()
        orch = cfg.get("orchestration", {})
        print("Orchestration graph:")
        for step in orch.get("sequence", []):
            deps = step.get("depends_on", [])
            print(f"  {step['name']} ← depends_on: {deps if deps else '[]'}")
        print(f"  cleanup_owner: {orch.get('cleanup_owner','?')}")
        return
    
    if args.drift:
        from gatekeeper_diagnostics import detect_schema_drift
        size = 999999 if args.full else 100
        r = detect_schema_drift(matches=None, sample_size=size)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    
    if args.lineage:
        from gatekeeper_config import get_config
        cfg = get_config()
        lineage = cfg.get("data_lineage", {})
        print("Data Lineage:")
        for collector, deps in lineage.items():
            print(f"  {collector}:")
            print(f"    provides: {deps.get('provides',[])}")
            print(f"    consumed_by: {deps.get('consumed_by',[])}")
        return
    
    if args.state_stats:
        from gatekeeper_hub import MATCH_STATES
        print("Match State Machine:")
        for state, cfg_s in MATCH_STATES.items():
            terminal = " (TERMINAL)" if cfg_s.get("terminal") else ""
            print(f"  {state}{terminal} → {cfg_s.get('transitions',[])}")
        return
    
    if args.migrate_schema:
        from gatekeeper_lifecycle import migrate_schema
        r = migrate_schema("v710", args.migrate_schema, dry_run=True)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    
    if args.diff:
        from gatekeeper_lifecycle import diff_match
        print(f"Diff for {args.diff} (stub — needs old/new match objects)")
        return
    
    if args.canary:
        print(f"Canary mode for {args.canary} — not implemented in CLI stub")
        return
    
    if args.canary_cleanup:
        print("Canary cleanup — not implemented in CLI stub")
        return
    
    if args.odds_conflicts:
        print(f"Odds conflicts for {args.odds_conflicts} — needs Redis connection")
        return
    
    # Default: show help
    p.print_help()

def now_msk():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=3))).isoformat()

if __name__ == "__main__":
    main()
