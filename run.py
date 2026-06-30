#!/usr/bin/env python3
"""Run the fish-image pipeline over a CSV.

  python3 run.py                      # 25-row test set
  python3 run.py --csv data/ryby.csv  # full 1531
  python3 run.py --limit 5            # just the first 5 rows
  python3 run.py --gap-generate       # Imagen-fill species with no usable free photo
  python3 run.py --no-grade           # skip the quality grade (still license-checks)

Per row:  Google image search -> LICENSE-CHECK (Gemini reads source page)
          -> DOWNLOAD -> GRADE quality (Gemini) -> PICK best -> cut bg + flip -> SAVE
Outputs:  out/<run>/images, out/<run>/originals, manifest.csv, contact_sheet.html
"""
import argparse, csv, html, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import config, sources, grade as grading, generate, process, license_check

BASE_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

MANIFEST_FIELDS = ["id", "kod", "nazov_lat", "nazov_sk", "skupina", "status",
                   "source", "license", "license_reason", "attribution", "score", "facing",
                   "n_candidates", "pick_rank", "image_file", "original_file", "page", "notes"]


def process_row(r, args, img_dir):
    latin, sk = r["nazov_lat"], r["nazov_sk"]
    rec = {k: r.get(k, "") for k in ("id", "kod", "nazov_lat", "nazov_sk", "skupina")}
    rec.update({"status": "no_photo", "source": "", "license": "", "license_reason": "",
                "attribution": "", "score": "", "facing": "", "n_candidates": 0,
                "pick_rank": "", "image_file": "", "original_file": "", "page": "", "notes": ""})

    cands = sources.find_candidates(latin, want=args.candidates)
    rec["n_candidates"] = len(cands)

    from src.http import get_bytes
    # 1) download + quality-grade EVERY candidate (for the use-case: sharp, centered,
    #    single subject, easily-separable background). Hard-rejects score 0.
    scored = []
    for c in cands:
        try:
            img = get_bytes(c["url"])
        except Exception:
            continue
        if not img or len(img) < 2000 or not process.is_image(img):
            continue   # skip downloads that aren't real raster images (e.g. HTML pages)
        v = {"score": 0.5, "facing": "other"} if args.no_grade else grading.grade(img, latin, sk)
        if not args.no_grade and not v.get("graded"):
            continue   # grading errored -> can't trust it, skip
        sc = grading.quality_score(v)
        if sc <= 0:
            continue   # hard-rejected (drawing / multi-fish / watermark / cropped / wrong species)
        scored.append((sc, v, c, img))

    # 2) rank by quality, keep the top K
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:args.top_k]

    # 3) license-check the top picks in rank order; the first FREE one wins.
    #    --no-license pauses the check: the top-ranked pick is taken as-is.
    chosen = None
    for rank, (sc, v, c, img) in enumerate(top, 1):
        if args.no_license:
            lic = {"free": True, "license": "UNVERIFIED",
                   "reason": "license check paused (--no-license)"}
        else:
            lic = license_check.check(c.get("page", ""), latin)
        if lic["free"]:
            chosen = (rank, sc, v, c, img, lic)
            break

    if chosen:
        rank, score, v, c, img, lic = chosen
        out, ext, notes = process.normalize(img, facing=v.get("facing", "other"))
        fn = f"{r['kod'] or r['id']}.{ext}"
        with open(os.path.join(img_dir, fn), "wb") as f:
            f.write(out)
        # also keep the untouched original of the winning photo
        orig_dir = os.path.join(os.path.dirname(img_dir), "originals")
        os.makedirs(orig_dir, exist_ok=True)
        ofn = f"{r['kod'] or r['id']}.{process._ext(img)}"
        with open(os.path.join(orig_dir, ofn), "wb") as f:
            f.write(img)
        rec.update({"status": "photo", "source": c["source"], "license": lic["license"],
                    "license_reason": lic["reason"], "attribution": (c["attribution"] or "")[:200],
                    "score": round(score, 3), "facing": v.get("facing", ""),
                    "n_candidates": len(cands), "pick_rank": rank,
                    "image_file": fn, "original_file": ofn, "page": c.get("page", ""),
                    "notes": ";".join(notes) + (f"; {v.get('note','')}" if v.get("note") else "")})
    elif args.gap_generate:
        gen = generate.generate(latin, sk)
        if gen:
            out, ext, notes = process.normalize(gen, facing="left", want_white_bg=False)
            fn = f"{r['kod'] or r['id']}.{ext}"
            with open(os.path.join(img_dir, fn), "wb") as f:
                f.write(out)
            rec.update({"status": "ai_generated", "source": "imagen", "license": "google-imagen",
                        "score": "", "image_file": fn,
                        "notes": "AI-GENERATED - representative, not verified species"})
    elif top:
        rec["notes"] = f"{len(top)} quality picks, none free in top {args.top_k}"
    return rec


def contact_sheet(rows, out_dir):
    cards = []
    for r in rows:
        img = (f"<img src='images/{html.escape(r['image_file'])}' loading='lazy'>"
               if r["image_file"] else "<div class='none'>no image</div>")
        badge = {"photo": "#2a7", "ai_generated": "#c83", "no_photo": "#999"}.get(r["status"], "#999")
        cards.append(f"""<div class=card>
  {img}
  <div class=meta>
    <b>{html.escape(r['nazov_lat'])}</b><br>
    <span class=sk>{html.escape(r['nazov_sk'])}</span><br>
    <span class=badge style='background:{badge}'>{r['status']}</span>
    <span class=src>{html.escape(str(r['source']))} · {html.escape(str(r['license']))[:24]}</span><br>
    <span class=sc>score {r['score']} · {r['n_candidates']} cand · rank {r.get('pick_rank','')} · {html.escape(str(r['facing']))}</span>
    <div class=why>{html.escape(str(r.get('license_reason','')))[:160]}</div>
  </div></div>""")
    doc = f"""<!doctype html><meta charset=utf-8><title>fish contact sheet</title>
<style>
body{{font:13px system-ui;margin:18px;background:#f4f4f5}}
h1{{font-size:18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;overflow:hidden}}
.card img{{width:100%;height:170px;object-fit:contain;background:#fff;display:block}}
.none{{height:170px;display:flex;align-items:center;justify-content:center;color:#aaa;background:#fafafa}}
.meta{{padding:8px}}
.sk{{color:#666}}
.badge{{color:#fff;padding:1px 6px;border-radius:4px;font-size:11px}}
.src{{color:#888;font-size:11px}}
.sc{{color:#999;font-size:11px}}
.why{{color:#555;font-size:11px;margin-top:4px;font-style:italic}}
</style>
<h1>Fish catalog — {len(rows)} rows · {sum(1 for r in rows if r['status']=='photo')} photos ·
{sum(1 for r in rows if r['status']=='ai_generated')} AI · {sum(1 for r in rows if r['status']=='no_photo')} missing</h1>
<div class=grid>{''.join(cards)}</div>"""
    with open(os.path.join(out_dir, "contact_sheet.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/ryby_test25.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-grade", action="store_true")
    ap.add_argument("--no-license", action="store_true",
                    help="pause the free-to-use check; take the top-ranked image as-is "
                         "(marked UNVERIFIED)")
    ap.add_argument("--gap-generate", action="store_true")
    ap.add_argument("--run-name", default="",
                    help="name for this run's output folder (default: timestamp)")
    ap.add_argument("--workers", type=int, default=25,
                    help="number of fish processed concurrently (default 25)")
    ap.add_argument("--candidates", type=int, default=25,
                    help="image candidates to fetch per fish from DuckDuckGo (default 25)")
    ap.add_argument("--top-k", type=int, default=5,
                    help="top quality picks to license-check, best first (default 5)")
    args = ap.parse_args()

    # each run gets its own folder: out/<run-name or timestamp>/
    name = args.run_name or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir = os.path.join(BASE_OUT, name)
    img_dir = os.path.join(run_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    if args.limit:
        rows = rows[:args.limit]

    from src.client import backend_name
    print(f"Source: DuckDuckGo image search"
          f" | license-check: " + ("PAUSED (--no-license)" if args.no_license else "on") +
          f" | grading: " +
          (f"{config.GEMINI_MODEL} ({backend_name()})" if config.ai_enabled() else "OFF (no AI backend)") +
          (f" | gap-gen={config.IMAGEN_MODEL}" if args.gap_generate and config.ai_enabled() else ""))
    print(f"Processing {len(rows)} rows from {args.csv}  ({args.workers} workers)")
    print(f"Output -> out/{name}/\n")

    if process.HAVE_REMBG:
        process.prewarm()   # initialise rembg session once, before threads start

    out_rows = [None] * len(rows)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_row, r, args, img_dir): i for i, r in enumerate(rows)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                r = rows[i]
                rec = {k: r.get(k, "") for k in ("id", "kod", "nazov_lat", "nazov_sk", "skupina")}
                rec.update({"status": "error", "source": "", "license": "", "attribution": "",
                            "score": "", "facing": "", "n_candidates": 0, "image_file": "",
                            "original_file": "", "page": "",
                            "notes": f"{type(e).__name__}: {str(e)[:80]}"})
            out_rows[i] = rec
            done += 1
            print(f"[{done:>4}/{len(rows)}] {rec['status']:12} {rec['nazov_lat'][:34]:34} "
                  f"{rec['source']:11} cand={rec['n_candidates']} score={rec['score']}")

    with open(os.path.join(run_dir, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader(); w.writerows(out_rows)
    contact_sheet(out_rows, run_dir)

    n_photo = sum(1 for r in out_rows if r["status"] == "photo")
    n_ai = sum(1 for r in out_rows if r["status"] == "ai_generated")
    n_no = sum(1 for r in out_rows if r["status"] == "no_photo")
    print(f"\nDONE  photo={n_photo}  ai={n_ai}  missing={n_no}  of {len(out_rows)}")
    print(f"  manifest: out/{name}/manifest.csv")
    print(f"  review:   out/{name}/contact_sheet.html")


if __name__ == "__main__":
    main()
