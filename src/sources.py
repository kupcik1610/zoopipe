"""Candidate-image finder: Google Programmable Search (image search).

Returns candidate dicts: {url, source, attribution, page}
  - url:  direct image URL
  - page: the web page the image lives on (used later to verify the license)
Licensing is NOT decided here -- every candidate's source page is checked by
Gemini in license_check.py before the image is allowed into the catalog.
"""
import re
from . import config
from .http import get_json

def _clean(name):
    """Strip trade/morph/size suffixes so we search the actual species.
    e.g. 'Poecilia reticulata BLUE TARZAN' -> 'Poecilia reticulata'."""
    n = re.sub(r"\b(XL|XXL|XS|S|M|L|cm|albino|albin|gold|long ?fin|lyra|f\.)\b.*$",
               "", name.strip(), flags=re.I)
    parts = n.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else n   # Genus species


def google_cse(latin, want=8):
    if not config.search_enabled():
        return []
    out = []
    try:
        d = get_json("https://www.googleapis.com/customsearch/v1", {
            "key": config.GCSE_KEY, "cx": config.GCSE_CX, "searchType": "image",
            "q": f"{_clean(latin)} fish", "imgType": "photo", "num": min(want, 10),
            "rights": "cc_publicdomain,cc_attribute,cc_sharealike",
        })
    except Exception:
        return out
    for it in d.get("items", []):
        img = it.get("image") or {}
        out.append({"url": it.get("link"), "source": "google_cse",
                    "attribution": it.get("displayLink", ""),
                    "page": img.get("contextLink", "")})
    return out


def find_candidates(latin):
    cands = google_cse(latin)
    seen, uniq = set(), []
    for c in cands:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"]); uniq.append(c)
    return uniq
