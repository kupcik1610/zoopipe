#!/usr/bin/env python3
"""Tiny SQLite-backed state for batched, resumable runs.

Two tables:
  runs  -- one row per CSV: your place in it (cursor) + the batch config.
  jobs  -- one row per picked image: where its original/plate live + status,
           moving  ready -> processing -> done | error.

A "batch" isn't its own table -- it's just the next `batch_size` rows of a CSV.
Each collect bumps the run's cursor and batch_seq; jobs are tagged with the
batch_seq they were collected in, so the progress page can show the latest one.

Web app and worker both touch this file, so we run in WAL mode with a busy
timeout and open a fresh short-lived connection per call.
"""
import json, os, sqlite3, time

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


def init():
    con = connect()
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
    con.commit()
    con.close()


# ---- runs -------------------------------------------------------------------
def get_run(csv):
    con = connect()
    row = con.execute("SELECT * FROM runs WHERE csv=?", (csv,)).fetchone()
    con.close()
    return row


def upsert_run(csv, cols, batch_size, results):
    """Create the run, or update its config (cols/batch_size/results) while
    keeping the existing cursor so you resume where you left off."""
    now = _now()
    con = connect()
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
    con.commit()
    con.close()
    return get_run(csv)


def confirm_batch(csv, batch):
    """Record that the user has reviewed/edited a batch and confirmed it done."""
    con = connect()
    con.execute(
        "UPDATE runs SET reviewed_seq=MAX(reviewed_seq, ?), updated_at=? WHERE csv=?",
        (batch, _now(), csv),
    )
    con.commit()
    con.close()


def advance_run(csv, rows_consumed):
    """Bump the cursor past the batch just collected and start a new batch_seq.
    Returns the new batch_seq (the batch the just-collected jobs belong to)."""
    now = _now()
    con = connect()
    con.execute(
        "UPDATE runs SET cursor=cursor+?, batch_seq=batch_seq+1, updated_at=? WHERE csv=?",
        (rows_consumed, now, csv),
    )
    con.commit()
    row = con.execute("SELECT batch_seq FROM runs WHERE csv=?", (csv,)).fetchone()
    con.close()
    return row["batch_seq"] if row else 0


def all_runs():
    con = connect()
    rows = con.execute("SELECT * FROM runs ORDER BY updated_at DESC").fetchall()
    con.close()
    return rows


def delete_run(csv):
    """Forget a run: drop its job ledger + cursor so the CSV resets to 'not
    started'. Does NOT touch already-downloaded originals or plates in out/."""
    con = connect()
    con.execute("DELETE FROM jobs WHERE csv=?", (csv,))
    con.execute("DELETE FROM runs WHERE csv=?", (csv,))
    con.commit()
    con.close()


# ---- jobs -------------------------------------------------------------------
def next_n(csv, slug):
    """Next free image index for a slug -- max over both this run's jobs and any
    files already on disk, so re-runs never collide."""
    con = connect()
    row = con.execute(
        "SELECT MAX(n) AS m FROM jobs WHERE slug=?", (slug,)
    ).fetchone()
    con.close()
    db_max = row["m"] or 0
    disk_max = 0
    odir = os.path.join(OUT_DIR, slug, "originals")
    if os.path.isdir(odir):
        for f in os.listdir(odir):
            stem = os.path.splitext(f)[0]
            if stem.isdigit():
                disk_max = max(disk_max, int(stem))
    return max(db_max, disk_max) + 1


def add_job(csv, batch, row_index, slug, query, n, source_url, orig_path):
    now = _now()
    con = connect()
    cur = con.execute(
        """
        INSERT INTO jobs (csv, batch, row_index, slug, query, n, source_url,
                          orig_path, status, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?, 'ready', '', ?, ?)
        """,
        (csv, batch, row_index, slug, query, n, source_url, orig_path, now, now),
    )
    con.commit()
    jid = cur.lastrowid
    con.close()
    return jid


def claim_job():
    """Atomically take the next ready job -> processing. Returns the row or None."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM jobs WHERE status='ready' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            con.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        con.commit()
        return row
    finally:
        con.close()


def finish_job(jid, status, plate_path=None, secs=None, notes=""):
    con = connect()
    con.execute(
        "UPDATE jobs SET status=?, plate_path=?, secs=?, notes=?, updated_at=? WHERE id=?",
        (status, plate_path, secs, notes, _now(), jid),
    )
    con.commit()
    con.close()


def retry_job(jid):
    """Flip an errored job back to ready so the worker re-attempts it."""
    con = connect()
    con.execute(
        "UPDATE jobs SET status='ready', notes='', secs=NULL, updated_at=? "
        "WHERE id=? AND status='error'",
        (_now(), jid),
    )
    con.commit()
    con.close()


def reset_stale():
    """On worker start, return any half-done 'processing' jobs to 'ready'
    (e.g. after a kill mid-image), so nothing is stranded."""
    con = connect()
    con.execute(
        "UPDATE jobs SET status='ready', updated_at=? WHERE status='processing'",
        (_now(),),
    )
    n = con.total_changes
    con.commit()
    con.close()
    return n


def jobs_for_batch(csv, batch):
    con = connect()
    rows = con.execute(
        "SELECT * FROM jobs WHERE csv=? AND batch=? ORDER BY id", (csv, batch)
    ).fetchall()
    con.close()
    return rows


def counts(csv, batch=None):
    """Status counts for a run (or one batch of it)."""
    con = connect()
    q = "SELECT status, COUNT(*) AS c FROM jobs WHERE csv=?"
    args = [csv]
    if batch is not None:
        q += " AND batch=?"
        args.append(batch)
    q += " GROUP BY status"
    rows = con.execute(q, args).fetchall()
    con.close()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["c"]
    out["total"] = sum(out[s] for s in STATUSES)
    return out


def has_ready_or_processing():
    con = connect()
    row = con.execute(
        "SELECT 1 FROM jobs WHERE status IN ('ready','processing') LIMIT 1"
    ).fetchone()
    con.close()
    return row is not None


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
