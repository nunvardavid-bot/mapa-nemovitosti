#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jedno kolo aktualizace pro GitHub Actions (běží každou hodinu, i když máš počítač vypnutý).

Načte uložený stav (data/state.json, mezi běhy ho drží cache GitHubu; poprvé seed/state.json.gz),
BUDGET_MIN minut stahuje novinky stejně jako server.py, uloží stav a do zadané složky
vyrobí web: index.html, kraje.json a čerstvý data.json.

  python3 run_once.py _site
"""
import gzip
import json
import os
import shutil
import sys
import time

import export_snapshot
import server

MINUTES = float(os.environ.get("BUDGET_MIN", "25"))
SITE = sys.argv[1] if len(sys.argv) > 1 else "_site"
SEED = os.path.join(server.ROOT, "seed", "state.json.gz")


def main():
    if not os.path.exists(server.STATE_FILE) and os.path.exists(SEED):
        os.makedirs(server.DATA_DIR, exist_ok=True)
        with gzip.open(SEED, "rb") as f, open(server.STATE_FILE, "wb") as out:
            shutil.copyfileobj(f, out)
        server.log("Uložený stav nenalezen, začínám z seed/state.json.gz")
    server.store.load()
    server.prepare()
    end = time.time() + MINUTES * 60
    while time.time() < end:
        time.sleep(15)

    server.store.dirty = True
    server.store.save()
    snap = export_snapshot.build(server.store.delta("", 0)["items"], with_img=True)
    os.makedirs(SITE, exist_ok=True)
    for name in ("index.html", "kraje.json"):
        shutil.copy(os.path.join(server.ROOT, "web", name), os.path.join(SITE, name))
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))

    vis, seen = server.counts_by_source()
    for src in ("sreality", "bezrealitky", "bazos", "ulovdomov"):
        server.log(f"{src}: na mapě {vis.get(src, 0)}, prošlo {seen.get(src, 0)}")
    server.log(f"Web připraven: {len(snap['rows'])} inzerátů")
    sys.stdout.flush()
    os._exit(0)     # vlákna se stahováním nečekají na dokončení rozběhnutých dotazů


if __name__ == "__main__":
    main()
