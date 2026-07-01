#!/usr/bin/env python3
"""Flask web front-end for the fish-image pipeline (batched + resumable).

Flow:
  1. pick a CSV from data/
  2. choose query column(s), results-per-query, and a BATCH SIZE
  3. collect one batch at a time: search the next `batch_size` rows, TICK the
     images you want -> their originals download immediately and queue as jobs
  4. press Process: a background worker (worker.py) background-removes the batch
     with birefnet; a progress table shows it happen live
  5. review/adjust the finished plates, then collect the next batch

Your place in each CSV (the cursor) and every job's status live in out/jobs.sqlite,
so you can close the app and pick any run back up exactly where you left off.

Heavy bg-removal runs in worker.py, NOT in the request. Download via stdlib
urllib; image processing via imaging.py (rembg birefnet + Pillow).

  .venv/bin/python app.py        # http://127.0.0.1:5001
"""
import csv, os, re, sys, time, json, subprocess, urllib.request, urllib.error
from flask import (Flask, request, render_template, abort,
                   send_from_directory, redirect, jsonify, Response)
from ddgs import DDGS

import db
import imaging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "out")

app = Flask(__name__)
db.init()


# ---- optional secret-link gate ----------------------------------------------
# Set APP_KEY in the environment to lock the app behind a secret link (used when
# it's exposed on a server). Unset -> no gate, so local use is unchanged.
#   * she opens  https://host/?key=SECRET  once -> we drop a cookie and redirect
#     to a clean URL, so she stays logged in with nothing to type or remember.
#   * anyone without the cookie or the key just gets a 404 (the app is invisible).
APP_KEY = os.environ.get("APP_KEY")
_COOKIE = "zoopipe_key"


@app.before_request
def _require_key():
    if not APP_KEY:
        return
    if request.cookies.get(_COOKIE) == APP_KEY:
        return                                   # already logged in
    if request.args.get("key") == APP_KEY:       # the secret link -> set cookie
        resp = redirect(request.path)
        resp.set_cookie(_COOKIE, APP_KEY, max_age=31536000,
                        httponly=True, samesite="Lax", secure=True)
        return resp
    return Response("Not found.", 404)


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


# ---- csv helpers ------------------------------------------------------------
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
    """Return (fieldnames, rows). utf-8-sig strips the BOM."""
    with open(csv_path(name), encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def build_query(row, cols):
    return " ".join((row.get(c) or "").strip() for c in cols).strip()


def slugify(s):
    """Folder-safe name, e.g. 'Anostomus anostomus' -> 'Anostomus_anostomus'."""
    s = re.sub(r"[^\w]+", "_", (s or "").strip(), flags=re.U).strip("_")
    return s[:60] or "fish"


# ---- search -----------------------------------------------------------------
def raw_image_search(query, max_results, retries=2):
    """DuckDuckGo image search, restricted to 'Large' so we get big photos.
    DDG is flaky under back-to-back queries, so retry with backoff and a
    roomier timeout than the 5s default."""
    last = None
    for attempt in range(retries + 1):
        try:
            with DDGS(timeout=20) as ddgs:
                return list(ddgs.images(query, size="Large", max_results=max_results))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


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


# ---- out/ path helpers ------------------------------------------------------
def resolve_out(rel):
    """Validate 'slug/n.jpg' stays inside out/, returning its absolute path."""
    rel = (rel or "").lstrip("/")
    path = os.path.realpath(os.path.join(OUT_DIR, rel))
    if not path.startswith(os.path.realpath(OUT_DIR) + os.sep) or not os.path.isfile(path):
        abort(404, f"image not found: {rel}")
    return path


# ---- worker spawn -----------------------------------------------------------
def spawn_worker():
    """Launch worker.py as a detached background process (no-op if one runs)."""
    if db.worker_running():
        return
    logf = open(os.path.join(OUT_DIR, "worker.log"), "a")
    subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "worker.py")],
        cwd=BASE_DIR, stdout=logf, stderr=logf, start_new_session=True,
    )


# ---- run / batch helpers ----------------------------------------------------
def run_progress(name):
    """Summary dict for a CSV's run (or None if not started)."""
    run = db.get_run(name)
    if not run:
        return None
    try:
        _, rows = read_csv(name)
        total_rows = len(rows)
    except Exception:
        total_rows = 0
    c = db.counts(name)
    return {
        "cursor": run["cursor"], "total_rows": total_rows,
        "batch_size": run["batch_size"], "batch_seq": run["batch_seq"],
        "done": c["done"], "ready": c["ready"], "processing": c["processing"],
        "error": c["error"], "images": c["total"],
        "complete": total_rows and run["cursor"] >= total_rows,
    }


# ---- pages ------------------------------------------------------------------
@app.get("/")
def index():
    csvs = list_csvs()
    items = [{"name": c, "progress": run_progress(c)} for c in csvs]
    return render_template("index.html", title="pick csv", crumb=[], items=items)


@app.post("/cancel")
def cancel_run():
    """Forget a run (clears its queue + cursor). Downloaded files in out/ stay."""
    name = request.form.get("csv", "")
    db.delete_run(name)
    return redirect("/")


@app.get("/configure")
def configure():
    name = request.args.get("csv", "")
    fields, rows = read_csv(name)
    run = db.get_run(name)
    sel = json.loads(run["cols"]) if run else []
    return render_template(
        "configure.html", title=name, crumb=[(name, None)],
        name=name, fields=fields, n_rows=len(rows), run=run, selected=sel,
        progress=run_progress(name),
    )


@app.post("/configure")
def configure_save():
    name = request.form.get("csv", "")
    fields, _ = read_csv(name)
    cols = [c for c in request.form.getlist("col") if c in fields]
    batch_size = max(1, min(500, request.form.get("batch_size", 25, type=int) or 25))
    results = max(1, min(50, request.form.get("results", 20, type=int) or 20))
    if not cols:
        return render_template("message.html", title="error", crumb=[],
                               heading="No columns selected",
                               message="Pick at least one column.")
    db.upsert_run(name, cols, batch_size, results)
    return redirect(f"/collect?csv={name}")


@app.get("/collect")
def collect():
    """Search the current batch -- the next `batch_size` rows from the cursor."""
    name = request.args.get("csv", "")
    run = db.get_run(name)
    if not run:
        return redirect(f"/configure?csv={name}")
    # can't start a new batch until the current one is confirmed done -> send
    # the user back to its progress table to finish reviewing/confirming.
    if run["batch_seq"] > run["reviewed_seq"]:
        return redirect(f"/progress?csv={name}&batch={run['batch_seq']}")
    fields, rows = read_csv(name)
    cols = [c for c in json.loads(run["cols"]) if c in fields]
    start = run["cursor"]
    end = min(len(rows), start + run["batch_size"])

    if start >= len(rows):
        return render_template("message.html", title="done", crumb=[(name, None)],
                               heading="Run complete",
                               message=f"All {len(rows)} rows of {name} collected.")

    blocks = []
    for idx in range(start, end):
        query = build_query(rows[idx], cols)
        if not query:
            blocks.append({"idx": idx, "empty": True})
            continue
        try:
            results = raw_image_search(query, run["results"])
        except Exception as e:
            blocks.append({"idx": idx, "query": query, "error": f"{type(e).__name__}: {e}"})
            continue
        blocks.append({"idx": idx, "query": query, "results": by_size(results)})

    crumb = [(name, f"/configure?csv={name}"), ("collect", None)]
    return render_template(
        "search.html", title="collect", crumb=crumb, name=name, selected=cols,
        blocks=blocks, start=start, end=end, total=len(rows),
        batch_no=run["batch_seq"] + 1,
    )


@app.get("/research")
def research():
    """Re-run the DDG search for a single row -> rendered fish block (for the
    per-fish 'retry search' button, so one timeout doesn't cost the batch)."""
    name = request.args.get("csv", "")
    idx = request.args.get("idx", type=int)
    run = db.get_run(name)
    if not run or idx is None:
        abort(404)
    fields, rows = read_csv(name)
    if idx < 0 or idx >= len(rows):
        abort(404)
    cols = [c for c in json.loads(run["cols"]) if c in fields]
    query = build_query(rows[idx], cols)
    if not query:
        block = {"idx": idx, "empty": True}
    else:
        try:
            block = {"idx": idx, "query": query,
                     "results": by_size(raw_image_search(query, run["results"]))}
        except Exception as e:
            block = {"idx": idx, "query": query, "error": f"{type(e).__name__}: {e}"}
    return render_template("_fishblock.html", b=block, selected=cols)


@app.post("/process")
def process_picks():
    name = request.form.get("csv", "")
    run = db.get_run(name)
    if not run:
        abort(400, "no run for this CSV")
    fields, rows = read_csv(name)
    cols = [c for c in json.loads(run["cols"]) if c in fields]
    picks = request.form.getlist("pick")

    if not picks:
        return render_template("message.html", title="nothing picked",
                               crumb=[(name, f"/collect?csv={name}")],
                               heading="Nothing picked",
                               message="Tick at least one image, then Process.")

    # advance past this batch first -> the batch_seq these jobs belong to
    span = min(run["batch_size"], max(0, len(rows) - run["cursor"]))
    batch = db.advance_run(name, span)

    # group picked URLs by absolute row index
    by_row = {}
    for token in picks:
        ridx, _, url = token.partition("|||")
        if url:
            by_row.setdefault(ridx, []).append(url)

    queued = 0
    for ridx, urls in by_row.items():
        try:
            row = rows[int(ridx)]
        except (ValueError, IndexError):
            continue
        query = build_query(row, cols)
        slug = slugify(query)
        orig_dir = os.path.join(OUT_DIR, slug, "originals")
        os.makedirs(orig_dir, exist_ok=True)
        for url in urls:
            try:
                raw = get_bytes(url)
            except Exception:
                continue                 # download failed -> just skip this pick
            if not imaging.is_image(raw):
                continue
            n = db.next_n(name, slug)
            ext = imaging._ext(raw)
            orig_rel = f"{slug}/originals/{n}.{ext}"
            with open(os.path.join(OUT_DIR, orig_rel), "wb") as f:
                f.write(raw)
            db.add_job(name, batch, int(ridx), slug, query, n, url, orig_rel)
            queued += 1

    if queued:
        spawn_worker()
    return redirect(f"/progress?csv={name}&batch={batch}")


@app.get("/progress")
def progress():
    name = request.args.get("csv", "")
    run = db.get_run(name)
    if not run:
        abort(404, "no run for this CSV")
    batch = request.args.get("batch", run["batch_seq"], type=int)
    jobs = db.jobs_for_batch(name, batch)
    c = db.counts(name, batch)
    confirmed = batch <= run["reviewed_seq"]
    crumb = [(name, f"/configure?csv={name}"), (f"batch {batch}", None)]
    return render_template(
        "progress.html", title="processing", crumb=crumb, name=name,
        batch=batch, jobs=jobs, counts=c, progress=run_progress(name),
        confirmed=confirmed,
    )


@app.get("/status")
def status():
    """JSON the progress page polls while the worker runs."""
    name = request.args.get("csv", "")
    run = db.get_run(name)
    if not run:
        abort(404)
    batch = request.args.get("batch", run["batch_seq"], type=int)
    jobs = db.jobs_for_batch(name, batch)
    c = db.counts(name, batch)
    return jsonify({
        "counts": c,
        "worker_running": db.worker_running(),
        "active": c["ready"] > 0 or c["processing"] > 0,
        "jobs": [{
            "id": j["id"], "slug": j["slug"], "n": j["n"],
            "status": j["status"], "secs": j["secs"], "notes": j["notes"],
            "orig": j["orig_path"], "plate": j["plate_path"],
        } for j in jobs],
    })


@app.post("/resume")
def resume_processing():
    """Relaunch the worker for jobs left ready/processing (e.g. after a crash)."""
    spawn_worker()
    return ("", 204)


@app.post("/confirm")
def confirm():
    """Mark a batch reviewed/done (after edits), then go collect the next one."""
    name = request.form.get("csv", "")
    batch = request.form.get("batch", 0, type=int)
    db.confirm_batch(name, batch)
    return redirect(f"/collect?csv={name}")


@app.post("/retry")
def retry():
    name = request.form.get("csv", "")
    batch = request.form.get("batch", 0, type=int)
    jid = request.form.get("id", 0, type=int)
    db.retry_job(jid)
    spawn_worker()
    return redirect(f"/progress?csv={name}&batch={batch}")


@app.get("/edit")
def edit():
    rel = request.args.get("img", "")
    resolve_out(rel)                 # 404s if outside out/ or missing
    saved = request.args.get("saved")
    return render_template("edit.html", title="adjust", crumb=[("adjust", None)],
                           rel=rel, saved=saved)


@app.post("/edit")
def edit_save():
    rel = request.form.get("img", "")
    dest = resolve_out(rel)
    angle = max(-180, min(180, request.form.get("rotate", 0, type=int) or 0))
    do_flip = bool(request.form.get("flip"))

    # pure geometry on the finished plate -- no background removal / rembg.
    # always trim+centre so the fish stays properly framed in the box.
    with open(dest, "rb") as f:
        raw = f.read()
    out, _, _ = imaging.retouch(raw, flip=do_flip, rotate=angle, trim=True)
    with open(dest, "wb") as f:
        f.write(out)
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    return redirect(f"/edit?img={rel}&saved={angle + 181}")


@app.get("/out/<path:filename>")
def serve_out(filename):
    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    # NB: avoid port 5000 -- macOS AirPlay Receiver squats on it (403 in browser).
    app.run(debug=True, port=5001)
