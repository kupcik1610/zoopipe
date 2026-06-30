"""Candidate-image finder: DuckDuckGo image search.

Returns candidate dicts: {url, source, attribution, page}
  - url:  direct image URL
  - page: the web page the image lives on (used later to verify the license)
Licensing is NOT decided here -- every candidate's source page is checked by
Gemini in license_check.py before the image is allowed into the catalog.
"""
import re
from ddgs import DDGS

def _clean(name):
    """Strip trade/morph/size suffixes so we search the actual species.
    e.g. 'Poecilia reticulata BLUE TARZAN' -> 'Poecilia reticulata'."""
    n = re.sub(r"\b(XL|XXL|XS|S|M|L|cm|albino|albin|gold|long ?fin|lyra|f\.)\b.*$",
               "", name.strip(), flags=re.I)
    parts = n.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else n   # Genus species


def ddg_images(latin, want=25):
    """DuckDuckGo image search -- no API key, no quota.
    License is decided downstream by the source-page check, so we don't
    filter here."""
    query = f"{_clean(latin)} fish"
    out = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.images(query, max_results=want):
                out.append({
                    "url": r.get("image"),          # direct image URL
                    "source": "ddg",
                    "attribution": r.get("source", ""),
                    "page": r.get("url", ""),        # page the image lives on
                })
    except Exception:
        pass
    return out[:want]


def find_candidates(latin, want=25):
    cands = ddg_images(latin, want=want)
    seen, uniq = set(), []
    for c in cands:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"]); uniq.append(c)
    return uniq
