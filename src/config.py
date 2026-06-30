"""Central config + the prompts Gemini runs against.

Secrets are read from environment variables (or a .env file next to run.py).
The pipeline finds candidates via DuckDuckGo image search, then uses Gemini
twice: once to verify the image is free to use (by reading its source page), and
once to grade the image quality. Both need an AI backend (Vertex or a Gemini key).
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

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
IMAGEN_MODEL = os.environ.get("IMAGEN_MODEL", "imagen-3.0-generate-002")

def ai_enabled():
    return bool((USE_VERTEX and GCP_PROJECT) or GEMINI_API_KEY)

# ---- the image we want ------------------------------------------------------
SPEC = (
    "a clean catalog photo of ONE whole fish/animal in SHARP focus, shown from the "
    "side, CENTERED with space around it, standing out clearly from its background so "
    "it is EASY TO CUT OUT automatically. (We remove the background and flip it to "
    "face left, so background colour and facing direction themselves don't matter.)"
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

# Prompt 2: grade the image quality + post-processing fitness. Strict JSON.
GRADE_PROMPT = """You grade a candidate image for an aquarium-shop product catalog.
The chosen image will be AUTO-PROCESSED: software removes its background, then flips
it so the head faces left. So judge both catalog quality AND how easy it is to edit.

Target species (scientific name): "{latin}"  (common/local name: "{sk}").
We need: {spec}

Answer ONLY with a compact JSON object, no markdown, with keys:
- "is_photo": true ONLY if a real photograph (NOT a drawing, painting, 3D render, diagram, or illustration)
- "is_animal": true if it clearly shows a live fish/aquatic animal (not a logo, person, plant-only, or multi-photo collage)
- "matches_species": one of "yes","likely","unsure","no" - does it plausibly show the target species
- "single_subject": true if exactly ONE animal is the clear subject
- "whole_body_visible": true if the entire animal is in frame, not cropped at the edges
- "side_profile": true if a lateral side view (not top/front/three-quarter view)
- "facing": "left", "right", or "other" - which way the head points
- "centered": true if the animal is roughly centered with margin around it
- "sharpness": "high", "medium", or "low" - how crisp/in-focus the animal is
- "background_separable": true if the animal stands out clearly from the background so AUTOMATIC background removal will work cleanly (good edge contrast; NOT camouflaged or blended into similar colours/clutter)
- "has_watermark_or_text": true if ANY overlaid text, signature, logo, copyright/credit mark, URL, date stamp, or visible border/frame
- "score": number 0.0-1.0 overall fit for a clean, easily-editable catalog image
Return only the JSON."""

# Prompt for Imagen gap-fill generation.
def imagen_prompt(latin, sk):
    return (
        f"Professional studio product photo of a single {latin} ({sk}) aquarium fish, "
        f"complete side profile view, the whole body in frame, head pointing to the LEFT, "
        f"isolated on a pure plain white background, soft even lighting, sharp focus, "
        f"photorealistic, no text, no watermark, no other objects."
    )
