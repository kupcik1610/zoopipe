#!/usr/bin/env python3
"""Single background worker: turn picked images into finished catalogue frames.

Drains `ready` photos from the ledger one at a time: download the original,
background-remove it (imaging.make_frame), write the white frame, mark it done.
One worker is plenty -- ~10s/image keeps up while you keep picking. A pidfile
guards against two running at once; on start any half-done job is reset to ready.

The web app's Process button just adds `ready` rows and calls spawn (see app.py).
The worker loads the ~1GB model lazily on its first job, then stays alive and
idle-polls so later jobs reuse it, exiting only after IDLE_TIMEOUT idle seconds.

    .venv/bin/python worker.py
"""
import os, time, urllib.request, urllib.error

import db
import imaging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
UA = "ryby-fish-catalog/1.0 (contact: kupco.patrik.16@gmail.com)"
IDLE_TIMEOUT = float(os.environ.get("WORKER_IDLE_TIMEOUT", "300"))


def log(msg):
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    """Download + frame one photo. Returns (status, frame_rel, secs, notes)."""
    try:
        raw = _download(p["source_url"])
    except Exception as e:
        return "error", "", None, f"download failed: {e}"
    if not imaging.is_image(raw):
        return "error", "", None, "download: not an image"

    orig_rel = f"{p['folder']}/originals/{p['id']}.{imaging._ext(raw)}"
    orig_abs = os.path.join(OUT_DIR, orig_rel)
    os.makedirs(os.path.dirname(orig_abs), exist_ok=True)
    with open(orig_abs, "wb") as f:
        f.write(raw)
    db.set_orig_path(p["id"], orig_rel)

    try:
        t0 = time.time()
        out, ext, notes = imaging.make_frame(raw)
        secs = round(time.time() - t0, 2)
    except Exception as e:
        return "error", "", None, f"{type(e).__name__}: {e}"

    frame_rel = f"{p['folder']}/{p['id']}.{ext}"
    frame_abs = os.path.join(OUT_DIR, frame_rel)
    os.makedirs(os.path.dirname(frame_abs), exist_ok=True)
    with open(frame_abs, "wb") as f:
        f.write(out)
    return "done", frame_rel, secs, ", ".join(notes) or "saved"


def main():
    db.init()
    if not db.acquire_lock():
        log("a worker is already running; exiting.")
        return
    try:
        reset = db.reset_stale()
        if reset:
            log(f"reset {reset} stale job(s) -> ready")
        log(f"draining queue (model '{imaging.REMBG_MODEL}'; first job loads it)…")
        done = 0
        last_work = time.time()
        while True:
            p = db.claim_photo()
            if not p:
                if time.time() - last_work > IDLE_TIMEOUT:
                    break
                time.sleep(1.0)
                continue
            status, frame, secs, notes = process_one(p)
            db.finish_photo(p["id"], status, frame_path=frame, secs=secs, notes=notes)
            done += 1
            last_work = time.time()
            log(f"#{p['id']} {p['species']}: {status} ({secs}s) {notes}")
        log(f"idle {int(IDLE_TIMEOUT)}s; exiting (processed {done} this run).")
    finally:
        db.release_lock()


if __name__ == "__main__":
    main()
