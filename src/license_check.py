"""Verify an image is free to use by reading its SOURCE PAGE with Gemini.

A license is a fact about the image's provenance, not its pixels -- so we fetch
the web page the image lives on, extract the text, and ask Gemini whether that
page explicitly grants a commercial + modify license. Returns the decision plus
a short human-readable reason that gets stored in the manifest.
"""
import json, re
from . import config
from .client import get_client
from .http import get_bytes

def _page_text(url, limit=6000):
    raw = get_bytes(url, timeout=20)
    html = raw.decode("utf-8", "replace")
    html = re.sub(r"(?is)<script.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text[:limit]

def check(page_url, latin):
    """Return {free: bool, license: str, reason: str}."""
    client = get_client()
    if client is None:
        return {"free": False, "license": "", "reason": "no AI backend configured"}
    if not page_url:
        return {"free": False, "license": "", "reason": "no source page to verify"}
    try:
        text = _page_text(page_url)
    except Exception as e:
        return {"free": False, "license": "", "reason": f"could not fetch source page ({type(e).__name__})"}

    from google.genai import types
    prompt = config.LICENSE_PROMPT.format(latin=latin, url=page_url, page=text)
    try:
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL, contents=[prompt],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
        )
        m = re.search(r"\{.*\}", resp.text or "", re.S)
        v = json.loads(m.group(0) if m else (resp.text or "{}"))
        return {"free": bool(v.get("free")),
                "license": str(v.get("license", "")),
                "reason": str(v.get("reason", ""))}
    except Exception as e:
        return {"free": False, "license": "", "reason": f"license check error ({type(e).__name__})"}
