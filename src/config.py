"""Central config + the prompts Gemini runs against.

Secrets are read from environment variables (or a .env file next to run.py).
The pipeline finds candidates via Google Custom Search, then uses Gemini twice:
once to verify the image is free to use (by reading its source page), and once
to grade the image quality. Both need an AI backend (Vertex or a Gemini key).
"""
import os

# ---- load .env (simple parser, no dependency) -------------------------------
def _load_dotenv():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()

# ---- AI backend (choose ONE) ------------------------------------------------
# Vertex AI = bills your $300 Google Cloud credits (recommended).
USE_VERTEX   = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes")
GCP_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
# Gemini Developer API key (AI Studio) -- alternative to Vertex.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---- Google Programmable Search (the image source) --------------------------
GCSE_KEY = os.environ.get("GCSE_KEY", "")
GCSE_CX  = os.environ.get("GCSE_CX", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
IMAGEN_MODEL = os.environ.get("IMAGEN_MODEL", "imagen-3.0-generate-002")

def ai_enabled():
    return bool((USE_VERTEX and GCP_PROJECT) or GEMINI_API_KEY)

def search_enabled():
    return bool(GCSE_KEY and GCSE_CX)

# ---- the image we want ------------------------------------------------------
SPEC = (
    "a clear SIDE-PROFILE photo of ONE single whole fish/animal, the entire body "
    "visible, sharp focus, suitable for a clean product catalog. (Background and "
    "facing direction don't matter -- we remove the background and flip to face left.)"
)

# Prompt 1: verify the image is FREE TO USE, by reading its source web page.
LICENSE_PROMPT = """You verify whether an image found online is FREE TO USE COMMERCIALLY,
including the right to MODIFY it (we crop, flip and change its background).

The image shows the aquarium species "{latin}".
Source page URL: {url}
Text extracted from that page (truncated):
\"\"\"{page}\"\"\"

Decide using ONLY explicit licensing information on the page. Rules:
- FREE only if the page explicitly grants a commercial + modify license:
  CC0, Public Domain, CC-BY, CC-BY-SA, or a clear "free to use, including commercially" statement.
- NOT FREE if: no license is stated, "all rights reserved", any NonCommercial (NC) or
  NoDerivatives (ND) license, "editorial use only", or any paid / stock-photo license.
- If you are unsure or the evidence is weak, answer NOT free.

Return ONLY a compact JSON object:
- "free": true or false
- "license": short label, e.g. "CC-BY 4.0", "CC0", "Public Domain", or "none"
- "reason": ONE short sentence explaining the decision, quoting the licensing evidence you saw on the page
"""

# Prompt 2: grade the image quality for the catalog. Must return strict JSON.
GRADE_PROMPT = """You grade a candidate image for an aquarium-shop product catalog.
Target species (scientific name): "{latin}"  (common/local name: "{sk}").
We need: {spec}

Look at the image and answer ONLY with a compact JSON object, no markdown, with keys:
- "is_photo": true ONLY if it is a real photograph (NOT a drawing, painting, engraving, sketch, diagram, map, or scientific illustration)
- "is_animal": true if it clearly shows a live fish/aquatic animal (not a logo, person, plant-only, multiple-photo collage)
- "has_watermark_or_text": true if there is ANY overlaid text, signature, logo, copyright/credit mark, date stamp, URL, or visible border/frame anywhere in the image
- "matches_species": one of "yes","likely","unsure","no" - does it plausibly show the target species
- "single_subject": true if exactly one animal is the clear subject
- "side_profile": true if shown from the side (lateral), not top/front/3-4 view
- "facing": "left", "right", or "other" - which way the head points
- "background_clean": true if background is plain/simple (easy to cut out)
- "blurry": true if out of focus or very low quality
- "score": number 0.0-1.0 overall quality for our purpose
Return only the JSON."""

# Prompt for Imagen gap-fill generation.
def imagen_prompt(latin, sk):
    return (
        f"Professional studio product photo of a single {latin} ({sk}) aquarium fish, "
        f"complete side profile view, the whole body in frame, head pointing to the LEFT, "
        f"isolated on a pure plain white background, soft even lighting, sharp focus, "
        f"photorealistic, no text, no watermark, no other objects."
    )
