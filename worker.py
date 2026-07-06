#!/usr/bin/env python3
"""Turn picked images into finished catalogue frames.

`run()` takes `ready` photos from the ledger one at a time: download the
original, background-remove it (imaging.make_frame), write the white frame, mark
it done. app.py runs this in a daemon thread that lives for the app's lifetime.
"""
import os, time, urllib.request, urllib.error

import db
import imaging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
UA = "ryby-fish-catalog/1.0 (contact: kupco.patrik.16@gmail.com)"


def log(msg):
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_out(rel, data):
    """Write `data` to OUT_DIR/rel, creating parent dirs as needed."""
    abs_path = os.path.join(OUT_DIR, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(data)


def _download(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:
            last = f"{type(e).__name__}"
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(last or "download failed")


def process_one(p):
    """Download + frame one photo. Returns (status, frame_rel, secs, notes).

    Never raises: every failure path (download, disk I/O, background removal) is
    turned into an ('error', ...) result so the caller can always finish the job.
    """
    try:
        raw = _download(p["source_url"])
        if not imaging.is_image(raw):
            return "error", "", None, "download: not an image"
    except Exception as e:
        return "error", "", None, f"download failed: {e}"

    try:
        orig_rel = f"{p['folder']}/originals/{p['id']}.{imaging._ext(raw)}"
        _write_out(orig_rel, raw)
        db.set_orig_path(p["id"], orig_rel)

        t0 = time.time()
        out, ext, notes = imaging.make_frame(raw)
        secs = round(time.time() - t0, 2)

        frame_rel = f"{p['folder']}/{p['id']}.{ext}"
        _write_out(frame_rel, out)
    except Exception as e:
        return "error", "", None, f"{type(e).__name__}: {e}"
    return "done", frame_rel, secs, ", ".join(notes) or "saved"


def run():
    """Process `ready` photos one at a time, forever. Any photo left mid-flight
    (still 'processing' after a restart) is put back to 'ready' first."""
    reset = db.reset_stale()
    if reset:
        log(f"reset {reset} stale job(s) -> ready")
    log(f"draining queue (model '{imaging.REMBG_MODEL}'; first job loads it)…")
    while True:
        try:
            p = db.claim_photo()
            if not p:
                time.sleep(1.0)
                continue
            status, frame, secs, notes = process_one(p)
            db.finish_photo(p["id"], status, frame_path=frame, secs=secs, notes=notes)
            log(f"#{p['id']} {p['species']}: {status} ({secs}s) {notes}")
        except Exception as e:
            # A dead worker freezes the whole queue, so never let the loop exit:
            # log and retry. Anything left 'processing' is recovered by
            # reset_stale() on the next restart.
            log(f"loop error: {type(e).__name__}: {e}")
            time.sleep(1.0)
