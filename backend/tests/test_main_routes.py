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
    # scattered mode: no explicit rejected_root, no single root path
    assert data["rejected_root"] == ""


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


def test_pipeline_run_returns_job_id_immediately(client, tmp_path):
    # pipeline runs in a background JobManager thread; the endpoint must
    # return {job_id} fast (never block the HTTP request for minutes).
    import json as _json

    src = tmp_path / "lib"
    src.mkdir()
    (src / "IMG_1.JPG").write_bytes(b"fake-1")
    from app.db import connect, init_db
    from app.config import update_settings

    db_path = tmp_path / "api_test.db"
    conn = connect(db_path)
    init_db(conn)
    update_settings(conn, source_dirs=[str(src)])
    conn.close()

    resp = client.post("/api/pipeline/run")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, int)

    # job shows up in the list and reports its kind
    lst = client.get("/api/jobs").json()["jobs"]
    assert any(j["id"] == job_id and j["kind"] == "pipeline" for j in lst)


# --- filesystem browsing endpoint ------------------------------------------------

def test_fs_list_returns_directories(client, tmp_path):
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub2").mkdir()
    r = client.get("/api/fs/list", params={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(tmp_path)
    assert [d["name"] for d in body["dirs"]] == ["sub1", "sub2"]
    assert body["home"]
    assert "主目录" in body["shortcuts"]


def test_fs_list_default_path_is_home(client):
    r = client.get("/api/fs/list")
    assert r.status_code == 200
    assert r.json()["path"] == r.json()["home"]


def test_fs_list_missing_path_is_422(client, tmp_path):
    r = client.get(
        "/api/fs/list", params={"path": str(tmp_path / "nope")})
    assert r.status_code == 422
    assert "不存在" in r.json()["detail"]


def test_put_settings_toggles_auto_resolve(client):
    r = client.put("/api/settings", json={"auto_resolve_identical": False})
    assert r.status_code == 200
    assert r.json()["auto_resolve_identical"] is False
    # persisted for the next GET
    assert client.get("/api/settings").json()["auto_resolve_identical"] \
        is False
    # and back on
    r = client.put("/api/settings", json={"auto_resolve_identical": True})
    assert r.json()["auto_resolve_identical"] is True


def test_put_settings_rejects_unknown_key(client):
    r = client.put("/api/settings", json={"foo": True})
    assert r.status_code == 422


def test_put_settings_rejects_non_bool(client):
    r = client.put("/api/settings", json={"auto_resolve_identical": "yes"})
    assert r.status_code == 422
