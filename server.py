#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mapa nemovitostí přímo od majitelů – celá ČR.

Spuštění:   python3 server.py      → http://localhost:8765

Zdroje
  Sreality      – jen inzeráty soukromých inzerentů (bez realitní kanceláře)
  Bezrealitky   – inzerují tam jen majitelé
  Bazoš         – realitky se odfiltrují podle textu, jména a počtu inzerátů

Server běží nepřetržitě: nové inzeráty hlídá každých pár minut, kompletní
průchod (kvůli smazaným inzerátům) dělá v delších intervalech. Vše se ukládá
do složky data/, takže po restartu se nezačíná od nuly.
"""
import gzip
import hashlib
import html
import json
import math
import os
import random
import re
import signal
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from bisect import bisect_left
from collections import Counter, deque
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PORT = int(os.environ.get("PORT", "8765"))
TZ = ZoneInfo("Europe/Prague")

# Jak často se co obnovuje (v sekundách)
SR_POLL_EVERY = 120          # nové a změněné inzeráty na Sreality
SR_FULL_EVERY = 12 * 3600    # kompletní průchod Sreality (stahuje ~0,5 GB)
BZ_POLL_EVERY = 300          # první stránka každé kategorie Bazoše
BZ_FULL_EVERY = 6 * 3600
BR_POLL_EVERY = 300          # první stránka každé kategorie Bezrealitek
BR_FULL_EVERY = 12 * 3600
UD_POLL_EVERY = 300          # nejnovější inzeráty UlovDomov
UD_FULL_EVERY = 6 * 3600     # celý UlovDomov je malý (~80 stránek), ale každý dotaz je pro ně náročný

# Minimální rozestup mezi dotazy na jeden web – ať nikoho nezahlcujeme
HOST_GAP = {
    "www.sreality.cz": 0.4,
    "reality.bazos.cz": 1.2,
    "www.bezrealitky.cz": 6.0,     # při rychlejším tempu začnou posílat data po kapkách
    "nominatim.openstreetmap.org": 1.1,
    "ud.api.ulovdomov.cz": 2.0,
    "www.ulovdomov.cz": 1.0,
}
UA = "Mozilla/5.0"
GEO_UA = "mapa-nemovitosti-od-majitelu/1.0 (osobni pouziti)"
CZ_BBOX = (48.5, 51.1, 12.0, 18.9)
DAY_MS = 86400 * 1000
HOUR_MS = 3600 * 1000


# ───────────────────────── pomocné funkce ─────────────────────────

def now_ms():
    return int(time.time() * 1000)


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def num(s):
    if s is None:
        return None
    s = re.sub(r"[\s ]", "", str(s)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def today():
    return datetime.now(TZ).date()


def day_ms(d, hour=0):
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=TZ).timestamp() * 1000)


def resume_point(name, every):
    """Kde pokračovat v přerušeném kompletním průchodu (po restartu se nezačíná od nuly)."""
    r = store.meta.get(f"{name}_resume")
    if r and now_ms() - r["started"] < every * 1000:
        return r["started"], r["i"]
    return now_ms(), 0


def save_resume(name, started, i):
    if i is None:
        store.meta.pop(f"{name}_resume", None)
    else:
        store.meta[f"{name}_resume"] = {"started": started, "i": i}
    store.dirty = True


def in_cz(la, lo):
    return la is not None and lo is not None and CZ_BBOX[0] < la < CZ_BBOX[1] and CZ_BBOX[2] < lo < CZ_BBOX[3]


def parse_disp(text):
    t = norm(text)
    m = re.search(r"(?<![\d.,])([1-9])\s*\+\s*(kk|1)\b", t)
    if m:
        n = int(m.group(1))
        return "5+" if n >= 5 else f"{n}+{m.group(2)}"
    if re.search(r"garsonier|garsonk", t):
        return "1+kk"
    if "atypick" in t:
        return "atyp"
    return None


AREA_RE = re.compile(r"(?<![\d+])(\d{1,3}(?:[  .]\d{3})+|\d+)(?:[,.](\d{1,2}))?\s*(?:m2|m²|m\^2|metr|mtr)", re.I)


def parse_area(text):
    for m in AREA_RE.finditer(text or ""):
        v = num(m.group(1).replace(".", " "))
        if v and m.group(2):
            v += float("0." + m.group(2))
        if v and 5 <= v <= 2_000_000:
            return round(v, 1)
    return None


_host_next, _host_lock = {}, threading.Lock()


def fetch(url, ua=UA, tries=4, body=None):
    """Vrátí (status, bytes). 404/410/422 vrací hned, jinak to zkouší znovu. body = JSON pro POST."""
    host = urllib.parse.urlsplit(url).hostname
    for attempt in range(tries):
        gap = HOST_GAP.get(host, 0.5)
        with _host_lock:
            now = time.monotonic()
            t = max(now, _host_next.get(host, 0.0))
            _host_next[host] = t + gap
        if t > now:
            time.sleep(t - now)
        headers = {"User-Agent": ua, "Accept-Encoding": "gzip"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                chunks = []
                while True:                      # celkový limit – web může posílat data po kapkách
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if time.monotonic() - t0 > 60:
                        raise TimeoutError("odpověď trvá déle než 60 s")
                body = b"".join(chunks)
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                slow_down(host, time.monotonic() - t0)
                return r.status, body
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 422):
                return e.code, b""
            wait = 60 * (attempt + 1) if e.code in (403, 429) else 5 * (attempt + 1)
            log(f"HTTP {e.code} z {host} – další pokus za {wait} s")
            time.sleep(wait)
        except Exception as e:  # síť, timeout…
            log(f"Chyba sítě ({host}): {e}")
            slow_down(host, time.monotonic() - t0)
            time.sleep(5 * (attempt + 1))
    return None, b""


_host_backoff = {}


def slow_down(host, took):
    """Když web odpovídá pomalu (přetížení nebo brzdí dotazy), ubrat: pauza 1, 2, 4… až 30 minut."""
    with _host_lock:
        if took > 15:
            pause = min(max(60, _host_backoff.get(host, 0) * 2), 1800)
            _host_backoff[host] = pause
            _host_next[host] = max(_host_next.get(host, 0.0), time.monotonic() + pause)
        elif took < 5:
            _host_backoff[host] = 0
            return
        else:
            return
    log(f"{host} odpovídá pomalu ({took:.0f} s) – pauza {pause // 60} min")


def fetch_json(url, **kw):
    st, body = fetch(url, **kw)
    if st == 200:
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def fetch_text(url, **kw):
    st, body = fetch(url, **kw)
    return st, body.decode("utf-8", "replace") if body else ""


# ───────────────────────── úložiště ─────────────────────────

class Store:
    """Všechny inzeráty v paměti + průběžné ukládání na disk.

    Klíče začínající podtržítkem jsou interní a do prohlížeče se neposílají.
    Každá veřejná změna dostane nové pořadové číslo (_seq), takže prohlížeč
    si stahuje jen rozdíly.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.seq = 0
        self.tomb = []          # [(seq, key)] smazané inzeráty
        self.tomb_floor = 0
        self.epoch = f"{int(time.time())}{random.randint(100, 999)}"
        self.meta = {}
        self.sr_since = {}      # hash_id -> datum vložení (YYYY-MM-DD)
        self.psc = {}           # PSČ -> [lat, lon] | None
        self.ud_owner = {}      # UlovDomov id -> "private" | "agent" | "external"
        self.status = {}
        self.dirty = False

    def upsert(self, key, data):
        with self.lock:
            it = self.items.get(key)
            if it is None:
                it = self.items[key] = {"k": key}
                changed = True
            else:
                changed = any(not k.startswith("_") and it.get(k) != v for k, v in data.items())
            it.update(data)
            if changed:
                self.seq += 1
                it["_seq"] = self.seq
            self.dirty = True
            return it

    def remove(self, key):
        with self.lock:
            if self.items.pop(key, None) is not None:
                self.seq += 1
                self.tomb.append((self.seq, key))
                if len(self.tomb) > 200_000:
                    cut = len(self.tomb) // 2
                    self.tomb_floor = self.tomb[cut][0]
                    self.tomb = self.tomb[cut:]
                self.dirty = True

    def prune(self, pred, started):
        with self.lock:
            dead = [k for k, it in self.items.items() if pred(it) and it.get("_seen", 0) < started]
            for k in dead:
                self.remove(k)
        return len(dead)

    @staticmethod
    def visible(it):
        return it.get("la") is not None and it.get("own") in ("yes", "maybe")

    @staticmethod
    def public(it):
        return {k: v for k, v in it.items() if not k.startswith("_")}

    def delta(self, epoch, after):
        with self.lock:
            full = epoch != self.epoch or after > self.seq or after < self.tomb_floor
            if full:
                after = 0
            items, removed = [], []
            for k, it in self.items.items():
                if it.get("_seq", 0) > after:
                    if self.visible(it):
                        items.append(self.public(it))
                    elif not full:
                        removed.append(k)
            if not full:
                removed += [k for s, k in self.tomb if s > after]
            return {"epoch": self.epoch, "seq": self.seq, "full": full, "items": items, "removed": removed}

    def set_status(self, src, **kw):
        with self.lock:
            self.status.setdefault(src, {}).update(kw)

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps({"items": self.items, "meta": self.meta, "sr_since": self.sr_since, "psc": self.psc,
                               "ud_owner": self.ud_owner}, ensure_ascii=False, separators=(",", ":"))
            self.dirty = False
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, STATE_FILE)

    def load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            log("Uložená data nejdou načíst, začínám znovu:", e)
            return
        self.items = d.get("items", {})
        self.meta = d.get("meta", {})
        self.sr_since = d.get("sr_since", {})
        self.psc = d.get("psc", {})
        self.ud_owner = d.get("ud_owner", {})
        for it in self.items.values():
            self.seq += 1
            it["_seq"] = self.seq
        log(f"Načteno {len(self.items)} uložených inzerátů")


store = Store()


def poll_window(last, start, initial_done):
    """Kdy mohly vzniknout inzeráty, které toto kolo našlo nově: (živě?, odhad času vložení v ms).

    live = True: kola jdou po pár minutách, čas je přesný. live = "approx": mezi koly byla delší
    pauza (např. hodinové běhy v cloudu), čas je střed mezi minulým a tímto kolem.
    """
    gap = start - last
    if not initial_done or gap > 2 * 3600:
        return False, None
    if gap < 900:
        return True, None
    return "approx", int((last + start) / 2 * 1000)


def added_fields(first_ms, live, est_ms, est_prec):
    """Nejlepší odhad času vložení: živé zachycení > odhad."""
    if live and first_ms and (est_ms is None or first_ms - est_ms < 2 * DAY_MS):
        return {"ad": first_ms, "pr": "live" if live is True else "approx"}
    if est_ms is not None:
        return {"ad": est_ms, "pr": est_prec}
    return {"ad": None, "pr": "?"}


# ───────────────────────── Sreality ─────────────────────────

SR = "https://www.sreality.cz/api/v1/estates"
SR_TYPE = {1: "prodej", 2: "pronajem"}
SR_MAIN = {1: "byt", 2: "dum", 3: "pozemek", 4: "komercni", 5: "ostatni"}
SR_SUB = {
    2: "1+kk", 3: "1+1", 4: "2+kk", 5: "2+1", 6: "3+kk", 7: "3+1", 8: "4+kk", 9: "4+1", 10: "5+kk", 11: "5+1",
    12: "6-a-vice", 16: "atypicky", 47: "pokoj", 37: "rodinny", 39: "vila", 43: "chalupa", 33: "chata",
    35: "pamatka", 40: "na-klic", 44: "zemedelska-usedlost", 54: "vicegeneracni-dum", 19: "bydleni",
    18: "komercni", 20: "pole", 22: "louka", 21: "les", 46: "rybnik", 48: "sady-vinice", 23: "zahrada",
    24: "ostatni-pozemky", 25: "kancelare", 26: "sklad", 27: "vyrobni-prostor", 28: "obchodni-prostor",
    29: "ubytovani", 30: "restaurace", 31: "zemedelsky", 38: "cinzovni-dum", 49: "virtualni-kancelar",
    32: "ostatni-komercni-prostory", 56: "ordinace", 57: "apartman", 34: "garaz", 52: "garazove-stani",
    50: "vinny-sklep", 51: "pudni-prostor", 53: "mobilni-domek", 36: "jine-nemovitosti",
}
SR_SUBS_BY_MAIN = {
    1: [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 16, 47],
    2: [37, 39, 43, 33, 35, 40, 44, 54],
    3: [19, 18, 20, 22, 21, 46, 48, 23, 24],
    4: [25, 26, 27, 28, 29, 30, 31, 38, 49, 32, 56, 57],
    5: [34, 52, 50, 51, 53, 36],
}
SR_DISP = {2: "1+kk", 3: "1+1", 4: "2+kk", 5: "2+1", 6: "3+kk", 7: "3+1", 8: "4+kk", 9: "4+1",
           10: "5+", 11: "5+", 12: "5+", 16: "atyp", 47: "atyp"}
SR_PAGE_CAP = 10000            # API nedovolí stránkovat dál než 10 000 výsledků
SR_IMG = "?fl=res,400,300,3|shr,,20|jpg,90"

sr_queue, sr_queued = deque(), set()
sr_windows = {"at": 0, "ids": {}}   # hash_id -> nejmenší okno (h), ve kterém se inzerát změnil


def sr_search(params):
    return fetch_json(f"{SR}/search?{urllib.parse.urlencode(params)}")


def sr_build(r):
    t = (r.get("category_type_cb") or {}).get("value")
    m = (r.get("category_main_cb") or {}).get("value")
    sub = (r.get("category_sub_cb") or {}).get("value")
    loc = r.get("locality") or {}
    la, lo = loc.get("gps_lat"), loc.get("gps_lon")
    if t not in SR_TYPE or not in_cz(la, lo):
        return None
    name = (r.get("advert_name") or "").replace(" ", " ")
    areas = [num(x) for x in re.findall(r"(?<![\d+])(\d{1,3}(?: \d{3})+|\d+(?:[.,]\d+)?)\s*m²", name)]
    cat = SR_MAIN.get(m, "ostatni")
    if m == 2 and sub in (33, 43):
        cat = "chata"
    elif m == 5 and sub in (34, 52):
        cat = "garaz"
    area = areas[0] if areas else None
    land = areas[1] if len(areas) > 1 else None
    if cat == "pozemek":
        area, land = None, area
    price = r.get("price_summary_czk") or r.get("price_czk")
    if not price or price <= 1:
        price = None
    size = area or land
    ppm = r.get("price_czk_m2") or (round(price / size) if price and size else None)
    city = loc.get("city") or loc.get("municipality") or ""
    part = loc.get("citypart") or loc.get("ward") or ""
    locality = ", ".join(x for x in (loc.get("street"), part if part != city else None, city) if x)
    c = loc.get("city_seo_name") or loc.get("municipality_seo_name") or "cr"
    w = loc.get("ward_seo_name") or loc.get("citypart_seo_name") or c
    slug = f"{c}-{w}-{loc.get('street_seo_name') or ''}"
    imgs = r.get("advert_images") or []
    hid = r["hash_id"]
    return {
        "s": "sreality", "t": name, "o": SR_TYPE[t], "c": cat,
        "d": SR_DISP.get(sub) if m == 1 else parse_disp(name),
        "p": price, "a": area, "l": land, "m": ppm,
        "la": round(la, 6), "lo": round(lo, 6), "ap": False, "loc": locality,
        "u": f"https://www.sreality.cz/detail/{SR_TYPE[t]}/{SR_MAIN.get(m, 'ostatni')}/{SR_SUB.get(sub, 'x')}/{slug}/{hid}",
        "img": ("https:" + imgs[0] + SR_IMG) if imgs else None,
        "own": "yes",
    }


def sr_added(it, hid):
    since = store.sr_since.get(str(hid))
    if not since:
        return {"ad": None, "pr": "?"}
    d = date.fromisoformat(since[:10])
    if d != today():
        return added_fields(it.get("_first"), it.get("_live"), day_ms(d, 12), "day")
    # Vloženo dnes, ale čas neznáme → odhad podle toho, kdy se inzerát naposledy změnil
    midnight = day_ms(d)
    at, win = sr_windows["at"], sr_windows["ids"].get(hid)
    if at and win == 1:
        est = at - HOUR_MS // 2
    elif at and win == 3:
        est = at - 2 * HOUR_MS
    elif at and win == 10:
        est = at - int(6.5 * HOUR_MS)
    else:
        est = midnight + max(0, ((at or now_ms()) - 10 * HOUR_MS - midnight) // 2)
    return added_fields(it.get("_first"), it.get("_live"), max(est, midnight), "approx")


def sr_ingest(results, live=False, first=None):
    now = now_ms()
    for r in results:
        if r.get("premise_id") or r.get("premise_logo"):
            continue          # inzerát realitní kanceláře
        data = sr_build(r)
        if not data:
            continue
        hid = r["hash_id"]
        key = f"sr:{hid}"
        with store.lock:
            old = store.items.get(key)
            data["_seen"] = now
            if old is None:
                data["_first"] = first or now
                data["_live"] = live
            it = store.upsert(key, data)
            store.upsert(key, sr_added(it, hid))
        if str(hid) not in store.sr_since and hid not in sr_queued:
            sr_queued.add(hid)
            (sr_queue.appendleft if live else sr_queue.append)(hid)


def sr_crawl(params, dims):
    """Projde dotaz po stránkách; když má víc než 10 000 výsledků, rozdělí ho."""
    probe = sr_search({**params, "limit": 1})
    if probe is None:
        return False
    total = probe["pagination"]["total"]
    if total > SR_PAGE_CAP and dims:
        ok = True
        if dims[0] == "main":
            for m in SR_MAIN:
                ok &= sr_crawl({**params, "category_main_cb": m}, dims[1:])
        else:
            for s in SR_SUBS_BY_MAIN[params["category_main_cb"]]:
                ok &= sr_crawl({**params, "category_sub_cb": s}, dims[1:])
        return ok
    ok = True
    for off in range(0, min(total, SR_PAGE_CAP), 1000):
        d = sr_search({**params, "limit": 1000, "offset": off})
        if d is None:
            ok = False
            continue
        sr_ingest(d.get("results", []))
    return ok


def sr_refresh_windows():
    """Které inzeráty se změnily za poslední 1 / 3 / 10 hodin (pro odhad hodiny vložení)."""
    ids, at = {}, now_ms()
    for h in (10, 3, 1):
        iso = datetime.fromtimestamp(at / 1000 - h * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for t in SR_TYPE:
            off = 0
            while True:
                d = sr_search({"category_type_cb": t, "watchdog_last_changed_from": iso, "limit": 1000, "offset": off})
                if not d:
                    break
                for r in d.get("results", []):
                    if not r.get("premise_id"):
                        ids[r["hash_id"]] = h
                off += 1000
                if off >= min(d["pagination"]["total"], SR_PAGE_CAP):
                    break
    sr_windows.update(at=at, ids=ids)


def sr_full_loop():
    while True:
        wait = store.meta.get("sr_last_full", 0) + SR_FULL_EVERY - time.time()
        if wait > 0:
            store.set_status("sreality", phase="hlídá nové inzeráty", progress=None)
            time.sleep(min(wait, 60))
            continue
        started, first = resume_point("sr", SR_FULL_EVERY)
        store.set_status("sreality", phase="zjišťuji čerstvé změny", progress=0)
        sr_refresh_windows()
        jobs = [(t, reg) for t in SR_TYPE for reg in range(1, 15)]
        ok = True
        for i, (t, reg) in enumerate(jobs):
            if i < first:
                continue
            store.set_status("sreality", phase="kompletní průchod", progress=i / len(jobs))
            ok &= sr_crawl({"category_type_cb": t, "locality_region_id": reg}, ["main", "sub"])
            if ok:
                save_resume("sr", started, i + 1)
        save_resume("sr", started, None)
        if ok:
            n = store.prune(lambda it: it.get("s") == "sreality", started)
            store.meta["sr_last_full"] = time.time()
            store.meta["sr_initial_done"] = True
            store.set_status("sreality", last_full=now_ms(), error=None)
            log(f"Sreality: kompletní průchod hotov, smazáno {n} starých")
        else:
            store.set_status("sreality", error="část stránek se nepodařilo stáhnout")
            time.sleep(300)


def sr_poll_loop():
    while True:
        start = time.time()
        last = store.meta.get("sr_last_poll") or (start - 3600)
        frm = max(last - 90, start - 24 * 3600)
        live, first = poll_window(last, start, store.meta.get("sr_initial_done"))
        iso = datetime.fromtimestamp(frm, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok = True
        for t in SR_TYPE:
            off = 0
            while True:
                d = sr_search({"category_type_cb": t, "watchdog_last_changed_from": iso, "limit": 500, "offset": off})
                if d is None:
                    ok = False
                    break
                sr_ingest(d.get("results", []), live=live, first=first)
                off += 500
                if off >= min(d["pagination"]["total"], SR_PAGE_CAP):
                    break
        if ok:
            store.meta["sr_last_poll"] = start
            store.set_status("sreality", last_poll=now_ms())
        time.sleep(max(10, SR_POLL_EVERY - (time.time() - start)))


def sr_detail_loop():
    """Datum vložení (since) je jen v detailu inzerátu – dotahuje se postupně, nejnovější první."""
    while True:
        try:
            hid = sr_queue.popleft()
        except IndexError:
            store.set_status("sreality", dates_pending=0)
            time.sleep(3)
            continue
        store.set_status("sreality", dates_pending=len(sr_queue) + 1)
        key = f"sr:{hid}"
        if key not in store.items:
            sr_queued.discard(hid)
            continue
        st, body = fetch(f"{SR}/{hid}")
        sr_queued.discard(hid)
        if st in (404, 410):
            store.remove(key)
            continue
        if st != 200:
            continue
        try:
            res = json.loads(body).get("result") or {}
        except ValueError:
            continue
        since = res.get("since") or res.get("edited")
        if not since:
            continue
        with store.lock:
            store.sr_since[str(hid)] = since
            it = store.items.get(key)
            if it:
                store.upsert(key, sr_added(it, hid))


# ───────────────────────── Bazoš ─────────────────────────

BZ = "https://reality.bazos.cz"
BZ_CATS = [("prodam", c) for c in ("byt", "dum", "pozemek", "chata", "garaz", "kancelar", "prostory",
                                   "restaurace", "sklad", "zahrada", "ostatni")] + \
          [("pronajmu", c) for c in ("byt", "dum", "pozemek", "garaz", "kancelar", "prostory",
                                     "restaurace", "sklad", "zahrada", "ostatni", "podnajem")]
BZ_CAT = {"byt": "byt", "podnajem": "byt", "dum": "dum", "pozemek": "pozemek", "zahrada": "pozemek",
          "chata": "chata", "garaz": "garaz", "kancelar": "komercni", "prostory": "komercni",
          "restaurace": "komercni", "sklad": "komercni", "ostatni": "ostatni"}

# Fráze, kterými se hlásí soukromí prodejci (vyřadí se před hledáním znaků RK, např. „RK nevolat“)
BZ_PRIVATE = re.compile(
    r"bez (rk|realit\w*|makler\w*|provize|zprostredkovatel\w*)|(zadna|nulova|zadne) provize"
    r"|provize( rk)?\s*:?\s*(ne\b|0\b|zadna|neni|nehradite|se neplati)"
    r"|(rk|realitk\w*|realitni kancelar\w*|makleri?)\s*(prosim |prosime )?(nevolat|nevolejte|nepiste|nekontaktujte|ne\b)"
    r"|nevolejte\s*(rk|realitk\w*)|nejsem\s*(rk|makler|realitk\w*)|\bne\s?rk\b"
    r"|(neni|nejsou) urcen\w* (pro )?(rk|realitni\w*|realitk\w*|makler\w*)( kancelare)?"
    r"|(primo )?od majitele|soukrom\w* (osoba|inzerent|prodej\w*|majitel\w*)|jsem majitel\w*|vlastnik"
)
# Jednoznačné znaky realitky v textu
BZ_AGENT = re.compile(
    r"realitn|\brk\b|makler|provize|zprostredk|s\.\s?r\.\s?o|\bsro\b|\ba\.\s?s\.|\bspol\. s"
    r"|nase spolecnost|nase kancelar|re/?max|century ?21|m&m|bidli|\breality\b|property|estate"
    r"|v zastoupeni|naseho klienta|pro klienta|nasi klienti|developer|cislo zakazky"
    r"|financovani zajistime|hypotecni (servis|poradenstvi)|kompletni servis|pravni servis"
)
# Znaky realitky ve jménu účtu – i „Jméno (pobočka)“, jak to mívají makléři velkých sítí
BZ_AGENT_USER = re.compile(r"realit|\breal\b|reality|estate|property|invest|holding|group|broker|\(.+|\s-\s\S|\.(cz|com|eu)\b")
# Poptávky, které se omylem ocitnou mezi nabídkami („Hledám pronájem“, „Koupím byt“)
BZ_WANTED = re.compile(r"^\W*(hledam|hledame|koupim|koupime|shanim|shanime|vymenim|vymena)\b"
                       r"|potrebuj\w* (koupit|najit|pronajmout)|hledam (pronajem|byt|dum|podnajem)")
# Píšou to realitky, ale často i manželé nebo rodina („nabízíme k prodeji náš byt“) → jen „nejisté“
BZ_WEAK = re.compile(
    r"nabizime k (prodeji|pronajmu)|\bnabizime\b|exkluziv|kontaktujte nas|zajistime|vyridime|pomuzeme"
    r"|investicni prilezitost|ev\. ?c\.|evidencni cislo"
)

bz_anchor_lock = threading.Lock()


def bz_flags(title, desc, user):
    text = norm(f"{title} {desc}")
    u = BZ_PRIVATE.sub(" ", norm(user))
    if BZ_AGENT.search(u) or BZ_AGENT_USER.search(u):
        return "agent_user"
    priv = bool(BZ_PRIVATE.search(text))
    clean = BZ_PRIVATE.sub(" ", text)
    if BZ_AGENT.search(clean):
        return "agent_priv" if priv else "agent"
    if priv:
        return "priv"
    if BZ_WEAK.search(clean):
        return "weak"
    return ""


def bz_own(kw, n):
    # n = počet inzerátů na stejném účtu (soukromník jich má obvykle 1–2)
    if kw == "agent_user" or kw == "agent" or n >= 8:
        return "no"
    if kw in ("agent_priv", "weak") or n >= 3:
        return "maybe"
    return "yes"


def bz_parse(page):
    m = re.search(r"inzerátů z ([\d\s ]+)", page)
    total = int(re.sub(r"\D", "", m.group(1))) if m else 0
    out = []
    for blk in page.split('class="inzeraty inzeratyflex"')[1:]:
        mid = re.search(r'href="(/inzerat/(\d+)/[^"]+)"', blk)
        if not mid:
            continue
        t = re.search(r"<h2 class=nadpis><a[^>]*>(.*?)</a>", blk, re.S)
        dt = re.search(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]", blk)
        img = re.search(r'<img src="(https://www\.bazos\.cz/img/[^"]+)"', blk)
        desc = re.search(r"<div class=popis>(.*?)</div>", blk, re.S)
        price = re.search(r'<div class="inzeratycena">(.*?)</div>', blk, re.S)
        lok = re.search(r'<div class="inzeratylok">(.*?)<br>(.*?)</div>', blk, re.S)
        usr = re.search(r"odeslatakci\('rating','\d+','(\d+)','([^']*)'\)", blk)
        strip = lambda s: html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()
        price_txt = strip(price.group(1)) if price else ""
        out.append({
            "id": int(mid.group(2)), "href": mid.group(1),
            "title": strip(t.group(1)) if t else "",
            "top": 'class="ztop"' in blk,
            "date": date(int(dt.group(3)), int(dt.group(2)), int(dt.group(1))) if dt else None,
            "img": img.group(1) if img else None,
            "desc": strip(desc.group(1)) if desc else "",
            "price": num(re.sub(r"[^\d]", "", price_txt)) if re.search(r"\d", price_txt) else None,
            "town": strip(lok.group(1)) if lok else "",
            "psc": re.sub(r"\D", "", lok.group(2)) if lok else "",
            "user": urllib.parse.unquote_plus(usr.group(2)) if usr else "",
            "uid": usr.group(1) if usr else "",   # účet – sdílí ho i makléři jedné kanceláře
        })
    return total, out


def bz_estimate(bid):
    """Bazoš čísluje inzeráty postupně → z čísla inzerátu se dá odhadnout čas vložení."""
    with bz_anchor_lock:
        anchors = store.meta.get("bz_anchors") or []
        if len(anchors) < 2:
            return None
        ids = [a[0] for a in anchors]
        i = bisect_left(ids, bid)
        if i == 0:
            (i0, t0), (i1, t1) = anchors[0], anchors[1]
        elif i >= len(anchors):
            (i0, t0), (i1, t1) = anchors[-2], anchors[-1]
        else:
            (i0, t0), (i1, t1) = anchors[i - 1], anchors[i]
    if i1 == i0:
        return t1
    est = t0 + (bid - i0) * (t1 - t0) / (i1 - i0)
    return int(min(est, now_ms()))


def bz_add_anchors(new):
    """Kotvy (číslo inzerátu, čas) – udržuje je seřazené a časově neklesající."""
    with bz_anchor_lock:
        pts = dict((a[0], a[1]) for a in store.meta.get("bz_anchors") or [])
        for i, t in new:
            pts[i] = t
        clean, last_t = [], -1
        for i, t in sorted(pts.items()):
            if t >= last_t:
                clean.append([i, t])
                last_t = t
        store.meta["bz_anchors"] = clean[-4000:]


def bz_coords(it):
    c = store.psc.get(it.get("_psc"))
    if not c:
        return {"la": None, "lo": None}
    rnd = random.Random(it["_bid"])
    ang, r = rnd.random() * 2 * math.pi, 0.2 + rnd.random() * 1.1   # rozprostření v okolí PSČ (km)
    la = c[0] + r / 111.0 * math.cos(ang)
    lo = c[1] + r / (111.0 * math.cos(math.radians(c[0]))) * math.sin(ang)
    return {"la": round(la, 6), "lo": round(lo, 6)}


def bz_dates(it):
    est = bz_estimate(it["_bid"])
    return added_fields(it.get("_first"), it.get("_live"), est, "approx")


def bz_ingest(rows, sec, cat, live, first=None):
    now = now_ms()
    by_date, fresh = {}, []
    for r in rows:
        if r["date"] and not r["top"]:
            by_date[r["date"]] = max(by_date.get(r["date"], 0), r["id"])
        if BZ_WANTED.search(norm(r["title"])):
            continue
        key = f"bz:{r['id']}"
        text = f"{r['title']} {r['desc']}"
        area = parse_area(r["title"]) or parse_area(r["desc"])
        c = BZ_CAT[cat]
        data = {
            "s": "bazos", "t": r["title"], "o": "prodej" if sec == "prodam" else "pronajem", "c": c,
            "d": parse_disp(r["title"]) or parse_disp(r["desc"]) if c in ("byt", "dum", "chata") else None,
            "p": r["price"] if r["price"] and r["price"] > 1 else None,
            "a": None if c == "pozemek" else area, "l": area if c == "pozemek" else None,
            "loc": f"{r['town']}, {r['psc'][:3]} {r['psc'][3:]}".strip(", "), "ap": True,
            "u": BZ + r["href"], "img": r["img"],
            "_bid": r["id"], "_psc": r["psc"], "_town": r["town"], "_user": r["user"], "_uid": r["uid"],
            "_kw": bz_flags(r["title"], r["desc"], r["user"]), "_tx": norm(text)[:400],
            "_cat": f"{sec}/{cat}", "_seen": now,
        }
        size = data["a"] or data["l"]
        data["m"] = round(data["p"] / size) if data["p"] and size else None
        with store.lock:
            old = store.items.get(key)
            if old is None:
                data["_first"] = first or now
                data["_live"] = live
                data["own"] = bz_own(data["_kw"], 0)
                if live is True:
                    fresh.append([r["id"], now])
            data.update(bz_coords(data))
            it = store.upsert(key, data)
            store.upsert(key, bz_dates(it))
    td = today()
    anchors = [[i, day_ms(d, 23)] for d, i in by_date.items() if d < td] + fresh
    if anchors:
        bz_add_anchors(anchors)


def bz_reclassify():
    with store.lock:
        bz = [it for it in store.items.values() if it.get("s") == "bazos"]
        counts = Counter(it["_uid"] for it in bz if it.get("_uid"))
        for it in bz:
            n = counts.get(it.get("_uid"), 0) if it.get("_uid") else 0
            store.upsert(it["k"], {"own": bz_own(it.get("_kw", ""), n)})


def bz_redate():
    with store.lock:
        for it in [i for i in store.items.values() if i.get("s") == "bazos"]:
            store.upsert(it["k"], bz_dates(it))


def bz_page_url(sec, cat, off):
    return f"{BZ}/{sec}/{cat}/" + (f"{off}/" if off else "")


def bz_full_loop():
    while True:
        wait = store.meta.get("bz_last_full", 0) + BZ_FULL_EVERY - time.time()
        if wait > 0:
            store.set_status("bazos", phase="hlídá nové inzeráty", progress=None)
            time.sleep(min(wait, 60))
            continue
        all_ok = True
        done = store.meta.setdefault("bz_done", {})
        run_started, first = resume_point("bz", BZ_FULL_EVERY)
        for ci, (sec, cat) in enumerate(BZ_CATS):
            if ci < first:
                continue
            started, off, total, ok, retries = now_ms(), 0, None, True, 0
            while total is None or off < total:
                store.set_status("bazos", phase=f"kompletní průchod ({sec} {cat})",
                                 progress=(ci + (off / total if total else 0)) / len(BZ_CATS))
                st, page = fetch_text(bz_page_url(sec, cat, off))
                new_total, rows = bz_parse(page) if st == 200 else (None, [])
                # Chyba nebo prázdná stránka uprostřed výpisu: zkusit znovu, jinak průchod neplatí
                if st != 200 or (not rows and off + 40 < (new_total or total or 0)):
                    retries += 1
                    log(f"Bazoš {sec}/{cat} od {off}: HTTP {st}, inzerátů {len(rows)} – pokus {retries}")
                    if retries <= 3:
                        time.sleep(20 * retries)
                        continue
                    ok = False
                    break
                retries, total = 0, new_total
                if off == 0:
                    store.meta.setdefault("bz_totals", {})[f"{sec}/{cat}"] = total
                if not rows:
                    break
                bz_ingest(rows, sec, cat, live=False)
                off += 40
                if off % 1000 == 0:
                    bz_reclassify()
            if ok:
                store.prune(lambda it, g=f"{sec}/{cat}": it.get("_cat") == g, started)
                done[f"{sec}/{cat}"] = True
            all_ok &= ok
            if all_ok:
                save_resume("bz", run_started, ci + 1)
            bz_reclassify()
            bz_redate()
        save_resume("bz", run_started, None)
        if all_ok:
            store.meta["bz_last_full"] = time.time()
            store.set_status("bazos", last_full=now_ms(), error=None)
            log("Bazoš: kompletní průchod hotov")
        else:
            store.set_status("bazos", error="část stránek se nepodařilo stáhnout")
            time.sleep(300)


def bz_poll_loop():
    time.sleep(20)
    while True:
        start = time.time()
        last = store.meta.get("bz_last_poll", 0)
        done = store.meta.get("bz_done", {})
        newest = 0
        for sec, cat in BZ_CATS:
            st, page = fetch_text(bz_page_url(sec, cat, 0))
            if st == 200:
                total, rows = bz_parse(page)
                store.meta.setdefault("bz_totals", {})[f"{sec}/{cat}"] = total
                newest = max([newest] + [r["id"] for r in rows if not r["top"]])
                live, first = poll_window(last, start, done.get(f"{sec}/{cat}"))
                bz_ingest(rows, sec, cat, live=live, first=first)
        if newest:
            bz_add_anchors([[newest, int(start * 1000)]])   # nejnovější inzerát v celé ČR ≈ teď
        bz_reclassify()
        bz_redate()
        store.meta["bz_last_poll"] = start
        store.set_status("bazos", last_poll=now_ms())
        time.sleep(max(10, BZ_POLL_EVERY - (time.time() - start)))


def geo_loop():
    """PSČ z Bazoše → souřadnice (OpenStreetMap Nominatim, max. 1 dotaz za sekundu, ukládá se)."""
    while True:
        with store.lock:
            pending = Counter()
            towns = {}
            for it in store.items.values():
                p = it.get("_psc")
                if it.get("s") == "bazos" and p and len(p) == 5 and p not in store.psc:
                    pending[p] += 1
                    towns[p] = it.get("_town", "")
        store.set_status("bazos", geo_pending=len(pending))
        if not pending:
            time.sleep(15)
            continue
        for p, _ in pending.most_common(40):
            base = "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=cz&"
            res = fetch_json(base + urllib.parse.urlencode({"postalcode": p}), ua=GEO_UA)
            if res is None:
                time.sleep(30)
                break
            if not res and towns.get(p):
                town = re.sub(r"\s+venkov$", "", towns[p])
                res = fetch_json(base + urllib.parse.urlencode({"q": town}), ua=GEO_UA) or []
            coords = [float(res[0]["lat"]), float(res[0]["lon"])] if res else None
            with store.lock:
                store.psc[p] = coords
                for it in [i for i in store.items.values() if i.get("_psc") == p and i.get("s") == "bazos"]:
                    store.upsert(it["k"], bz_coords(it))


# ───────────────────────── Bezrealitky ─────────────────────────

BR = "https://www.bezrealitky.cz"
BR_OFFERS = {"nabidka-prodej": "prodej", "nabidka-pronajem": "pronajem"}
BR_ESTATES = {"byt": "byt", "dum": "dum", "pozemek": "pozemek", "chata-chalupa": "chata", "garaz": "garaz",
              "kancelar": "komercni", "nebytovy-prostor": "komercni"}
BR_DISP = {"DISP_1_KK": "1+kk", "DISP_1_1": "1+1", "DISP_2_KK": "2+kk", "DISP_2_1": "2+1", "DISP_3_KK": "3+kk",
           "DISP_3_1": "3+1", "DISP_4_KK": "4+kk", "DISP_4_1": "4+1", "GARSONIERA": "1+kk", "OSTATNI": "atyp"}
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def br_field(obj, prefix):
    for k, v in obj.items():
        if k == prefix or k.startswith(prefix + "("):
            return v
    return None


def br_img(cache, ref, flt="RECORD_THUMB"):
    im = cache.get(ref.get("__ref")) if isinstance(ref, dict) else None
    if not im:
        return None
    for k, v in im.items():
        if k.startswith("url(") and flt in k:
            return v
    return None


def br_parse(page):
    m = NEXT_RE.search(page)
    if not m:
        return None, []
    try:
        cache = json.loads(m.group(1))["props"]["pageProps"]["apolloCache"]
    except (ValueError, KeyError, TypeError):
        return None, []
    lst = None
    for k, v in cache.get("ROOT_QUERY", {}).items():
        if k.startswith("listAdverts(") and '"discountedOnly"' not in k and isinstance(v, dict) and "list" in v:
            lst = v
            break
    if not lst:
        return None, []
    out = []
    for ref in lst["list"]:
        a = cache.get(ref.get("__ref")) if isinstance(ref, dict) else None
        if not a:
            continue
        imgs = [br_img(cache, a.get("mainImage") or {})] + [br_img(cache, r) for r in (br_field(a, "publicImages") or [])]
        # v názvu fotky je čas nahrání (unix), např. 1793105671-abcdef-x.jpg nebo 1086533-1793972420-foto.jpg
        stamps = [int(x) for u in imgs if u for x in re.findall(r"(?<!\d)(1[6-9]\d{8})(?!\d)", u.rsplit("/", 1)[-1])]
        out.append({"a": a, "img": imgs[0], "img_ts": min(stamps) * 1000 if stamps else None})
    return lst.get("totalCount"), out


def br_ingest(rows, offer, estate, live, first=None):
    now = now_ms()
    for row in rows:
        a = row["a"]
        gps = a.get("gps") or {}
        la, lo = gps.get("lat"), gps.get("lng")
        if not in_cz(la, lo):
            continue
        key = f"br:{a['id']}"
        c = BR_ESTATES[estate]
        surf, land = a.get("surface"), a.get("surfaceLand")
        if c == "pozemek":
            surf, land = None, land or surf
        price = a.get("price") or None
        size = surf or land
        title = br_field(a, "imageAltText") or ""
        disp = BR_DISP.get(a.get("disposition") or "")
        if not disp and (a.get("disposition") or "").startswith("DISP_"):
            disp = "5+"
        data = {
            "s": "bezrealitky", "t": title, "o": BR_OFFERS[offer], "c": c,
            "d": disp or (parse_disp(title) if c in ("byt", "dum", "chata") else None),
            "p": price, "a": surf, "l": land, "m": round(price / size) if price and size else None,
            "la": round(la, 6), "lo": round(lo, 6), "ap": False, "loc": br_field(a, "address") or "",
            "u": f"{BR}/nemovitosti-byty-domy/{a.get('uri') or a['id']}", "img": row["img"], "own": "yes",
            "_cat": f"{offer}/{estate}", "_seen": now,
        }
        if a.get("currency") and a["currency"] != "CZK":
            data["cur"] = a["currency"]
        with store.lock:
            old = store.items.get(key)
            if old is None:
                data["_first"] = first or now
                data["_live"] = live
            it = store.upsert(key, data)
            store.upsert(key, added_fields(it.get("_first"), it.get("_live"), row["img_ts"], "approx"))


def br_page_url(offer, estate, page):
    return f"{BR}/vypis/{offer}/{estate}" + (f"?page={page}" if page > 1 else "")


# Pronájem chat na Bezrealitkách = rekreační pobyty na pár dní, ne nabídky nemovitostí → vynechat
BR_COMBOS = [(o, e) for o in BR_OFFERS for e in BR_ESTATES if (o, e) != ("nabidka-pronajem", "chata-chalupa")]


def br_full_loop():
    time.sleep(5)
    while True:
        wait = store.meta.get("br_last_full", 0) + BR_FULL_EVERY - time.time()
        if wait > 0:
            store.set_status("bezrealitky", phase="hlídá nové inzeráty", progress=None)
            time.sleep(min(wait, 60))
            continue
        all_ok = True
        done = store.meta.setdefault("br_done", {})
        run_started, first = resume_point("br", BR_FULL_EVERY)
        for ci, (offer, estate) in enumerate(BR_COMBOS):
            if ci < first:
                continue
            started, page, pages, ok, retries = now_ms(), 1, None, True, 0
            while pages is None or page <= pages:
                store.set_status("bezrealitky", phase=f"kompletní průchod ({estate})",
                                 progress=(ci + ((page - 1) / pages if pages else 0)) / len(BR_COMBOS))
                st, html_ = fetch_text(br_page_url(offer, estate, page))
                if st == 404 and page == 1:
                    break                               # kategorie neexistuje
                total, rows = br_parse(html_) if st == 200 else (None, [])
                # Chyba nebo prázdná stránka uprostřed výpisu: zkusit znovu, jinak průchod neplatí
                if total is None or (not rows and page * 15 < total - 15):
                    retries += 1
                    log(f"Bezrealitky {offer}/{estate} str. {page}: HTTP {st}, inzerátů {len(rows)} – pokus {retries}")
                    if retries <= 3:
                        time.sleep(20 * retries)
                        continue
                    ok = False
                    break
                retries = 0
                store.meta.setdefault("br_totals", {})[f"{offer}/{estate}"] = total
                pages = math.ceil(total / 15) if total else 0
                if not rows:
                    break
                br_ingest(rows, offer, estate, live=False)
                page += 1
            if ok:
                store.prune(lambda it, g=f"{offer}/{estate}": it.get("_cat") == g and it.get("s") == "bezrealitky",
                            started)
                done[f"{offer}/{estate}"] = True
            all_ok &= ok
            if all_ok:
                save_resume("br", run_started, ci + 1)
        save_resume("br", run_started, None)
        if all_ok:
            store.meta["br_last_full"] = time.time()
            store.set_status("bezrealitky", last_full=now_ms(), error=None)
            log("Bezrealitky: kompletní průchod hotov")
        else:
            store.set_status("bezrealitky", error="část stránek se nepodařilo stáhnout")
            time.sleep(300)


def br_poll_loop():
    time.sleep(40)
    while True:
        start = time.time()
        last = store.meta.get("br_last_poll", 0)
        done = store.meta.get("br_done", {})
        for offer, estate in BR_COMBOS:
            st, page = fetch_text(br_page_url(offer, estate, 1))
            if st == 200:
                total, rows = br_parse(page)
                if total is not None:
                    store.meta.setdefault("br_totals", {})[f"{offer}/{estate}"] = total
                live, first = poll_window(last, start, done.get(f"{offer}/{estate}"))
                br_ingest(rows, offer, estate, live=live, first=first)
        store.meta["br_last_poll"] = start
        store.set_status("bezrealitky", last_poll=now_ms())
        time.sleep(max(10, BR_POLL_EVERY - (time.time() - start)))


# ───────────────────────── UlovDomov ─────────────────────────

UD_FIND = "https://ud.api.ulovdomov.cz/v1/offer/find"
UD_CONTACT = "https://www.ulovdomov.cz/fe-api/offer/contact-info/{}"
UD_TYPES = {"sale": "prodej", "rent": "pronajem", "coliving": "pronajem"}
UD_PROP = {"flat": "byt", "house": "dum", "land": "pozemek"}
UD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
UD_BOUNDS = {"northEast": {"lat": 51.06, "lng": 18.87}, "southWest": {"lat": 48.55, "lng": 12.09}}
UD_PER_PAGE = 100

ud_queue, ud_queued = deque(), set()


def ud_find(otype, page):
    q = urllib.parse.urlencode({"page": page, "perPage": UD_PER_PAGE, "sorting": "latest"})
    t0 = time.monotonic()
    d = fetch_json(f"{UD_FIND}?{q}", body={"offerType": otype, "bounds": UD_BOUNDS})
    if not d or not d.get("success"):
        log(f"UlovDomov {otype} str. {page}: bez dat ({time.monotonic() - t0:.0f} s)")
        return None, []
    return (d.get("extraData") or {}).get("total", 0), (d.get("data") or {}).get("offers") or []


def ud_disp(v, title):
    m = re.match(r"(one|two|three|four|five|six)Plus(Kk|One)", v or "")
    if m:
        n = UD_NUM[m.group(1)]
        return "5+" if n >= 5 else f"{n}+{'kk' if m.group(2) == 'Kk' else '1'}"
    if v == "sixAndMore":
        return "5+"
    if v == "atypical":
        return "atyp"
    return parse_disp(title)


def ud_own(owner, kw):
    """owner = příznak z karty inzerátu, kw = znaky realitky v textu (stejná pravidla jako Bazoš)."""
    if owner in ("agent", "external"):
        return "no"
    if owner == "private":
        # soukromý účet, ale text jako od realitky („v zastoupení majitele…“) → nejisté
        return "maybe" if kw in ("agent", "agent_priv", "weak") else "yes"
    return "no" if kw == "agent" else "pending"


def ud_time(s):
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def ud_ingest(offers, otype):
    now = now_ms()
    for o in offers:
        g = o.get("geoCoordinates") or {}
        la, lo = g.get("lat"), g.get("lng")
        if not in_cz(la, lo):
            continue
        oid = o["id"]
        key = f"ud:{oid}"
        c = UD_PROP.get(o.get("propertyType"), "ostatni")
        area = o.get("area") or None
        pr = o.get("rentalPrice") or {}
        price = pr.get("value") or None
        title = o.get("title") or ""
        kw = bz_flags(title, o.get("description") or "", "")
        photos = o.get("photos") or []
        own = ud_own(store.ud_owner.get(str(oid)), kw)
        pub = ud_time(o.get("published"))
        loc = [(o.get(k) or {}).get("title") for k in ("street", "villagePart", "village")]
        data = {
            "s": "ulovdomov", "t": title, "o": UD_TYPES[otype], "c": c,
            "d": "atyp" if otype == "coliving" else (ud_disp(o.get("disposition"), title) if c in ("byt", "dum") else None),
            "p": price, "a": None if c == "pozemek" else area, "l": area if c == "pozemek" else None,
            "m": round(price / area) if price and area else None,
            "la": round(la, 6), "lo": round(lo, 6), "ap": False,
            "loc": ", ".join(x for i, x in enumerate(loc) if x and x not in loc[i + 1:]),
            "u": o.get("absoluteUrl") or f"https://www.ulovdomov.cz/inzerat/{o.get('seo') or 'x'}/{oid}",
            "img": photos[0].get("path") if photos else None,
            "own": own, "_kw": kw, "_cat": otype, "_seen": now,
        }
        data.update({"ad": pub, "pr": "live"} if pub else {"ad": now, "pr": "approx"})
        if pr.get("currency") and pr["currency"] != "CZK":
            data["cur"] = pr["currency"]
        with store.lock:
            if key not in store.items:
                data["_first"] = now
            store.upsert(key, data)
        if own == "pending" and oid not in ud_queued:
            ud_queued.add(oid)
            ud_queue.append(oid)


UD_OTYPES = ["sale", "rent", "coliving"]


def ud_full_loop():
    time.sleep(8)
    while True:
        wait = store.meta.get("ud_last_full", 0) + UD_FULL_EVERY - time.time()
        if wait > 0:
            store.set_status("ulovdomov", phase="hlídá nové inzeráty", progress=None)
            time.sleep(min(wait, 60))
            continue
        all_ok = True
        for ti, otype in enumerate(UD_OTYPES):
            started, page, pages, ok = now_ms(), 1, None, True
            while pages is None or page <= pages:
                store.set_status("ulovdomov", phase=f"kompletní průchod ({otype})",
                                 progress=(ti + ((page - 1) / pages if pages else 0)) / len(UD_OTYPES))
                total, offers = ud_find(otype, page)
                if total is None:
                    ok = False
                    break
                store.meta.setdefault("ud_totals", {})[otype] = total
                pages = math.ceil(total / UD_PER_PAGE)
                if not offers:
                    break
                ud_ingest(offers, otype)
                page += 1
            if ok:
                store.prune(lambda it, g=otype: it.get("s") == "ulovdomov" and it.get("_cat") == g, started)
            all_ok &= ok
        if all_ok:
            store.meta["ud_last_full"] = time.time()
            store.set_status("ulovdomov", last_full=now_ms(), error=None)
            log("UlovDomov: kompletní průchod hotov")
        else:
            store.set_status("ulovdomov", error="část stránek se nepodařilo stáhnout")
            time.sleep(300)


def ud_poll_loop():
    time.sleep(60)
    while True:
        start = time.time()
        for otype in UD_OTYPES:
            total, offers = ud_find(otype, 1)
            if total is not None:
                store.meta.setdefault("ud_totals", {})[otype] = total
                ud_ingest(offers, otype)
        store.set_status("ulovdomov", last_poll=now_ms())
        time.sleep(max(10, UD_POLL_EVERY - (time.time() - start)))


def ud_owner_loop():
    """Na kartě inzerátu je, jestli inzeruje soukromý majitel. Kontakty se neukládají, jen ano/ne."""
    while True:
        try:
            oid = ud_queue.popleft()
        except IndexError:
            store.set_status("ulovdomov", owner_pending=0)
            time.sleep(3)
            continue
        store.set_status("ulovdomov", owner_pending=len(ud_queue) + 1)
        key = f"ud:{oid}"
        if key not in store.items:
            ud_queued.discard(oid)
            continue
        st, body = fetch(UD_CONTACT.format(oid))
        ud_queued.discard(oid)
        if st in (404, 410):
            store.remove(key)
            continue
        try:
            c = json.loads(body) if st == 200 else None
        except ValueError:
            c = None
        if not isinstance(c, dict) or ("is_private_landlord" not in c and not c.get("external_url")):
            continue          # zkusí se znovu při příštím průchodu
        owner = "external" if c.get("external_url") else ("private" if c.get("is_private_landlord") else "agent")
        with store.lock:
            store.ud_owner[str(oid)] = owner
            it = store.items.get(key)
            if it:
                store.upsert(key, {"own": ud_own(owner, it.get("_kw", ""))})


# ───────────────────────── web server ─────────────────────────

def counts_by_source():
    """(viditelné na mapě, prošlé celkem vč. realitek) podle zdroje"""
    vis, seen = Counter(), Counter()
    with store.lock:
        for it in store.items.values():
            seen[it["s"]] += 1
            if store.visible(it):
                vis[it["s"]] += 1
    return vis, seen


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_body(self, body, ctype):
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body, 5)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self.send_body(f.read(), "text/html; charset=utf-8")
            if u.path == "/api/listings":
                after = int((qs.get("after") or ["0"])[0] or 0)
                d = store.delta((qs.get("epoch") or [""])[0], after)
                return self.send_body(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode(),
                                      "application/json; charset=utf-8")
            if u.path == "/api/status":
                with store.lock:
                    st = json.loads(json.dumps(store.status))
                    st.setdefault("bazos", {})["expected"] = sum((store.meta.get("bz_totals") or {}).values())
                    st.setdefault("bezrealitky", {})["expected"] = sum((store.meta.get("br_totals") or {}).values())
                    st.setdefault("ulovdomov", {})["expected"] = sum((store.meta.get("ud_totals") or {}).values())
                    for src, key in (("sreality", "sr_last_full"), ("bazos", "bz_last_full"),
                                     ("bezrealitky", "br_last_full"), ("ulovdomov", "ud_last_full")):
                        st.setdefault(src, {})["complete"] = bool(store.meta.get(key))
                vis, seen = counts_by_source()
                for src in ("sreality", "bazos", "bezrealitky", "ulovdomov"):
                    st.setdefault(src, {}).update(count=vis.get(src, 0), stored=seen.get(src, 0))
                return self.send_body(json.dumps({"now": now_ms(), "sources": st}).encode(), "application/json")
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass


def guarded(fn):
    def run():
        while True:
            try:
                fn()
            except Exception:
                log(f"Chyba ve vlákně {fn.__name__}:\n{traceback.format_exc()}")
                time.sleep(30)
    return run


def saver_loop():
    while True:
        time.sleep(60)
        store.save()


def prepare():
    """Úklid po načtení uloženého stavu a spuštění všech vláken, která stahují data."""
    for fn in (sr_full_loop, sr_poll_loop, sr_detail_loop, bz_full_loop, bz_poll_loop, geo_loop,
               br_full_loop, br_poll_loop, ud_full_loop, ud_poll_loop, ud_owner_loop, saver_loop):
        threading.Thread(target=guarded(fn), name=fn.__name__, daemon=True).start()
    # Znovu vyhodnotit uložené inzeráty z Bazoše, jen když se změnila pravidla (realitka / majitel)
    rules = hashlib.sha1("|".join(r.pattern for r in (BZ_PRIVATE, BZ_AGENT, BZ_AGENT_USER, BZ_WEAK, BZ_WANTED))
                         .encode()).hexdigest()
    with store.lock:
        recheck = store.meta.get("bz_rules") != rules
        store.meta["bz_rules"] = rules
        br_cats = {f"{o}/{e}" for o, e in BR_COMBOS}
        for k in list(store.meta.get("br_totals", {})):
            if k not in br_cats:
                store.meta["br_totals"].pop(k)
        for k, it in list(store.items.items()):
            if it.get("s") == "bezrealitky" and it.get("_cat") not in br_cats:
                store.remove(k)
            if it.get("s") != "bazos" or not recheck:
                continue
            if BZ_WANTED.search(norm(it.get("t"))):
                store.remove(k)
            elif "_tx" in it:
                it["_kw"] = bz_flags(it["_tx"], "", it.get("_user", ""))
    bz_reclassify()
    # Inzeráty Sreality, kterým chybí datum vložení, zařadit do fronty
    with store.lock:
        for k, it in store.items.items():
            if it.get("s") == "sreality" and k[3:] not in store.sr_since:
                hid = int(k[3:])
                sr_queued.add(hid)
                sr_queue.append(hid)
            elif it.get("s") == "ulovdomov" and it.get("own") == "pending":
                oid = int(k[3:])
                ud_queued.add(oid)
                ud_queue.append(oid)


def main():
    store.load()
    prepare()

    def stop(*_):
        store.save()
        log("Uloženo, konec.")
        os._exit(0)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    log(f"Mapa běží na {url}  (ukončení: Ctrl+C)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.save()
        log("Uloženo, konec.")


if __name__ == "__main__":
    main()
