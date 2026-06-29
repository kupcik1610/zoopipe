"""Central config + the exact image spec we grade against.

Secrets are read from environment variables (or a .env file next to run.py).
Nothing here is required to run the FREE stage (iNaturalist/Wikimedia/GBIF).
The Google stages activate automatically once the matching keys are present.
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

# ---- Google Programmable Search (optional candidate source) -----------------
GCSE_KEY = os.environ.get("GCSE_KEY", "")
GCSE_CX  = os.environ.get("GCSE_CX", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
IMAGEN_MODEL = os.environ.get("IMAGEN_MODEL", "imagen-3.0-generate-002")

def ai_enabled():
    return bool((USE_VERTEX and GCP_PROJECT) or GEMINI_API_KEY)

# ---- licensing policy -------------------------------------------------------
# We FLIP and WHITEN images => we create derivatives. So we may ONLY use
# licenses that allow BOTH commercial use AND modification.
# That excludes every NC (non-commercial) and ND (no-derivatives) license.
ALLOWED_LICENSES = {
    "cc0", "cc-0", "pd", "publicdomain", "public domain", "no known copyright",
    "cc-by", "cc-by-4.0", "cc-by-3.0", "cc-by-2.0", "cc-by-2.5", "cc-by-1.0",
    "cc-by-sa", "cc-by-sa-4.0", "cc-by-sa-3.0", "cc-by-sa-2.0",  # SA = allowed but attribution+sharealike
    "attribution", "attribution-sharealike",
}

def license_ok(code):
    if not code:
        return False
    c = code.strip().lower().replace("_", "-").replace("https://", "").replace("http://", "")
    # normalise URLs like creativecommons.org/licenses/by/4.0/
    if "creativecommons.org/publicdomain" in c:
        return True
    if "creativecommons.org/licenses/by/" in c or "creativecommons.org/licenses/by-sa/" in c:
        return True
    if "/by-nc" in c or "/by-nd" in c or "-nc-" in c or c.endswith("-nc") or "-nd" in c:
        return False
    return c in ALLOWED_LICENSES

# ---- the image we want ------------------------------------------------------
SPEC = (
    "a clear SIDE-PROFILE photo of ONE single whole fish/animal, the entire body "
    "visible, sharp focus, plain/uncluttered background, suitable for a clean "
    "product catalog. The animal should ideally face left (head on the left)."
)

# Prompt for the Gemini vision grader. Must return strict JSON.
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
- "usable": true ONLY if it is a clean real photo, single subject, no watermark/text, good for a catalog
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
