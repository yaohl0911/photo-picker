# -*- coding: utf-8 -*-
"""HTTP smoke tests for app.main (TestClient).

These require fastapi, which cannot be imported inside DSH (pydantic_core
native lib is rejected by AMFI). The module-level importorskip makes the
whole file skip in DSH; run the user-terminal venv for full verification.
"""
import pytest

pytest.importorskip("fastapi", exc_type=ImportError)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_PICKER_DB", str(tmp_path / "api_test.db"))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_get_overview(client):
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_photos"] == 0
    assert data["pending_groups"] == 0
    assert data["model_version"] == 0


def test_get_groups_empty(client):
    resp = client.get("/api/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["groups"] == []
    assert data["total"] == 0


def test_groups_invalid_status_422(client):
    resp = client.get("/api/groups", params={"status": "bogus"})
    assert resp.status_code == 422


def test_decision_missing_group_404(client):
    resp = client.post("/api/groups/999/decision", json={"keep_photo_ids": [1]})
    assert resp.status_code == 404


def test_feedback_invalid_action_422(client):
    resp = client.post("/api/feedback", json={"photo_id": 1, "action": "meh"})
    assert resp.status_code == 422


def test_feedback_missing_photo_404(client):
    resp = client.post("/api/feedback", json={"photo_id": 999, "action": "like"})
    assert resp.status_code == 404


def test_thumbnail_404(client):
    resp = client.get("/api/files/999/thumbnail")
    assert resp.status_code == 404


def test_thumbnail_200_from_cache(client, tmp_path, monkeypatch):
    import sqlite3
    from app import db as db_mod
    from app import imaging

    cache_root = tmp_path / "cache"
    monkeypatch.setattr(imaging, "CACHE_ROOT", str(cache_root))
    db_path = tmp_path / "api_test.db"
    conn = db_mod.connect(db_path)
    conn.execute("INSERT INTO files (path, mtime, size, format) VALUES (?,?,?,?)",
                 (str(tmp_path / "x.jpg"), 1000.0, 10, "jpg"))
    fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    cache_dir = cache_root / "thumbs"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "{}_{}_{}.webp".format(fid, 256, 1000)
    cache_file.write_bytes(b"fake-webp-bytes")

    resp = client.get("/api/files/{}/thumbnail".format(fid))
    assert resp.status_code == 200
    assert resp.content == b"fake-webp-bytes"
    assert resp.headers["content-type"].startswith("image/webp")


def test_root_placeholder(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_get_settings_defaults(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_dirs"] == []
    assert data["phash_threshold"] == 10
    assert data["time_window_sec"] == 60
    assert isinstance(data["rejected_root"], str) and data["rejected_root"]


def test_get_settings_after_scan(client, tmp_path):
    src = str(tmp_path / "photos")
    resp = client.post("/api/scan", json={"source_dirs": [src]})
    assert resp.status_code == 200
    resp = client.get("/api/settings")
    data = resp.json()
    assert data["source_dirs"] == [src]


def test_history_empty(client):
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
