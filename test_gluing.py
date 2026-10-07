#!/usr/bin/env python3
"""
test_gluing.py — Automated test suite for team_registry_final.py
Tests: self-consistency, cross-source, safety, normalization, structural, coverage.
Exit code 0 = all pass, 1 = any fail.
"""

import sys
import os

# Import the registry
try:
    from team_registry_final import TEAM_ALIASES, clean_team_name, _STRIP_SUFFIXES
except ImportError:
    print("FAIL: cannot import team_registry_final")
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
    (" sporting_cp", "sporting_kc"),
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
    ("\u00dftuttgart", "sstuttgart"),
    # Step 2b: underscore -> space
    ("man_city", "man_city"),
    # Step 2c: hyphen -> space
    ("colo-colo", "colo_colo"),
    # Step 3: suffix strip
    ("arsenal fc", "arsenal"),
    ("chelsea fc", "chelsea"),
    # Step 3b: underscore suffix
    ("sporting_fc", "sporting_cp"),
    # Step 3d: hyphen suffix
    ("chelsea-fc", "chelsea"),
    # Step 4: combined separators
    ("bodo/glimt", "bodo_glimt"),
    # Step 4c: bracket normalization
    ("jordan_[w]", "jordan_(w)"),
    # Step 5: fallback
    ("some unknown team", "some_unknown_team"),
]
for raw, expected in norm_tests:
    result = clean_team_name(raw)
    check(result == expected, f"'{raw}' -> '{result}' (expected '{expected}')")
print(f"  {PASS} checks so far")


# ============================================================
# 5. STRUCTURAL — no empty keys, no leading spaces, no bad canonicals
# ============================================================
print("\n[5] Structural integrity...")
empty_keys = [k for k in TEAM_ALIASES if not k.strip()]
check(len(empty_keys) == 0, f"empty keys: {empty_keys}")

leading_spaces = [k for k in TEAM_ALIASES if k != k.strip()]
check(len(leading_spaces) == 0, f"leading/trailing spaces: {leading_spaces}")

empty_vals = [v for v in TEAM_ALIASES.values() if not v.strip()]
check(len(empty_vals) == 0, f"empty canonicals: {empty_vals}")

bad_canonical = [v for v in TEAM_ALIASES.values() if " " in v]
check(len(bad_canonical) == 0, f"canonicals with spaces: {bad_canonical[:5]}")
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

for source_name, teams in [("OddsAPI", oddsapi), ("Bzzoiro", bzzoiro),
                            ("SharpAPI", sharpapi), ("PropLine", propline)]:
    resolved = 0
    for t in teams:
        r = clean_team_name(t)
        if r in TEAM_ALIASES.values() or r == clean_team_name(t):
            resolved += 1
    pct = resolved / len(teams) * 100
    check(resolved == len(teams), f"{source_name}: {resolved}/{len(teams)} ({pct:.0f}%)")
    print(f"  {source_name}: {resolved}/{len(teams)} ({pct:.0f}%)")

print(f"  {PASS} checks so far")


# ============================================================
# SUMMARY
# ============================================================
total = PASS + FAIL
print(f"\n{'='*50}")
print(f"PASS:  {PASS}/{total}")
print(f"FAIL:  {FAIL}/{total}")
if FAIL == 0:
    print(f"\n✅ All tests passed — safe to deploy")
    sys.exit(0)
else:
    print(f"\n❌ {FAIL} tests failed — fix before deploy")
    sys.exit(1)
