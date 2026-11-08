# Nemovitosti od majitelů – mapa celé ČR

Mapa inzerátů přímo od majitelů, bez realitních kanceláří, ze čtyř zdrojů:

| Zdroj | Co se bere | Poloha |
|---|---|---|
| Sreality | jen soukromí inzerenti (bez RK) | přesná |
| Bezrealitky | všechny inzeráty (inzerují tam jen majitelé), kromě rekreačních pobytů v chatách | přesná |
| Bazoš | realitky se odfiltrují podle jména účtu, textu a počtu inzerátů na účtu | přibližná (podle PSČ) |
| UlovDomov | jen účty „soukromý pronajímatel“, navíc kontrola textu | přesná |

## Spuštění

Dvojklik na `start.command`, nebo v Terminálu:

```
python3 server.py
```

Otevře se http://localhost:8765. Okno Terminálu nech otevřené – dokud server
běží, hlídá nové inzeráty (Sreality každé 2 minuty, ostatní každých 5 minut)
a mapa se sama obnovuje každých 30 sekund.

**První spuštění trvá 1–2 hodiny.** Weby se procházejí pomalu, aby je server
nezahltil (Bezrealitky při rychlejším tempu začnou odpovídat velmi pomalu).
Inzeráty na mapě přibývají průběžně a stav je vidět vpravo dole. Vše se ukládá
do složky `data/`; když server zavřeš, příště pokračuje, kde skončil.

## Veřejná verze, která se obnovuje sama (GitHub)

Složka `web/` je veřejná verze mapy a soubor `.github/workflows/aktualizace.yml`
ji každou hodinu obnovuje na GitHub Pages, i když je počítač vypnutý:

1. GitHub Actions spustí `run_once.py`: načte uložený stav (mezi běhy ho drží cache
   GitHubu, napoprvé `seed/state.json.gz`), 25 minut stahuje novinky stejně jako
   `server.py` a vyrobí `index.html`, `kraje.json` a čerstvý `data.json`.
2. Výsledek se zveřejní na `https://<uživatel>.github.io/mapa-nemovitosti/`.

Ruční spuštění: na GitHubu záložka **Actions → Aktualizace mapy → Run workflow**.
Stránka se nezobrazí ve vyhledávačích, ale kdo má odkaz, ten ji otevře.

## Jak se určuje, kdy byl inzerát přidán

- **Sreality**: skutečné datum vložení z detailu inzerátu (ne datum poslední
  aktualizace, které je u většiny inzerátů „dnes“).
- **Bezrealitky**: čas nahrání fotek.
- **Bazoš**: odhad z čísla inzerátu (Bazoš čísluje postupně), protože datum
  u topovaných inzerátů ukazuje den topování, ne den vložení.
- **UlovDomov**: přesný čas zveřejnění.
- Inzeráty, které se objeví, když server už běží, mají čas přesný na pár minut.

„≈“ u času znamená odhad. Filtry 1 / 3 / 10 hodin jsou nejpřesnější, když
server běží delší dobu.

## Ovládání

- Najetí myší na tečku: cena, plocha, cena za m², kdy byl inzerát přidán.
- Kliknutí na tečku: otevře originální inzerát. Když je na jednom místě víc
  inzerátů, ukáže se jejich seznam.
- Filtry nahoře: prodej/pronájem, typ nemovitosti, dispozice (1+kk až 5+),
  stáří inzerátu, cena, plocha, zdroj, oblast.
- „i nejisté“ zobrazí i inzeráty z Bazoše a UlovDomova, u kterých si filtr
  není jistý, že nejde o realitku (v popupu označené ⚠).

## Co mapa neumí

Všechny nemovitosti od majitelů to nikdy nebudou: spousta lidí prodává přes
Facebookové skupiny, cedule nebo známé. Filtr na Bazoši je odhad podle textu
a účtu – občas propustí realitku a občas vyřadí majitele.

Intervaly obnovy se dají změnit na začátku `server.py`.
