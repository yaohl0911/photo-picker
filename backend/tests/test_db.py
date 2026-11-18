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
