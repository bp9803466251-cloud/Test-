#!/usr/bin/env python3
"""
test_gluing.py — Automated test suite for team_registry.py
Tests: self-consistency, cross-source, safety, normalization, structural, coverage.
Exit code 0 = all pass, 1 = any fail.
"""

import sys
import os

# Import the registry
try:
    from team_registry import TEAM_ALIASES, clean_team_name, _STRIP_SUFFIXES
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
    check(ca == cb == expected, f"'{a}' -> '{ca}' vs '{b}' -> '{cb}' (expected '{expected}')")
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
    check(ca != cb, f"'{a}' -> '{ca}' should NOT equal '{b}' -> '{cb}'")
print(f"  {PASS} checks so far")


# ============================================================
# 4. NORMALIZATION — all 10 steps of clean_team_name work
# ============================================================
print("\n[4] Normalization steps...")
norm_tests = [
    # Step 1: lowercase + strip
    ("  Arsenal  ", "arsenal"),
    ("CHELSEA", "chelsea"),
    # Step 2: NFD diacritics
    ("Copenh\u00e4gen", "copenhagen"),
    ("S\u00e3o Paulo", "sao_paulo"),
    # Step 2a: special chars
    ("Br\u00f8ndby", "brondby"),
    # Step 2b: underscore -> space
    ("man_city", "man_city"),
    # Step 2c: hyphen -> space
    ("colo-colo", "colo_colo"),
    # Step 3: suffix strip
    ("arsenal fc", "arsenal"),
    ("chelsea fc", "chelsea"),
    # Step 3b: underscore suffix strip
    ("sporting_fc", "sporting_cp"),
    # Step 3d: hyphen suffix strip
    ("chelsea-fc", "chelsea"),
    # Step 4: slash -> underscore
    ("bodo/glimt", "bodo_glimt"),
    # Step 4c: bracket normalization
    ("jordan_[w]", "jordan_w"),
    # Step 5: fallback
    ("unknown team", "unknown_team"),
    # None guard
    (None, ""),
    ("", ""),
]
for inp, expected in norm_tests:
    result = clean_team_name(inp)
    check(result == expected, f"clean_team_name({inp!r}) = '{result}' (expected '{expected}')")
print(f"  {PASS} checks so far")


# ============================================================
# 5. STRUCTURAL — no empty keys, spaces, bad canonicals
# ============================================================
print("\n[5] Structural integrity...")
structural_fail = 0
for key, val in TEAM_ALIASES.items():
    if not key:
        print(f"  FAIL: empty key -> '{val}'")
        structural_fail += 1
    if key != key.strip():
        print(f"  FAIL: key has leading/trailing spaces: '{key}'")
        structural_fail += 1
    if not val:
        print(f"  FAIL: empty canonical for key '{key}'")
        structural_fail += 1
    if " " in val:
        print(f"  FAIL: space in canonical '{val}' (key '{key}')")
        structural_fail += 1

if structural_fail == 0:
    PASS += 1
    print("  ✅ All structural checks pass")
else:
    FAIL += structural_fail
print(f"  {PASS} checks so far")


# ============================================================
# 6. SOURCE COVERAGE — sample teams from each API resolve
# ============================================================
print("\n[6] Source coverage...")
sources = {
    "The Odds API": [
        ("Manchester City", "man_city"),
        ("Bayern Munich", "bayern_munich"),
        ("Real Madrid", "real_madrid"),
        ("Arsenal", "arsenal"),
        ("Liverpool", "liverpool"),
        ("Chelsea", "chelsea"),
        ("Barcelona", "barcelona"),
        ("PSG", "psg"),
        ("Juventus", "juventus"),
        ("Inter Milan", "inter_milan"),
    ],
    "Bzzoiro (EN)": [
        ("Bayern Munchen", "bayern_munich"),
        ("Borussia Dortmund", "dortmund"),
        ("Feyenoord", "feyenoord"),
        ("Celtic", "celtic"),
        ("Benfica", "benfica"),
        ("Sporting CP", "sporting_cp"),
        ("Porto", "porto"),
        ("Ajax", "ajax"),
        ("PSV", "psv"),
        ("Feyenoord Rotterdam", "feyenoord"),
    ],
    "SharpAPI": [
        ("bayern munchen", "bayern_munich"),
        ("bayern munich", "bayern_munich"),
        ("borussia dortmund gmbh", "dortmund"),
        ("feyenoord rotterdam", "feyenoord"),
        ("celtic glasgow", "celtic"),
        ("ferencvarosi", "ferencvaros"),
        ("nfc volos", "volos"),
        ("benfica sl", "benfica"),
        ("catanzaro fc", "catanzaro"),
        ("kobenhavn", "copenhagen"),
    ],
    "PropLine": [
        ("Bodo/Glimt", "bodo_glimt"),
        ("Colo-Colo", "colo_colo"),
        ("Cruzeiro-MG", "cruzeiro"),
        ("Botafogo RJ", "botafogo"),
        ("Vasco da Gama-RJ", "vasco"),
        ("Red Bull Bragantino", "bragantino"),
        ("São Paulo", "sao_paulo"),
        ("Copenh\u00e4gen", "copenhagen"),
        ("Malm\u00f6 FF", "malmo_ff"),
        ("Br\u00f8ndby IF", "brondby"),
    ],
}
for source_name, teams in sources.items():
    source_pass = 0
    source_fail = 0
    for team_name, expected in teams:
        result = clean_team_name(team_name)
        if result == expected:
            source_pass += 1
            PASS += 1
        else:
            source_fail += 1
            FAIL += 1
            print(f"  FAIL: [{source_name}] '{team_name}' -> '{result}' (expected '{expected}')")
    pct = 100 * source_pass / len(teams) if teams else 0
    print(f"  {source_name}: {source_pass}/{len(teams)} ({pct:.0f}%)")

# ============================================================
# 7. CONTRACT — test_contracts.py assertions
# ============================================================
print("\n[7] Contract assertions...")
contract_tests = [
    (clean_team_name("Manchester United") == "man", "Manchester United -> man"),
    (clean_team_name("Br\u00f8ndby IF") == "brondby", "Brøndby IF -> brondby"),
    (clean_team_name("K\u00f6ln") == "cologne", "Köln -> cologne"),
    (clean_team_name("Malm\u00f6 FF") == "malmo_ff", "Malmö FF -> malmo_ff"),
    (clean_team_name("") == "", "empty -> empty"),
    (clean_team_name(None) == "", "None -> empty"),
]
for cond, msg in contract_tests:
    check(cond, msg)
print(f"  {PASS} checks so far")


# ============================================================
# RESULTS
# ============================================================
print(f"\n{'='*60}")
print(f"PASS:  {PASS}")
print(f"FAIL:  {FAIL}")
print(f"WARN:  {WARN}")
print(f"{'='*60}")

if FAIL > 0:
    print(f"\n❌ {FAIL} test(s) failed")
    sys.exit(1)
else:
    print(f"\n✅ All {PASS} tests passed")
    sys.exit(0)
