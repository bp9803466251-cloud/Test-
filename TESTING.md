# Team Registry — Testing Guide

## Files

| File | Purpose |
|------|---------|
| `team_registry.py` | Registry + `clean_team_name()` function |
| `test_gluing.py` | Automated test suite (~2947 checks) |
| `.github/workflows/test_gluing_ci.yml` | CI/CD via GitHub Actions |

## Quick Start

```bash
# Run all tests
python test_gluing.py
```

## Test Categories

### 1. Self-consistency (~2883 checks)
Every alias in `TEAM_ALIASES` must resolve to its declared canonical via `clean_team_name()`.

### 2. Cross-source gluing (~20 pairs)
Names from different APIs (OddsAPI, Bzzoiro, SharpAPI, PropLine) must glue to the same canonical.

### 3. Safety (~11 pairs)
Different teams must NOT glue together. Example: `inter_milan` != `internacional`.

### 4. Normalization (~12 checks)
Basic steps of `clean_team_name()`:
- lowercase + strip
- Non-ASCII keys in registry (Brøndby, Köln, Malmö)
- None / empty handling
- Fallback (spaces -> underscores)

### 5. Structural integrity (~5 checks)
- No empty keys
- No leading/trailing spaces in keys
- No empty canonical values
- No spaces in canonical values
- Version check

### 6. Source coverage (~4 checks)
Sample teams from each API resolve to known canonicals.

### 7. Contract tests (~12 checks)
Tests from `test_contracts.py`:
- `clean_team_name` basic cases
- `build_canonical_id` with valid/empty inputs
- Registry size and version

## Deployment Workflow

```bash
# 1. Verify tests pass
python test_gluing.py

# 2. Run collectors (recomputes canonical_id)

# 3. Check for duplicate matches

# 4. Run pipeline
```

## CI/CD

GitHub Actions runs `test_gluing.py` on every push/PR that touches `team_registry.py` or `test_gluing.py`. Tests run on Python 3.10, 3.11, and 3.12.

**Exit code 0** = tests passed, PR can merge.
**Exit code 1** = tests failed, PR is blocked.

## Stats (v9.3-audited)

- **2 883 aliases**
- **1 509 unique canonical names**
- **58 countries/leagues**
