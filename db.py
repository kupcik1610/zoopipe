#!/usr/bin/env python3
"""Tiny SQLite-backed state for batched, resumable runs.

Two tables:
  runs  -- one row per CSV: your place in it (cursor) + the batch config.
  jobs  -- one row per picked image: where its original/frame live + status,
           moving  ready -> processing -> done | error.

A "batch" isn't its own table -- it's just the next `batch_size` rows of a CSV.
Each collect bumps the run's cursor and batch_seq; jobs are tagged with the
batch_seq they were collected in, so the progress page can show the latest one.

Web app and worker both touch this file, so we run in WAL mode with a busy
timeout and open a fresh short-lived connection per call (see _db()).
"""
import json, os, sqlite3, time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
DB_PATH = os.path.join(OUT_DIR, "jobs.sqlite")
LOCK_PATH = os.path.join(OUT_DIR, "worker.lock")

STATUSES = ("ready", "processing", "done", "error")


def _now():
    return int(time.time())


def connect():
    os.makedirs(OUT_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def _db():
    """A short-lived connection that commits on clean exit and always closes.
    Reads commit harmlessly; writes are durable once the `with` block ends."""
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                csv         TEXT PRIMARY KEY,
                cols        TEXT NOT NULL,        -- json list of column names
                batch_size  INTEGER NOT NULL,
                results     INTEGER NOT NULL,     -- results per query
                cursor      INTEGER NOT NULL DEFAULT 0,
                batch_seq   INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                csv         TEXT NOT NULL,
                batch       INTEGER NOT NULL,
                row_index   INTEGER NOT NULL,
                slug        TEXT NOT NULL,
                query       TEXT NOT NULL,
                n           INTEGER NOT NULL,     -- index within the slug folder
                source_url  TEXT NOT NULL,
                orig_path   TEXT NOT NULL,        -- rel under out/, e.g. slug/originals/3.jpg
                plate_path  TEXT,                 -- rel under out/, set when done
                status      TEXT NOT NULL DEFAULT 'ready',
                notes       TEXT DEFAULT '',
                secs        REAL,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS jobs_csv_batch ON jobs(csv, batch);
            """
        )
        # migration: reviewed_seq = highest batch the user has confirmed as done
        cols = [r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()]
        if "reviewed_seq" not in cols:
            con.execute("ALTER TABLE runs ADD COLUMN reviewed_seq INTEGER NOT NULL DEFAULT 0")


# ---- runs -------------------------------------------------------------------
def get_run(csv):
    with _db() as con:
        return con.execute("SELECT * FROM runs WHERE csv=?", (csv,)).fetchone()


def upsert_run(csv, cols, batch_size, results):
    """Create the run, or update its config (cols/batch_size/results) while
    keeping the existing cursor so you resume where you left off."""
    now = _now()
    with _db() as con:
        con.execute(
            """
            INSERT INTO runs (csv, cols, batch_size, results, cursor, batch_seq, created_at, updated_at)
            VALUES (?,?,?,?,0,0,?,?)
            ON CONFLICT(csv) DO UPDATE SET
                cols=excluded.cols,
                batch_size=excluded.batch_size,
                results=excluded.results,
                updated_at=excluded.updated_at
            """,
            (csv, json.dumps(cols), batch_size, results, now, now),
        )
    return get_run(csv)


def confirm_batch(csv, batch):
    """Record that the user has reviewed/edited a batch and confirmed it done."""
    with _db() as con:
        con.execute(
            "UPDATE runs SET reviewed_seq=MAX(reviewed_seq, ?), updated_at=? WHERE csv=?",
            (batch, _now(), csv),
        )


def advance_run(csv, rows_consumed):
    """Bump the cursor past the batch just collected and start a new batch_seq.
    Returns the new batch_seq (the batch the just-collected jobs belong to)."""
    now = _now()
    with _db() as con:
        con.execute(
            "UPDATE runs SET cursor=cursor+?, batch_seq=batch_seq+1, updated_at=? WHERE csv=?",
            (rows_consumed, now, csv),
        )
        row = con.execute("SELECT batch_seq FROM runs WHERE csv=?", (csv,)).fetchone()
    return row["batch_seq"] if row else 0


def delete_run(csv):
    """Forget a run: drop its job ledger + cursor so the CSV resets to 'not
    started'. Does NOT touch already-downloaded originals or frames in out/."""
    with _db() as con:
        con.execute("DELETE FROM jobs WHERE csv=?", (csv,))
        con.execute("DELETE FROM runs WHERE csv=?", (csv,))


# ---- jobs -------------------------------------------------------------------
def next_n(csv, slug):
    """Next free image index for a slug -- max over both this run's jobs and any
    files already on disk, so re-runs never collide."""
    with _db() as con:
        row = con.execute("SELECT MAX(n) AS m FROM jobs WHERE slug=?", (slug,)).fetchone()
    db_max = row["m"] or 0
    disk_max = 0
    odir = os.path.join(OUT_DIR, slug, "originals")
    if os.path.isdir(odir):
        for f in os.listdir(odir):
            stem = os.path.splitext(f)[0]
            if stem.isdigit():
                disk_max = max(disk_max, int(stem))
    return max(db_max, disk_max) + 1


def add_job(csv, batch, row_index, slug, query, n, source_url, orig_path,
            status="ready", notes=""):
    """Queue a job. Normally status='ready'; pass status='error' (with a note)
    to record a pick whose download failed -- it shows on the progress table and
    the worker re-downloads it from source_url on retry."""
    now = _now()
    with _db() as con:
        cur = con.execute(
            """
            INSERT INTO jobs (csv, batch, row_index, slug, query, n, source_url,
                              orig_path, status, notes, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?, ?, ?, ?, ?)
            """,
            (csv, batch, row_index, slug, query, n, source_url, orig_path,
             status, notes, now, now),
        )
        return cur.lastrowid


def set_orig_path(jid, orig_path):
    """Record where a job's original landed -- used when the worker (re)downloads
    a job whose original was missing (a retried download failure)."""
    with _db() as con:
        con.execute(
            "UPDATE jobs SET orig_path=?, updated_at=? WHERE id=?",
            (orig_path, _now(), jid),
        )


def repick_rows(csv):
    """Rows that still need a photo picked because their download failed and
    nothing usable ever landed for them -- i.e. a row with a download-failure
    error job (status='error', no orig_path) and NO other job that is done or
    still in flight. These get resurfaced on the collect page so the user can
    pick a different image. Returns rows of (row_index, query, slug)."""
    with _db() as con:
        return con.execute(
            """
            SELECT DISTINCT j.row_index AS row_index, j.query AS query, j.slug AS slug
              FROM jobs j
             WHERE j.csv=? AND j.status='error'
                   AND (j.orig_path='' OR j.orig_path IS NULL)
                   AND NOT EXISTS (
                       SELECT 1 FROM jobs k
                        WHERE k.csv=j.csv AND k.row_index=j.row_index
                              AND k.status IN ('ready','processing','done')
                   )
             ORDER BY j.row_index
            """,
            (csv,),
        ).fetchall()


def pending_download_failures(csv):
    """How many rows are still awaiting a re-pick (see repick_rows). Keeps a run
    from counting 'complete' while some fish still have no usable image."""
    return len(repick_rows(csv))


def claim_job():
    """Atomically take the next ready job -> processing. Returns the row or None."""
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM jobs WHERE status='ready' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            con.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        return row


def finish_job(jid, status, plate_path=None, secs=None, notes=""):
    with _db() as con:
        con.execute(
            "UPDATE jobs SET status=?, plate_path=?, secs=?, notes=?, updated_at=? WHERE id=?",
            (status, plate_path, secs, notes, _now(), jid),
        )


def retry_job(jid):
    """Flip an errored job back to ready so the worker re-attempts it."""
    with _db() as con:
        con.execute(
            "UPDATE jobs SET status='ready', notes='', secs=NULL, updated_at=? "
            "WHERE id=? AND status='error'",
            (_now(), jid),
        )


def reset_stale():
    """On worker start, return any half-done 'processing' jobs to 'ready'
    (e.g. after a kill mid-image), so nothing is stranded."""
    with _db() as con:
        con.execute(
            "UPDATE jobs SET status='ready', updated_at=? WHERE status='processing'",
            (_now(),),
        )
        return con.total_changes


def get_job(jid):
    with _db() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def jobs_for_batch(csv, batch):
    # order by row_index (CSV order) then id so a species' images sit together
    # and in pick order -- the progress page groups on this to show one section
    # per species instead of one flat list.
    with _db() as con:
        return con.execute(
            "SELECT * FROM jobs WHERE csv=? AND batch=? ORDER BY row_index, id",
            (csv, batch),
        ).fetchall()


def counts(csv, batch=None):
    """Status counts for a run (or one batch of it)."""
    q = "SELECT status, COUNT(*) AS c FROM jobs WHERE csv=?"
    args = [csv]
    if batch is not None:
        q += " AND batch=?"
        args.append(batch)
    q += " GROUP BY status"
    with _db() as con:
        rows = con.execute(q, args).fetchall()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["c"]
    out["total"] = sum(out[s] for s in STATUSES)
    return out


# ---- worker lock ------------------------------------------------------------
def worker_running():
    """True if a worker process holds the lock and is still alive."""
    try:
        with open(LOCK_PATH) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)          # signal 0 = liveness probe, doesn't kill
        return True
    except OSError:
        return False


def acquire_lock():
    """Claim the worker lock. Returns True on success, False if one is alive."""
    os.makedirs(OUT_DIR, exist_ok=True)
    if worker_running():
        return False
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass
