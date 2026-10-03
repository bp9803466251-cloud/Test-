# ═══════════════════════════════════════════════════════════════════════
# GatekeeperAI — Makefile (§21.7, §24.2)
# ═══════════════════════════════════════════════════════════════════════

PYTHON ?= python3
GK_CONFIG ?= gatekeeper_config.yaml

.PHONY: test test-contracts test-fixtures config-check check-version sync-shared clean

# ── Тесты (§21.8) ─────────────────────────────────────────────────────

test: test-contracts test-fixtures
	@echo "All tests passed ✅"

test-contracts:
	$(PYTHON) test_contracts.py

test-fixtures:
	$(PYTHON) test_fixtures.py

# ── Конфигурация (§24.3) ──────────────────────────────────────────────

config-check:
	$(PYTHON) -c "from gatekeeper_config import get_config, validate_config; \
	cfg = get_config(); \
	errors = validate_config(cfg); \
	print('Config valid ✅' if not errors else 'Config errors: ' + str(errors)); \
	exit(1 if errors else 0)"

# ── Версия хаба (§22.8, §24.2) ────────────────────────────────────────

check-version:
	$(PYTHON) -c "from gatekeeper_hub import __version__; print(f'Hub version: {__version__}')"

# ── Shared Code Distribution (§24.2) ──────────────────────────────────

sync-shared:
	@echo "Syncing shared code from gatekeeper-shared..."
	@TAG=$$(python3 -c "import yaml; print(yaml.safe_load(open('$(GK_CONFIG)')['shared_code']['pinned_version']))" 2>/dev/null || echo 'v8.9') && \
	echo "Pinned version: $$TAG" && \
	git clone --depth 1 --branch $$TAG https://github.com/gatekeeper-shared.git /tmp/gk-shared 2>/dev/null || true && \
	cp /tmp/gk-shared/gatekeeper_hub.py . 2>/dev/null || true && \
	cp /tmp/gk-shared/redis_hub.py . 2>/dev/null || true && \
	cp /tmp/gk-shared/search_module.py . 2>/dev/null || true && \
	cp /tmp/gk-shared/gatekeeper_config.yaml . 2>/dev/null || true && \
	rm -rf /tmp/gk-shared && \
	echo "Synced. Run 'make test' to verify."

# ── Очистка ───────────────────────────────────────────────────────────

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -f export_*.json canary_*.json 2>/dev/null || true
