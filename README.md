# zoopipe

Pre každý druh z `data/*.csv` (podľa **latinského názvu**) nájde čistý katalógový
obrázok: jeden subjekt, bočný profil, hlava vľavo, biele pozadie.

`page*.html` (export z MiniZOO) → `parse/parse.py` → `data/ryby.csv` → `run.py` → `out/`.
Teraz ryby, ale štruktúra je univerzálna pre akékoľvek zvieratá.

## Ako to funguje (na jeden druh)

1. **Google Custom Search** — nájde kandidátov (obrázky) podľa latinského názvu.
2. **Overenie licencie (Gemini)** — pre každého kandidáta prečíta **zdrojovú
   stránku** obrázka a rozhodne, či je voľne použiteľný (komerčne **aj** úprava).
   Krátke zdôvodnenie sa uloží do manifestu (`license_reason`). Bez explicitnej
   voľnej licencie = zamietnuté.
3. **Hodnotenie kvality (Gemini vision)** — z voľných kandidátov vyberie najlepší
   (jeden subjekt? bočný pohľad? vodoznak? ktorým smerom hľadí?).
4. **Príprava** — `rembg` odstráni pozadie, `pillow` flipne na hlavu-vľavo a dá
   na biele pozadie. Originál sa tiež uloží do `originals/`.
5. Druhy bez použiteľnej voľnej fotky vie `--gap-generate` dokresliť cez **Imagen**
   (označené `ai_generated` — nevydávajú sa za reálnu fotku).

## Spustenie

```bash
python3 run.py                       # 25-riadkový test set
python3 run.py --csv data/ryby.csv   # celý zoznam (1531)
python3 run.py --gap-generate        # chýbajúce druhy dogeneruje AI (Imagen)
python3 run.py --limit 5             # rýchly smoke test
python3 run.py --workers 8           # počet súbežne spracovaných druhov
```

Najprv preparsovať zdroj (ak `data/ryby.csv` ešte nie je):
```bash
python3 parse/parse.py               # číta parse/page*.html → data/ryby.csv
```

Výstup v `out/<run>/`: `images/<kod>.jpg`, `originals/<kod>.<ext>`,
`manifest.csv`, `contact_sheet.html` (otvor v prehliadači a prejdi výsledky).

## Nastavenie (`.env`)

`cp .env.example .env` a vyplň **oboje**:

1. **AI backend** (Gemini + Imagen):
   - **Vertex AI** (míňa $300 GCP kredit) — `GOOGLE_GENAI_USE_VERTEXAI=true` +
     `GOOGLE_CLOUD_PROJECT` + auth (`GOOGLE_APPLICATION_CREDENTIALS` na JSON kľúč,
     alebo `gcloud auth application-default login`), alebo
   - **Gemini API kľúč** z https://aistudio.google.com/apikey → `GEMINI_API_KEY`.
2. **Google Custom Search** (zdroj obrázkov) — `GCSE_KEY` + `GCSE_CX`:
   - cx: https://programmablesearchengine.google.com (celý web + Image search)
   - key: https://console.cloud.google.com/apis/credentials (+ zapni „Custom Search API“)

Knižnice:
```bash
python3 -m pip install --user google-genai pillow rembg
```

> Akceptujeme len licencie povoľujúce komerčné použitie **aj** úpravu
> (CC0, CC-BY, CC-BY-SA, public domain) — lebo obrázok orezávame, flipujeme
> a meníme pozadie. „Žiadna licencia uvedená“ = všetky práva vyhradené = zamietnuté.
