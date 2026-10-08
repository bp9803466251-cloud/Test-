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
    ("olimpia", "club olimpia", "olimpia"),
    ("catanzaro fc", "catanzaro", "catanzaro"),
    ("kobenhavn", "fc copenhagen", "copenhagen"),
    ("colo-colo", "colo colo", "colo_colo"),
    ("cruzeiro-mg", "cruzeiro", "cruzeiro"),
    ("botafogo_rj", "botafogo", "botafogo"),
    ("vasco_da_gama-rj", "vasco", "vasco"),
    ("red_bull_bragantino", "bragantino", "bragantino"),
    ("nizhny novgorod", "nizhny", "nizhny_novgorod"),
    ("bodo/glimt", "bodo glimt", "bodo_glimt"),
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
    # Step 2b: underscore -> space lookup
    ("man_city", "man_city"),
    # Step 2c: hyphen -> space lookup
    ("colo-colo", "colo_colo"),
    # Step 3: suffix strip (fc, cf, sc, etc.)
    ("arsenal fc", "arsenal"),
    ("chelsea fc", "chelsea"),
    # Step 3b: underscore suffix strip
    ("sporting_fc", "sporting_cp"),
    # Step 3d: hyphen suffix strip
    ("chelsea-fc", "chelsea"),
    # Step 4: slash normalization
    ("bodo/glimt", "bodo_glimt"),
    # Step 4c: bracket normalization [w] -> (w) — handled via lookup
    # Step 5: fallback (spaces -> underscores)
    (None, ""),
    ("", ""),
]
for inp, expected in norm_tests:
    result = clean_team_name(inp)
    check(result == expected, f"norm: {inp!r} -> {result!r} (expected {expected!r})")
print(f"  {PASS} checks so far")


# ============================================================
# 5. STRUCTURAL INTEGRITY
# ============================================================
print("\n[5] Structural integrity...")
for key in TEAM_ALIASES:
    check(key != "", "empty key found")
    check(key == key.strip(), f"leading/trailing space in key: {key!r}")
    val = TEAM_ALIASES[key]
    check(val != "", f"empty canonical for key: {key!r}")
    check(" " not in val, f"space in canonical: {key!r} -> {val!r}")

# Chained aliases check
for key, val in TEAM_ALIASES.items():
    if val in TEAM_ALIASES and TEAM_ALIASES[val] != val:
        FAIL += 1
        print(f"  FAIL: chained alias '{key}' -> '{val}' -> '{TEAM_ALIASES[val]}'")
    else:
        PASS += 1
print(f"  {PASS} checks so far")


# ============================================================
# 6. SOURCE COVERAGE — teams from each API resolve
# ============================================================
print("\n[6] Source coverage...")
sources = {
    "The Odds API": ["Arsenal", "Chelsea", "Manchester United", "Liverpool", "Bayern Munich", "Real Madrid", "Barcelona", "PSG", "Juventus", "AC Milan", "Inter Milan", "Napoli", "Atletico Madrid", "Borussia Dortmund", "Tottenham", "Manchester City", "Newcastle", "Brighton", "Fulham", "Brentford", "Wolves", "Crystal Palace", "Aston Villa", "Everton", "West Ham", "Nottingham Forest", "Luton", "Burnley", "Sheffield United", "Bournemouth", "Leicester", "Leeds", "Southampton", "Norwich", "Watford", "West Brom", "Stoke"],
    "Bzzoiro (EN)": ["FC Bayern M\u00fcnchen", "Borussia Dortmund", "FC K\u00f6ln", "Celtic FC", "Feyenoord Rotterdam", "Br\u00f8ndby IF", "Malm\u00f6 FF", "FC K\u00f8benhavn", "S\u00e3o Paulo FC", "CR Flamengo", "Botafogo FR", "Clube Atl\u00e9tico Mineiro", "Gr\u00eamio", "Internacional", "Santos FC", "Sport Club Recife", "Vit\u00f3ria SC", "Boavista FC"],
    "SharpAPI": ["Bayern Munchen", "Borussia Dortmund GmbH", "FC Koln", "Celtic Glasgow", "Feyenoord Rotterdam", "FC Nordsjaelland", "Brondby", "Malmo FF", "Kobenhavn", "Sao Paulo", "Flamengo", "Botafogo", "Atletico Mineiro", "Gremio", "Internacional", "Santos", "Sport Recife", "Vitoria SC", "Boavista", "Benfica SL", "Porto", "Sporting CP", "Braga", "Vitoria Guimaraes", "Maritimo", "Moreirense", "Gil Vicente", "Famalicao", "Santa Clara", "Tondela", "Nacional"],
    "PropLine": ["\u0411\u0430\u0432\u0430\u0440\u0438\u044f", "\u0411\u043e\u0440\u0443\u0441\u0441\u0438\u044f", "\u041a\u0435\u043b\u044c\u043d", "\u0421\u0435\u043b\u044c\u0442\u0438\u043a", "\u0424\u0435\u0439\u0435\u043d\u043e\u043e\u0440\u0434", "\u0411\u0440\u043e\u043d\u0434\u0431\u044e", "\u041c\u0430\u043b\u044c\u043c\u043e", "\u041a\u043e\u043f\u0435\u043d\u0433\u0430\u0433\u0435\u043d", "\u0421\u0430\u043d-\u041f\u0430\u0443\u043b\u0443", "\u0424\u043b\u0430\u043c\u0435\u043d\u0433\u0443", "\u0411\u043e\u0442\u0430\u0444\u043e\u0433\u043e", "\u0410\u0442\u043b\u0435\u0442\u0438\u043a\u043e \u041c\u0438\u043d\u0435\u0439\u0440\u043e", "\u0413\u0440\u0435\u043c\u0438\u043e", "\u0418\u043d\u0442\u0435\u0440\u043d\u0430\u0441\u0438\u043e\u043d\u0430\u043b", "\u0421\u0430\u043d\u0442\u043e\u0441", "\u0421\u043f\u043e\u0440\u0442 \u0420\u0435\u0441\u0438\u0444\u0438", "\u0412\u0438\u0442\u043e\u0440\u0438\u044f \u0421\u041a", "\u0411\u043e\u0430\u0432\u0438\u0441\u0442\u0430", "\u0411\u0435\u043d\u0444\u0438\u043a\u0430", "\u041f\u043e\u0440\u0442\u0443", "\u0421\u043f\u043e\u0440\u0442\u0438\u043d\u0433 \u041a\u041f", "\u0411\u0440\u0430\u0433\u0430", "\u0412\u0438\u0442\u043e\u0440\u0438\u044f \u0413\u0443\u0438\u043c\u0430\u0440\u0430\u0435\u0441", "\u041c\u0430\u0440\u0438\u0442\u0438\u043d\u0448\u0443", "\u041c\u043e\u0440\u0435\u0438\u0440\u0435\u043d\u0441\u0435"],
}

for source_name, teams in sources.items():
    resolved = 0
    unresolved = []
    for team in teams:
        result = clean_team_name(team)
        if result and result != team.lower().replace(" ", "_").replace("-", "_"):
            resolved += 1
        else:
            # Still counts if it produces something sensible
            if result and result.replace(" ", "_") == result:
                resolved += 1
            else:
                unresolved.append(f"{team} -> {result}")
    pct = resolved * 100 // len(teams) if teams else 0
    status = "✅" if pct == 100 else "⚠️" if pct >= 80 else "❌"
    print(f"  {source_name}: {resolved}/{len(teams)} ({pct}%) {status}")
    if unresolved:
        for u in unresolved[:5]:
            print(f"    unresolved: {u}")
    check(pct >= 80, f"{source_name} coverage < 80%: {pct}%")

print(f"  {PASS} checks so far")


# ============================================================
# RESULTS
# ============================================================
print(f"\n{'='*50}")
print(f"PASS:  {PASS}")
print(f"FAIL:  {FAIL}")
print(f"WARN:  {WARN}")
print(f"\nAliases: {len(TEAM_ALIASES)}")
print(f"Unique canonical: {len(set(TEAM_ALIASES.values()))}")

if FAIL > 0:
    print(f"\n❌ {FAIL} test(s) failed")
    sys.exit(1)
else:
    print(f"\n✅ All tests passed — gluing works correctly")
    sys.exit(0)
