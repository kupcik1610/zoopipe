#!/usr/bin/env python3
"""Batch background-removal worker POOL.

Drains `ready` jobs from the jobs ledger: reads the already-downloaded original
off disk, runs the birefnet recipe (imaging.normalize), writes the white
catalogue plate to out/<slug>/<n>.jpg, records timing/notes, and moves on.

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
  WORKER_IDLE_TIMEOUT  secs a warm worker idles before exit (default 300)

Warm once, not per batch
------------------------
The ~10-40s birefnet warm-up is paid once per worker at start; when the queue
drains a worker stays alive and idle-polls, keeping the model warm. It exits
only after WORKER_IDLE_TIMEOUT idle seconds, to free its ~2-3GB of RAM. The web
app's Process button just adds `ready` jobs; still-warm workers pick them up
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
# how long a warm worker stays idle-polling before exiting to free RAM
IDLE_TIMEOUT = float(os.environ.get("WORKER_IDLE_TIMEOUT", "300"))


def log(msg, tag="pool"):
    print(f"[{tag} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def drain(idx):
    """One pool worker: warm the model once, then claim/process/idle-poll until
    IDLE_TIMEOUT passes with an empty queue. Reports its phase for the UI."""
    tag = f"w{idx}"
    db.init()
    db.set_worker_phase(idx, "warming")
    log(f"warming model '{imaging.REMBG_MODEL}' "
        f"({THREADS} threads; first run may download ~1GB)…", tag)
    imaging.warm()
    log("ready; draining queue.", tag)

    done = 0
    last_work = time.time()
    while True:
        job = db.claim_job()
        if not job:
            if time.time() - last_work > IDLE_TIMEOUT:
                break
            db.set_worker_phase(idx, "idle")
            time.sleep(1.0)
            continue
        db.set_worker_phase(idx, "running")
        label = f"#{job['id']} {job['slug']}/{job['n']}"
        status, notes, secs = process_one(job)
        plate = f"{job['slug']}/{job['n']}.jpg" if status == "done" else None
        db.finish_job(job["id"], status, plate_path=plate, secs=secs, notes=notes)
        done += 1
        last_work = time.time()
        log(f"{label}: {status} ({secs}s) {notes}", tag)

    db.set_worker_phase(idx, "done")
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
