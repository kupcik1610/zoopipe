#!/usr/bin/env python3
"""Flask web front-end for the fish-image pipeline (batched + resumable).

Flow:
  1. pick a CSV from data/
  2. choose query column(s), results-per-query, and a BATCH SIZE
  3. collect one batch at a time: search the next `batch_size` rows, TICK the
     images you want -> their originals download immediately and queue as jobs
  4. press Process: a background worker (worker.py) background-removes the batch
     with birefnet; a progress table shows it happen live
  5. review/adjust the finished frames, then collect the next batch

Your place in each CSV (the cursor) and every job's status live in out/jobs.sqlite,
so you can close the app and pick any run back up exactly where you left off.

Heavy bg-removal runs in worker.py, NOT in the request. Download via stdlib
urllib; image processing via imaging.py (rembg birefnet + Pillow).

  .venv/bin/python app.py                 # http://127.0.0.1:5001 (prod-safe)
  FLASK_DEBUG=1 .venv/bin/python app.py   # local dev: reloader + debugger on

This is also the server entrypoint (systemd runs it); HOST/PORT/FLASK_DEBUG
come from the environment. See _serve() at the bottom.
"""
import csv, os, re, sys, time, json, subprocess, urllib.request, urllib.error
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

# folder timestamps are shown in local (Bratislava) time even though the VM runs UTC
_TZ = ZoneInfo("Europe/Bratislava")

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
#   * the user opens  https://host/?key=SECRET  once -> we drop a cookie and
#     redirect to a clean URL, so they stay logged in with nothing to remember.
#   * anyone without the cookie or the key just gets a 404 (the app is invisible).
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


_rowcount_cache = {}    # abs path -> ((mtime_ns, size), data-row count)

def count_rows(name):
    """Number of data rows in a CSV, memoized by file mtime+size. The home page
    calls this once per CSV on every render, so we avoid re-parsing each file
    just to get a length; the cache invalidates whenever the file changes."""
    path = csv_path(name)
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _rowcount_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    # csv.reader (not raw line count) so quoted fields with embedded newlines
    # count as one row; minus the header row.
    with open(path, encoding="utf-8-sig") as f:
        count = max(0, sum(1 for _ in csv.reader(f)) - 1)
    _rowcount_cache[path] = (key, count)
    return count


def dup_primaries(rows, primary):
    """Set of primary values (casefolded) that appear on more than one row --
    those are the rows a secondary term is appended to, to tell them apart."""
    counts = Counter((r.get(primary) or "").strip().casefold()
                     for r in rows if (r.get(primary) or "").strip())
    return {v for v, n in counts.items() if n > 1}


def build_query(row, cols, dups=None):
    """Query for a row. cols[0] is the primary term (always used); cols[1], if
    present, is the secondary term, appended ONLY when this row's primary value
    is duplicated across the dataset (dups) -- to disambiguate those rows."""
    primary = (row.get(cols[0]) or "").strip()
    if len(cols) > 1 and dups is not None and primary.casefold() in dups:
        secondary = (row.get(cols[1]) or "").strip()
        if secondary:
            return f"{primary} {secondary}".strip()
    return primary


def slugify(s):
    """Folder-safe name, e.g. 'Anostomus anostomus' -> 'Anostomus_anostomus'."""
    s = re.sub(r"[^\w]+", "_", (s or "").strip(), flags=re.U).strip("_")
    return s[:60] or "fish"


def csv_folder(name):
    """Top-level out/ folder for a CSV run -- its filename without the .csv
    extension, slugified (e.g. 'terarium.csv' -> 'terarium'). Every species
    folder for that CSV lives under it (out/terarium/<slug>/...), so each CSV
    gets one big folder and the Drive sync mirrors the catalogue's structure."""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    return slugify(stem)


def run_folder(name, run):
    """Top-level out/ folder for one run, suffixed with a readable run-start
    timestamp (e.g. 'hady_2026-07-06_01-30'). Derived from the run's created_at
    so it's stable across all of the run's batches; every species folder lives
    under it, so the dated folder is what shows up in Drive."""
    ts = datetime.fromtimestamp(run["created_at"], _TZ).strftime("%Y-%m-%d_%H-%M")
    return f"{csv_folder(name)}_{ts}"


def query_context(name, run):
    """Shared per-row query setup for a run -> (rows, cols, dups).

    cols is the run's configured columns filtered to those still in the CSV;
    dups is the duplicate-primary set (or None when there's no secondary term).
    Feed a single row through build_query(rows[idx], cols, dups) to get its term.
    """
    fields, rows = read_csv(name)
    cols = [c for c in json.loads(run["cols"]) if c in fields]
    dups = dup_primaries(rows, cols[0]) if len(cols) > 1 else None
    return rows, cols, dups


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
    """Launch worker.py as a background child (no-op if one already runs).

    NOT detached: it shares our process group, so a terminal Ctrl-C reaches the
    worker pool too and any in-flight job is killed -- reset_stale() flips it
    back to 'ready' on the next start, so nothing is lost."""
    if db.worker_running():
        return
    # Popen dups the fd for the child at spawn; close our copy so each Process
    # click doesn't leak an open handle in the web process.
    with open(os.path.join(OUT_DIR, "worker.log"), "a") as logf:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "worker.py")],
            cwd=BASE_DIR, stdout=logf, stderr=logf,
        )


# ---- run / batch helpers ----------------------------------------------------
def group_jobs(jobs):
    """Group a batch's jobs by species for the progress table, keeping the order
    they arrive in (row_index, from jobs_for_batch). Returns a list of
    {slug, query, jobs, done, total} -- one entry per species -- so the page can
    show a header + per-species progress instead of one flat list. `query` is the
    clean species name stored on each job (e.g. 'Boa constrictor')."""
    groups = []
    by_slug = {}
    for j in jobs:
        g = by_slug.get(j["slug"])
        if g is None:
            g = {"slug": j["slug"], "query": j["query"] or j["slug"],
                 "jobs": [], "done": 0, "total": 0}
            by_slug[j["slug"]] = g
            groups.append(g)
        g["jobs"].append(j)
        g["total"] += 1
        if j["status"] == "done":
            g["done"] += 1
    return groups


def run_progress(name):
    """Summary dict for a CSV's run (or None if not started)."""
    run = db.get_run(name)
    if not run:
        return None
    try:
        total_rows = count_rows(name)
    except Exception:
        total_rows = 0
    c = db.counts(name)
    dl_pending = db.pending_download_failures(name)
    return {
        "cursor": run["cursor"], "total_rows": total_rows,
        "batch_size": run["batch_size"], "batch_seq": run["batch_seq"],
        "done": c["done"], "ready": c["ready"], "processing": c["processing"],
        "error": c["error"], "images": c["total"],
        "dl_pending": dl_pending,
        # a run is only complete once every row is collected, the final batch has
        # been confirmed (else it'd skip the review/edit/confirm step), AND no
        # download is still owed a retry -- those get their own retry batch.
        "complete": bool(total_rows and run["cursor"] >= total_rows
                         and run["reviewed_seq"] >= run["batch_seq"]
                         and dl_pending == 0),
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
    primary = request.form.get("primary", "")
    secondary = request.form.get("secondary", "")
    batch_size = max(1, min(500, request.form.get("batch_size", 25, type=int) or 25))
    results = max(1, min(50, request.form.get("results", 20, type=int) or 20))
    if primary not in fields:
        return render_template("message.html", title="error", crumb=[],
                               heading="No primary term selected",
                               message="Pick a primary search column.")
    cols = [primary]
    if secondary in fields and secondary != primary:
        cols.append(secondary)
    db.upsert_run(name, cols, batch_size, results)
    return redirect(f"/collect?csv={name}")


@app.get("/collect")
def collect():
    """Show the current batch -- the next `batch_size` rows from the cursor.

    The searches are NOT run here. The page renders instantly with one skeleton
    block per row; the browser then streams each row's images in via /research
    (see collect.js). That way the user can start picking the first fish while
    the rest are still loading, instead of waiting for the whole batch to search."""
    name = request.args.get("csv", "")
    run = db.get_run(name)
    if not run:
        return redirect(f"/configure?csv={name}")
    # can't start a new batch until the current one is confirmed done -> send
    # the user back to its progress table to finish reviewing/confirming.
    if run["batch_seq"] > run["reviewed_seq"]:
        return redirect(f"/progress?csv={name}&batch={run['batch_seq']}")
    rows, cols, dups = query_context(name, run)
    start = run["cursor"]
    end = min(len(rows), start + run["batch_size"])

    # fish from earlier batches whose download failed and still have no usable
    # image -> resurface them (first) so the user can pick a different photo.
    repick_blocks = []
    for r in db.repick_rows(name):
        idx = r["row_index"]
        if start <= idx < end or not (0 <= idx < len(rows)):
            continue                       # in this batch already, or stale
        query = build_query(rows[idx], cols, dups)
        if query:
            repick_blocks.append({"idx": idx, "query": query,
                                  "pending": True, "repick": True})

    if start >= len(rows) and not repick_blocks:
        return render_template("message.html", title="done", crumb=[(name, None)],
                               heading="Run complete",
                               message=f"All {len(rows)} rows of {name} collected.")

    blocks = list(repick_blocks)
    for idx in range(start, end):
        query = build_query(rows[idx], cols, dups)
        if not query:
            blocks.append({"idx": idx, "empty": True})
        else:
            blocks.append({"idx": idx, "query": query, "pending": True})

    crumb = [(name, f"/configure?csv={name}"), ("collect", None)]
    return render_template(
        "search.html", title="collect", crumb=crumb, name=name, selected=cols,
        blocks=blocks, start=start, end=end, total=len(rows),
        batch_no=run["batch_seq"] + 1, repicks=len(repick_blocks),
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
    rows, cols, dups = query_context(name, run)
    if idx < 0 or idx >= len(rows):
        abort(404)
    query = build_query(rows[idx], cols, dups)
    if not query:
        block = {"idx": idx, "empty": True}
    else:
        try:
            # keep DDG's own order; just drop any result with no image URL
            # (it'd render an unpickable, broken card).
            results = [r for r in raw_image_search(query, run["results"])
                       if r.get("image")]
            block = {"idx": idx, "query": query, "results": results}
        except Exception as e:
            block = {"idx": idx, "query": query, "error": f"{type(e).__name__}: {e}"}
    # keep the "download failed -- pick another" marker across the reload
    if request.args.get("repick"):
        block["repick"] = True
    return render_template("_fishblock.html", b=block, selected=cols)


@app.post("/process")
def process_picks():
    name = request.form.get("csv", "")
    run = db.get_run(name)
    if not run:
        abort(400, "no run for this CSV")
    rows, cols, dups = query_context(name, run)
    picks = request.form.getlist("pick")

    if not picks:
        return render_template("message.html", title="nothing picked",
                               crumb=[(name, f"/collect?csv={name}")],
                               heading="Nothing picked",
                               message="Tick at least one image, then Process.")

    # advance past this batch first -> the batch_seq these jobs belong to
    span = min(run["batch_size"], max(0, len(rows) - run["cursor"]))
    batch = db.advance_run(name, span)

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
        query = build_query(row, cols, dups)
        # prefix with the CSV's own folder so all its species group under one
        # top-level dir (out/<csv>/<slug>/...); slug is stored on each job, so
        # the worker and next_n build every downstream path from this same value.
        slug = f"{run_folder(name, run)}/{slugify(query)}"
        orig_dir = os.path.join(OUT_DIR, slug, "originals")
        os.makedirs(orig_dir, exist_ok=True)
        for url in urls:
            n = db.next_n(name, slug)
            note = None
            try:
                raw = get_bytes(url)
            except Exception as e:
                note = f"download failed: {type(e).__name__}"
            else:
                if not imaging.is_image(raw):
                    note = "download: not an image"
            if note:
                # don't silently drop a failed pick -- record it as an error job
                # so it shows on the progress table and can be retried (the worker
                # re-downloads from source_url).
                db.add_job(name, batch, int(ridx), slug, query, n, url, "",
                           status="error", notes=note)
                continue
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
        batch=batch, groups=group_jobs(jobs), counts=c,
        progress=run_progress(name), confirmed=confirmed,
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
            "orig": j["orig_path"], "frame": j["plate_path"],
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

    # pure geometry on the finished frame -- no background removal / rembg.
    # always trim+centre so the fish stays properly framed in the box.
    with open(dest, "rb") as f:
        raw = f.read()
    out, _, _ = imaging.adjust_frame(raw, flip=do_flip, rotate=angle, trim=True)
    with open(dest, "wb") as f:
        f.write(out)
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    return redirect(f"/edit?img={rel}&saved={angle + 181}")


@app.get("/out/<path:filename>")
def serve_out(filename):
    return send_from_directory(OUT_DIR, filename)


def _serve():
    """Run the app for both local use and the server (systemd's ExecStart).

    Debug is OFF unless FLASK_DEBUG=1 -- in production its reloader double-spawns
    the process (two worker supervisors, double auto-pull) and its error page is
    a remote-code-execution hole, so it must never be on there. HOST/PORT come
    from the environment (the systemd unit sets them; locally they default to a
    localhost server on 127.0.0.1:5001)."""
    host = os.environ.get("HOST", "127.0.0.1")
    # avoid 5000 -- macOS AirPlay Receiver squats on it (403 in browser).
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    url = f"http://{host}:{port}"
    print(f"\n  zoopipe is running  ->  {url}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    _serve()
