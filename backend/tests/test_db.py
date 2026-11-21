"""Tests for app.db (schema, connection, singleton) and app.config (settings KV)."""

import sqlite3

import pytest

from app.config import get_settings, update_settings
from app.db import SCHEMA_VERSION, connect, get_db, init_db

EXPECTED_TABLES = {
    "files",
    "photos",
    "groups",
    "quality_scores",
    "decisions",
    "feedback_events",
    "preference_model",
    "move_log",
    "scan_runs",
    "settings",
    "meta",
}


def _table_names(conn):
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_connect_applies_pragmas_and_row_factory(tmp_path):
    conn = connect(str(tmp_path / "pragma.db"))
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_init_db_creates_all_tables(db):
    assert EXPECTED_TABLES <= _table_names(db)


def test_init_db_is_idempotent(db):
    init_db(db)
    init_db(db)
    assert EXPECTED_TABLES <= _table_names(db)


def test_files_path_is_unique(db):
    db.execute("INSERT INTO files (path, mtime, size) VALUES ('/a.jpg', 1.0, 10)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO files (path, mtime, size) VALUES ('/a.jpg', 1.0, 10)")


def test_photos_group_id_enforced_by_foreign_key(db):
    db.execute("INSERT INTO files (path) VALUES ('/f.jpg')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO photos (primary_file_id, group_id) VALUES (1, 999)"
        )


def test_get_settings_returns_defaults(db):
    assert get_settings(db) == {
        "source_dirs": [],
        "phash_threshold": 10,
        "time_window_sec": 60,
        "rejected_root": "",
        "face_engine": "auto",
        "auto_resolve_identical": True,
    }


def test_update_settings_roundtrip(db):
    update_settings(
        db,
        source_dirs=["/photos/2026", "/photos/2025"],
        phash_threshold=8,
        rejected_root="/tmp/rejected",
    )
    settings = get_settings(db)
    assert settings["source_dirs"] == ["/photos/2026", "/photos/2025"]
    assert settings["phash_threshold"] == 8
    assert settings["rejected_root"] == "/tmp/rejected"
    # untouched keys keep their defaults
    assert settings["time_window_sec"] == 60
    assert settings["face_engine"] == "auto"


def test_update_settings_rejects_unknown_field(db):
    with pytest.raises(ValueError):
        update_settings(db, does_not_exist=1)


def test_get_db_returns_singleton_connection(tmp_path):
    db_path = str(tmp_path / "app.db")
    first = get_db(db_path)
    second = get_db(db_path)
    try:
        assert first is second
        assert first.execute("SELECT 1").fetchone()[0] == 1
    finally:
        first.close()


def test_schema_version_recorded_in_meta(db):
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert int(row["value"]) == SCHEMA_VERSION


class TestGhostAutoDecisionPurge:
    def _mk_group_photo(self, conn, name, gid=None):
        cur = conn.execute(
            "INSERT INTO files (path, mtime, size, format)"
            " VALUES (?, 1.0, 8, 'jpg')", (f"/x/{name}",))
        fid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO photos (primary_file_id, group_id) VALUES (?, ?)",
            (fid, gid))
        pid = cur.lastrowid
        conn.execute(
            "UPDATE files SET photo_id = ? WHERE id = ?", (pid, fid))
        return pid

    def test_ghost_auto_decision_removed_with_group(self, tmp_path):
        conn = connect(str(tmp_path / "t.db"))
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO groups (status, created_at)"
            " VALUES ('decided', '2026-11-20T00:00:00')")
        gid = cur.lastrowid
        pid = self._mk_group_photo(conn, "keeper.jpg", gid)
        conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at,"
            " source) VALUES (?, ?, ?, 'auto')",
            (gid, f"[{pid}]", "2026-11-20T00:00:01"))
        conn.commit()

        init_db(conn)  # migration runs

        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM groups WHERE id = ?", (gid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT group_id FROM photos WHERE id = ?", (pid,)
        ).fetchone()[0] is None

    def test_auto_decision_with_moves_is_kept(self, tmp_path):
        conn = connect(str(tmp_path / "t.db"))
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO groups (status, created_at)"
            " VALUES ('decided', '2026-11-20T00:00:00')")
        gid = cur.lastrowid
        pid = self._mk_group_photo(conn, "keeper.jpg", gid)
        cur = conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at,"
            " source) VALUES (?, ?, ?, 'auto')",
            (gid, f"[{pid}]", "2026-11-20T00:00:01"))
        did = cur.lastrowid
        conn.execute(
            "INSERT INTO move_log (decision_id, file_id, src_path, dst_path,"
            " moved_at, undone) VALUES (?, NULL, '/a.jpg', '/b.jpg',"
            " '2026-11-20T00:00:02', 0)", (did,))
        conn.commit()

        init_db(conn)

        assert conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE id = ?", (did,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM groups WHERE id = ?", (gid,)
        ).fetchone()[0] == 1

    def test_group_with_second_decision_keeps_group(self, tmp_path):
        # the group also has a manual decision: only the ghost auto row
        # disappears, the group (still owned by the manual decision) stays
        conn = connect(str(tmp_path / "t.db"))
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO groups (status, created_at)"
            " VALUES ('decided', '2026-11-20T00:00:00')")
        gid = cur.lastrowid
        pid = self._mk_group_photo(conn, "keeper.jpg", gid)
        conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at,"
            " source) VALUES (?, ?, ?, 'auto')",
            (gid, f"[{pid}]", "2026-11-20T00:00:01"))
        cur = conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at)"
            " VALUES (?, ?, '2026-11-20T00:05:00')", (gid, f"[{pid}]"))
        manual_id = cur.lastrowid
        conn.commit()

        init_db(conn)

        rows = conn.execute("SELECT id FROM decisions").fetchall()
        assert [r[0] for r in rows] == [manual_id]
        assert conn.execute(
            "SELECT COUNT(*) FROM groups WHERE id = ?", (gid,)
        ).fetchone()[0] == 1

    def test_manual_zero_move_decision_is_kept(self, tmp_path):
        # the noise rule only applies to auto rows: manual decisions are
        # always real user intent and stay in history
        conn = connect(str(tmp_path / "t.db"))
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO groups (status, created_at)"
            " VALUES ('decided', '2026-11-20T00:00:00')")
        gid = cur.lastrowid
        pid = self._mk_group_photo(conn, "keeper.jpg", gid)
        conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at)"
            " VALUES (?, ?, '2026-11-20T00:00:01')", (gid, f"[{pid}]"))
        conn.commit()

        init_db(conn)

        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_photos_have_is_screenshot_column(db):
    """基础 schema 即含 is_screenshot（grouper/auto_resolve 的 SQL 过滤依赖它，
    不等截图模块惰性 ALTER）。"""
    row = db.execute(
        "SELECT is_screenshot FROM photos LIMIT 1").fetchone()
    # 空表也能查询成功；列默认 NULL
    cols = {r["name"] for r in db.execute("PRAGMA table_info(photos)")}
    assert "is_screenshot" in cols
