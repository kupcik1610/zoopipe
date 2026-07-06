# zoopipe

Flask appka na zbieranie čistých katalógových obrázkov zvierat (jeden subjekt,
biele pozadie). Jedna obrazovka: galéria druhov z CSV, pri každom druhu si nájdeš
a vyberieš fotky, tie sa na pozadí orežú a nahrajú na MiniZOO.

`page*.html` (export z MiniZOO) → `parse/parse.py` → `data/*.csv` → `app.py` → `out/`.

## Ako to funguje

Otvoríš appku → **galéria** všetkých druhov vo vybranom CSV. Nič sa nepredhľadáva
vopred a nie sú žiadne dávky — pracuješ v ľubovoľnom poradí a stav si appka pamätá
(jeden riadok v `out/jobs.sqlite` na každú vybranú fotku), takže môžeš kedykoľvek
zavrieť a pokračovať.

1. **Vyber CSV** hore (dropdown). Karty sa dajú filtrovať: *All / To do / Working
   / Done / Uploaded*.
2. **Search** na karte → inline sa spustí DuckDuckGo image search (podľa latinského
   názvu; text sa dá upraviť a hľadať znova). **Klikni** na fotky, ktoré chceš.
3. **Process** → hneď sa vráti, panel sa zavrie a môžeš ísť na ďalší druh. Na pozadí
   jeden **worker** (vlákno v appke, `worker.run`) fotku stiahne, cez **birefnet**
   (rembg) odstráni pozadie a uloží plát do `out/<csv>/<idpr>_<druh>/`. Karty sa
   dopĺňajú naživo.
4. Pri hotovom pláte: **edit** (otočiť/zrkadliť, vždy sa vycentruje) a **upload**
   (na produkt v MiniZOO cez `upload.py`, `cookie.txt` musí byť prihlásený).

Kvalita odstránenia pozadia sa ladí v `imaging.py` (`REMBG_MODEL`, `EDGE_ERODE`,
`EDGE_FEATHER`, `CANVAS`).

## Spustenie

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py        # http://127.0.0.1:5001
```

Model na odstránenie pozadia (~1 GB) sa stiahne sám pri prvom spracovaní; prvá
fotka preto chvíľu trvá. Worker beží nonstop ako jedno vlákno vnútri appky —
netreba ho spúšťať zvlášť. Na debug sa dá pustiť aj samostatne (drení frontu
donekonečna):

```bash
.venv/bin/python worker.py
```

Preparsovať zdroj (ak CSV ešte nie je):
```bash
python3 parse/parse.py         # číta parse/page*.html → data/*.csv
```

## Súbory

| súbor | čo robí |
|---|---|
| `app.py` | Flask: galéria, `/search`, `/process`, `/status`, `/edit`, `/upload` |
| `worker.py` | worker vlákno (`run`): stiahni → odstráň pozadie → ulož plát |
| `db.py` | SQLite, jedna tabuľka `photos` (ready→processing→done/error) |
| `imaging.py` | rembg cut-out + biely rámik |
| `upload.py` | verné nahratie fotky na produkt MiniZOO |
| `templates/index.html` + `static/app.js` + `static/style.css` | celé UI |
