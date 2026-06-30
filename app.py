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
from flask import (Flask, request, render_template_string, abort,
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


# ---- templates --------------------------------------------------------------
BASE = """<!doctype html><meta charset=utf-8><title>zoopipe — {{ title }}</title>
<style>
 body{font:14px system-ui;margin:24px;background:#f4f4f5;color:#222;max-width:1100px}
 h1{font-size:20px} h2{font-size:15px;color:#555;margin:18px 0 6px}
 a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
 .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0}
 label{display:inline-block;margin:3px 14px 3px 0}
 input[type=number]{width:80px;padding:4px}
 button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:9px 18px;
   font-size:14px;cursor:pointer;margin-top:10px}
 .bar{position:sticky;top:0;background:#f4f4f5;padding:10px 0;z-index:5}
 .cols{columns:3;margin:6px 0}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
 .thumb{background:#fff;border:2px solid #e2e2e2;border-radius:6px;overflow:hidden;
   position:relative}
 .thumb:has(input.pick:checked){border-color:#2563eb;box-shadow:0 0 0 2px #2563eb33}
 .thumb img{width:100%;height:140px;object-fit:contain;background:#fafafa;display:block}
 .thumb .m{padding:6px;font-size:11px;color:#666;word-break:break-all}
 .thumb .t{color:#222;font-weight:600}
 .thumb .pickrow{padding:6px;background:#f8f8f8;border-top:1px solid #eee;font-size:12px}
 .crumb{color:#888;font-size:13px;margin-bottom:12px}
 .err{color:#b91c1c} .ok{color:#15803d} .muted{color:#888}
</style>
<div class=crumb><a href="/">data</a>{{ crumb|safe }}</div>
{{ body|safe }}
"""

# Refresh edited thumbnails when returning to the processed list: the editor
# records `{rel: timestamp}` in sessionStorage; this busts the cache for those.
LIST_JS = """<script>
addEventListener('pageshow',function(){
  var e; try{e=JSON.parse(sessionStorage.getItem('edited')||'{}')}catch(_){e={}}
  document.querySelectorAll('img[data-rel]').forEach(function(i){
    var r=i.getAttribute('data-rel'); if(e[r]) i.src='/out/'+r+'?v='+e[r];
  });
});
</script>"""

# Editor save: POST via fetch, note the edited image for the list, then go back.
EDIT_JS = """<script>
function doSave(e){
  e.preventDefault();
  var f=e.target, b=f.querySelector('button[type=submit]');
  b.disabled=true; b.textContent='Saving…';
  fetch(f.action,{method:'POST',headers:{'X-Requested-With':'fetch'},body:new FormData(f)})
    .then(function(r){ if(!r.ok) throw 0;
      var e2; try{e2=JSON.parse(sessionStorage.getItem('edited')||'{}')}catch(_){e2={}}
      e2[f.img.value]=Date.now(); sessionStorage.setItem('edited',JSON.stringify(e2));
      history.back();
    })
    .catch(function(){ b.disabled=false; b.textContent='Save'; alert('Save failed'); });
  return false;
}
</script>"""


@app.get("/")
def index():
    csvs = list_csvs()
    items = "".join(
        f"<li><a href='/configure?csv={c}'>{c}</a></li>" for c in csvs
    ) or "<li class=muted>no .csv files in data/</li>"
    body = f"<h1>Pick a CSV</h1><div class=card><ul>{items}</ul></div>"
    return render_template_string(BASE, title="pick csv", crumb="", body=body)


@app.get("/configure")
def configure():
    name = request.args.get("csv", "")
    fields, rows = read_csv(name)
    checks = "".join(
        f"<label><input type=checkbox name=col value='{c}'> {c}</label>"
        for c in fields
    )
    body = f"""<h1>{name}</h1>
    <p class=muted>{len(rows)} rows · {len(fields)} columns</p>
    <form method=post action="/search" class=card>
      <input type=hidden name=csv value="{name}">
      <h2>Search columns</h2>
      <div class=cols>{checks}</div>
      <label>Results per query <input type=number name=results value=10 min=1 max=50></label>
      <label>Rows to process <input type=number name=rows value=5 min=1 max={len(rows) or 1}></label>
      <button type=submit>Search</button>
    </form>"""
    crumb = f" / <a href='/configure?csv={name}'>{name}</a>"
    return render_template_string(BASE, title=name, crumb=crumb, body=body)


@app.post("/search")
def search():
    name = request.form.get("csv", "")
    fields, rows = read_csv(name)
    selected = [c for c in request.form.getlist("col") if c in fields]
    n_results = max(1, min(50, request.form.get("results", 10, type=int) or 10))
    n_rows = max(1, min(len(rows), request.form.get("rows", 5, type=int) or 5))

    if not selected:
        body = "<h1>No columns selected</h1><p class=err>Pick at least one column.</p>"
        return render_template_string(BASE, title="error", crumb="", body=body)

    # carry the search config through to /process as hidden fields
    hidden = (f"<input type=hidden name=csv value='{name}'>"
              + "".join(f"<input type=hidden name=col value='{c}'>" for c in selected))

    blocks = []
    for idx, r in enumerate(rows[:n_rows]):
        query = build_query(r, selected)
        if not query:
            blocks.append(f"<h2>(empty query)</h2><p class=muted>row {idx} has no values "
                          f"in {', '.join(selected)}</p>")
            continue
        try:
            results = raw_image_search(query, n_results)
        except Exception as e:
            blocks.append(f"<h2>{query}</h2><p class=err>search failed: "
                          f"{type(e).__name__}: {e}</p>")
            continue
        kept = by_size(results)
        cards = "".join(
            f"""<div class=thumb>
              <a href="{res.get('image','')}" target=_blank>
                <img src="{res.get('thumbnail') or res.get('image','')}" loading=lazy></a>
              <div class=m>
                <div class=t>{(res.get('title') or '')[:80]}</div>
                {res.get('width','?')}×{res.get('height','?')} · {res.get('source','')}<br>
                <a href="{res.get('url','')}" target=_blank>source page</a>
              </div>
              <div class=pickrow>
                <label><input class=pick type=checkbox name=pick
                   value="{idx}|||{res.get('image','')}"> pick</label>
              </div></div>""" for res in kept
        ) or f"<p class=muted>no images</p>"
        blocks.append(f"<h2>{query} <span class=muted>· {len(kept)}</span></h2>"
                      f"<div class=grid>{cards}</div>")

    body = (f"""<form method=post action="/process">{hidden}
      <div class=bar><button type=submit>Process picked images</button></div>
      <h1>Results</h1>
      {''.join(blocks)}
      <div class=bar><button type=submit>Process picked images</button></div>
    </form>""")
    crumb = f" / <a href='/configure?csv={name}'>{name}</a> / results"
    return render_template_string(BASE, title="results", crumb=crumb, body=body)


@app.post("/process")
def process_picks():
    ensure_warmed()        # init rembg once

    name = request.form.get("csv", "")
    fields, rows = read_csv(name)
    cols = [c for c in request.form.getlist("col") if c in fields]
    picks = request.form.getlist("pick")

    if not picks:
        body = "<h1>Nothing picked</h1><p class=err>Tick at least one image first.</p>"
        return render_template_string(BASE, title="nothing picked", crumb="", body=body)

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

        cards = "".join(
            (f"<div class=thumb><img src='/out/{rel}' data-rel='{rel}'>"
             f"<div class=pickrow><a href='/edit?img={rel}'>edit</a></div></div>"
             if rel else
             f"<div class=thumb><div class=m><span class=err>{note}</span></div></div>")
            for rel, note in produced
        )
        blocks.append(f"<h2>{query}</h2><div class=grid>{cards}</div>")

    body = f"<h1>Processed</h1>{''.join(blocks)}" + LIST_JS
    return render_template_string(BASE, title="processed", crumb=" / processed", body=body)


@app.get("/edit")
def edit():
    rel = request.args.get("img", "")
    resolve_out(rel)                 # 404s if outside out/ or missing
    saved = request.args.get("saved")
    has_orig = original_for(rel) is not None
    warn = ("" if has_orig else
            "<p class=err>No saved original — background removal won't re-run cleanly.</p>")
    msg = "<p class=ok>Saved.</p>" if saved else ""
    body = f"""<h1>Adjust — {rel}</h1>{msg}{warn}
    <div class=card>
      <img id=pv src="/out/{rel}?v={saved or 0}"
           style="max-width:520px;max-height:460px;background:#fafafa;transition:transform .05s">
      <form id=ef method=post action="/edit" onsubmit="return doSave(event)">
        <input type=hidden name=img value="{rel}">
        <p><label>Rotate <output id=deg>0</output>°<br>
          <input type=range name=rotate id=rot min=-180 max=180 value=0 step=1
                 style="width:420px"
                 oninput="deg.textContent=this.value;pv.style.transform='rotate('+this.value+'deg)'">
        </label></p>
        <p><label><input type=checkbox name=flip> flip (head faces left)</label>
           <label><input type=checkbox name=trim checked> trim to subject</label></p>
        <button type=submit>Save</button>
        <button type=button onclick="history.back()"
                style="background:#e2e2e2;color:#222;margin-left:8px">Cancel</button>
      </form>
    </div>""" + EDIT_JS
    return render_template_string(BASE, title="adjust", crumb=" / adjust", body=body)


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
