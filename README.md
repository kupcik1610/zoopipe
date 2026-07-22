# zoopipe

Flask appka na zbieranie čistých katalógových obrázkov zvierat (jeden subjekt,
biele pozadie). Jedna obrazovka: galéria druhov z CSV, pri každom druhu si nájdeš
a vyberieš fotky. Fotka slúži ako predloha — z nej **Gemini** vygeneruje čistý
obrázok toho istého zvieraťa (podľa latinského názvu) na bielom pozadí, ktorý sa
potom nahrá na MiniZOO.

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
   jeden **worker** (vlákno v appke, `worker.run`) fotku stiahne, pošle ju **Gemini**
   ako predlohu (`gemini.make_frame`), vygenerovaný obrázok na bielom pozadí uloží
   ako plát do `out/<csv>/<idpr>_<druh>/`. Karty sa dopĺňajú naživo.
4. Pri hotovom pláte: **edit** (otočiť/zrkadliť, vždy sa vycentruje) a **upload**
   (na produkt v MiniZOO cez `upload.py`, `cookie.txt` musí byť prihlásený).

Generovanie sa ladí v `gemini.py` (`GEMINI_MODEL`, prompt, `GEMINI_BG_TOL`);
veľkosť plátu je `CANVAS` v `imaging.py`.

## Spustenie

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # http://127.0.0.1:5001
```

Na generovanie treba prístup ku Gemini image modelu (default `gemini-3-pro-image`,
"Nano Banana Pro"; override cez `GEMINI_MODEL`, napr. `gemini-3.1-flash-image`).
Dva backendy:

- **Vertex AI** (účtuje sa na Google Cloud projekt → **platia free kredity**):
  ```bash
  gcloud auth login                     # alebo bež na GCE VM (token z metadata)
  export VERTEX_PROJECT=<tvoj-projekt>  # zapne Vertex backend
  export VERTEX_LOCATION=global         # (default)
  ```
  Projekt musí mať zapnuté **Vertex AI API** a účet rolu *Vertex AI User*.
- **AI Studio** (samostatné prepay účtovanie, nie Cloud kredity):
  ```bash
  export GEMINI_API_KEY=...             # https://aistudio.google.com/apikey
  ```

Bez prístupu sa fotky stiahnu, ale generovanie zlyhá (job skončí ako `error`).
Worker beží nonstop ako jedno vlákno vnútri appky — netreba (ani sa nedá) ho
spúšťať zvlášť. Viac naraz vybraných fotiek jedného druhu ide do **jedného**
generovania ako viac predlôh → jeden plát.

Preparsovať zdroj (ak CSV ešte nie je):
```bash
python3 parse/parse.py         # číta parse/page*.html → data/*.csv
```

## Súbory

| súbor | čo robí |
|---|---|
| `app.py` | Flask: galéria, `/search`, `/process`, `/status`, `/edit`, `/upload` |
| `worker.py` | worker vlákno (`run`): stiahni → vygeneruj cez Gemini → ulož plát |
| `db.py` | SQLite, jedna tabuľka `photos` (ready→processing→done/error) |
| `gemini.py` | Gemini generovanie: predloha → čistý obrázok na bielom |
| `imaging.py` | biely rámik + edit (otočiť/zrkadliť/orezať) |
| `upload.py` | verné nahratie fotky na produkt MiniZOO |
| `templates/index.html` + `static/app.js` + `static/style.css` | celé UI |
