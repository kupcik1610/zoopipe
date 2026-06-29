# zoopipe

Pre každý druh z `data/*.csv` (podľa **latinského názvu**) nájde čistý katalógový
obrázok: jeden subjekt, bočný profil, hlava vľavo, biele pozadie.

`page*.html` (export z MiniZOO) → `parse/parse.py` → `data/ryby.csv` → `run.py` → `out/`.
Teraz ryby, ale štruktúra je univerzálna pre akékoľvek zvieratá.

## Spustenie

```bash
# bez kľúčov, bez inštalácie — len free zdroje, 25-riadkový test set:
python3 run.py --no-grade

python3 run.py                       # 25 riadkov, s AI hodnotením
python3 run.py --csv data/ryby.csv   # celý zoznam (1531)
python3 run.py --gap-generate        # chýbajúce druhy dogeneruje AI
python3 run.py --limit 5             # rýchly smoke test
```

Najprv preparsovať zdroj (ak `data/ryby.csv` ešte nie je):
```bash
python3 parse/parse.py               # číta parse/page*.html → data/ryby.csv
```

Výstup v `out/<run>/`: `images/<kod>.jpg`, `manifest.csv`, `contact_sheet.html`
(otvor v prehliadači a prejdi výsledky).

## Kde sa používa AI

Pipeline beží aj úplne bez AI (free zdroje iNaturalist / Wikimedia / GBIF).
S kľúčmi sa zapnú dva Google kroky cez `google-genai`:

- **Hodnotenie (Gemini vision, `gemini-2.5-flash`)** — každý nájdený kandidát sa
  ohodnotí: je tam jeden subjekt? bočný pohľad? ktorým smerom? čisté pozadie?
  Z toho sa vyberie najlepší. Vypína sa cez `--no-grade`.
- **Dogenerovanie (Imagen, `imagen-3.0-generate-002`)** — druhy bez použiteľnej
  reálnej fotky vie cez `--gap-generate` dokresliť. Sú označené ako
  `ai_generated` v manifeste — nevydávajú sa za reálnu fotku.

### Zapnutie AI

1. `cp .env.example .env` a vyplň **jeden** backend:
   - **Vertex AI** (míňa $300 GCP kredit) — `GOOGLE_GENAI_USE_VERTEXAI=true` +
     `GOOGLE_CLOUD_PROJECT` + auth (`GOOGLE_APPLICATION_CREDENTIALS` na JSON kľúč,
     alebo `gcloud auth application-default login`), alebo
   - **Gemini API kľúč** z https://aistudio.google.com/apikey → `GEMINI_API_KEY`.
2. Knižnice:
   ```bash
   python3 -m pip install --user google-genai pillow rembg
   ```
   `pillow`+`rembg` robia flip a biele pozadie; bez nich sa uloží surová fotka.

> Používame len licencie povoľujúce komerčné použitie **aj** úpravu
> (CC0, CC-BY, CC-BY-SA, public domain) — lebo obrázok flipujeme a whitujeme.
