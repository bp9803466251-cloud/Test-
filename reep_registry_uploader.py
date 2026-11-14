#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reep_registry_uploader.py — v0.4-registry-delegation (rebuild)

v0.4.2-selftest-isolation (исправление загрязнения прода селфтестом, прогон #3):
  - T11/T12 (checkpoint round-trip) исполнялись с реальным транспортом:
    _checkpoint_redis тянет redis_command из redis_hub в момент вызова,
    а в Actions с живыми секретами это прод — тестовый test-tag записывался
    в Redis реальным SET. Изоляция: ин-мемори фейк-транспорт для T11/T12,
    реальный восстанавливается в finally + проба T19 на восстановление.
  - Проба T11a: фейк-хранилище реально использовано тестом.

v0.4.3-alias-routing (исправление контрактного брака роутинга алиасов, прогон #16):
  - split_global_scoped: ВСЕ алиасы команд уходят формой team_alias
    (уникальные — в глобальный хэш registry:alias:team, двусмысленные с
    гео — scoped-ключ). Прежняя отсылка формы team попадала в team-ветку
    хаба: сверка canonical_name, которого у алиаса нет, — фантомный
    конфликт (прогон #16: 36579 конфликтов, записи отброшены) и медленный
    поштучный чтение-путь (~0.1 c/запись -> таймаут #16).
  - алиасы соревнований уходят формой competition с полями alias + slug +
    canonical_name (канон лиги-цели): хаб v9.6.9.3 пишет их только в карту
    registry:alias:league, тело лиги не трогает.
  - P0: ключ лиг canonical -> canonical_name (хаб читает canonical_name;
    прежде канон деградировал в slug — тела лиг без имён).

v0.4-registry-delegation (Шаг 9.4, памятка v1.10.0, §14.1/§14.4; вердикты владельца от 12.11):
  - Локальные _HOMOGLYPH/_CYR_LAT/_fold_mixed/normalize/translit/slugify УДАЛЕНЫ:
    вторая копия словаря исключена, делегация в team_registry — строгий импорт,
    ImportError -> FATAL + SystemExit(1).
  - country_code: seed-артефакт country_code_map (§14.4); ISO-2 в нижнем регистре
    (образец §12.6: fc_atletico_balti_md); канон-фиксы Молдова->md, Замбия->za;
    алиасы (United States->us) — явными записями журнала, не тихо.
  - P0 вобрал seed-карты: build_country_records -> entity_type country_code
    (шестая форма хаба v9.6.9.1); Reep-канон побеждает seed-канон.
  - Fix лестницы: cc для коллизий — нижний код (был верхний RU после консолидации;
    самотест v0.3 в этой среде был красным: 11 PASS / 3 FAIL — ожидания кодировали
    legacy-fallback, поднят до честного ISO-канона).

Автономный заливщик реестра Reep. По памятке v1.9.3 (§9 п.4, §14, §16):
Reep-снапшот -> normalize() (§5) -> JSONL дельты -> bulk upsert_registry.

Режимы:
  --from-url          по умолчанию: скачать таблицы Reep, залить реестр
  --from-dir DIR      читать локальные CSV/gz из каталога (сверка схемы, восстановление)
  --parties P0,P1:0   выбрать партии (по умолчанию P0 — разминка, §16)
  --dry-run           разбор и оценка объёмов без записи (§8 вариант 2)
  --resume            продолжить с чекпоинта
  --selftest          встроенные пробы на синтетике

Строгие правила (памятка):
  - запись в registry:* — только через hub.upsert_registry (§1, §7);
  - fallback-словарей и молчаливой самоканонизации нет (§14.1, §6.4);
  - чексуммы сверяются с указателем reep.football/data/latest.json;
    расхождение — IntegrityError, приём неподтверждённых данных запрещён;
  - смена схемы Reep — exit 1 со списком реально найденных колонок;
  - flush отсюда недоступен: только redis_flush.py (урок F1).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

VERSION = "0.4.3-alias-routing"
REEP_BASE = "https://reep.football"
POINTER_URL = REEP_BASE + "/data/latest.json"
DOWNLOAD_URL = REEP_BASE + "/downloads/csv/{table}"
USER_AGENT = "GatekeeperAI-reep-registry-uploader/" + VERSION
TABLES = ("entities", "provider_aliases", "redirects")
CACHE_DIR_DEFAULT = "reep_cache"
CHECKPOINT_KEY = "registry:upload:checkpoint"
STRIP_SUFFIX_ENABLED = False   # §12.1: OFF до вердикта; включение — отдельный прогон с pre-diff
SOFT_LIMIT_DEFAULT = 10000     # §13.4: команд/день Upstash
HUB_BATCH = 500
CHUNK_TEAM = 10000
CHUNK_ALIAS = 50000
STOP_FILE = "STOP"
REPORT_FILE = "report_v0_3.json"  # имя закреплено контрактом (workflow-артефакт, §9 п.7) — не переименовывать


# --- исключения -------------------------------------------------------------

class UploaderError(Exception): pass
class NetworkError(UploaderError): pass
class SchemaError(UploaderError): pass
class IntegrityError(UploaderError): pass
class HubContractError(UploaderError): pass
class SoftLimitReached(Exception): pass


# --- детекция колонок (смена схемы Reep — честный отказ, не мусор) ----------

REQUIRED = {
    "entities": ("id", "type", "name"),
    "provider_aliases": ("reep_id", "alias"),
    "redirects": ("old_id", "reep_id"),
}

COL_CANDS = {
    "entities": {
        "id": ("reep_id", "id", "entity_id"),
        "type": ("type", "entity_type", "kind"),
        "name": ("canonical_name", "name", "label", "display_name"),
        "latin": ("name_en", "english_name", "canonical_name_en", "name_latin"),
        "country": ("country", "country_name", "country_slug", "nation"),
        "slug": ("slug", "canonical_slug"),
    },
    "provider_aliases": {
        "reep_id": ("reep_id", "entity_id", "id"),
        "provider": ("provider", "provider_id", "source"),
        "external_id": ("external_id", "ext_id", "provider_entity_id", "external_entity_id"),
        "alias": ("alias", "name", "label", "display_name"),
        "type": ("type", "entity_type", "kind"),
    },
    "redirects": {
        "old_id": ("old_id", "from_id", "source_id", "redirect_from", "id"),
        "reep_id": ("reep_id", "target_id", "canonical_id", "to_id", "redirect_to"),
    },
}


def detect_columns(header, table):
    spec = COL_CANDS[table]
    low = [h.strip().lower() for h in header]
    found, missing = {}, []
    for role, cands in spec.items():
        for c in cands:
            if c in low:
                found[role] = low.index(c)
                break
        else:
            missing.append(role)
    req_missing = [r for r in REQUIRED[table] if r in missing]
    if req_missing:
        raise SchemaError(
            "%s: не найдены обязательные колонки %s; заголовок: %r" % (table, req_missing, header))
    return found


def open_table(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return open(path, "rt", encoding="utf-8", errors="replace", newline="")


def open_parsed(path, table):
    f = open_table(path)
    r = csv.reader(f)
    header = next(r, None)
    if header is None:
        raise SchemaError("%s: пустой файл" % path)
    return f, r, detect_columns(header, table)


# --- Нормализация §5: делегация в team_registry (Strict Degradation, §14.1) ---
# Эпитафия: локальные _HOMOGLYPH/_CYR_LAT/_fold_mixed удалены — вторая копия
# словаря исключена из ВСЕХ путей (§14.1); резолв мимо единого реестра запрещён.
try:
    from team_registry import normalize as _registry_normalize
    from team_registry import translit as _registry_translit
    from team_registry import slugify as _registry_slugify
except ImportError as _reg_err:
    print("[FATAL] team_registry недоступен (%s) — Strict Degradation Protocol "
          "(§14.1): нормализация мимо единого реестра запрещена." % _reg_err)
    raise SystemExit(1)

# Seed-артефакт карт стран (§14.4): без него P0 не заселяет registry:map:country_code.
try:
    from country_code_map import COUNTRY_CODE_MAP as _SEED_COUNTRY_MAP
except ImportError as _seed_err:
    print("[FATAL] country_code_map недоступен (%s) — seed-артефакт заселения "
          "обязателен (§14.4)." % _seed_err)
    raise SystemExit(1)


def normalize(name):
    return _registry_normalize(name)


def translit(s):
    return _registry_translit(s)


def slugify(name):
    return _registry_slugify(name)


# Директива 12.11: дыры seed-карты восполняются явно (вердикт владельца 12.11).
CANON_COUNTRY_FIX = {"moldova": "md", "zambia": "za"}
# Явный алиас канона (вердикт владельца: USA vs United States — алиас, не тихо).
CANON_COUNTRY_ALIAS = {"united states": "us"}
COUNTRY_ALIAS_JOURNAL = []   # каждая подмена фиксируется — попадает в отчёт


def country_code(country):
    """Код страны лестницы коллизий (§12.6/§12.8): ISO-2 в нижнем регистре.
    Seed — country_code_map; неизвестное — транслит 3-буквенный + запись в журнал."""
    if not country:
        return ""
    n = _registry_normalize(country)
    if not n:
        return ""
    if n in CANON_COUNTRY_FIX:
        COUNTRY_ALIAS_JOURNAL.append({"kind": "canon_fix", "input": country,
                                      "code": CANON_COUNTRY_FIX[n]})
        return CANON_COUNTRY_FIX[n]
    if n in CANON_COUNTRY_ALIAS:
        COUNTRY_ALIAS_JOURNAL.append({"kind": "alias", "input": country,
                                      "code": CANON_COUNTRY_ALIAS[n]})
        return CANON_COUNTRY_ALIAS[n]
    cands = [k for k, v in _SEED_COUNTRY_MAP.items() if _registry_normalize(str(v)) == n]
    if cands:
        two = [k for k in cands if len(k) == 2]
        return (two[0] if two else sorted(cands)[0]).lower()
    code = _registry_translit(re.sub(r"[^a-z ]", "", n)).replace(" ", "")[:3]
    if code:
        COUNTRY_ALIAS_JOURNAL.append({"kind": "fallback", "input": country, "code": code})
    return code


# --- сеть -------------------------------------------------------------------

def http_open(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def http_get_bytes(url, timeout=60, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            with http_open(url, timeout) as resp:
                return resp.geturl(), resp.read()
        except Exception as exc:
            last = exc
            time.sleep(2 * attempt)
    raise NetworkError("%s: %s" % (url, last))


def probe_url(url):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(2)
            return resp.status in (200, 206)
    except Exception:
        return False


_TAG_RE = re.compile(r"/releases/([^/]+)/")


def extract_tag(url):
    m = _TAG_RE.search(url or "")
    return m.group(1) if m else None


def substitute_tag(url, tag):
    return _TAG_RE.sub("/releases/%s/" % tag, url)


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha_strict(path, ptr, table):
    expected = ((ptr or {}).get("files") or {}).get(table + ".csv.gz", {}).get("sha256")
    if not expected:
        print("[warn] %s: в указателе нет чексуммы — приём без верификации" % table)
        return True
    got = file_sha(path)
    if got != expected:
        raise IntegrityError(
            "%s: sha256 не совпал (got %s, ожидается %s). Приём неподтверждённых данных запрещён."
            % (table, got[:12], expected[:12]))
    return True


def _download_stream(url, path):
    """Стрим в .part, переименование по завершении; 3 ретрая; возвращает (final_url, sha256)."""
    last = None
    for attempt in range(1, 4):
        h = hashlib.sha256()
        tmp = path + ".part"
        try:
            with http_open(url) as resp:
                final_url = resp.geturl()
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
            os.replace(tmp, path)
            return final_url, h.hexdigest()
        except Exception as exc:
            last = exc
            if os.path.exists(tmp):
                os.remove(tmp)
            time.sleep(2 * attempt)
    raise NetworkError("%s: %s" % (url, last))


def ensure_table(table, cache_dir, ptr):
    """Скачать таблицу и сверить чексумму с указателем. Расхождение — отказ."""
    tag_dir = os.path.join(cache_dir, ptr["release_tag"])
    os.makedirs(tag_dir, exist_ok=True)
    path = os.path.join(tag_dir, table + ".csv.gz")
    if os.path.exists(path):
        try:
            verify_sha_strict(path, ptr, table)
            return path
        except IntegrityError:
            print("[warn] %s: кэш не совпал с указателем — перекачиваем" % table)
    final_url, _ = _download_stream(DOWNLOAD_URL.format(table=table), path)
    served = extract_tag(final_url) or "?"
    try:
        verify_sha_strict(path, ptr, table)
        print("[info] %s: скачан (обслужен релиз %s)" % (table, served))
        return path
    except IntegrityError:
        if served != ptr["release_tag"]:
            fresh = substitute_tag(final_url, ptr["release_tag"])
            if probe_url(fresh):
                print("[info] %s: повторная качка свежего релиза %s" % (table, ptr["release_tag"]))
                _download_stream(fresh, path)
                verify_sha_strict(path, ptr, table)
                return path
        raise


def fetch_pointer(cache_dir):
    _, data = http_get_bytes(POINTER_URL)
    ptr = json.loads(data.decode("utf-8"))
    if not ptr.get("release_tag"):
        raise IntegrityError("указатель latest.json без release_tag")
    with open(os.path.join(cache_dir, "pointer.json"), "w", encoding="utf-8") as f:
        json.dump(ptr, f, ensure_ascii=False, indent=2)
    return ptr


# --- разбор таблиц ----------------------------------------------------------

def parse_redirects(path):
    f, r, idx = open_parsed(path, "redirects")
    m = {}
    with f:
        for row in r:
            old, new = row[idx["old_id"]].strip(), row[idx["reep_id"]].strip()
            if old and new:
                m[old] = new
    return m


def resolve_redirects(rid, rmap):
    """Защита от self-loop и циклов (поймана на пробах v0.2)."""
    seen = set()
    while rid in rmap and rid not in seen:
        seen.add(rid)
        rid = rmap[rid]
    return rid


class Entities:
    def __init__(self):
        self.meta = {}            # reep_id -> {type, name, latin, country}
        self.teams = {}           # reep_id -> slug
        self.comps = {}           # reep_id -> slug
        self.cc = {}              # reep_id -> код страны
        self.skipped_by_type = {}
        self.skipped_empty = 0
        self.slug_skipped = 0


def parse_entities(path, rmap):
    f, r, idx = open_parsed(path, "entities")
    ents = Entities()
    i_id, i_type = idx["id"], idx["type"]
    i_name, i_latin = idx.get("name"), idx.get("latin")
    i_country = idx.get("country")
    with f:
        for row in r:
            rid = resolve_redirects(row[i_id].strip(), rmap)
            etype = (row[i_type].strip().lower() if i_type is not None else "unknown")
            if etype not in ("team", "competition"):
                ents.skipped_by_type[etype] = ents.skipped_by_type.get(etype, 0) + 1
                continue
            name = row[i_name].strip() if i_name is not None else ""
            if not name:
                ents.skipped_empty += 1
                continue
            if rid in ents.meta:
                continue
            ents.meta[rid] = {
                "type": etype,
                "name": name,
                "latin": row[i_latin].strip() if i_latin is not None else "",
                "country": row[i_country].strip() if i_country is not None else "",
            }
    return ents


class SlugState:
    """Лестница §12.8: база -> +код страны -> +reep_id. Коллизия — skip + флаг (§12.6)."""

    def __init__(self):
        self.taken = set()
        self.collisions = []

    def assign(self, base, cc, rid):
        if not base:
            return None
        if base not in self.taken:
            self.taken.add(base)
            return base
        cand = "%s_%s" % (base, cc) if cc else "%s_%s" % (base, rid)
        if cand not in self.taken:
            self.taken.add(cand)
            return cand
        cand = "%s_%s_%s" % (base, cc, rid)
        if cand not in self.taken:
            self.taken.add(cand)
            return cand
        self.collisions.append(base)
        return None


def build_slugs(ents):
    state = SlugState()
    for rid in sorted(ents.meta):
        ent = ents.meta[rid]
        base = slugify(ent["latin"] or ent["name"])
        if ent["type"] == "team":
            cc = country_code(ent["country"])
            ents.cc[rid] = cc
        else:
            cc = ""
        slug = state.assign(base, cc, rid)
        if slug is None:
            ents.slug_skipped += 1
            continue
        (ents.teams if ent["type"] == "team" else ents.comps)[rid] = slug
    return state


def parse_aliases(path, ents, rmap):
    """Поток по provider_aliases: алиасы (нормализованные §5) + внешние мосты."""
    f, r, idx = open_parsed(path, "provider_aliases")
    i_rid, i_alias = idx["reep_id"], idx["alias"]
    i_prov, i_ext, i_type = idx.get("provider"), idx.get("external_id"), idx.get("type")
    aliases, ext_bridges = [], []
    orphans = skipped_empty = 0
    with f:
        for row in r:
            rid = resolve_redirects(row[i_rid].strip(), rmap)
            alias = row[i_alias].strip()
            if not alias:
                skipped_empty += 1
                continue
            slug = ents.teams.get(rid) or ents.comps.get(rid)
            if not slug:
                orphans += 1
                continue
            etype = ents.meta.get(rid, {}).get("type") or "team"
            if i_type is not None and row[i_type].strip():
                etype = row[i_type].strip().lower()
            norm = normalize(alias)
            if not norm:
                skipped_empty += 1
                continue
            prov = row[i_prov].strip() if i_prov is not None else ""
            aliases.append((etype, norm, slug, ents.cc.get(rid, ""), prov))
            if i_ext is not None and row[i_ext].strip():
                ext_bridges.append((etype, prov, row[i_ext].strip(), slug))
    return aliases, ext_bridges, orphans, skipped_empty


def split_global_scoped(aliases, comp_canon=None):
    """§4 шаг 3: алиас, ведущий к slug'ам разных команд, в глобальный индекс не пишется.

    [v0.4.3] Роутинг по формам хаба: ЛЮБОЙ алиас команды уходит формой
    team_alias — уникальный глобально (хэш registry:alias:team), двусмысленный
    с гео — scoped-ключом. Прежняя отсылка уникальных алиасов формой team
    (v0.4.2) попадала в team-ветку хаба: сверка canonical_name у алиаса —
    фантомный конфликт, запись отбрасывалась (прогон #16: 36579 конфликтов,
    алиасы не записаны; см. docstring версии).
    Алиас соревнования уходит формой competition с полями alias + slug +
    canonical_name: хаб v9.6.9.3 пишет его только в карту
    registry:alias:league ({alias: канон}), тело лиги не создаёт."""
    comp_canon = comp_canon or {}
    by_norm = {}
    for etype, norm, slug, cc, prov in aliases:
        if etype == "team":
            by_norm.setdefault(norm, set()).add(slug)
    recs, ambiguous = [], 0
    seen = set()
    for etype, norm, slug, cc, prov in aliases:
        if etype == "team" and len(by_norm.get(norm, ())) > 1:
            if not cc:
                ambiguous += 1
                continue
            key = ("scoped", norm, slug, cc)
            rec = {"entity_type": "team_alias", "alias": norm, "slug": slug, "country": cc}
        elif etype == "team":
            key = (etype, norm, slug)
            rec = {"entity_type": "team_alias", "alias": norm, "slug": slug}
        else:
            key = (etype, norm, slug)
            rec = {"entity_type": etype, "alias": norm, "slug": slug,
                   "canonical_name": comp_canon.get(slug, "")}
        if key in seen:
            continue
        seen.add(key)
        rec["provider"] = prov
        recs.append(rec)
    return recs, ambiguous


# --- партии (§16) -----------------------------------------------------------

def build_country_records(ents):
    """Seed страны (§14.4): карта -> registry:map:country_code (шестая форма хаба).
    Reep-канон побеждает seed-канон; каждое расхождение — явная запись журнала."""
    canon = {}
    for code, name in _SEED_COUNTRY_MAP.items():
        canon[str(code).upper()] = str(name)
    for cname, code in (("Moldova", "MD"), ("Zambia", "ZA")):
        if code not in canon:
            canon[code] = cname
            COUNTRY_ALIAS_JOURNAL.append({"kind": "canon_fix", "code": code, "canonical": cname})
    for cname in sorted({e["country"] for e in ents.meta.values() if e.get("country")}):
        code = country_code(cname)
        if not code:
            continue
        cu = code.upper()
        if cu in canon and canon[cu] != cname:
            COUNTRY_ALIAS_JOURNAL.append({"kind": "reep_alias", "code": cu,
                                          "seed": canon[cu], "reep": cname})
            canon[cu] = cname  # Reep-канон побеждает (§14.1)
        elif cu not in canon:
            canon[cu] = cname
    return [{"entity_type": "country_code", "code": c, "canonical": canon[c]}
            for c in sorted(canon)]


def build_parties(ents, alias_recs, ext_bridges):
    parties = []
    # P0: seed-карты стран (§14.4) + лиги + их reep-мосты (~3 тыс. записей — разминка)
    recs = build_country_records(ents)
    for rid in sorted(ents.comps):
        recs.append({"entity_type": "competition", "reep_id": rid,
                     "slug": ents.comps[rid], "canonical_name": ents.meta[rid]["name"]})
    for rid in sorted(ents.comps):
        recs.append({"entity_type": "reep_bridge", "reep_id": rid, "slug": ents.comps[rid]})
    parties.append({"id": "P0", "records": recs})
    # P1: команды чанками (тело + reep-мост)
    teams = sorted(ents.teams)
    for n, start in enumerate(range(0, len(teams), CHUNK_TEAM)):
        recs = []
        for rid in teams[start:start + CHUNK_TEAM]:
            recs.append({"entity_type": "team", "reep_id": rid, "slug": ents.teams[rid],
                         "canonical_name": ents.meta[rid]["name"]})
            recs.append({"entity_type": "reep_bridge", "reep_id": rid, "slug": ents.teams[rid]})
        parties.append({"id": "P1:%d" % n, "records": recs})
    # P2..: алиасы и внешние мосты по провайдерам чанками
    groups = {}
    for rec in alias_recs:
        groups.setdefault(rec.get("provider") or "", []).append(rec)
    for rec in ext_bridges:
        groups.setdefault("bridge:" + (rec[1] or ""), []).append(
            {"entity_type": "ext_bridge", "entity": rec[0], "provider": rec[1],
             "external_id": rec[2], "slug": rec[3]})
    for prov in sorted(groups):
        items = groups[prov]
        for n, start in enumerate(range(0, len(items), CHUNK_ALIAS)):
            parties.append({"id": "P2:%s:%d" % (prov or "unknown", n),
                            "records": items[start:start + CHUNK_ALIAS]})
    return parties


def flush_records(hub, recs):
    """Разбивка по entity_type и батчевая подача (сигнатура хаба — допущение A1)."""
    groups = {}
    for rec in recs:
        groups.setdefault(rec["entity_type"], []).append(rec)
    for et, items in groups.items():
        for start in range(0, len(items), HUB_BATCH):
            batch = [{k: v for k, v in rec.items() if k != "entity_type"}
                     for rec in items[start:start + HUB_BATCH]]
            hub.upsert(et, batch)


# --- хаб: единственная дверь записи ----------------------------------------

class Hub:
    def __init__(self, command_limit):
        import gatekeeper_hub as gh   # строгий импорт; деградации нет (§14.1)
        self._gh = gh
        if not hasattr(gh, "upsert_registry"):
            raise HubContractError(
                "gatekeeper_hub.upsert_registry не найден — свери контракт (допущение A1)")
        self.commands = 0
        self.limit = command_limit

    def upsert(self, entity_type, records, source="reep"):
        if not records:
            return
        self.commands += len(records) + 1
        if self.limit and self.commands > self.limit:
            raise SoftLimitReached(
                "софт-лимит %d команд исчерпан — чекпоинт сохранён, продолжение по --resume" % self.limit)
        self._gh.upsert_registry(entity_type, records, source)


def hub_client(hub):
    if hub is None:
        return None
    for attr in ("client", "redis", "r", "_redis"):
        c = getattr(hub._gh, attr, None)
        if c is not None and hasattr(c, "get"):
            return c
    return None


# --- чекпоинты --------------------------------------------------------------

def checkpoint_path(cache_dir, tag):
    return os.path.join(cache_dir, tag, "checkpoint.json")


def _checkpoint_redis(cmd_parts):
    """[v0.4.1 FIX] Прямая запись/чтение чекпоинта через Root-транспорт.

    hub_client(hub) перебирал атрибуты client/redis/r/_redis у модуля
    gatekeeper_hub и почти всегда возвращал None (клиент живёт под другим
    именем) — чекпоинт в Redis молча не записывался никогда. Теперь —
    redis_command из redis_hub (та же дверь, что у history и мостов).
    Локальная копия остаётся источником истины при недоступном Redis."""
    try:
        from redis_hub import redis_command
        return redis_command(cmd_parts)
    except Exception as exc:
        print("[warn] чекпоинт: Redis-транспорт недоступен: %s (локальная копия в силе)" % exc)
        return None


def save_checkpoint(cache_dir, tag, done, hub=None):
    data = json.dumps({"tag": tag, "done": sorted(done), "ts": time.time()})
    p = checkpoint_path(cache_dir, tag)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(data)
    _checkpoint_redis(["SET", CHECKPOINT_KEY, data])


def load_checkpoint(cache_dir, tag, hub=None):
    raw = _checkpoint_redis(["GET", CHECKPOINT_KEY])
    if raw:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            d = json.loads(raw)
            if d.get("tag") == tag:
                return set(d.get("done") or [])
        except Exception:
            pass
    p = checkpoint_path(cache_dir, tag)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("tag") == tag:
            return set(d.get("done") or [])
    return set()


def is_shutdown_requested():
    return os.path.exists(STOP_FILE) or os.environ.get("GATEKEEPER_STOP") == "1"


# --- основной прогон --------------------------------------------------------

def write_delta(tag_dir, party_id, records):
    d = os.path.join(tag_dir, "deltas")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", party_id)
    path = os.path.join(d, safe + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def run(args):
    t0 = time.time()
    cache_dir = args.cache_dir
    os.makedirs(cache_dir, exist_ok=True)

    if args.from_dir:
        tag = os.path.basename(os.path.normpath(args.from_dir)) or "local"
        tag_dir = args.from_dir
        ptr = None
        print("[info] режим --from-dir: %s (чексуммы по локальному pointer.json, если есть)" % tag_dir)
        for table in TABLES:
            p = os.path.join(tag_dir, table + ".csv.gz")
            if not os.path.exists(p):
                if os.path.exists(p[:-3]):
                    print("[warn] %s: найден неупакованный csv.gz — возьму .csv" % table)
                else:
                    raise SchemaError("%s: файл не найден в %s" % (table, tag_dir))
    else:
        ptr = fetch_pointer(cache_dir)
        tag = ptr["release_tag"]
        tag_dir = os.path.join(cache_dir, tag)
        os.makedirs(tag_dir, exist_ok=True)
        for table in TABLES:
            ensure_table(table, cache_dir, ptr)

    print("[info] разбор redirects...")
    rmap = parse_redirects(os.path.join(tag_dir, "redirects.csv.gz"))
    print("[info] разбор entities...")
    ents = parse_entities(os.path.join(tag_dir, "entities.csv.gz"), rmap)
    state = build_slugs(ents)
    print("[info] разбор provider_aliases...")
    aliases, ext_bridges, orphans, skip_empty = parse_aliases(
        os.path.join(tag_dir, "provider_aliases.csv.gz"), ents, rmap)
    comp_canon = {slug: (ents.meta.get(rid) or {}).get("name", "")
                  for rid, slug in ents.comps.items()}
    alias_recs, ambiguous = split_global_scoped(aliases, comp_canon)
    parties = build_parties(ents, alias_recs, ext_bridges)
    wanted = [p for p in parties if p["id"] in set(x.strip() for x in args.parties.split(","))] \
        if args.parties != "ALL" else parties

    report = {
        "version": VERSION,
        "tag": tag,
        "mode": "dir" if args.from_dir else "url",
        "entities": {
            "teams": len(ents.teams), "competitions": len(ents.comps),
            "skipped_by_type": ents.skipped_by_type, "skipped_empty": ents.skipped_empty,
            "slug_collisions": len(state.collisions), "slug_skipped": ents.slug_skipped,
        },
        "aliases": {"records": len(alias_recs), "orphans": orphans,
                    "skipped_empty": skip_empty, "ambiguous_global": ambiguous},
        "ext_bridges": len(ext_bridges),
        "parties_total": len(parties),
        "parties_wanted": [p["id"] for p in wanted],
        "countries": {
            "records": sum(1 for p in parties for r in p["records"]
                           if r["entity_type"] == "country_code"),
            "alias_journal": COUNTRY_ALIAS_JOURNAL,
        },
    }

    if args.dry_run:
        report["status"] = "dry-run"
        report["records_estimate"] = sum(len(p["records"]) for p in wanted)
        _finish(report, cache_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
        return 0

    hub = Hub(int(os.environ.get("GATEKEEPER_COMMAND_SOFT_LIMIT", SOFT_LIMIT_DEFAULT)))
    done = load_checkpoint(cache_dir, tag, hub) if args.resume else set()
    completed, status = [], "done"
    for party in wanted:
        if party["id"] in done:
            print("[info] %s: уже завершена (resume)" % party["id"])
            completed.append(party["id"])
            continue
        if is_shutdown_requested():
            status = "stopped"
            break
        try:
            write_delta(tag_dir, party["id"], party["records"])
            flush_records(hub, party["records"])
            completed.append(party["id"])
            save_checkpoint(cache_dir, tag, done | set(completed), hub)
            print("[info] %s: залито записей %d" % (party["id"], len(party["records"])))
        except SoftLimitReached as exc:
            print("[warn] %s" % exc)
            status = "degraded"
            break
        except HubContractError:
            raise
        except Exception as exc:
            print("[error] %s: %r" % (party["id"], exc))
            status = "error"
            break

    report.update({"status": status, "completed": completed, "commands": hub.commands})
    _finish(report, cache_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    return 0 if status in ("done", "degraded", "stopped") else 1


def _finish(report, cache_dir):
    with open(os.path.join(cache_dir, REPORT_FILE), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


# --- встроенные пробы на синтетике -----------------------------------------

def selftest():
    import tempfile
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS %s" % name)
        else:
            fail += 1
            print("FAIL %s %s" % (name, extra))

    tmp = tempfile.mkdtemp(prefix="reep_selftest_")

    def gz(name, text):
        p = os.path.join(tmp, name)
        with gzip.open(p, "wt", encoding="utf-8", newline="") as f:
            f.write(text)
        return p

    ents_csv = gz("entities.csv.gz", (
        "id,type,name,name_en,country\n"
        'e1,competition,FA Premier League,FA Premier League,England\n'
        't1,team,Arsenal,Arsenal,England\n'
        't2,team,Арсенал Тула,Arsenal Tula,Russia\n'
        't3,team,Arsenal Tula,Arsenal Tula,Russia\n'
        't4,team,"Спартак, Spartak Moscow",Spartak Moscow,Russia\n'
        't7,team,Arsenal Tula,Arsenal Tula,Russia\n'
        't8,team,Dinamo,Dinamo,\n'))
    al_csv = gz("provider_aliases.csv.gz", (
        "provider,external_id,reep_id,alias\n"
        "opta,123,t1,Arsenal\n"
        "transfermarkt,9,t2,Арсенал Тула\n"
        "sofascore,7,t99,Orphan FC\n"
        "fotmob,1,t5,Tula Twin\n"
        "fotmob,2,t2,Tula\n"
        "fotmob,3,t3,Tula\n"
        "fotmob,4,t8,Tula\n"
        "opta,55,e1,EPL\n"))
    rd_csv = gz("redirects.csv.gz", "old_id,reep_id\nt5,t3\nx,x\n")

    rmap = parse_redirects(rd_csv)
    ents = parse_entities(ents_csv, rmap)
    state = build_slugs(ents)
    aliases, ext_bridges, orphans, skip_empty = parse_aliases(al_csv, ents, rmap)
    comp_canon = {slug: (ents.meta.get(rid) or {}).get("name", "")
                  for rid, slug in ents.comps.items()}
    alias_recs, ambiguous = split_global_scoped(aliases, comp_canon)
    parties = build_parties(ents, alias_recs, ext_bridges)

    check("T1 redirect resolution", ents.teams.get("t3") == "arsenal_tula_ru", ents.teams)
    check("T2 mixed-token folding", normalize("FС") == "fc", repr(normalize("FС")))
    check("T3 ladder: base", ents.teams.get("t1") == "arsenal", ents.teams)
    check("T3 ladder: +cc", ents.teams.get("t3") == "arsenal_tula_ru", ents.teams)
    check("T3 ladder: 2nd collision -> reep_id suffix", ents.teams.get("t7") == "arsenal_tula_ru_t7", ents.teams)
    check("T4 translit slug", ents.teams.get("t4") == "spartak_moscow", ents.teams)
    check("T5 orphan alias", orphans >= 1, orphans)  # t99 (вне реестра)
    check("T6 ambiguous global suppressed", ambiguous >= 1, ambiguous)
    check("T7 ext bridge captured", any(r[0] == "team" for r in ext_bridges), ext_bridges[:2])
    check("T8 parties contain P0 and P1", parties[0]["id"] == "P0" and any(p["id"].startswith("P1:") for p in parties), [p["id"] for p in parties[:3]])
    check("T9 comp records", ents.comps.get("e1") == "fa_premier_league", ents.comps)
    # [v0.4.3 ALIAS-ROUTING] контракт форм алиасов (прогон #16: 36579 фантомных конфликтов)
    _team_form = [r for r in alias_recs if r.get("entity_type") == "team"]
    _ta_recs = [r for r in alias_recs if r.get("entity_type") == "team_alias"]
    _ca_recs = [r for r in alias_recs
                if r.get("entity_type") == "competition" and r.get("alias")]
    check("T20 alias never in body forms", not _team_form, _team_form[:3])
    check("T21 global team alias as team_alias",
          any(r.get("alias") == "arsenal" and r.get("slug") == "arsenal"
              and "country" not in r for r in _ta_recs), _ta_recs[:3])
    check("T22 comp alias carries canonical_name",
          any(r.get("alias") == "epl" and r.get("canonical_name") == "FA Premier League"
              for r in _ca_recs), _ca_recs[:2])
    check("T23 P0 comp canon key",
          any(r.get("canonical_name") == "FA Premier League"
              for p in parties if p["id"] == "P0"
              for r in p["records"] if r.get("entity_type") == "competition"), "P0")

    try:
        bad = gz("bad.csv.gz", "foo,bar\n1,2\n")
        open_parsed(bad, "entities")
        check("T10 schema exit", False, "SchemaError не поднят")
    except SchemaError as exc:
        check("T10 schema exit", "не найдены обязательные" in str(exc) and "foo" in str(exc), exc)

    # [v0.4.2 SELFTEST-ISOLATION] checkpoint round-trip: ин-мемори транспорт
    # вместо прод-Redis. _checkpoint_redis импортирует redis_command из
    # redis_hub в момент вызова — с живыми секретами Actions селфтест писал
    # test-tag в прод (прогон #3, tag: test-tag после пуржа #28).
    _real_ckpt = _checkpoint_redis
    _fake_store = {}

    def _fake_ckpt(cmd_parts):
        op = cmd_parts[0] if cmd_parts else ""
        if op == "SET":
            _fake_store[cmd_parts[1]] = cmd_parts[2]
            return "OK"
        if op == "GET":
            return _fake_store.get(cmd_parts[1])
        return None

    globals()["_checkpoint_redis"] = _fake_ckpt
    try:
        save_checkpoint(tmp, "test-tag", {"P0"}, hub=None)
        check("T11 checkpoint round-trip", load_checkpoint(tmp, "test-tag", hub=None) == {"P0"})
        check("T12 checkpoint tag mismatch", load_checkpoint(tmp, "other-tag", hub=None) == set())
        check("T11a fake transport used", CHECKPOINT_KEY in _fake_store,
              "фейк-хранилище пусто — тест пошёл мимо изоляции")
    finally:
        globals()["_checkpoint_redis"] = _real_ckpt
    check("T19 transport restored", _checkpoint_redis is _real_ckpt,
          "реальный транспорт не восстановлен после T11/T12")

    # T13-T17: v0.4-registry-delegation (§14.1 делегация, §14.4 seed стран)
    check("T13 delegation normalize", normalize("Süper Lig") == "super lig",
          repr(normalize("Süper Lig")))
    check("T14 canon-fix Moldova", country_code("Moldova") == "md",
          repr(country_code("Moldova")))
    check("T15 canon-fix Zambia", country_code("Zambia") == "za",
          repr(country_code("Zambia")))
    _us = country_code("United States")
    check("T16 explicit alias United States->us",
          _us == "us" and any(j.get("kind") == "alias" and j.get("input") == "United States"
                              and j.get("code") == "us" for j in COUNTRY_ALIAS_JOURNAL),
          COUNTRY_ALIAS_JOURNAL[-3:])
    check("T17 epitaph no local dicts",
          not hasattr(sys.modules[__name__], "_HOMOGLYPH")
          and not hasattr(sys.modules[__name__], "_CYR_LAT"), "epitaph")
    check("T18 P0 carries country_code records",
          any(r["entity_type"] == "country_code" for r in parties[0]["records"]),
          parties[0]["records"][:2])

    print("---")
    print("итог: %d PASS, %d FAIL" % (ok, fail))
    return 0 if fail == 0 else 1


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Reep registry uploader %s" % VERSION)
    ap.add_argument("--from-dir", metavar="DIR", help="читать локальные CSV/gz вместо сети")
    ap.add_argument("--parties", default="P0", help="P0,P1:0,... или ALL (§16)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--cache-dir", default=CACHE_DIR_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.selftest:
            return selftest()
        return run(args)
    except (SchemaError, IntegrityError, HubContractError, NetworkError) as exc:
        print("[FATAL] %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
