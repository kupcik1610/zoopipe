#!/usr/bin/env python3
"""Batch background-removal worker.

Drains every `ready` job from the jobs ledger: reads the already-downloaded
original off disk, runs the birefnet recipe (imaging.normalize), writes the
white catalogue plate to out/<slug>/<n>.jpg, records timing/notes, and moves on.
Exits when the queue is empty -- that's the "on demand" model.

Started for you by the web app's Process button (as a detached subprocess), but
also runnable straight from a terminal:

    .venv/bin/python worker.py

Only one runs at a time (a pid lockfile self-guards). Safe to kill mid-run:
on the next start any half-done 'processing' job is reset back to 'ready'.
"""
import os, sys, time

import db
import imaging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")


def log(msg):
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def process_one(job):
    """Normalize one job's original into its plate. Returns (status, notes, secs)."""
    src = os.path.join(OUT_DIR, job["orig_path"])
    if not os.path.isfile(src):
        return "error", f"original missing: {job['orig_path']}", None
    try:
        with open(src, "rb") as f:
            raw = f.read()
        t0 = time.time()
        out, ext, notes = imaging.normalize(raw, trim=True)
        secs = round(time.time() - t0, 2)
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}", None

    note_str = ", ".join(notes)
    if any(n.startswith("rembg-failed") or n.startswith("rembg-missing") for n in notes):
        # bg removal didn't actually happen -> treat as an error so it's visible
        return "error", note_str or "bg-removal failed", secs

    plate_rel = f"{job['slug']}/{job['n']}.{ext}"
    dest = os.path.join(OUT_DIR, plate_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(out)
    return "done", note_str or "saved", secs


def main():
    db.init()
    if not db.acquire_lock():
        log("another worker is already running; exiting.")
        return
    try:
        reset = db.reset_stale()
        if reset:
            log(f"reset {reset} stale processing job(s) -> ready")

        log(f"warming model '{imaging.REMBG_MODEL}' (first run may download ~1GB)…")
        imaging.warm()
        log("ready; draining queue.")

        done = 0
        while True:
            job = db.claim_job()
            if not job:
                break
            label = f"#{job['id']} {job['slug']}/{job['n']}"
            status, notes, secs = process_one(job)
            plate = f"{job['slug']}/{job['n']}.jpg" if status == "done" else None
            db.finish_job(job["id"], status, plate_path=plate, secs=secs, notes=notes)
            done += 1
            log(f"{label}: {status} ({secs}s) {notes}")

        log(f"queue empty; processed {done} job(s) this run.")
    finally:
        db.release_lock()


if __name__ == "__main__":
    sys.exit(main())
