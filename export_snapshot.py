#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vyexportuje aktuální inzeráty z běžícího server.py do kompaktního snímku pro sdílenou verzi mapy.

  python3 export_snapshot.py                    → sdileni/data.json (stránka na claude.ai, bez fotek)
  python3 export_snapshot.py --img web/data.json  → verze pro GitHub Pages, s fotkami
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
WITH_IMG = "--img" in sys.argv
OUT = os.path.join(ROOT, ARGS[0] if ARGS else os.path.join("sdileni", "data.json"))

SRC = ["sreality", "bezrealitky", "bazos", "ulovdomov"]
CATS = ["byt", "dum", "pozemek", "chata", "komercni", "garaz", "ostatni"]
DISPS = ["1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1", "5+", "atyp"]
PREC = ["live", "approx", "day", "?"]
PREFIX = {
    "sreality": "https://www.sreality.cz/detail/",
    "bezrealitky": "https://www.bezrealitky.cz/nemovitosti-byty-domy/",
    "bazos": "https://reality.bazos.cz/inzerat/",
    "ulovdomov": "https://www.ulovdomov.cz/inzerat/",
}


def num(v):
    if v is None:
        return None
    return int(round(v)) if abs(v - round(v)) < 0.05 else round(v, 1)


def build(items, with_img):
    """Veřejné inzeráty (jak je vrací /api/listings) → kompaktní snímek pro stránku."""
    rows = []
    for it in items:
        pre = PREFIX.get(it["s"], "")
        u = it.get("u") or ""
        rows.append([
            SRC.index(it["s"]),
            0 if it["o"] == "prodej" else 1,
            CATS.index(it["c"]) if it["c"] in CATS else len(CATS) - 1,
            DISPS.index(it["d"]) if it.get("d") in DISPS else -1,
            num(it.get("p")), num(it.get("a")), num(it.get("l")), num(it.get("m")),
            round(it["la"], 5), round(it["lo"], 5),
            int(it["ad"] // 60000) if it.get("ad") else None,     # minuty od 1970
            PREC.index(it.get("pr")) if it.get("pr") in PREC else 3,
            1 if it.get("own") == "maybe" else 0,
            1 if it.get("ap") else 0,
            it.get("t") or "", it.get("loc") or "",
            u[len(pre):] if pre and u.startswith(pre) else u,
            it.get("cur") or "",
        ] + ([it.get("img") or ""] if with_img else []))
    return {
        "at": int(time.time() * 1000), "src": SRC, "cats": CATS, "disps": DISPS, "prec": PREC, "prefix": PREFIX,
        "cols": ["s", "o", "c", "d", "p", "a", "l", "m", "la", "lo", "ad", "pr", "maybe", "ap", "t", "loc", "u", "cur"]
        + (["img"] if with_img else []),
        "rows": rows,
    }


def main():
    with urllib.request.urlopen("http://localhost:8765/api/listings?after=0", timeout=60) as r:
        out = build(json.load(r)["items"], WITH_IMG)
    rows = out["rows"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Uloženo {len(rows)} inzerátů do {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
