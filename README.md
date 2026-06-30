# zoopipe

Flask appka na zbieranie čistých katalógových obrázkov rýb (jeden subjekt,
biele pozadie, hlava vľavo).

`page*.html` (export z MiniZOO) → `parse/parse.py` → `data/ryby.csv` → `app.py` → `out/`.
Štruktúra je univerzálna pre akékoľvek zvieratá, teraz ryby.

## Ako to funguje (dávkovo, s možnosťou pokračovať)

Veľké CSV (napr. ~1500 rýb) sa nespracúva naraz — ide po **dávkach** a tvoja
pozícia v CSV (cursor) aj stav každého obrázka sa pamätajú v `out/jobs.sqlite`,
takže appku môžeš zavrieť a kedykoľvek pokračovať tam, kde si skončil.

1. **Vyber CSV** z `data/` — domovská stránka ukáže priebeh každého „runu“.
2. **Vyber stĺpec(ce)** pre vyhľadávací dopyt, počet výsledkov a **veľkosť
   dávky** (rows per batch, default 50).
3. **Collect** — pre aktuálnu dávku (ďalších `batch_size` riadkov) sa spustí
   DuckDuckGo image search; **zaškrtni** obrázky, ktoré chceš.
4. **Process** — vybrané obrázky sa hneď stiahnu (originály do
   `out/<ryba>/originals/`) a zaradia do fronty. Na pozadí beží `worker.py`,
   ktorý cez **birefnet** (rembg) odstráni pozadie a uloží plát do
   `out/<ryba>/`. **Tabuľka priebehu** ukazuje spracovanie naživo.
5. Po dokončení uprav jednotlivé obrázky v editore (`/edit`) a **collectni
   ďalšiu dávku**.

Kvalita odstránenia pozadia (model + voľby) sa ladí v `imaging.py`
(konštanty `REMBG_MODEL`, `USE_ALPHA_MATTING`, `POST_PROCESS_MASK`).
Recepty sa dajú porovnať v experimentálnej appke `bg_lab.py`.

## Spustenie

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py        # http://127.0.0.1:5001
```

Worker spúšťa tlačidlo **Process** automaticky (ako samostatný proces). Dá sa
spustiť aj ručne v termináli — spracuje všetko, čo je vo fronte, a skončí:

```bash
.venv/bin/python worker.py     # cez noc: nohup .venv/bin/python worker.py &
```

Najprv preparsovať zdroj (ak `data/ryby.csv` ešte nie je):
```bash
python3 parse/parse.py         # číta parse/page*.html → data/ryby.csv
```
