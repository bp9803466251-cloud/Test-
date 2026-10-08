# Team Registry — Testing Guide

## Files

| File | Purpose |
|------|---------|
| `team_registry.py` | Registry + `clean_team_name()` function |
| `test_gluing.py` | Automated test suite (~17000 checks) |
| `Makefile` | Short commands for common tasks |
| `.github/workflows/test_gluing_ci.yml` | CI/CD via GitHub Actions |

## Quick Start

```bash
# Run all tests
python test_gluing.py

# Or via Makefile
make test

# Pre-deploy check (syntax + tests)
make all
```

## Test Categories

### 1. Self-consistency (~2900 checks)
Every alias in `TEAM_ALIASES` must resolve to its declared canonical via `clean_team_name()`.

### 2. Cross-source gluing (~20 pairs)
Names from different APIs (OddsAPI, Bzzoiro, SharpAPI, PropLine) must glue to the same canonical.

### 3. Safety (~11 pairs)
Different teams must NOT glue together. Example: `inter_milan` != `internacional`.

### 4. Normalization (~15 checks)
All 10 steps of `clean_team_name()` work correctly:
- Step 1: lowercase + strip
- Step 2: NFD diacritics (a -> a, e -> e, etc.)
- Step 2a: special chars (o-stroke -> o, ae-ligature -> ae, etc.)
- Step 2b: underscore -> space lookup
- Step 2c: hyphen -> space lookup
- Step 3: suffix strip (fc, cf, sc, afc, etc.)
- Step 3b: underscore suffix strip
- Step 3d: hyphen suffix strip
- Step 4: slash -> underscore
- Step 4c: bracket normalization [w] -> (w)
- Step 5: fallback (spaces/hyphens -> underscores)

### 5. Structural integrity (~14000 checks)
- No empty keys
- No leading/trailing spaces in keys
- No empty canonical values
- No spaces in canonical values
- No chained aliases

### 6. Source coverage (4 sources)
Sample teams from each API resolve to known canonicals.

## Deployment Workflow

```bash
# 1. Pre-deploy: verify tests pass
make all

# 2. Run collectors (recomputes canonical_id)
make collectors

# 3. Check for duplicate matches
make dump

# 4. Run pipeline
make pipeline

# 5. Verify Value output
```

## When to Run Tests

| Event | Action |
|-------|--------|
| Added new aliases | `make test` |
| Updated API (new teams appeared) | `make test` + `make dump` |
| Weekly maintenance | Full cycle: test -> collectors -> dump -> pipeline |
| Before release | Full cycle + manual Value check |

## Failure Troubleshooting

| Test Category | Likely Cause | Fix |
|---------------|-------------|-----|
| Self-consistency | Diacritics in key, special chars | Fix key in `TEAM_ALIASES` |
| Cross-source | Missing alias from one API | Add the alias |
| Safety | Short alias collides | Remove or qualify the alias |
| Normalization | Step order or logic bug | Check `clean_team_name()` implementation |
| Structural | Empty key or space in key | Fix the dict entry |
| Source coverage | Team from API not in registry | Add team + aliases |

## CI/CD

GitHub Actions runs `test_gluing.py` on every push/PR that touches `team_registry.py` or `test_gluing.py`. Tests run on Python 3.10, 3.11, and 3.12.

**Exit code 0** = tests passed, PR can merge.
**Exit code 1** = tests failed, PR is blocked.

## Stats (v9.3-audited)

- **2885 aliases**
- **1509 unique canonical names**
- **58 countries/leagues**
- **10 normalization steps**
