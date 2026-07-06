#!/usr/bin/env python3
"""Tiny SQLite state: one table, `photos`.

Each picked image is one row that moves ready -> processing -> done | error.
The gallery is just the CSV's rows joined to these by product id (idpr); you work
species in any order and your place is implied by which photos exist.

Connections are short-lived (WAL + busy timeout) so many can touch the DB at
once; `claim_photo` uses BEGIN IMMEDIATE so a row is only ever picked up once.
"""
import os, sqlite3, time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
DB_PATH = os.path.join(OUT_DIR, "jobs.sqlite")


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
            CREATE TABLE IF NOT EXISTS photos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                csv         TEXT NOT NULL,
                idpr        TEXT NOT NULL,        -- CSV `id`: the minizoo product id
                species     TEXT NOT NULL,        -- display name (Slovak)
                query       TEXT NOT NULL,        -- the term searched
                folder      TEXT NOT NULL,        -- rel dir under out/ for this species
                source_url  TEXT NOT NULL,
                orig_path   TEXT DEFAULT '',      -- rel under out/, set once downloaded
                frame_path  TEXT DEFAULT '',      -- rel under out/, set when done
                status      TEXT NOT NULL DEFAULT 'ready',
                secs        REAL,
                notes       TEXT DEFAULT '',
                uploaded_at INTEGER,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS photos_csv ON photos(csv);
            CREATE INDEX IF NOT EXISTS photos_status ON photos(status);
            """
        )


# ---- photos -----------------------------------------------------------------
def add_photo(csv, idpr, species, query, folder, source_url):
    now = _now()
    with _db() as con:
        cur = con.execute(
            """INSERT INTO photos (csv, idpr, species, query, folder, source_url,
                                   status, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'ready', ?, ?)""",
            (csv, idpr, species, query, folder, source_url, now, now),
        )
        return cur.lastrowid


def claim_photo():
    """Atomically take the next ready photo -> processing. Returns row or None."""
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM photos WHERE status='ready' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            con.execute(
                "UPDATE photos SET status='processing', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        return row


def set_orig_path(pid, orig_path):
    with _db() as con:
        con.execute("UPDATE photos SET orig_path=?, updated_at=? WHERE id=?",
                    (orig_path, _now(), pid))


def finish_photo(pid, status, frame_path="", secs=None, notes=""):
    with _db() as con:
        con.execute(
            "UPDATE photos SET status=?, frame_path=?, secs=?, notes=?, updated_at=? WHERE id=?",
            (status, frame_path, secs, notes, _now(), pid),
        )


def retry_photo(pid):
    with _db() as con:
        con.execute(
            "UPDATE photos SET status='ready', notes='', secs=NULL, updated_at=? "
            "WHERE id=? AND status='error'",
            (_now(), pid),
        )


def mark_uploaded(pid):
    with _db() as con:
        con.execute("UPDATE photos SET uploaded_at=?, updated_at=? WHERE id=?",
                    (_now(), _now(), pid))


def delete_photo(pid):
    """Remove a photo row and return it (so the caller can delete its files)."""
    with _db() as con:
        row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
        con.execute("DELETE FROM photos WHERE id=?", (pid,))
        return row


def get_photo(pid):
    with _db() as con:
        return con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()


def photos_for_csv(csv):
    with _db() as con:
        return con.execute(
            "SELECT * FROM photos WHERE csv=? ORDER BY idpr, id", (csv,)
        ).fetchall()


def reset_stale():
    """Return any half-done 'processing' rows to 'ready' (called on startup)."""
    with _db() as con:
        con.execute("UPDATE photos SET status='ready', updated_at=? WHERE status='processing'",
                    (_now(),))
        return con.total_changes
