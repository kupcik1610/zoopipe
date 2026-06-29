"""Candidate-image finders. Each returns a list of candidate dicts:
   {url, source, license, attribution, page}
Only candidates whose license permits commercial use AND modification are kept
(see config.license_ok) -- because we flip + whiten = derivative works.
"""
from . import config
from .http import get_json

def _clean(name):
    # strip trade/morph suffixes so we match the actual species
    # e.g. "Poecilia reticulata BLUE TARZAN" -> "Poecilia reticulata"
    import re
    n = name.strip()
    n = re.sub(r"\b(XL|XXL|XS|S|M|L|cm|albino|albin|gold|long ?fin|lyra|f\.)\b.*$", "", n, flags=re.I)
    parts = n.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else n   # Genus species

# ---- iNaturalist ------------------------------------------------------------
def inaturalist(latin, want=4):
    out = []
    try:
        d = get_json("https://api.inaturalist.org/v1/taxa",
                     {"q": _clean(latin), "rank": "species", "per_page": 1})
    except Exception:
        return out
    if not d.get("results"):
        return out
    t = d["results"][0]
    photos = []
    if t.get("default_photo"):
        photos.append(t["default_photo"])
    for tp in (t.get("taxon_photos") or []):
        if tp.get("photo"):
            photos.append(tp["photo"])
    for p in photos[:want]:
        lic = p.get("license_code")
        if not config.license_ok(lic):
            continue
        url = p.get("medium_url") or p.get("url") or ""
        url = url.replace("/square.", "/large.").replace("/medium.", "/large.")
        if url:
            out.append({"url": url, "source": "inaturalist", "license": lic,
                        "attribution": p.get("attribution", ""), "page": t.get("wikipedia_url", "")})
    return out

# ---- Wikimedia Commons ------------------------------------------------------
def wikimedia(latin, want=3):
    out = []
    try:
        d = get_json("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f'filetype:bitmap "{_clean(latin)}"', "gsrnamespace": 6,
            "gsrlimit": want * 2, "prop": "imageinfo", "iiprop": "url|extmetadata",
            "iiurlwidth": 1024,
        })
    except Exception:
        return out
    pages = (d.get("query") or {}).get("pages") or {}
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName", {}) or {}).get("value", "") or \
              (meta.get("License", {}) or {}).get("value", "")
        if not config.license_ok(lic):
            continue
        url = info.get("thumburl") or info.get("url")
        if url:
            out.append({"url": url, "source": "wikimedia", "license": lic,
                        "attribution": (meta.get("Artist", {}) or {}).get("value", ""),
                        "page": info.get("descriptionurl", "")})
        if len(out) >= want:
            break
    return out

# ---- GBIF -------------------------------------------------------------------
def gbif(latin, want=3):
    out = []
    try:
        m = get_json("https://api.gbif.org/v1/species/match", {"name": _clean(latin)})
        key = m.get("usageKey")
        if not key:
            return out
        occ = get_json("https://api.gbif.org/v1/occurrence/search",
                       {"taxonKey": key, "mediaType": "StillImage", "limit": want * 3})
    except Exception:
        return out
    for rec in occ.get("results", []):
        for med in rec.get("media", []):
            if med.get("type") and med["type"] != "StillImage":
                continue
            lic = med.get("license", "")
            if not config.license_ok(lic):
                continue
            url = med.get("identifier")
            if url:
                out.append({"url": url, "source": "gbif", "license": lic,
                            "attribution": med.get("rightsHolder", "") or med.get("creator", ""),
                            "page": rec.get("references", "")})
            if len(out) >= want:
                return out
    return out

# ---- Google Custom Search (optional, needs keys) ----------------------------
def google_cse(latin, want=4):
    if not (config.GCSE_KEY and config.GCSE_CX):
        return []
    out = []
    try:
        d = get_json("https://www.googleapis.com/customsearch/v1", {
            "key": config.GCSE_KEY, "cx": config.GCSE_CX, "searchType": "image",
            "q": f"{_clean(latin)} fish", "imgType": "photo", "num": want,
            "rights": "cc_publicdomain,cc_attribute,cc_sharealike",
        })
    except Exception:
        return out
    for it in d.get("items", []):
        out.append({"url": it.get("link"), "source": "google_cse",
                    "license": "cc (unverified - check source page)",
                    "attribution": it.get("displayLink", ""),
                    "page": (it.get("image") or {}).get("contextLink", "")})
    return out

# ---- aggregate --------------------------------------------------------------
def find_candidates(latin):
    cands = []
    for fn in (inaturalist, wikimedia, gbif, google_cse):
        try:
            cands.extend(fn(latin))
        except Exception:
            pass
    # de-dup by url
    seen, uniq = set(), []
    for c in cands:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"]); uniq.append(c)
    return uniq
