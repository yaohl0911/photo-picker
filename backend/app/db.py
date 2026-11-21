"""SQLite connection management and schema for photo-picker.

Tables follow docs/superpowers/specs/2026-11-18-photo-dedup-design.md §4.
The schema is versioned via the `meta` table (key `schema_version`).
"""

import sqlite3
import threading

SCHEMA_VERSION = 1

# DDL for every table. CREATE TABLE IF NOT EXISTS keeps init_db idempotent.
# SQLite resolves foreign keys at DML time, so creation order does not matter
# for the circular files <-> photos references.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | decided | not_duplicate
    recommended_photo_id INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    mtime REAL,
    size INTEGER,
    file_hash TEXT,
    format TEXT,
    photo_id INTEGER REFERENCES photos(id),
    captured_at TEXT,
    camera TEXT,
    exif_json TEXT,
    hash_phash TEXT,
    hash_ahash TEXT,
    hash_dhash TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    primary_file_id INTEGER REFERENCES files(id),
    group_id INTEGER REFERENCES groups(id),
    is_screenshot INTEGER   -- NULL=未分类, 1=截图(截图页处理), 0=照片
);

CREATE TABLE IF NOT EXISTS quality_scores (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id),
    sharpness REAL,
    exposure REAL,
    noise REAL,
    composition REAL,
    faces REAL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    group_id INTEGER REFERENCES groups(id),
    kept_photo_ids TEXT,   -- JSON array
    decided_at TEXT,
    applied_at TEXT,
    source TEXT NOT NULL DEFAULT 'user'  -- user | auto
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY,
    photo_id INTEGER,
    action TEXT,           -- like | dislike
    group_id INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS preference_model (
    version INTEGER PRIMARY KEY,
    weights_json TEXT,
    bias REAL,
    sample_count INTEGER,
    trained_at TEXT
);

CREATE TABLE IF NOT EXISTS move_log (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions(id),
    file_id INTEGER REFERENCES files(id),
    src_path TEXT,
    dst_path TEXT,
    moved_at TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS video_moves (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions(id),
    src_path TEXT,
    dst_path TEXT,
    moved_at TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    phase TEXT,
    progress REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- photo membership lookups run per candidate during raw-jpg pairing;
-- without this index each one rescans the whole files table
CREATE INDEX IF NOT EXISTS idx_files_photo ON files(photo_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the app's standard pragmas applied."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if missing and (re)record the schema version.

    Idempotent: safe to call on every startup. Also applies lightweight
    column migrations for tables created before a column existed.
    """
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "decisions", "source",
                   "TEXT NOT NULL DEFAULT 'user'")
    _ensure_column(conn, "photos", "is_screenshot", "INTEGER")
    _backfill_recommended_for_decided(conn)
    _purge_ghost_auto_decisions(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _purge_ghost_auto_decisions(conn: sqlite3.Connection) -> None:
    """Remove phantom auto decisions that never moved a single file.

    Before row-purge existed, auto-resolve "executed" on photos whose
    files the user had already deleted outside the app: decisions with
    moved_files = 0 and, after the row purge, groups holding at most the
    keeper. Those history rows are pure noise (nothing was ever moved,
    nothing to undo) — drop the decision row and dissolve the leftover
    single-member group. Rows with a move/video_moves entry (real work)
    or sharing the group with another decision are kept.
    """
    rows = conn.execute(
        """
        SELECT d.id AS did, d.group_id AS gid FROM decisions d
        WHERE d.source = 'auto'
          AND NOT EXISTS (SELECT 1 FROM move_log m
                          WHERE m.decision_id = d.id)
          AND NOT EXISTS (SELECT 1 FROM video_moves v
                          WHERE v.decision_id = d.id)
          AND (SELECT COUNT(*) FROM photos p
               WHERE p.group_id = d.group_id) <= 1
        """
    ).fetchall()
    has_samples = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table'"
        " AND name = 'preference_samples'").fetchone()
    for r in rows:
        if has_samples:
            conn.execute(
                "DELETE FROM preference_samples WHERE decision_id = ?",
                (r["did"],))
        conn.execute("DELETE FROM decisions WHERE id = ?", (r["did"],))
        # dissolve the group only when no other decision owns it
        still_owned = conn.execute(
            "SELECT 1 FROM decisions WHERE group_id = ? LIMIT 1",
            (r["gid"],)).fetchone()
        if still_owned is None:
            conn.execute(
                "UPDATE photos SET group_id = NULL WHERE group_id = ?",
                (r["gid"],))
            conn.execute("DELETE FROM groups WHERE id = ?", (r["gid"],))


def _backfill_recommended_for_decided(conn: sqlite3.Connection) -> None:
    """One-time data fix: decided groups' recommended_photo_id.

    Auto-resolve decisions written by executor directly (learn=False)
    used to leave ``recommended_photo_id`` NULL, so opening such a group
    in the compare view fell back to a fresh quality ranking and showed
    a misleading "recommended" star on a photo that was actually
    rejected. Backfill = first kept photo of the latest decision.
    Idempotent: only touches rows still NULL.
    """
    conn.execute(
        """
        UPDATE groups SET recommended_photo_id = (
            SELECT json_extract(d.kept_photo_ids, '$[0]')
            FROM decisions d
            WHERE d.group_id = groups.id
              AND d.kept_photo_ids IS NOT NULL
              AND json_array_length(d.kept_photo_ids) > 0
            ORDER BY d.id DESC LIMIT 1)
        WHERE status = 'decided' AND recommended_photo_id IS NULL
        """
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl_type: str) -> None:
    """Add ``column`` to an existing ``table`` when it is missing."""
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


# App-level connection cache: one shared connection per database path.
_connections: dict[str, sqlite3.Connection] = {}
_lock = threading.Lock()


def get_db(db_path: str) -> sqlite3.Connection:
    """Return the app-level singleton connection for db_path.

    For the API layer; call init_db() once at startup after this.
    """
    with _lock:
        conn = _connections.get(db_path)
        if conn is None:
            conn = connect(db_path)
            _connections[db_path] = conn
        return conn
