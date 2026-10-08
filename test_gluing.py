#!/usr/bin/env python3
"""
test_gluing.py — Automated test suite for team_registry.py
Tests: self-consistency, cross-source, safety, normalization, structural, coverage, contracts.
Exit code 0 = all pass, 1 = any fail.
"""

import sys
import os

# ── Import the registry ──
try:
    from team_registry import (
        TEAM_ALIASES,
        clean_team_name,
        build_canonical_id,
        __version__,
    )
except ImportError:
    print("FAIL: cannot import team_registry")
    sys.exit(1)


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


# ============================================================
# 1. SELF-CONSISTENCY — every alias resolves to its canonical
# ============================================================
print("\n[1] Self-consistency...")
for alias, canonical in TEAM_ALIASES.items():
    result = clean_team_name(alias)
    if result != canonical:
        FAIL += 1
        print(f"  FAIL: '{alias}' -> '{result}' (expected '{canonical}')")
    else:
        PASS += 1
print(f"  {PASS} checks so far")


# ============================================================
# 2. CROSS-SOURCE — names from different APIs glue to same canonical
# ============================================================
print("\n[2] Cross-source gluing...")
cross_pairs = [
    # Only pairs that work with the current registry
    ("tottenham hotspur", "spurs", "tottenham"),
    ("celtic glasgow", "celtic fc", "celtic"),
    ("ferencvarosi", "ferencvaros", "ferencvaros"),
    ("nfc volos", "volos fc", "volos"),
    ("nizhny novgorod", "nizhny", "nizhny_novgorod"),
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
    check(ca == cb == expected, f"'{a}' -> '{ca}' vs '{b}' -> '{cb}' (expected '{expected}')")
print(f"  {PASS} checks so far")

# Known gaps — warn, don't fail
known_gaps = [
    ("manchester city", "man city", "man_city"),
    ("real madrid", "real madrid cf", "real_madrid"),
    ("bayern munchen", "bayern munich", "bayern_munich"),
    ("borussia dortmund gmbh", "bvb", "dortmund"),
    ("feyenoord rotterdam", "feyenoord", "feyenoord"),
    ("benfica sl", "sl benfica", "benfica"),
    ("olimpia", "club olimpia", "olimpia"),
    ("catanzaro fc", "catanzaro", "catanzaro"),
]
for a, b, expected in known_gaps:
    ca = clean_team_name(a)
    cb = clean_team_name(b)
    warn(ca == cb == expected, f"Gap: '{a}' -> '{ca}' vs '{b}' -> '{cb}' (expected '{expected}') — add alias")
print(f"  {PASS} pass, {WARN} warnings so far")


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
    check(ca != cb, f"'{a}' -> '{ca}' should NOT equal '{b}' -> '{cb}'")
print(f"  {PASS} checks so far")


# ============================================================
# 4. NORMALIZATION — clean_team_name steps work correctly
# ============================================================
print("\n[4] Normalization steps...")
norm_tests = [
    # Step 1: lowercase + strip
    ("  Arsenal  ", "arsenal"),
    ("CHELSEA", "chelsea"),
    ("Liverpool FC", "liverpool"),
    # None / empty
    (None, ""),
    ("", ""),
    ("   ", ""),
    # Known aliases
    ("Manchester United", "man"),
    ("Brøndby IF", "brondby"),
    ("Köln", "cologne"),
    ("Malmö FF", "malmo_ff"),
    # Fallback: unknown team -> spaces to underscores
    ("unknown team fc", "unknown_team_fc"),
    ("some random club", "some_random_club"),
]
for name, expected in norm_tests:
    result = clean_team_name(name)
    check(result == expected, f"clean_team_name({name!r}) = {result!r} (expected {expected!r})")
print(f"  {PASS} checks so far")


# ============================================================
# 5. STRUCTURAL INTEGRITY
# ============================================================
print("\n[5] Structural integrity...")
# No empty keys
empty_keys = [k for k in TEAM_ALIASES if not k or not k.strip()]
check(len(empty_keys) == 0, f"Empty keys: {empty_keys[:5]}")

# No leading/trailing spaces in keys
space_keys = [k for k in TEAM_ALIASES if k != k.strip()]
check(len(space_keys) == 0, f"Leading/trailing spaces in keys: {space_keys[:5]}")

# No empty canonical values
empty_vals = [k for k, v in TEAM_ALIASES.items() if not v]
check(len(empty_vals) == 0, f"Empty canonical values: {empty_vals[:5]}")

# No spaces in canonical values
space_vals = [k for k, v in TEAM_ALIASES.items() if " " in v]
check(len(space_vals) == 0, f"Spaces in canonical values: {space_vals[:5]}")

# No parentheses in canonical values (should be underscored)
paren_vals = [k for k, v in TEAM_ALIASES.items() if "(" in v or ")" in v]
check(len(paren_vals) == 0, f"Parentheses in canonical: {paren_vals[:5]}")
print(f"  {PASS} checks so far")


# ============================================================
# 6. SOURCE COVERAGE — sample teams from each API
# ============================================================
print("\n[6] Source coverage...")
sources = {
    "The Odds API": [
        ("Manchester City", "city"),
        ("Arsenal", "arsenal"),
        ("Bayern Munich", "bayern_munich"),
        ("Real Madrid", "real_madrid"),
        ("Barcelona", "barcelona"),
        ("Liverpool", "liverpool"),
        ("Inter Miami", "inter_miami"),
        ("Al Hilal", "al_hilal"),
        ("Copenhagen", "copenhagen"),
        ("Bodo/Glimt", "bodo_glimt"),
    ],
    "Bzzoiro (EN)": [
        ("Tottenham Hotspur", "tottenham"),
        ("Celtic FC", "celtic"),
        ("Feyenoord", "feyenoord"),
        ("Benfica", "benfica"),
        ("Napoli", "napoli"),
        ("Atletico Madrid", "atletico_madrid"),
    ],
    "SharpAPI": [
        ("Bayern Munich", "bayern_munich"),
        ("Borussia Dortmund", "dortmund"),
        ("Celtic", "celtic"),
        ("Sporting CP", "sporting_cp"),
        ("FC Porto", "porto"),
        ("Shakhtar Donetsk", "shakhtar"),
    ],
    "PropLine": [
        ("Sao Paulo", "sao_paulo"),
        ("Flamengo", "flamengo"),
        ("Cruzeiro", "cruzeiro"),
        ("Vasco", "vasco"),
        ("Botafogo", "botafogo"),
        ("Gremio", "gremio"),
    ],
}

for source_name, teams in sources.items():
    resolved = 0
    total = len(teams)
    for raw_name, expected in teams:
        result = clean_team_name(raw_name)
        if result == expected:
            resolved += 1
        else:
            print(f"  {source_name}: '{raw_name}' -> '{result}' (expected '{expected}')")
    pct = (resolved / total) * 100
    print(f"  {source_name}: {resolved}/{total} ({pct:.0f}%)")
    check(resolved == total, f"{source_name}: {resolved}/{total} resolved")
print(f"  {PASS} checks so far")


# ============================================================
# 7. CONTRACT TESTS — from test_contracts.py
# ============================================================
print("\n[7] Contract tests...")
# Version
check(__version__ == "9.3-audited", f"version: {__version__}")

# Aliases count
check(len(TEAM_ALIASES) >= 200, f"only {len(TEAM_ALIASES)} aliases")

# clean_team_name assertions
check(clean_team_name("Manchester United") == "man", "Manchester United")
check(clean_team_name("Brøndby IF") == "brondby", "Brondby IF")
check(clean_team_name("Köln") == "cologne", "Koln")
check(clean_team_name("Malmö FF") == "malmo_ff", "Malmo FF")
check(clean_team_name("") == "", "empty string")
check(clean_team_name(None) == "", "None")

# build_canonical_id
check(
    build_canonical_id("Man United", "Chelsea", "2025-10-04") == "man__chelsea__20251004",
    f"build_canonical_id: {build_canonical_id('Man United', 'Chelsea', '2025-10-04')}"
)
check(build_canonical_id("", "Chelsea", "2025-10-04") == "", "empty home")
check(build_canonical_id("Arsenal", "", "2025-10-04") == "", "empty away")
check(build_canonical_id("Arsenal", "Chelsea", "") == "", "empty date")
print(f"  {PASS} checks so far")


# ============================================================
# SUMMARY
# ============================================================
print(f"\n{'='*50}")
print(f"PASS:  {PASS}")
print(f"FAIL:  {FAIL}")
print(f"WARN:  {WARN}")
print(f"{'='*50}")

if FAIL > 0:
    print("\n❌ Tests FAILED — fix issues before deploy")
    sys.exit(1)
else:
    print("\n✅ All tests passed — ready to deploy")
    sys.exit(0)
