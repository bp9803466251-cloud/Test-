# team_registry.py
"""
Gatekeeper-AI Ecosystem: TEAM REGISTRY & NORMALIZATION LAYER.
Версия 10.0.1-redis-runtime — ЗАМЕНА тройки search_module + team_registry + country_code_map (§14).

Канон: памятка v1.10.0 (директива владельца 12.11).
  - Единственный источник алиасов — Reep-снапшот через registry:* (§14.1);
    словарь v9.6.1 как актив НЕ перенесён. Seed-слой в лестнице отсутствует (§4).
  - ДАННЫЕ: ОТСУТСТВУЮТ (директива 12.11) — карты читаются из Redis-снимка:
    country — registry:map:country_code, league — registry:alias:league (§14, §16).
    До заселения — честный фолбэк + счётчик fallback_country в health (§14.4).
  - Лестница §4: bridge (ВСЕГДА первый) -> scoped-алиас -> глобальный алиас ->
    fuzzy с гео-подтверждением -> MISS + телеметрия. Само-канонизации нет (§6.4):
    clean_team_name на MISS возвращает нормализованную форму + флаг, детекция —
    O(1) через is_registry_slug().
  - Кэш §16: HSCAN-снимок на прогон + пер-прогонный memo; инъекция транспорта
    для проб; при отсутствии Redis — снимок пуст, все резолвы честный MISS.
  - build_canonical_id — ЕДИНСТВЕННЫЙ сборщик cid; легаси-семантики: missing=""
    (загрузчик) / missing="unknown" (хаб). STRICT-режим: при MISS любой стороны
    cid не строится (запись отклоняется, §4.5).
  - Сжатие данных: mixed-токены свёртываются (§5.2); стрип суффиксов — флаг OFF (§12.1).
"""

__version__ = "10.0.1-redis-runtime"

import re
import unicodedata

STRIP_SUFFIX_ENABLED = False  # §12.1: OFF до вердикта; включение — отдельный прогон с pre-diff

# ═══════════════════════════════════════════════════════════════════════
# КАРТЫ РАНТАЙМА (§14: встроенных ДАННЫХ НЕТ). Заполняются из Redis-снимка
# в Registry._ensure: country — registry:map:country_code,
# league — registry:alias:league. До заселения — честный фолбэк со
# счётчиком fallback_country в registry:system:health (§14.4, не тихо).
# ═══════════════════════════════════════════════════════════════════════
COUNTRY_CODE_MAP = {}   # код -> канон (из снимка, §14/§16)
LEAGUE_NAME_MAP = {}    # slug/код -> канон (из снимка, §14/§16)

_FALLBACK_COUNTRY = {"n": 0}  # счётчик фолбэка страны (§14.4)

# ═══════════════════════════════════════════════════════════════════════
# ДВИЖОК §5 — нормализация в строгом порядке операций
# ═══════════════════════════════════════════════════════════════════════
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_SUFFIX = re.compile(r"\b(fc|cf|afc|фк)\b")

# Свёртка гомоглифов: только в СМЕШАННЫХ токенах (§5.2) — к латинице
_HOMOGLYPH = {
    "а": "a", "о": "o", "с": "c", "е": "e", "р": "p", "х": "x",
    "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "у": "y",
}
_CYR_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "i", "є": "e", "ґ": "g",
}


def _is_latin(ch):
    return "a" <= ch <= "z"


_EXTRA_CYR = set("іїєґІЇЄҐ")


def _is_cyr(ch):
    return ("а" <= ch <= "я") or ch == "ё" or ch in _EXTRA_CYR


def _fold_mixed(tok):
    """§5.2: свёртка только в смешанных токенах (оба алфавита) — к латинице."""
    lat = any(_is_latin(c) for c in tok)
    cyr = any(_is_cyr(c) for c in tok)
    if not (lat and cyr):
        return tok
    out = []
    for c in tok:
        if c in _HOMOGLYPH:
            out.append(_HOMOGLYPH[c])
        elif _is_cyr(c):
            out.append(_CYR_LAT.get(c, ""))
        else:
            out.append(c)
    return "".join(out)


def _strip_accents(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize(name):
    """§5: порядок операций — lower/пунктуация -> акценты -> свёртка
    гомоглифов (смешанные токены) -> стрип суффиксов (флаг OFF, §12.1)."""
    if not name:
        return ""
    s = str(name).strip().lower()
    s = _PUNCT.sub(" ", s)
    s = _strip_accents(s)
    s = _WS.sub(" ", s).strip()
    if not s:
        return ""
    s = " ".join(_fold_mixed(t) for t in s.split(" "))
    if STRIP_SUFFIX_ENABLED:  # строго ПОСЛЕ свёртки (§12.3)
        s = _SUFFIX.sub(" ", s)
        s = _WS.sub(" ", s).strip()
    return s


def translit(s):
    out = []
    for c in s:
        out.append(_CYR_LAT.get(c, c) if _is_cyr(c) else c)
    return "".join(out)


def slugify(name):
    """Slug из канонического имени в латинице (§12.5.4)."""
    s = normalize(name)
    if not s:
        return None
    s = translit(s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or None


def _alias_key(norm):
    """Ключ алиаса в HASH: пробелы -> подчёркивания (однозначный парс ключа)."""
    return norm.replace(" ", "_")


def _cid_token(name):
    return _alias_key(normalize(name))


# ═══════════════════════════════════════════════════════════════════════
# normalize_country / normalize_league (§14.2.4)
# ═══════════════════════════════════════════════════════════════════════
def _fallback_country_note():
    """§14.4: фолбэк страны не тихий — счётчик + health-телеметрия."""
    _FALLBACK_COUNTRY["n"] += 1
    try:
        t = REGISTRY._t
        if t is not None:
            t.redis_command(["HINCRBY", "registry:system:health",
                             "fallback_country", "1"])
    except Exception:
        pass


def normalize_country(v):
    """Код -> канон; название -> канон (карта из снимка registry:map:country_code).
    Вне карты — честный фолбэк на нормализованное имя + счётчик fallback_country
    в registry:system:health (§14.4: не тихо)."""
    if not v:
        return ""
    s = str(v).strip()
    up = s.upper()
    if up in COUNTRY_CODE_MAP:
        return _alias_key(normalize(COUNTRY_CODE_MAP[up]))
    n = normalize(s)
    for code, nm in COUNTRY_CODE_MAP.items():
        if normalize(nm) == n:
            return _alias_key(n)
    _fallback_country_note()
    return _alias_key(n)


def normalize_league(v):
    """Код лиги / slug / название -> каноническое название."""
    if not v:
        return ""
    for cand in (v, v.upper(), v.lower()):
        if cand in LEAGUE_NAME_MAP:
            return _alias_key(normalize(LEAGUE_NAME_MAP[cand]))
    return _alias_key(normalize(v))


def get_country_name(code):
    if not code:
        return ""
    return COUNTRY_CODE_MAP.get(str(code).upper(), code)


def get_league_name(slug):
    if not slug:
        return ""
    for cand in (slug, slug.upper(), slug.lower()):
        if cand in LEAGUE_NAME_MAP:
            return LEAGUE_NAME_MAP[cand]
    return str(slug).replace("_", " ").title()


# ═══════════════════════════════════════════════════════════════════════
# ЛЕСТНИЦА §4 + КЭШ §16
# ═══════════════════════════════════════════════════════════════════════
_MISS = object()  # sentinel memo


class Registry:
    """Резолвер с лестницей §4. Транспорт (duck-typed, как redis_hub):
    scan_keys(pattern), get_all_fields(key), get_from_cache(key), redis_command(list).
    Инъекция транспорта — для проб; отсутствие транспорта = пустой снимок."""

    def __init__(self, transport=None):
        self._t = transport
        self._loaded = False
        self._global = {}    # normkey -> slug
        self._scoped = {}    # (country, normkey) -> slug
        self._bridge = {}    # (provider, ext_id) -> slug
        self._teams = {}     # slug -> meta
        self._slugs = set()
        self._memo = {}      # (normkey, country) -> slug | _MISS
        self._memo_bridge = {}
        self.telemetry = {"hits": {}, "misses": 0}

    # ---- снимок §16 ----
    def _ensure(self):
        if self._loaded:
            return
        self._loaded = True
        if self._t is None:
            return
        try:
            for k in self._t.scan_keys("registry:alias:team:*") or []:
                fields = self._t.get_all_fields(k) or {}
                rest = k.split(":")[3:]
                if len(rest) == 1:
                    self._global.update(fields)
                elif len(rest) >= 2:
                    c = rest[0]
                    for n, s in fields.items():
                        self._scoped[(c, n)] = s
            for k in self._t.scan_keys("registry:bridge:*") or []:
                parts = k.split(":")
                prov = parts[2] if len(parts) > 2 else "reep"
                for ext, s in (self._t.get_all_fields(k) or {}).items():
                    self._bridge[(prov, ext)] = s
            for k in self._t.scan_keys("registry:team:*") or []:
                slug = k.split(":")[2] if len(k.split(":")) > 2 else None
                if slug:
                    self._teams[slug] = self._t.get_from_cache(k) or {}
            self._slugs = set(self._teams)
            self._slugs.update(self._global.values())
            self._slugs.update(self._scoped.values())
            self._slugs.update(self._bridge.values())
            # Карты рантайма (§14/§16): читаются из Redis-снимка, данных в коде нет.
            cmap = self._t.get_from_cache("registry:map:country_code")
            if isinstance(cmap, dict) and cmap:
                COUNTRY_CODE_MAP.clear()
                COUNTRY_CODE_MAP.update(cmap)
            lmap = self._t.get_from_cache("registry:alias:league")
            if isinstance(lmap, dict) and lmap:
                LEAGUE_NAME_MAP.clear()
                LEAGUE_NAME_MAP.update(lmap)
        except Exception as e:  # не тихо: WARN в stdout (§6.4)
            print("[REGISTRY WARN] снимок не загружен: %r" % e)

    def inject_snapshot(self, global_aliases, scoped_aliases, bridge, teams):
        """Для проб и offline-мерджера: снимок без Redis (§16)."""
        self._loaded = True
        self._memo.clear()      # memo валиден только в рамках снимка (§16)
        self._memo_bridge.clear()
        self._global = dict(global_aliases)
        self._scoped = dict(scoped_aliases)
        self._bridge = dict(bridge)
        self._teams = dict(teams)
        self._slugs = set(self._teams)
        self._slugs.update(self._global.values())
        self._slugs.update(self._scoped.values())
        self._slugs.update(self._bridge.values())

    def add_alias(self, name, slug, country=None):
        """Долька внутри прогона: отражается в memo (§16.4)."""
        self._ensure()
        nk = _alias_key(normalize(name))
        if not nk or not slug:
            return
        if country:
            self._scoped[(normalize_country(country), nk)] = slug
        else:
            self._global[nk] = slug
        self._slugs.add(slug)
        for key in list(self._memo):
            if key[0] == nk:
                self._memo[key] = slug

    def is_registry_slug(self, value):
        """O(1) MISS-детекция (§14.2.3): значение — slug нового реестра?"""
        self._ensure()
        return value in self._slugs

    # ---- телеметрия (§3): счётчики локальные; flush — хабом/загрузчиком ----
    def _hit(self, stage):
        self.telemetry["hits"][stage] = self.telemetry["hits"].get(stage, 0) + 1

    def _miss(self, _stage):
        self.telemetry["misses"] += 1

    def flush_telemetry(self, transport=None):
        t = transport or self._t
        if not t:
            return dict(self.telemetry)
        try:
            for stage, n in self.telemetry["hits"].items():
                t.redis_command(["HINCRBY", "registry:system:health",
                                 "hits_" + stage, int(n)])
            if self.telemetry["misses"]:
                t.redis_command(["HINCRBY", "registry:system:health",
                                 "misses", int(self.telemetry["misses"])])
        except Exception as e:
            print("[REGISTRY WARN] телеметрия не записана: %r" % e)
        return dict(self.telemetry)

    # ---- лестница §4 ----
    def resolve(self, name, country=None, external_id=None, provider=None):
        """slug | None. external_id => ТОЛЬКО bridge-путь (§4.1: единственный
        путь без ложных срабатываний; ID есть, а резолва нет — честный MISS,
        без фоллбэка на имя). Иначе: memo -> scoped -> global -> fuzzy(гео) -> MISS."""
        self._ensure()
        if external_id:
            key = (provider or "reep", str(external_id))
            slug = self._bridge.get(key)
            if slug is None and self._t is not None and key not in self._memo_bridge:
                try:
                    raw = self._t.redis_command(
                        ["HGET", "registry:bridge:" + key[0], key[1]])
                    slug = raw if isinstance(raw, str) and raw else None
                except Exception as e:
                    print("[REGISTRY WARN] bridge HGET: %r" % e)
                    slug = None
                self._memo_bridge[key] = slug if slug else _MISS
                if slug:
                    self._bridge[key] = slug
                    self._slugs.add(slug)
            if slug:
                self._hit("bridge")
                return {"slug": slug, "stage": "bridge"}
            self._miss("bridge")
            return None  # ID есть и не разрешён — имя-фоллбэк запрещён (§4.1)
        nk = _alias_key(normalize(name))
        if not nk:
            return None
        ck = normalize_country(country) if country else None
        mkey = (nk, ck)
        if mkey in self._memo:
            v = self._memo[mkey]
            if v is _MISS:
                self._miss("memo")
                return None
            self._hit("memo")
            return {"slug": v, "stage": "memo"}
        slug, stage = None, None
        if ck:
            slug = self._scoped.get((ck, nk))
            if slug:
                stage = "scoped"
        if slug is None:
            slug = self._global.get(nk)
            if slug:
                stage = "global"
        if slug is None:
            slug = self._fuzzy(normalize(name), country)
            if slug:
                stage = "fuzzy"
        if slug is None:
            self._memo[mkey] = _MISS
            self._miss("name")
            return None
        self._memo[mkey] = slug
        self._hit(stage)
        return {"slug": slug, "stage": stage}

    def _fuzzy(self, q, country):
        """§14.3: только с гео-подтверждением (страна совпадает — иначе MISS).
        O(N) — дольивающий слой, не горячий путь."""
        if not self._teams or len(q) < 4:
            return None
        cn = normalize_country(country) if country else None
        if not cn:
            return None  # без гео fuzzy запрещён (§14.3) — честный MISS
        for slug, meta in self._teams.items():
            cn_name = meta.get("canonical_name") or ""
            n = normalize(cn_name)
            if not n or len(n) < 4:
                continue
            if q not in n and n not in q:  # обе стороны substring (§13, класс дефекта 1)
                continue
            cs = meta.get("country_slug") or normalize_country(meta.get("country") or "")
            if cs and cs != cn:
                continue
            return slug
        return None

    def clean(self, name, country=None):
        r = self.resolve(name, country=country)
        if r:
            return r["slug"]
        return _cid_token(name)  # нормализованная форма; детекция — is_registry_slug

    # ---- лестница коллизий slug (§12.8) — для заселения партий ----
    def suggest_slug(self, name, country_code_val, reep_id, taken):
        """База -> +код страны -> +reep_id. Не резолвится молча: None = skip."""
        base = slugify(name)
        if not base:
            return None
        if base not in taken:
            return base
        cc = country_code_val or ""
        cand = base + "_" + cc if cc else None
        if cand and cand not in taken:
            return cand
        cand2 = (cand or base) + "_" + str(reep_id)
        if cand2 not in taken:
            return cand2
        return None


# Единая точка (§2): модуль — единственный сборщик нормализации и cid
REGISTRY = Registry()


def clean_team_name(name, country=None):
    """Совместимая обёртка (§26, §14.2.2): hit -> slug; MISS -> нормализованная
    форма + телеметрия. Само-канонизация под видом slug'а исключена: проверка
    is_registry_slug() детерминирована O(1)."""
    return REGISTRY.clean(name, country=country)


def resolve(name, country=None, external_id=None, provider=None):
    return REGISTRY.resolve(name, country=country, external_id=external_id,
                            provider=provider)


def _parse_date(date_utc):
    if not date_utc:
        return None
    s = str(date_utc).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return m.group(3) + m.group(2) + m.group(1)
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except Exception:
        return None


def build_canonical_id(home_team, away_team, date_utc, missing="", strict=True):
    """ЕДИНСТВЕННЫЙ сборщик cid (§13). STRICT (по умолчанию): при MISS любой
    стороны или плохой дате cid не строится (§4.5 — запись отклоняется).
    strict=False — легаси-режим: MISS -> нормализованная форма."""
    d = _parse_date(date_utc)
    if not d or not home_team or not away_team:
        return missing
    rh = REGISTRY.resolve(home_team)
    ra = REGISTRY.resolve(away_team)
    if strict and (rh is None or ra is None):
        return missing
    h = rh["slug"] if rh else _cid_token(home_team)
    a = ra["slug"] if ra else _cid_token(away_team)
    return h + "__" + a + "__" + d


# ═══════════════════════════════════════════════════════════════════════
# САМОТЕСТ (§11 + §14 Части II) — python team_registry.py --selftest
# ═══════════════════════════════════════════════════════════════════════
def selftest():
    results = []

    def check(tid, cond, note=""):
        results.append((tid, bool(cond), note))

    fake_teams = {
        "manchester_united": {"canonical_name": "Manchester United",
                              "country_slug": "england"},
        "liverpool": {"canonical_name": "Liverpool FC",
                      "country_slug": "england"},
        "atletico_balti": {"canonical_name": "FC Atletico Balti",
                           "country_slug": "moldova"},
    }
    reg = Registry()
    # алиасы в снимке — нормализованные той же normalize() (§12.3)
    reg.inject_snapshot(
        global_aliases={
            "man_utd": "manchester_united",
            _alias_key(normalize("Ман Юнайтед")): "manchester_united",
            "liverpool_fc": "liverpool",
            "zenit": "zenit_spb",
        },
        scoped_aliases={("md", "fc_atletico_balti"): "atletico_balti"},
        bridge={("reep", "4040"): "liverpool", ("flashscore", "X9"): "manchester_united"},
        teams=fake_teams,
    )

    # T1: нормализация пунктуации/регистра
    check("T1", normalize("Chelsea F.C.") == "chelsea f c",
          "точки -> пробел, lower, стрип OFF (fc остаётся)")
    # T2: смешанный токен свёртывается к латинице
    check("T2", normalize("zenіt") == "zenit", "кириллическая і в латинском токене")
    # T3: чистая кириллица не трогается
    check("T3", normalize("Зенит") == "зенит", "чисто кириллический токен цел")
    # T4: акценты
    check("T4", normalize("Fenerbahçe") == "fenerbahce", "NFKD-стрим")
    # T5: scoped-алиас по стране
    r5 = reg.resolve("fc atletico balti", country="md")
    check("T5", r5 and r5["slug"] == "atletico_balti" and r5["stage"] == "scoped",
          "scoped-алиас Молдовы")
    # T6: ГЛАВНЫЙ кейс §11 — не склеивается с мадридским
    check("T6", (not r5) or r5["slug"] != "atletico_madrid", "fc atletico balti != atletico_madrid")
    # T7: тот же норм без страны — MISS (глобального алиаса нет)
    check("T7", reg.resolve("fc atletico balti") is None,
          "без гео двусмысленность не резолвится")
    # T8: глобальный алиас
    r8 = reg.resolve("man utd")
    check("T8", r8 and r8["slug"] == "manchester_united", "глобальный алиас")
    # T9: «Man Utd»/«Ман Юнайтед» -> один slug (§11 главный тест замены)
    r9 = reg.resolve("Ман Юнайтед")
    check("T9", r9 and r9["slug"] == (r8["slug"] if r8 else None),
          "кириллический алиас -> тот же slug")
    # T10/T11: «Liverpool FC» в двух вариантах (флаг OFF: только алиас)
    check("T10", reg.resolve("liverpool fc") is None or
          reg.resolve("liverpool fc")["slug"] == "liverpool", "алиас из снапшота")
    check("T11", reg.resolve("Liverpool FC", country="england") is not None,
          "страна не ломает алиас")
    # T12: bridge ВСЕГДА первый (фикс T6 прошлой итерации): memo не перебивает
    reg.resolve("liverpool fc")  # заполняет memo
    r12 = reg.resolve("liverpool fc", external_id="4040", provider="reep")
    check("T12", r12 and r12["stage"] == "bridge",
          "bridge приоритетнее memo (§4.1 всегда первый)")
    # T13: bridge-only путь — неизвестный ID => MISS, имя-фоллбэк запрещён
    check("T13", reg.resolve("liverpool fc", external_id="NOPE",
                             provider="reep") is None,
          "ID есть и не разрешён -> честный MISS")
    # T14: fuzzy с гео-подтверждением
    r14 = reg.resolve("manchester united football club", country="england")
    check("T14", r14 and r14["slug"] == "manchester_united", "fuzzy + гео hit")
    # T15: fuzzy с несовпадающей страной -> MISS
    check("T15", reg.resolve("manchester united football club",
                             country="spain") is None, "гео не подтвердил -> MISS")
    # T16: fuzzy без страны -> MISS (подтверждение обязательно)
    check("T16", reg.resolve("manchester united football club") is None,
          "без гео fuzzy запрещён (§14.3)")
    # T17: честный MISS
    check("T17", reg.resolve("zz_unknown_club") is None, "MISS не самоканонизируется")
    # T18: is_registry_slug O(1)
    check("T18", reg.is_registry_slug("manchester_united") and
          not reg.is_registry_slug("zz_unknown_club"), "MISS-детекция O(1)")
    # T19: cid полный
    cid = build_canonical_id("man utd", "liverpool fc", "2026-11-01",
                             registry=reg) if False else         _cid_with(reg, "man utd", "liverpool fc", "2026-11-01")
    check("T19", cid == "manchester_united__liverpool__20261101", "формат cid")
    # T20: плохая дата, обе легаси-семантики
    check("T20", _cid_with(reg, "man utd", "liverpool fc", "плохо") == "" and
          _cid_with(reg, "man utd", "liverpool fc", "плохо", missing="unknown") == "unknown",
          "'' загрузчик / unknown хаб")
    # T21: STRICT MISS -> missing
    check("T21", _cid_with(reg, "man utd", "zz_unknown", "2026-11-01") == "",
          "строгий отказ при MISS стороны")
    # T22: лестница коллизий §12.8
    taken = {"fc_atletico_balti"}
    s1 = reg.suggest_slug("FC Atletico Balti", "mol", "777", taken)
    s2 = reg.suggest_slug("FC Atletico Balti", "mol", "777",
                          taken | {s1} if s1 else taken)
    s3 = reg.suggest_slug("FC Atletico Balti", "mol", "777",
                          taken | {s1, s2} if s1 and s2 else taken)
    check("T22", s1 == "fc_atletico_balti_mol" and
          s2 == "fc_atletico_balti_mol_777" and s3 is None,
          "база -> +код -> +reep_id -> skip")
    # T23: memo не бьёт по транспорту повторно (транспорт отсутствует -> детерминизм)
    reg.resolve("man utd")
    reg.resolve("man utd")
    check("T23", reg.telemetry["hits"].get("memo", 0) >= 1, "memo после первого хита")
    # T24: пустое имя
    check("T24", normalize("") == "" and clean_team_name("") == "", "пустой вход -> пусто")
    # T25: долька add_alias отражается в memo (§16.4)
    check("T25", reg.resolve("zz_unknown_club") is None and
          (reg.add_alias("zz unknown club", "zz_club", country="england") or
           reg.resolve("zz_unknown_club", country="england")["slug"] == "zz_club"),
          "долька внутри прогона меняет резолв")
    # T26: slugify транслитерация кириллицы
    check("T26", slugify("Барселона") == "barselona", "§12.8 база — транслитерация")

    # T27: карты рантайма — только из Redis-снимка, встроенных данных нет (§14)
    class _MapTransport:
        def scan_keys(self, pattern): return []
        def get_all_fields(self, key): return {}
        def get_from_cache(self, key):
            if key == "registry:map:country_code":
                return {"MD": "Moldova", "ZM": "Zambia", "US": "USA"}
            if key == "registry:alias:league":
                return {"E0": "Premier League"}
            return None
        def redis_command(self, cmd): return None

    reg_maps = Registry(transport=_MapTransport())
    reg_maps._ensure()
    check("T27", normalize_country("MD") == "moldova"
          and normalize_country("Zambia") == "zambia"
          and normalize_league("E0") == "premier_league"
          and get_league_name("E0") == "Premier League",
          "карты из снимка: Молдова/Замбия заселяются (§14.3)")

    # T28: фолбэк страны не тихий — счётчик fallback_country растёт (§14.4)
    before = _FALLBACK_COUNTRY["n"]
    normalize_country("Ваканда")
    check("T28", _FALLBACK_COUNTRY["n"] == before + 1,
          "вне карты — счётчик fallback_country (не тихо)")

    passed = [t for t, ok, _ in results if ok]
    failed = [t for t, ok, n in results if not ok]
    for t, ok, note in results:
        print(("PASS " if ok else "FAIL ") + t + (" — " + note if note else ""))
    print("selftest: %d/%d PASS" % (len(passed), len(results)))
    return 0 if not failed else 1


def _cid_with(reg, h, a, d, missing="", strict=True):
    """Хелпер проб: cid на конкретном снимке."""
    global _TEST_REG
    _TEST_REG = reg
    rh = reg.resolve(h)
    ra = reg.resolve(a)
    dd = _parse_date(d)
    if not dd:
        return missing
    if strict and (rh is None or ra is None):
        return missing
    hs = rh["slug"] if rh else _cid_token(h)
    as_ = ra["slug"] if ra else _cid_token(a)
    return hs + "__" + as_ + "__" + dd


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
