# zoopipe

Flask appka na zbieranie čistých katalógových obrázkov rýb (jeden subjekt,
biele pozadie, hlava vľavo).

`page*.html` (export z MiniZOO) → `parse/parse.py` → `data/ryby.csv` → `app.py` → `out/`.
Štruktúra je univerzálna pre akékoľvek zvieratá, teraz ryby.

## Ako to funguje

1. **Vyber CSV** z `data/`.
2. **Vyber stĺpec(ce)**, z ktorých sa zostaví vyhľadávací dopyt (napr. latinský
   názov) + koľko výsledkov a minimálnu šírku.
3. **DuckDuckGo image search** ukáže kandidátov — **zaškrtni**
   tie, ktoré chceš.
4. Vybrané obrázky sa stiahnu, `rembg` odstráni pozadie, `pillow` dá na biele
   pozadie a uloží do `out/<ryba>/` (originál do `out/<ryba>/originals/`).
5. V editore (`/edit`) môžeš dorotovať / flipnúť / orezať jednotlivý obrázok.

## Spustenie

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py        # http://127.0.0.1:5001
```

Najprv preparsovať zdroj (ak `data/ryby.csv` ešte nie je):
```bash
python3 parse/parse.py         # číta parse/page*.html → data/ryby.csv
```
