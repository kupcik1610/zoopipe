#!/usr/bin/env python3
"""Diagnostic: for given species, show every candidate's source/license and the
grader's full verdict, and save each candidate image to out/diag/<latin>/ so we
can eyeball WHY it was accepted or rejected."""
import warnings; warnings.filterwarnings("ignore")
import os, sys, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import sources, grade as grading
from src.http import get_bytes

SPECIES = sys.argv[1:] or ["Acipenser ruthenus", "Caridina japonica", "Amphiprion ocellaris"]
OUT = "out/diag"

def is_image(b):
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or (b[:4]==b"RIFF" and b[8:12]==b"WEBP")

for latin in SPECIES:
    print("\n" + "=" * 70 + f"\n{latin}")
    cands = sources.find_candidates(latin)
    print(f"  {len(cands)} candidates")
    d = os.path.join(OUT, re.sub(r"[^a-z0-9]+", "_", latin.lower()))
    os.makedirs(d, exist_ok=True)
    for i, c in enumerate(cands):
        try:
            img = get_bytes(c["url"])
        except Exception as e:
            print(f"  [{i}] {c['source']:11} DOWNLOAD-FAIL {type(e).__name__}  {c['url'][:60]}")
            continue
        if not is_image(img):
            print(f"  [{i}] {c['source']:11} NOT-AN-IMAGE ({len(img)}b, starts {img[:12]!r})  lic={c['license'][:18]}")
            continue
        v = grading.grade(img, latin, "")
        sc = grading.quality_score(v)
        ext = "jpg" if img[:3]==b"\xff\xd8\xff" else ("png" if img[1:4]==b"PNG" else "img")
        open(os.path.join(d, f"{i}_{c['source']}_score{sc:.2f}.{ext}"), "wb").write(img)
        keys = {k: v.get(k) for k in ("is_photo","is_animal","has_watermark_or_text",
                "matches_species","single_subject","side_profile","facing","background_clean","blurry")}
        print(f"  [{i}] {c['source']:11} lic={c['license'][:16]:16} score={sc:.2f}  {json.dumps(keys,ensure_ascii=False)}")
print(f"\nimages saved under {OUT}/")
