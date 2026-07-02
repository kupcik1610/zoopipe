#!/usr/bin/env python3
"""Batch background-removal worker POOL.

Drains `ready` jobs from the jobs ledger: reads the already-downloaded original
off disk, runs the birefnet recipe (imaging.make_frame), writes the white
catalogue frame to out/<slug>/<n>.jpg, records timing/notes, and moves on.

Parallel pool
-------------
`worker.py` is a SUPERVISOR: it spawns WORKER_CONCURRENCY child processes (each
a `--child`), then waits. The SQLite claim (BEGIN IMMEDIATE in db.claim_job) is
atomic, so N children safely pull from one queue with no double-processing.

Each child caps its ONNX threads via OMP_NUM_THREADS=WORKER_THREADS so the pool
doesn't oversubscribe cores: on a 64-core box, 8 workers x 8 threads = 64.
birefnet scales poorly with threads, so more workers beats more threads/worker.

  WORKER_CONCURRENCY   number of worker processes           (default 1)
  WORKER_THREADS       ONNX threads per worker (OMP)        (default 8)
  WORKER_IDLE_TIMEOUT  secs an idle worker waits before exit (default 300)

Stay alive between batches
--------------------------
A worker loads the model lazily on its first job (~10-40s the first time), then
stays alive and idle-polls so later jobs reuse the already-loaded model. It
exits only after WORKER_IDLE_TIMEOUT idle seconds, to free its ~2-3GB of RAM.
The web app's Process button just adds `ready` jobs; a live worker picks them up
within a second (spawn_worker no-ops while the supervisor holds the lock).

    .venv/bin/python worker.py            # supervisor (spawns the pool)

Only one supervisor runs at a time (a pid lockfile self-guards). Safe to kill:
on the next start any half-done 'processing' job is reset back to 'ready'.
"""
import os, sys, time, subprocess

import db
import imaging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")

CONCURRENCY = max(1, int(os.environ.get("WORKER_CONCURRENCY", "1") or 1))
THREADS = max(1, int(os.environ.get("WORKER_THREADS", "8") or 8))
# how long an idle worker stays idle-polling before exiting to free RAM
IDLE_TIMEOUT = float(os.environ.get("WORKER_IDLE_TIMEOUT", "300"))


def log(msg, tag="pool"):
    print(f"[{tag} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def process_one(job):
    """Turn one job's original into its frame.
    Returns (status, notes, secs, frame_rel) -- frame_rel is None unless done."""
    src = os.path.join(OUT_DIR, job["orig_path"])
    if not os.path.isfile(src):
        return "error", f"original missing: {job['orig_path']}", None, None
    try:
        with open(src, "rb") as f:
            raw = f.read()
        t0 = time.time()
        out, ext, notes = imaging.make_frame(raw)
        secs = round(time.time() - t0, 2)
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}", None, None

    frame_rel = f"{job['slug']}/{job['n']}.{ext}"
    dest = os.path.join(OUT_DIR, frame_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(out)
    return "done", ", ".join(notes) or "saved", secs, frame_rel


def drain(idx):
    """One pool worker: claim/process/idle-poll until IDLE_TIMEOUT passes with
    an empty queue. The model loads lazily on the first job."""
    tag = f"w{idx}"
    db.init()
    log(f"ready; draining queue (model '{imaging.REMBG_MODEL}', {THREADS} "
        f"threads; first job may load the model / download ~1GB)…", tag)

    done = 0
    last_work = time.time()
    while True:
        job = db.claim_job()
        if not job:
            if time.time() - last_work > IDLE_TIMEOUT:
                break
            time.sleep(1.0)
            continue
        label = f"#{job['id']} {job['slug']}/{job['n']}"
        status, notes, secs, frame = process_one(job)
        db.finish_job(job["id"], status, plate_path=frame, secs=secs, notes=notes)
        done += 1
        last_work = time.time()
        log(f"{label}: {status} ({secs}s) {notes}", tag)

    log(f"idle {int(IDLE_TIMEOUT)}s; exiting (processed {done} job(s) this run).", tag)


def supervise():
    """Spawn CONCURRENCY child workers (each thread-capped) and wait for them."""
    db.init()
    if not db.acquire_lock():
        log("a worker pool is already running; exiting.")
        return
    try:
        reset = db.reset_stale()
        if reset:
            log(f"reset {reset} stale processing job(s) -> ready")
        log(f"starting {CONCURRENCY} worker(s) x {THREADS} threads.")

        procs = []
        for idx in range(CONCURRENCY):
            env = dict(os.environ, OMP_NUM_THREADS=str(THREADS))
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "worker.py"), "--child", str(idx)],
                cwd=BASE_DIR, env=env,
            ))
        for p in procs:
            p.wait()
        log("all workers exited.")
    finally:
        db.release_lock()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        return drain(int(sys.argv[2]))
    return supervise()


if __name__ == "__main__":
    sys.exit(main())
