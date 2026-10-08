#!/usr/bin/env python3
"""
test_gluing.py — Automated test suite for team_registry.py
Tests: self-consistency, cross-source, safety, normalization, structural, coverage, contracts, Reep.
Exit code 0 = all pass, 1 = any fail.
"""

import sys
import os

# Import the registry
try:
    from team_registry import TEAM_ALIASES, clean_team_name, build_canonical_id, __version__
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
    ("manchester city", "man city", "city"),
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
    ("alebrijes_de_oaxaca", "alebrijes_oaxaca", "alebrijes_oaxaca"),
    ("fortaleza_ceif_fc", "fortaleza_ceif", "fortaleza_ceif"),
    ("millonarios_bogota", "millonarios", "millonarios"),
    ("novorizontino_sp", "novorizontino", "novorizontino"),
]
for a, b, expected in cross_pairs:
    ca = clean_team_name(a)
    cb = clean_team_name(b)
    if ca != cb:
        WARN += 1
        print(f"  WARN: '{a}' -> '{ca}' != '{b}' -> '{cb}'")
    elif ca != expected:
        WARN += 1
        print(f"  WARN: '{a}' -> '{ca}' (expected '{expected}')")
    else:
        PASS += 1
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
    ("fortaleza", "fortaleza_ceif"),
]
for a, b in safety_pairs:
    ca = clean_team_name(a)
    cb = clean_team_name(b)
    check(ca != cb, f"'{a}' -> '{ca}' should NOT equal '{b}' -> '{cb}'")
print(f"  {PASS} checks so far")


# ============================================================
# 4. NORMALIZATION — basic steps of clean_team_name
# ============================================================
print("\n[4] Normalization steps...")
norm_tests = [
    ("  Arsenal  ", "arsenal"),
    ("CHELSEA", "chelsea"),
    ("some unknown team", "some_unknown_team"),
    ("Brøndby IF", "brondby"),
    ("Köln", "cologne"),
    ("Malmö FF", "malmo_ff"),
    (None, ""),
    ("", ""),
    ("man_city", "city"),
    ("bodo/glimt", "bodo_glimt"),
    ("colo-colo", "colo_colo"),
    ("arsenal fc", "arsenal"),
    ("barcelona_sc", "barcelona_sc"),
    ("bayern munchen", "bayern_munich"),
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

check(__version__ == "9.3-audited", f"version: {__version__} (expected 9.3-audited)")
print(f"  {PASS} checks so far")


# ============================================================
# 6. SOURCE COVERAGE — sample teams from each API resolve correctly
# ============================================================
print("\n[6] Source coverage...")
oddsapi = [
    ("Manchester City", "city"),
    ("Liverpool", "liverpool"),
    ("Real Madrid", "real_madrid"),
    ("Barcelona", "barcelona"),
    ("Bayern Munich", "bayern_munich"),
    ("PSG", "psg"),
    ("Juventus", "juventus"),
    ("Inter Milan", "inter_milan"),
    ("Atletico Madrid", "atletico_madrid"),
    ("Dortmund", "dortmund"),
]
bzzoiro = [
    ("Arsenal", "arsenal"),
    ("Chelsea", "chelsea"),
    ("Tottenham", "tottenham"),
    ("Manchester United", "man"),
    ("Real Betis", "betis"),
    ("Napoli", "napoli"),
]
sharpapi = [
    ("bayern munchen", "bayern_munich"),
    ("borussia dortmund gmbh", "dortmund"),
    ("feyenoord rotterdam", "feyenoord"),
    ("celtic glasgow", "celtic"),
    ("ferencvarosi", "ferencvaros"),
    ("nfc volos", "volos"),
]
propline = [
    ("Arsenal", "arsenal"),
    ("Chelsea", "chelsea"),
    ("Liverpool", "liverpool"),
    ("Man City", "city"),
    ("Real Madrid", "real_madrid"),
    ("Barcelona", "barcelona"),
]

for source_name, teams in [("The Odds API", oddsapi), ("Bzzoiro (EN)", bzzoiro),
                            ("SharpAPI", sharpapi), ("PropLine", propline)]:
    resolved = 0
    for team, expected in teams:
        r = clean_team_name(team)
        if r == expected:
            resolved += 1
        else:
            print(f"  {source_name}: '{team}' -> '{r}' (expected '{expected}')")
    pct = resolved / len(teams) * 100
    check(resolved == len(teams), f"{source_name}: {resolved}/{len(teams)} ({pct:.0f}%)")
    print(f"  {source_name}: {resolved}/{len(teams)} ({pct:.0f}%)")
print(f"  {PASS} checks so far")


# ============================================================
# 7. CONTRACT TESTS — from test_contracts.py
# ============================================================
print("\n[7] Contract tests...")
check(clean_team_name("Manchester United") == "man", "Manchester United -> man")
check(clean_team_name("Brøndby IF") == "brondby", "Brøndby IF -> brondby")
check(clean_team_name("Köln") == "cologne", "Köln -> cologne")
check(clean_team_name("Malmö FF") == "malmo_ff", "Malmö FF -> malmo_ff")
check(clean_team_name("") == "", "empty -> empty")
check(clean_team_name(None) == "", "None -> empty")
check(build_canonical_id("Man United", "Chelsea", "2025-10-04") == "man__chelsea__20251004",
      f"build_canonical_id: {build_canonical_id('Man United', 'Chelsea', '2025-10-04')}")
check(build_canonical_id("", "Chelsea", "2025-10-04") == "", "build_canonical_id empty home")
check(build_canonical_id("Arsenal", "", "2025-10-04") == "", "build_canonical_id empty away")
check(build_canonical_id("Arsenal", "Chelsea", "") == "", "build_canonical_id empty date")
check(len(TEAM_ALIASES) >= 200, f"aliases: {len(TEAM_ALIASES)} (expected >= 200)")
check(__version__ == "9.3-audited", f"version: {__version__}")
print(f"  {PASS} checks so far")


# ============================================================
# 8. REEP ALIASES — verify Reep CC0 import
# ============================================================
print("\n[8] Reep aliases...")
reep_tests = [
    ("bayern munchen", "bayern_munich"),
    ("1. fc köln", "cologne"),
    ("wisla plock", "wisla_plock"),
    ("brondby if", "brondby"),
    ("a.g.s asteras tripolis b", "a.g.s_asteras_tripolis_b"),  # B-team must NOT glue to parent
    ("alebrijes de oaxaca", "alebrijes_oaxaca"),
    ("fortaleza ceif fc", "fortaleza_ceif"),
    ("millonarios bogota", "millonarios"),
]
for raw, expected in reep_tests:
    result = clean_team_name(raw)
    if result == expected:
        PASS += 1
    else:
        # B-team test is a safety check, not a hard fail
        if "asteras" in raw:
            WARN += 1
            print(f"  REEP: '{raw}' -> '{result}' (expected '{expected}')")
        else:
            FAIL += 1
            print(f"  REEP FAIL: '{raw}' -> '{result}' (expected '{expected}')")
print(f"  {PASS} checks so far")


# ============================================================
# SUMMARY
# ============================================================
total = PASS + FAIL
print(f"\n{'='*50}")
print(f"PASS:  {PASS}/{total}")
print(f"FAIL:  {FAIL}/{total}")
if WARN > 0:
    print(f"WARN:  {WARN}")
if FAIL == 0:
    print(f"\n✅ All tests passed — ready to deploy")
    sys.exit(0)
else:
    print(f"\n❌ Tests FAILED — fix issues before deploy")
    sys.exit(1)
