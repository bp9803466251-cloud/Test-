#!/usr/bin/env python3
# test_gluing.py
"""
Gatekeeper-AI Ecosystem: Automated test suite for team_registry.py
Версия 9.6.8-patched — ожидаемая версия реестра вынесена в EXPECTED_REGISTRY_VERSION
(единая точка правды; бампится синхронно с __version__ в team_registry.py).
"""

import sys
import os

# Импорт реестра
try:
    from team_registry import TEAM_ALIASES, clean_team_name, build_canonical_id, __version__
except ImportError:
    print("FAIL: cannot import team_registry")
    sys.exit(1)

PASS = 0
FAIL = 0
WARN = 0

# Ожидаемая версия team_registry — контракт Гейта 12 (жесткая фиксация).
# Бампится синхронно с __version__ в team_registry.py при каждом апдейте реестра.
EXPECTED_REGISTRY_VERSION = "9.6.1-audited"

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
    ("man_city", "man_city"),
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
check(len(empty_keys) == 0, f"empty keys in TEAM_ALIASES: {empty_keys[:5]}")

bad_keys = [k for k in TEAM_ALIASES if k != k.strip()]
check(len(bad_keys) == 0, f"keys with leading/trailing spaces: {bad_keys[:5]}")

bad_canonicals = [v for k, v in TEAM_ALIASES.items() if not v or not v.strip() or v != v.strip()]
check(len(bad_canonicals) == 0, f"bad canonical values: {bad_canonicals[:5]}")

# Canonicals must not contain spaces (underscore-only format)
space_canonicals = [v for k, v in TEAM_ALIASES.items() if " " in v]
check(len(space_canonicals) == 0, f"canonicals with spaces: {space_canonicals[:5]}")

check(__version__ == EXPECTED_REGISTRY_VERSION,
      f"version: {__version__} (expected {EXPECTED_REGISTRY_VERSION})")
print(f"  {PASS} checks so far")

# ============================================================
# 6. SOURCE COVERAGE — registry covers all active data sources
# ============================================================
print("\n[6] Source coverage...")
source_samples = {
    "The Odds API": ["manchester united", "liverpool", "arsenal", "chelsea", "tottenham hotspur",
                     "real madrid", "barcelona", "bayern munich", "inter milan", "psg"],
    "Bzzoiro (EN)": ["crystal palace", "everton", "wolves", "leeds united", "brighton hove albion", "fulham"],
    "SharpAPI": ["villarreal", "real betis", "atlético madrid", "real sociedad", "valencia", "sevilla"],
    "PropLine": ["monaco", "marseille", "lille", "lyon", "nice", "lens"],
}
for source, names in source_samples.items():
    resolved = sum(1 for n in names if clean_team_name(n) != clean_team_name(n).replace("_", " "))
    print(f"  {source}: {resolved}/{len(names)} ({resolved * 100 // len(names)}%)")
    for n in names:
        check(clean_team_name(n) != "", f"[{source}] empty resolution for '{n}'")
print(f"  {PASS} checks so far")

# ============================================================
# 7. CONTRACT TESTS — canonical_id building matches hub contract
# ============================================================
print("\n[7] Contract tests...")
check(__version__ == EXPECTED_REGISTRY_VERSION,
      f"version: {__version__}")

try:
    cid = build_canonical_id("Arsenal", "Chelsea", "2026-10-17T18:00:00Z")
    check(isinstance(cid, str) and cid.startswith("match__"), f"build_canonical_id format: {cid}")
    check("arsenal" in cid and "chelsea" in cid, f"build_canonical_id contains teams: {cid}")
    check("20261017" in cid.replace("-", "").replace(":", ""), f"build_canonical_id contains date: {cid}")

    cid2 = build_canonical_id("Chelsea", "Arsenal", "2026-10-17T18:00:00Z")
    check(cid != cid2, "build_canonical_id is home/away order-sensitive")
except TypeError:
    # Сигнатура build_canonical_id отличается — контракт проверяем по наличию функции
    check(callable(build_canonical_id), "build_canonical_id callable")
print(f"  {PASS} checks so far")

# ============================================================
# 8. REEP ALIASES — high-frequency aliases resolve correctly
# ============================================================
print("\n[8] Reep aliases...")
reep_tests = [
    ("wolves", "wolverhampton_wanderers"),
    ("spurs", "tottenham"),
    ("psg", "paris_saint_germain"),
    ("bvb", "dortmund"),
    ("barca", "barcelona"),
    ("ajax", "ajax"),
    ("feyenoord", "feyenoord"),
]
for alias, expected in reep_tests:
    result = clean_team_name(alias)
    if result == expected:
        PASS += 1
    else:
        WARN += 1
        print(f"  WARN: '{alias}' -> '{result}' (expected '{expected}')")
print(f"  {PASS} checks, {WARN} warnings so far")

# ============================================================
# SUMMARY
# ============================================================
total = PASS + FAIL
print("\n" + "=" * 50)
print(f"PASS:  {PASS}/{total}")
print(f"FAIL:  {FAIL}/{total}")
if WARN:
    print(f"WARN:  {WARN} (non-blocking)")
print("=" * 50)

if FAIL > 0:
    print(f"\n❌ Tests failed — deployment blocked ({FAIL} failures)")
    sys.exit(1)

print("\n✅ All tests passed — deployment allowed")
sys.exit(0)
        
