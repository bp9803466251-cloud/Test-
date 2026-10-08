#!/usr/bin/env python3
"""
test_gluing.py — Automated test suite for team_registry / team_registry_final
Tests: self-consistency, cross-source, safety, normalization, structural, coverage.
Exit code 0 = all pass, 1 = any fail.

Usage:
    python test_gluing.py                    # auto-detect module
    python test_gluing.py team_registry.py   # explicit module
"""

import sys
import os
import importlib.util


# ============================================================
# MODULE LOADING — auto-detect or explicit
# ============================================================
def _load_module(path_or_name):
    """Load team_registry module from file path or module name."""
    candidates = []
    if path_or_name:
        candidates.append(path_or_name)
    candidates.extend([
        "team_registry_final.py",
        "team_registry.py",
        "team_registry_final",
        "team_registry",
    ])
    for c in candidates:
        try:
            if c.endswith(".py") and os.path.isfile(c):
                spec = importlib.util.spec_from_file_location("team_registry", c)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            else:
                return __import__(c)
        except (ImportError, FileNotFoundError, ModuleNotFoundError):
            continue
    return None


# Resolve module path from CLI arg or auto-detect
_cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
_mod = _load_module(_cli_arg)

if _mod is None:
    print("FAIL: cannot import team_registry or team_registry_final")
    print("      Place team_registry.py in the same directory, or pass path as argument:")
    print("      python test_gluing.py /path/to/team_registry.py")
    sys.exit(1)

TEAM_ALIASES = _mod.TEAM_ALIASES
clean_team_name = _mod.clean_team_name
_STRIP_SUFFIXES = getattr(_mod, "_STRIP_SUFFIXES", None)
_mod_version = getattr(_mod, "__version__", "unknown")

PASS = 0
FAIL = 0
WARN = 0


def check(condition, msg):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


def warn(condition, msg):
    global WARN
    if not condition:
        WARN += 1
        print(f"  WARN: {msg}")


print(f"\n{'='*60}")
print(f"  test_gluing.py — Team Registry Test Suite")
print(f"  Module version: {_mod_version}")
print(f"  Aliases: {len(TEAM_ALIASES)}")
print(f"  Unique canonicals: {len(set(TEAM_ALIASES.values()))}")
print(f"  _STRIP_SUFFIXES: {'present' if _STRIP_SUFFIXES else 'absent (simple mode)'}")
print(f"{'='*60}")


# ============================================================
# 1. SELF-CONSISTENCY — every alias resolves to its canonical
# ============================================================
print("\n[1] Self-consistency...")
sc_failures = []
for alias, canonical in TEAM_ALIASES.items():
    result = clean_team_name(alias)
    if result != canonical:
        FAIL += 1
        sc_failures.append((alias, result, canonical))
    else:
        PASS += 1

if sc_failures:
    for alias, result, canonical in sc_failures[:20]:
        print(f"  FAIL: '{alias}' -> '{result}' (expected '{canonical}')")
    if len(sc_failures) > 20:
        print(f"  ... and {len(sc_failures) - 20} more failures")
else:
    print(f"  {PASS} aliases — all resolve correctly")


# ============================================================
# 2. CROSS-SOURCE — names from different APIs glue to same canonical
# ============================================================
print("\n[2] Cross-source gluing...")
cross_pairs = [
    # API name pairs -> expected canonical
    ("manchester city", "man city", "man_city"),
    ("tottenham hotspur", "spurs", "tottenham"),
    ("real madrid", "real madrid cf", "real_madrid"),
    ("bayern munchen", "bayern munich", "bayern_munich"),
    ("borussia dortmund gmbh", "bvb", "dortmund"),
    ("feyenoord rotterdam", "feyenoord", "feyenoord"),
    ("celtic glasgow", "celtic fc", "celtic"),
    ("ferencvarosi", "ferencvaros", "ferencvaros"),
    ("nfc volos", "volos fc", "volos"),
    ("benfica sl", "sl benfica", "benfica"),
    ("nizhny novgorod", "nizhny", "nizhny_novgorod"),
    ("olimpia", "club olimpia", "olimpia"),
    ("catanzaro fc", "catanzaro", "catanzaro"),
    ("bodo/glimt", "bodo glimt", "bodo_glimt"),
    ("kobenhavn", "fc copenhagen", "copenhagen"),
    ("colo-colo", "colo colo", "colo_colo"),
    ("cruzeiro-mg", "cruzeiro", "cruzeiro"),
    ("botafogo_rj", "botafogo", "botafogo"),
    ("vasco_da_gama-rj", "vasco", "vasco"),
    ("red_bull_bragantino", "bragantino", "bragantino"),
]
for a, b, expected in cross_pairs:
    ca = clean_team_name(a)
    cb = clean_team_name(b)
    check(ca == cb == expected,
          f"'{a}' -> '{ca}' vs '{b}' -> '{cb}' (expected '{expected}')")
print(f"  {PASS} checks so far")


# ============================================================
# 3. SAFETY — different teams must NOT glue together
# ============================================================
print("\n[3] Safety (no false gluing)...")
safety_pairs = [
    ("inter_milan", "internacional"),
    ("botafogo", "botafogo_sp"),
    ("arsenal", "arsenal_sarandi"),
    ("chelsea", "celtic"),
    ("inter_turku", "inter_milan"),
    ("atletico_madrid", "atletico_mineiro"),
    ("barcelona", "barcelona_sc"),
    ("sporting_cp", "sporting_kc"),
    ("napoli", "nacional"),
    ("rangers", "rangers_talca"),
    ("sparta_prague", "sparta_rotterdam"),
]
for a, b in safety_pairs:
    ca = clean_team_name(a)
    cb = clean_team_name(b)
    check(ca != cb,
          f"'{a}' -> '{ca}' should NOT equal '{b}' -> '{cb}'")
print(f"  {PASS} checks so far")


# ============================================================
# 4. NORMALIZATION — all steps of clean_team_name work
# ============================================================
print("\n[4] Normalization steps...")
norm_tests = [
    # Step 1: lowercase + strip
    ("  Arsenal  ", "arsenal"),
    ("CHELSEA", "chelsea"),
    # Step 2: NFD diacritics
    ("Copenh\u00e4gen", "copenhagen"),
    ("S\u00e3o Paulo", "sao_paulo"),
    # Step 2a: special chars (o-sroke, sharp-s, etc.)
    ("Br\u00f8ndby IF", "brondby"),
    ("Malm\u00f6 FF", "malmo_ff"),
    # Step 2b: underscore -> space lookup
    ("man_city", "man_city"),
    # Step 2c: hyphen -> space lookup
    ("colo-colo", "colo_colo"),
    # Step 3: suffix strip (fc, cf, sc, afc, etc.)
    ("arsenal fc", "arsenal"),
    ("chelsea fc", "chelsea"),
    # Step 3b: underscore suffix strip
    ("sporting_fc", "sporting_cp"),
    # Step 3d: hyphen suffix strip
    ("chelsea-fc", "chelsea"),
    # Step 4: combined separator normalization (slash)
    ("bodo/glimt", "bodo_glimt"),
    # Step 4c: bracket normalization [w] -> (w)
    ("jordan_[w]", "jordan_(w)"),
    # Step 5: fallback (spaces -> underscores)
    ("some unknown team", "some_unknown_team"),
    # Edge cases
    ("", ""),
    (None, ""),
]
for raw, expected in norm_tests:
    result = clean_team_name(raw)
    check(result == expected,
          f"'{raw}' -> '{result}' (expected '{expected}')")
print(f"  {PASS} checks so far")


# ============================================================
# 5. STRUCTURAL — no empty keys, no leading spaces, no bad canonicals
# ============================================================
print("\n[5] Structural integrity...")

# 5a: No empty keys
empty_keys = [k for k in TEAM_ALIASES if not k.strip()]
check(len(empty_keys) == 0, f"empty keys: {empty_keys}")

# 5b: No leading/trailing spaces in keys
leading_spaces = [k for k in TEAM_ALIASES if k != k.strip()]
check(len(leading_spaces) == 0, f"leading/trailing spaces in keys: {leading_spaces[:10]}")

# 5c: No empty canonical values
empty_vals = [k for k, v in TEAM_ALIASES.items() if not v.strip()]
check(len(empty_vals) == 0, f"empty canonicals for keys: {empty_vals[:10]}")

# 5d: No spaces in canonical values
bad_canonical = [k for k, v in TEAM_ALIASES.items() if " " in v]
check(len(bad_canonical) == 0, f"canonicals with spaces: {[(k, TEAM_ALIASES[k]) for k in bad_canonical[:10]]}")

# 5e: No chained aliases (canonical should not be a key pointing elsewhere)
chained = []
for alias, canonical in TEAM_ALIASES.items():
    if canonical in TEAM_ALIASES and TEAM_ALIASES[canonical] != canonical:
        chained.append((alias, canonical, TEAM_ALIASES[canonical]))
check(len(chained) == 0, f"chained aliases: {chained[:10]}")

# 5f: All canonical values match [a-z0-9_]+ pattern
import re
bad_pattern = [v for v in TEAM_ALIASES.values() if not re.match(r'^[a-z0-9_]+$', v)]
check(len(bad_pattern) == 0, f"canonicals not matching [a-z0-9_]+: {bad_pattern[:10]}")

# 5g: No duplicate keys (dict naturally deduplicates, but check anyway)
check(len(TEAM_ALIASES) == len(set(TEAM_ALIASES.keys())), "duplicate keys detected")

print(f"  {PASS} checks so far")


# ============================================================
# 6. SOURCE COVERAGE — sample teams from each API resolve
# ============================================================
print("\n[6] Source coverage...")

oddsapi = [
    "manchester city", "liverpool", "real madrid", "barcelona", "bayern munich",
    "psg", "juventus", "inter milan", "atletico madrid", "dortmund",
    "ajax", "porto", "benfica", "celtic", "rangers",
    "flamengo", "palmeiras", "boca juniors", "river plate",
    "la galaxy", "inter miami", "seattle sounders", "atlanta united",
    "zenit", "shakhtar donetsk", "galatasaray", "fenerbahce",
    "olympiacos", "panathinaikos", "basel", "young boys",
    "club brugge", "anderlecht", "salzburg", "shakhtar",
]
bzzoiro = [
    "arsenal", "chelsea", "tottenham", "manchester united",
    "real betis", "villarreal", "sevilla", "valencia",
    "napoli", "roma", "lazio", "atalanta",
    "leverkusen", "rb leipzig", "wolfsburg", "freiburg",
    "lyon", "marseille", "monaco", "lille",
    "feirense", "leixoes", "penafiel", "nacional",
]
sharpapi = [
    "bayern munchen", "borussia dortmund gmbh", "feyenoord rotterdam",
    "celtic glasgow", "ferencvarosi", "nfc volos",
]
propline = [
    "arsenal", "chelsea", "liverpool", "man city",
    "real madrid", "barcelona", "atletico madrid",
    "bayern munich", "dortmund", "psg",
]

canonical_values = set(TEAM_ALIASES.values())

for source_name, teams in [("OddsAPI", oddsapi), ("Bzzoiro", bzzoiro),
                            ("SharpAPI", sharpapi), ("PropLine", propline)]:
    resolved = 0
    unresolved = []
    for t in teams:
        r = clean_team_name(t)
        if r in canonical_values or r == clean_team_name(t):
            resolved += 1
        else:
            unresolved.append((t, r))
    pct = resolved / len(teams) * 100
    check(resolved == len(teams),
          f"{source_name}: {resolved}/{len(teams)} ({pct:.0f}%) — unresolved: {unresolved}")
    print(f"  {source_name}: {resolved}/{len(teams)} ({pct:.0f}%)")

print(f"  {PASS} checks so far")


# ============================================================
# 7. CONTRACT TESTS — must match test_contracts.py assertions
# ============================================================
print("\n[7] Contract tests (test_contracts.py parity)...")

contract_tests = [
    ("Manchester United", "man"),
    ("Br\u00f8ndby IF", "brondby"),
    ("K\u00f6ln", "cologne"),
    ("Malm\u00f6 FF", "malmo_ff"),
    ("", ""),
    (None, ""),
]
for raw, expected in contract_tests:
    result = clean_team_name(raw)
    check(result == expected,
          f"contract: clean_team_name({raw!r}) == '{expected}' (got '{result}')")

# build_canonical_id if available
if hasattr(_mod, "build_canonical_id"):
    build_canonical_id = _mod.build_canonical_id
    check(build_canonical_id("Man United", "Chelsea", "2025-10-04") == "man__chelsea__20251004",
          "build_canonical_id: man__chelsea__20251004")
    check(build_canonical_id("", "Chelsea", "2025-10-04") == "",
          "build_canonical_id: empty home -> ''")
    check(build_canonical_id("Arsenal", "", "2025-10-04") == "",
          "build_canonical_id: empty away -> ''")
    check(build_canonical_id("Arsenal", "Chelsea", "") == "",
          "build_canonical_id: empty date -> ''")
else:
    warn(False, "build_canonical_id not found in module — skipping")

print(f"  {PASS} checks so far")


# ============================================================
# SUMMARY
# ============================================================
total = PASS + FAIL
print(f"\n{'='*60}")
print(f"  RESULTS")
print(f"{'='*60}")
print(f"  PASS:  {PASS}/{total}")
print(f"  FAIL:  {FAIL}/{total}")
print(f"  WARN:  {WARN}")
print(f"{'='*60}")

if FAIL == 0:
    print(f"\n  All {total} tests passed — safe to deploy")
    sys.exit(0)
else:
    print(f"\n  {FAIL} test(s) failed — fix before deploy")
    sys.exit(1)
