#!/usr/bin/env python3
"""bg_lab.py -- a throwaway side app for comparing background-removal recipes.

Standalone from the main Zoopipe pipeline (app.py). It does NOT touch out/ --
it reads the originals there read-only and writes cut-outs to out_lab/.

Run:    .venv/bin/python bg_lab.py     then open http://127.0.0.1:5002

What it does
------------
- Finds every source image under  out/<Species>/originals/*
- Runs each through a set of VARIANTS (model + options combos), saving a
  TRANSPARENT png per variant to  out_lab/<variant>/<species>__<stem>.png
  (transparency, shown over a checkerboard, is the honest way to see haloing
  and chopped fins -- a white flatten hides edge problems).
- A gallery shows original vs every generated variant side by side, with the
  per-variant wall-clock time so you can weigh quality against speed.

Heads up: the first time a model runs, rembg downloads it to ~/.u2net/.
u2net is already cached; isnet is small; birefnet-general is large (~1 GB) and
several times slower on CPU. Generate variants one at a time from the UI.
"""
import io, os, json, time, glob

from flask import (Flask, request, render_template_string, redirect,
                   url_for, send_file, abort)
from PIL import Image
from rembg import remove as rembg_remove, new_session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")        # read-only source of originals
LAB_DIR = os.path.join(BASE_DIR, "out_lab")    # everything we generate

# ---- the recipes under test -------------------------------------------------
# Each variant = a rembg model plus options. Tweak freely; add/remove rows.
# `alpha_matting` refines soft edges (fins, antialiasing); `post_process`
# cleans speckles/holes in the mask. `needs_dl` just flags a first-run download.
AM = dict(                                     # shared alpha-matting knobs
    alpha_matting=True,
    alpha_matting_foreground_threshold=270,
    alpha_matting_background_threshold=20,
    alpha_matting_erode_size=11,
)
VARIANTS = {
    "u2net":        {"model": "u2net", "label": "u2net (current default)"},
    "isnet":        {"model": "isnet-general-use", "label": "isnet-general", "needs_dl": True},
    "isnet_am":     {"model": "isnet-general-use", "label": "isnet + alpha-matting",
                     "post_process": True, "alpha": True, "needs_dl": True},
    "birefnet":     {"model": "birefnet-general", "label": "birefnet-general", "needs_dl": True},
    "birefnet_am":  {"model": "birefnet-general", "label": "birefnet + alpha-matting",
                     "post_process": True, "alpha": True, "needs_dl": True},
}

app = Flask(__name__)
_sessions = {}                                 # model name -> rembg session (reused)


def session_for(model):
    if model not in _sessions:
        _sessions[model] = new_session(model)
    return _sessions[model]


def cut_out(image_bytes, cfg):
    """Run one recipe -> transparent RGBA cut-out (PIL Image)."""
    kw = {"session": session_for(cfg["model"])}
    if cfg.get("alpha"):
        kw.update(AM)
    if cfg.get("post_process"):
        kw["post_process_mask"] = True
    out = rembg_remove(image_bytes, **kw)
    return Image.open(io.BytesIO(out)).convert("RGBA")


# ---- source discovery -------------------------------------------------------
def originals():
    """List of dicts for every out/<Species>/originals/* image."""
    items = []
    for path in sorted(glob.glob(os.path.join(OUT_DIR, "*", "originals", "*"))):
        species = os.path.basename(os.path.dirname(os.path.dirname(path)))
        fname = os.path.basename(path)
        stem = os.path.splitext(fname)[0]
        if fname.startswith("."):
            continue
        items.append({
            "species": species,
            "fname": fname,
            "key": f"{species}__{stem}.png",     # flat name inside a variant dir
            "path": path,
        })
    return items


def lab_path(variant, key):
    return os.path.join(LAB_DIR, variant, key)


def stats_path(variant):
    return os.path.join(LAB_DIR, variant, "_stats.json")


def load_stats(variant):
    try:
        with open(stats_path(variant)) as f:
            return json.load(f)
    except Exception:
        return {}


# ---- generation -------------------------------------------------------------
def generate(variant):
    """Process every original through `variant`, write pngs + a _stats.json."""
    cfg = VARIANTS[variant]
    out_dir = os.path.join(LAB_DIR, variant)
    os.makedirs(out_dir, exist_ok=True)
    per, ok, failed = {}, 0, 0
    t_all = time.time()
    for it in originals():
        try:
            with open(it["path"], "rb") as f:
                raw = f.read()
            t0 = time.time()
            img = cut_out(raw, cfg)
            dt = time.time() - t0
            img.save(lab_path(variant, it["key"]), format="PNG")
            per[it["key"]] = round(dt, 2)
            ok += 1
        except Exception as e:
            per[it["key"]] = f"ERR:{type(e).__name__}"
            failed += 1
    stats = {
        "total_s": round(time.time() - t_all, 1),
        "count": ok, "failed": failed,
        "avg_s": round((time.time() - t_all) / max(ok, 1), 2),
        "per": per,
    }
    with open(stats_path(variant), "w") as f:
        json.dump(stats, f, indent=2)
    return stats


# ---- routes -----------------------------------------------------------------
@app.route("/")
def index():
    imgs = originals()
    cols = []
    for vid, cfg in VARIANTS.items():
        st = load_stats(vid)
        cols.append({"id": vid, "label": cfg["label"],
                     "needs_dl": cfg.get("needs_dl", False), "stats": st})
    return render_template_string(PAGE, imgs=imgs, cols=cols,
                                  variants=VARIANTS, msg=request.args.get("msg"))


@app.route("/run/<variant>", methods=["POST"])
def run(variant):
    if variant not in VARIANTS:
        abort(404)
    st = generate(variant)
    msg = (f"{variant}: {st['count']} done in {st['total_s']}s "
           f"(avg {st['avg_s']}s/img" + (f", {st['failed']} failed" if st['failed'] else "") + ")")
    return redirect(url_for("index", msg=msg) + f"#{variant}")


@app.route("/orig/<species>/<fname>")
def orig(species, fname):
    p = os.path.join(OUT_DIR, species, "originals", fname)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p)


@app.route("/lab/<variant>/<key>")
def lab(variant, key):
    p = lab_path(variant, key)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="image/png")


# ---- view (single self-contained template) ----------------------------------
PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>bg-lab</title>
<style>
  :root{--cell:200px}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.4 system-ui,sans-serif;background:#14161a;color:#e6e8ec}
  header{position:sticky;top:0;z-index:5;background:#1b1e24;padding:12px 16px;
         border-bottom:1px solid #2a2e36;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:600}
  .msg{color:#8fe3a0}
  .toggles{margin-left:auto;display:flex;gap:8px}
  button,.btn{font:inherit;background:#2a2e36;color:#e6e8ec;border:1px solid #3a3f4a;
         border-radius:6px;padding:5px 10px;cursor:pointer}
  button:hover{background:#343945}
  table{border-collapse:collapse;width:max-content}
  th,td{border:1px solid #2a2e36;padding:6px;text-align:center;vertical-align:top}
  th{position:sticky;top:53px;background:#1b1e24;z-index:4}
  th.rowhead,td.rowhead{position:sticky;left:0;background:#1b1e24;z-index:3;
         text-align:left;max-width:150px;font-size:12px}
  th.rowhead{z-index:6}
  .cap{font-size:11px;color:#9aa0aa;margin-top:4px}
  .dl{color:#e0b15a;font-size:11px}
  /* checkerboard so haloing / chopped edges are visible */
  .ph{width:var(--cell);height:calc(var(--cell)*0.78);display:flex;
      align-items:center;justify-content:center;border-radius:4px;
      background-image:linear-gradient(45deg,#666 25%,transparent 25%),
        linear-gradient(-45deg,#666 25%,transparent 25%),
        linear-gradient(45deg,transparent 75%,#666 75%),
        linear-gradient(-45deg,transparent 75%,#666 75%);
      background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0;
      background-color:#999}
  body.whitebg .ph{background:#fff!important}
  body.smallcell{--cell:130px}
  .ph img{max-width:100%;max-height:100%;object-fit:contain}
  .none{color:#6a707a;font-size:12px}
  .stat{font-size:11px;color:#9aa0aa;font-weight:400}
  a{color:#6db3ff}
</style></head><body>
<header>
  <h1>bg-lab</h1>
  <span>{{ imgs|length }} source images</span>
  {% if msg %}<span class=msg>✓ {{ msg }}</span>{% endif %}
  <div class=toggles>
    <button onclick="document.body.classList.toggle('whitebg')">white / checker bg</button>
    <button onclick="document.body.classList.toggle('smallcell')">smaller</button>
  </div>
</header>
<table>
<thead><tr>
  <th class=rowhead>image</th>
  {% for c in cols %}
  <th id="{{ c.id }}">
    {{ c.label }}{% if c.needs_dl %} <span class=dl title="downloads model on first run">⤓</span>{% endif %}
    <div>
      <form method=post action="/run/{{ c.id }}" style="display:inline">
        <button>generate ▸</button>
      </form>
    </div>
    {% if c.stats %}<div class=stat>{{ c.stats.count }} imgs · {{ c.stats.total_s }}s ·
      avg {{ c.stats.avg_s }}s{% if c.stats.failed %} · {{ c.stats.failed }} err{% endif %}</div>{% endif %}
  </th>
  {% endfor %}
</tr></thead>
<tbody>
{% for im in imgs %}
<tr>
  <td class=rowhead>{{ im.species }}<br><span class=cap>{{ im.fname }}</span></td>
  <td>
    <div class=ph><img loading=lazy src="/orig/{{ im.species }}/{{ im.fname }}"></div>
    <div class=cap>original</div>
  </td>
  {% for c in cols %}
  <td>
    {% set per = c.stats.per if c.stats else {} %}
    {% if per.get(im.key) is number %}
      <div class=ph><img loading=lazy src="/lab/{{ c.id }}/{{ im.key }}"></div>
      <div class=cap>{{ per.get(im.key) }}s</div>
    {% elif per.get(im.key) %}
      <div class=ph><span class=none>{{ per.get(im.key) }}</span></div>
    {% else %}
      <div class=ph><span class=none>— not generated —</span></div>
    {% endif %}
  </td>
  {% endfor %}
</tr>
{% endfor %}
</tbody>
</table>
</body></html>"""


if __name__ == "__main__":
    os.makedirs(LAB_DIR, exist_ok=True)
    # 5001 is the main app; 5000 is macOS AirPlay. Use 5002.
    app.run(debug=True, port=5002)
