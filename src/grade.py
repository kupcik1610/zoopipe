"""AI vision grading via google-genai (Vertex AI or Gemini Dev API).

Sends the candidate image + our spec, gets back strict JSON judging whether the
photo is a single, side-on, ideally-left-facing, clean-background subject with
no watermark. Falls back to a neutral 'ungraded' verdict if no backend is set.
"""
import json, re
from . import config
from .client import get_client

def _mime(b):
    if b[:3] == b"\xff\xd8\xff":               return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":           return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    return "image/jpeg"

def grade(image_bytes, latin, sk):
    client = get_client()
    if client is None:
        return {"graded": False, "usable": True, "score": 0.5, "facing": "other",
                "note": "no AI backend - candidate accepted ungraded"}
    from google.genai import types
    prompt = config.GRADE_PROMPT.format(latin=latin, sk=sk, spec=config.SPEC)
    last = ""
    for attempt in range(2):
        try:
            resp = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=image_bytes, mime_type=_mime(image_bytes)),
                          prompt],
                config=types.GenerateContentConfig(
                    temperature=0, response_mime_type="application/json"),
            )
            text = resp.text or ""
            m = re.search(r"\{.*\}", text, re.S)
            v = json.loads(m.group(0) if m else text)
            v["graded"] = True
            return v
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
    return {"graded": False, "usable": True, "score": 0.4, "facing": "other",
            "note": f"grade error: {last}"}

def quality_score(v):
    """Collapse a verdict into a single sortable score (higher = better)."""
    if not v.get("graded"):
        return v.get("score", 0.4)
    # hard rejections: not a photo, not an animal, or any watermark/text overlay
    if (v.get("is_photo") is False or v.get("is_animal") is False
            or v.get("has_watermark_or_text") is True or v.get("usable") is False):
        return 0.0
    s = float(v.get("score", 0.5) or 0.5)
    if v.get("single_subject"):    s += 0.15
    if v.get("side_profile"):      s += 0.20
    if v.get("background_clean"):  s += 0.10
    if v.get("blurry"):            s -= 0.30
    if v.get("matches_species") == "yes":   s += 0.20
    elif v.get("matches_species") == "no":  s -= 0.40
    return max(0.0, min(2.0, s))
