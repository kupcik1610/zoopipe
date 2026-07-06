#!/usr/bin/env python3
"""Flask front-end for the catalogue-frame pipeline.

One screen: a gallery of every species in a CSV. Click a card to search
DuckDuckGo for that species, tick the photos you want, Process. Picking never
blocks -- Process just queues the photos and returns; a background thread
(worker.run) downloads + background-removes them and the cards fill in live.
Finished frames can be nudged (rotate/mirror) and uploaded to minizoo.

State lives in out/jobs.sqlite (one `photos` row per picked image), so you can
close the app and pick any CSV back up exactly where you left off.

  .venv/bin/python app.py                 # http://127.0.0.1:5001
  FLASK_DEBUG=1 .venv/bin/python app.py   # local dev: reloader + debugger
"""
import csv, json, os, re, threading, time

from flask import (Flask, request, render_template, abort,
                   send_from_directory, redirect, jsonify, Response)
from ddgs import DDGS

import db
import imaging
import upload
import worker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "out")

# CSV columns (same schema across every data/*.csv)
COL_ID, COL_NAME, COL_LATIN = "id", "nazov_sk", "nazov_lat"

app = Flask(__name__)
db.init()


# ---- optional secret-link gate ----------------------------------------------
APP_KEY = os.environ.get("APP_KEY")
_COOKIE = "zoopipe_key"


@app.before_request
def _require_key():
    if not APP_KEY:
        return
    if request.cookies.get(_COOKIE) == APP_KEY:
        return
    if request.args.get("key") == APP_KEY:
        resp = redirect(request.path)
        resp.set_cookie(_COOKIE, APP_KEY, max_age=31536000,
                        httponly=True, samesite="Lax", secure=True)
        return resp
    return Response("Not found.", 404)


# ---- helpers ----------------------------------------------------------------
def list_csvs():
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(f for f in os.listdir(DATA_DIR) if f.lower().endswith(".csv"))


def csv_path(name):
    name = os.path.basename(name or "")
    path = os.path.join(DATA_DIR, name)
    if not name.lower().endswith(".csv") or not os.path.isfile(path):
        abort(404, f"CSV not found: {name}")
    return path


def read_csv(name):
    with open(csv_path(name), encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def slugify(s):
    s = re.sub(r"[^\w]+", "_", (s or "").strip(), flags=re.U).strip("_")
    return s[:60] or "x"


def stem(name):
    return slugify(os.path.splitext(os.path.basename(name or ""))[0])


def folder_for(name, idpr, query):
    """out/ dir for one species: out/<csv>/<idpr>_<slug>/ ."""
    return f"{stem(name)}/{idpr}_{slugify(query)}"


def row_species(row):
    """(display name, search query) for a CSV row. Latin name searches best."""
    latin = (row.get(COL_LATIN) or "").strip()
    sk = (row.get(COL_NAME) or "").strip()
    return (sk or latin), (latin or sk)


def resolve_out(rel):
    rel = (rel or "").lstrip("/")
    path = os.path.realpath(os.path.join(OUT_DIR, rel))
    if not path.startswith(os.path.realpath(OUT_DIR) + os.sep) or not os.path.isfile(path):
        abort(404, f"image not found: {rel}")
    return path


def start_worker():
    """Run worker.run() in a background thread that lives for the app's lifetime."""
    threading.Thread(target=worker.run, name="worker", daemon=True).start()


def image_search(query, max_results=20, retries=2):
    """DDG image search, biased to large photos. DDG is flaky, so retry."""
    last = None
    for attempt in range(retries + 1):
        try:
            with DDGS(timeout=20) as ddgs:
                return list(ddgs.images(query, size="Large", max_results=max_results))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def photo_dict(p):
    return {"id": p["id"], "idpr": p["idpr"], "species": p["species"],
            "status": p["status"], "frame": p["frame_path"], "orig": p["orig_path"],
            "notes": p["notes"], "uploaded": bool(p["uploaded_at"])}


def build_cards(name):
    """One card per CSV row, with its photos attached and a rolled-up state."""
    rows = read_csv(name)
    by_idpr = {}
    for p in db.photos_for_csv(name):
        by_idpr.setdefault(p["idpr"], []).append(photo_dict(p))
    cards = []
    for row in rows:
        idpr = (row.get(COL_ID) or "").strip()
        if not idpr:
            continue
        display, query = row_species(row)
        photos = by_idpr.get(idpr, [])
        if any(x["uploaded"] for x in photos):
            state = "uploaded"
        elif any(x["status"] == "done" for x in photos):
            state = "done"
        elif any(x["status"] in ("ready", "processing") for x in photos):
            state = "working"
        elif any(x["status"] == "error" for x in photos):
            state = "error"
        else:
            state = "todo"
        cards.append({"idpr": idpr, "name": display, "latin": query,
                      "photos": photos, "state": state})
    return cards


def category_summary():
    """One row per CSV for the landing page: species count + how far along."""
    out = []
    for f in list_csvs():
        try:
            rows = read_csv(f)
        except Exception:
            continue
        idprs = {(r.get(COL_ID) or "").strip() for r in rows}
        idprs.discard("")
        done, up, work = set(), set(), set()
        for p in db.photos_for_csv(f):
            if p["uploaded_at"]:
                up.add(p["idpr"])
            elif p["status"] == "done":
                done.add(p["idpr"])
            elif p["status"] in ("ready", "processing"):
                work.add(p["idpr"])
        out.append({
            "file": f, "label": os.path.splitext(f)[0].replace("_", " ").title(),
            "rows": len(idprs), "uploaded": len(up),
            "done": len(done - up), "working": len(work),
        })
    return out


PER_PAGE = 25


# ---- pages ------------------------------------------------------------------
@app.get("/")
def index():
    name = request.args.get("csv", "")
    if not name:
        return render_template("pick.html", title="categories",
                               cats=category_summary())
    flt = request.args.get("filter", "all")
    cards = build_cards(name)

    tally = {"todo": 0, "working": 0, "done": 0, "uploaded": 0, "error": 0}
    for c in cards:
        tally[c["state"]] += 1
    counts = {"all": len(cards), "todo": tally["todo"], "done": tally["done"],
              "uploaded": tally["uploaded"], "working": tally["working"] + tally["error"]}

    def keep(state):
        if flt == "all":
            return True
        if flt == "working":
            return state in ("working", "error")
        return state == flt
    shown = [c for c in cards if keep(c["state"])]

    total = len(shown)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    page_cards = shown[(page - 1) * PER_PAGE: page * PER_PAGE]

    photos_json = json.dumps({c["idpr"]: c["photos"] for c in page_cards})
    return render_template(
        "index.html", title=name, name=name, cards=page_cards,
        counts=counts, filter=flt, page=page, pages=pages, total=total,
        photos_json=photos_json)


@app.get("/search")
def search():
    """DDG results for one species -> JSON (the card fetches this on expand)."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    n = max(1, min(200, request.args.get("max", 20, type=int) or 20))
    try:
        results = [{"image": r.get("image"), "thumb": r.get("thumbnail") or r.get("image"),
                    "title": (r.get("title") or "")[:90], "source": r.get("source") or "",
                    "url": r.get("url") or "", "w": r.get("width"), "h": r.get("height")}
                   for r in image_search(q, max_results=n) if r.get("image")]
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}", "results": []}), 502


@app.post("/process")
def process():
    """Queue the ticked images for a species and return instantly; they get
    downloaded and framed in the background. Body: csv, idpr, urls[]."""
    name = request.form.get("csv", "")
    idpr = (request.form.get("idpr") or "").strip()
    urls = [u for u in request.form.getlist("url") if u.strip()]
    if not (name and idpr and urls):
        return jsonify({"ok": False, "error": "missing csv/idpr/urls"}), 400
    # look the species up in the CSV so the queued rows carry a clean name
    display, query = idpr, idpr
    for row in read_csv(name):
        if (row.get(COL_ID) or "").strip() == idpr:
            display, query = row_species(row)
            break
    folder = folder_for(name, idpr, query)
    added = []
    for u in urls:
        pid = db.add_photo(name, idpr, display, query, folder, u)
        added.append(pid)
    return jsonify({"ok": True, "ids": added})


@app.get("/status")
def status():
    """Live photo statuses for the whole CSV (the page polls this)."""
    name = request.args.get("csv", "")
    photos = [photo_dict(p) for p in db.photos_for_csv(name)]
    active = any(p["status"] in ("ready", "processing") for p in photos)
    return jsonify({"photos": photos, "active": active})


@app.post("/retry")
def retry():
    db.retry_photo(request.form.get("id", 0, type=int))
    return ("", 204)


@app.post("/delete")
def delete():
    row = db.delete_photo(request.form.get("id", 0, type=int))
    if row:
        for rel in (row["frame_path"], row["orig_path"]):
            if rel:
                try:
                    os.remove(os.path.join(OUT_DIR, rel))
                except OSError:
                    pass
    return ("", 204)


@app.post("/edit")
def edit():
    """Rotate / mirror a finished frame in place (always re-centred)."""
    pid = request.form.get("id", 0, type=int)
    p = db.get_photo(pid)
    if not p or p["status"] != "done" or not p["frame_path"]:
        return jsonify({"ok": False, "error": "no finished frame"}), 400
    dest = resolve_out(p["frame_path"])
    angle = max(-180, min(180, request.form.get("rotate", 0, type=int) or 0))
    do_flip = bool(request.form.get("flip"))
    with open(dest, "rb") as f:
        raw = f.read()
    out, _, _ = imaging.adjust_frame(raw, flip=do_flip, rotate=angle, trim=True)
    with open(dest, "wb") as f:
        f.write(out)
    return jsonify({"ok": True})


@app.post("/upload")
def upload_image():
    """Push one finished frame to its minizoo product (idpr = CSV id)."""
    pid = request.form.get("id", 0, type=int)
    p = db.get_photo(pid)
    if not p or p["status"] != "done" or not p["frame_path"]:
        return jsonify({"ok": False, "error": "frame not finished"}), 400
    src = os.path.join(OUT_DIR, p["frame_path"])
    if not os.path.isfile(src):
        return jsonify({"ok": False, "error": "frame file missing"}), 400
    nazov = (p["species"] or "").replace("/", "_").replace("\\", "_")
    fname = f"{p['idpr']}_{nazov}.jpg"
    try:
        result = upload.upload_one(p["idpr"], src, filename=fname)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
    if result in ("ok", "dry"):
        db.mark_uploaded(pid)
    return jsonify({"ok": result in ("ok", "dry"), "result": result})


@app.get("/out/<path:filename>")
def serve_out(filename):
    return send_from_directory(OUT_DIR, filename)


def _serve():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # under the debug reloader, only the child that serves should start it
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_worker()
    print(f"\n  zoopipe -> http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    _serve()
