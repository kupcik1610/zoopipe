#!/usr/bin/env python3
"""Flask web front-end for the fish-image pipeline.

Flow:
  1. pick a CSV from data/
  2. choose which column(s) build the DuckDuckGo query + how many results
  3. see the raw search results and TICK the images you want (optionally flip)
  4. the picked images are downloaded, background-removed + whitened, and saved
     into out/<fish>/  -- one subfolder per fish.

Download via stdlib urllib; image normalize via rembg + Pillow (white bg + flip).
Search is synchronous; fine for hand-picked batches.

  .venv/bin/python app.py        # http://127.0.0.1:5001
"""
import csv, io, os, re, time, urllib.request, urllib.error
from flask import (Flask, request, render_template, abort,
                   send_from_directory, redirect)
from ddgs import DDGS

# Pillow + rembg are OPTIONAL: without them downloads are saved raw and
# normalization is skipped (recorded in the per-image notes).
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    from rembg import remove as _rembg_remove
    HAVE_REMBG = True
except Exception:
    HAVE_REMBG = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "out")
CANVAS = (600, 470)             # final output size; fish fitted, rest padded white

app = Flask(__name__)
_warmed = False


# ---- download (stdlib HTTP) -------------------------------------------------
UA = "ryby-fish-catalog/1.0 (contact: kupco.patrik.16@gmail.com)"

def get_bytes(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (400, 401, 403, 404):
                break          # not worth retrying
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"request failed for {url[:90]} -> {last}")


# ---- image processing: cut out -> fit on white canvas -> flip ---------------
def normalize(image_bytes, flip=False, rotate=0, trim=False):
    """Return (out_bytes, ext, notes[]).

    Pipeline: remove background -> rotate (degrees, clockwise) -> trim to the
    subject's bounding box -> fit onto a fixed CANVAS-sized white background ->
    flip so the head points LEFT. Rotating/trimming happen on the transparent
    cut-out so corners stay clean and the crop hugs the animal. The subject is
    scaled proportionally and centered, leftover space padded white -- it never
    distorts, an aspect-ratio mismatch just adds white.
    """
    notes = []
    if not HAVE_PIL:
        return image_bytes, _ext(image_bytes), ["pillow-missing: saved raw"]

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as e:
        # bytes Pillow can't decode (e.g. AVIF/SVG/corrupt) -> save raw, don't crash
        return image_bytes, _ext(image_bytes), [f"normalize-skipped:{type(e).__name__}"]

    # 1) background removal -> transparent RGBA cut-out
    has_alpha = False
    if HAVE_REMBG:
        try:
            cut = _rembg_remove(image_bytes)
            img = Image.open(io.BytesIO(cut)).convert("RGBA")
            has_alpha = True
            notes.append("bg-removed")
        except Exception as e:
            notes.append(f"rembg-failed:{type(e).__name__}")
    else:
        notes.append("rembg-missing: bg not whitened")

    # 2) rotate on the transparent canvas (CSS-style: positive = clockwise)
    if rotate:
        img = img.rotate(-rotate, resample=Image.BICUBIC, expand=True)
        notes.append(f"rotated:{rotate}")

    # 3) trim to the subject's bounding box (+ small proportional margin)
    if trim:
        bbox = img.getchannel("A").getbbox() if has_alpha else _nonwhite_bbox(img)
        if bbox:
            m = round(0.03 * max(img.width, img.height))
            l, t, r, b = bbox
            img = img.crop((max(0, l - m), max(0, t - m),
                            min(img.width, r + m), min(img.height, b + m)))
            notes.append("trimmed")

    # 4) fit the cut-out onto a fixed white canvas, scaled + centered
    w, h = CANVAS
    scale = min(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    bg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    bg.alpha_composite(img, ((w - nw) // 2, (h - nh) // 2))
    img = bg

    # 5) flip so the head points left
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
        notes.append("flipped-to-left")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "jpg", notes


def _nonwhite_bbox(img, thresh=12):
    """Bounding box of the non-white content (fallback when there's no alpha)."""
    from PIL import ImageChops
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > thresh else 0)
    return diff.getbbox()


def _ext(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def is_image(b):
    """True only for real raster image bytes (rejects HTML pages, SVG, etc.)."""
    if not b or len(b) < 64:
        return False
    return (b[:3] == b"\xff\xd8\xff"                      # JPEG
            or b[:8] == b"\x89PNG\r\n\x1a\n"              # PNG
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")  # WEBP
            or b[:6] in (b"GIF87a", b"GIF89a"))           # GIF


def prewarm():
    """Create the rembg model session once so the first /process request doesn't
    pay the init cost mid-download."""
    if not (HAVE_PIL and HAVE_REMBG):
        return
    try:
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (128, 128, 128)).save(buf, format="PNG")
        _rembg_remove(buf.getvalue())
    except Exception:
        pass


def ensure_warmed():
    """Run prewarm() once, before the first image is processed."""
    global _warmed
    if not _warmed:
        prewarm()
        _warmed = True


# ---- helpers ----------------------------------------------------------------
def list_csvs():
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(f for f in os.listdir(DATA_DIR) if f.lower().endswith(".csv"))


def csv_path(name):
    """Resolve `name` to a CSV inside data/, or 404. Blocks path traversal."""
    name = os.path.basename(name or "")
    path = os.path.join(DATA_DIR, name)
    if not name.lower().endswith(".csv") or not os.path.isfile(path):
        abort(404, f"CSV not found: {name}")
    return path


def read_csv(name):
    """Return (fieldnames, rows). utf-8-sig strips the BOM (matches run.py)."""
    with open(csv_path(name), encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def build_query(row, cols):
    return " ".join((row.get(c) or "").strip() for c in cols).strip()


def slugify(s):
    """Folder-safe name from a query, e.g. 'Anostomus anostomus' -> 'Anostomus_anostomus'."""
    s = re.sub(r"[^\w]+", "_", (s or "").strip(), flags=re.U).strip("_")
    return s[:60] or "fish"


def raw_image_search(query, max_results):
    """DuckDuckGo image search, restricted to its 'Large' size bucket so we only
    get nice big photos. Returns the raw result dicts, unmodified."""
    with DDGS() as ddgs:
        return list(ddgs.images(query, size="Large", max_results=max_results))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def by_size(results):
    """Sort the (already Large-only) results biggest-area first."""
    kept = [r for r in results if r.get("image")]
    kept.sort(key=lambda r: _int(r.get("width")) * _int(r.get("height")), reverse=True)
    return kept


def next_index(fish_dir):
    """Next free <n>.jpg in a fish folder, so re-processing appends."""
    if not os.path.isdir(fish_dir):
        return 1
    nums = [int(m.group(1)) for f in os.listdir(fish_dir)
            if (m := re.match(r"(\d+)\.jpg$", f))]
    return max(nums, default=0) + 1


def resolve_out(rel):
    """Validate 'slug/n.jpg' stays inside out/, returning its absolute path."""
    rel = (rel or "").lstrip("/")
    path = os.path.realpath(os.path.join(OUT_DIR, rel))
    if not path.startswith(os.path.realpath(OUT_DIR) + os.sep) or not os.path.isfile(path):
        abort(404, f"image not found: {rel}")
    return path


def original_for(rel):
    """Find the untouched original (out/<slug>/originals/<n>.*) behind a processed image."""
    slug, fn = os.path.split(rel)
    stem = os.path.splitext(fn)[0]
    odir = os.path.join(OUT_DIR, slug, "originals")
    if os.path.isdir(odir):
        for f in os.listdir(odir):
            if os.path.splitext(f)[0] == stem:
                return os.path.join(odir, f)
    return None


@app.get("/")
def index():
    return render_template("index.html", title="pick csv", crumb=[],
                           csvs=list_csvs())


@app.get("/configure")
def configure():
    name = request.args.get("csv", "")
    fields, rows = read_csv(name)
    crumb = [(name, None)]
    return render_template("configure.html", title=name, crumb=crumb,
                           name=name, fields=fields, n_rows=len(rows))


@app.post("/search")
def search():
    name = request.form.get("csv", "")
    fields, rows = read_csv(name)
    selected = [c for c in request.form.getlist("col") if c in fields]
    n_results = max(1, min(50, request.form.get("results", 10, type=int) or 10))
    n_rows = max(1, min(len(rows), request.form.get("rows", 5, type=int) or 5))

    if not selected:
        return render_template("message.html", title="error", crumb=[],
                               heading="No columns selected",
                               message="Pick at least one column.")

    blocks = []
    for idx, r in enumerate(rows[:n_rows]):
        query = build_query(r, selected)
        if not query:
            blocks.append({"idx": idx, "empty": True})
            continue
        try:
            results = raw_image_search(query, n_results)
        except Exception as e:
            blocks.append({"query": query, "error": f"{type(e).__name__}: {e}"})
            continue
        blocks.append({"idx": idx, "query": query, "results": by_size(results)})

    crumb = [(name, f"/configure?csv={name}"), ("results", None)]
    return render_template("search.html", title="results", crumb=crumb,
                           name=name, selected=selected, blocks=blocks)


@app.post("/process")
def process_picks():
    ensure_warmed()        # init rembg once

    name = request.form.get("csv", "")
    fields, rows = read_csv(name)
    cols = [c for c in request.form.getlist("col") if c in fields]
    picks = request.form.getlist("pick")

    if not picks:
        return render_template("message.html", title="nothing picked", crumb=[],
                               heading="Nothing picked",
                               message="Tick at least one image first.")

    # group picked image URLs by row index
    by_row = {}
    for token in picks:
        ridx, _, url = token.partition("|||")
        if url:
            by_row.setdefault(ridx, []).append(url)

    blocks = []
    for ridx, items in by_row.items():
        try:
            row = rows[int(ridx)]
        except (ValueError, IndexError):
            continue
        query = build_query(row, cols)
        slug = slugify(query)
        fish_dir = os.path.join(OUT_DIR, slug)
        orig_dir = os.path.join(fish_dir, "originals")
        os.makedirs(orig_dir, exist_ok=True)
        n = next_index(fish_dir)

        produced = []
        for url in items:
            try:
                raw = get_bytes(url)
            except Exception as e:
                produced.append((None, f"download failed: {type(e).__name__}"))
                continue
            if not is_image(raw):
                produced.append((None, "not a raster image, skipped"))
                continue
            with open(os.path.join(orig_dir, f"{n}.{_ext(raw)}"), "wb") as f:
                f.write(raw)
            out, ext, notes = normalize(raw, trim=True)
            fn = f"{n}.{ext}"
            with open(os.path.join(fish_dir, fn), "wb") as f:
                f.write(out)
            produced.append((f"{slug}/{fn}", ", ".join(notes) or "saved"))
            n += 1

        blocks.append({"query": query, "produced": produced})

    return render_template("process.html", title="processed",
                           crumb=[("processed", None)], blocks=blocks)


@app.get("/edit")
def edit():
    rel = request.args.get("img", "")
    resolve_out(rel)                 # 404s if outside out/ or missing
    saved = request.args.get("saved")
    has_orig = original_for(rel) is not None
    return render_template("edit.html", title="adjust", crumb=[("adjust", None)],
                           rel=rel, saved=saved, has_orig=has_orig)


@app.post("/edit")
def edit_save():
    ensure_warmed()

    rel = request.form.get("img", "")
    dest = resolve_out(rel)
    angle = max(-180, min(180, request.form.get("rotate", 0, type=int) or 0))
    do_flip = bool(request.form.get("flip"))
    do_trim = bool(request.form.get("trim"))

    src = original_for(rel) or dest     # prefer the untouched original
    with open(src, "rb") as f:
        raw = f.read()
    out, _, _ = normalize(raw, flip=do_flip, rotate=angle, trim=do_trim)
    with open(dest, "wb") as f:
        f.write(out)
    # JS save (fetch) -> 204, the page calls history.back() itself.
    # No-JS fallback -> reload the editor with a "Saved." note.
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    return redirect(f"/edit?img={rel}&saved={angle + 181}")


@app.get("/out/<path:filename>")
def serve_out(filename):
    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    # NB: avoid port 5000 -- macOS AirPlay Receiver (ControlCenter) squats on it
    # and returns 403 in the browser.
    app.run(debug=True, port=5001)
