"""Screenshot detection and bulk rejection tests."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_mod          # noqa: E402
from app import executor, screenshots  # noqa: E402


# --- classify (pure) -----------------------------------------------------------


def test_exact_screen_size_is_screenshot():
    ok, why = screenshots._classify("IMG_001.PNG", "png", None, (1170, 2532))
    assert ok and "1170" in why


def test_rotated_screen_size_counts():
    ok, _ = screenshots._classify("IMG_002.PNG", "png", None, (2778, 1284))
    assert ok


def test_screenshot_prefix_by_name():
    ok, why = screenshots._classify(
        "Screenshot_2026-10-31-09-12-33.png", "png", None, (1080, 2340))
    assert ok and "Screenshot_" in why


def test_png_no_camera_phone_ratio_band():
    ok, why = screenshots._classify("WX_20260901.png", "png", None, (1080, 2340))
    assert ok


def test_camera_photo_not_screenshot():
    ok, _ = screenshots._classify(
        "IMG_5207.HEIC", "heic", "Apple iPhone", (4032, 3024))
    assert not ok


def test_small_png_not_screenshot():
    ok, _ = screenshots._classify("icon.png", "png", None, (64, 64))
    assert not ok


def test_weird_ratio_png_not_screenshot():
    ok, _ = screenshots._classify("pano.png", "png", None, (4000, 1000))
    assert not ok


# --- analysis (needs real images -> PIL) ----------------------------------------


pytest.importorskip("PIL", reason="PIL unavailable in this env")
pytest.importorskip("PIL._imaging", reason="native imaging missing")


def _mk_png(path, w, h):
    from PIL import Image
    Image.new("RGB", (w, h), "#3366aa").save(path, "PNG")


@pytest.fixture()
def sconn(tmp_path):
    conn = db_mod.connect(str(tmp_path / "t.db"))
    db_mod.init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES"
        " ('source_dirs', ?)", (json.dumps([str(tmp_path / "src")]),))
    (tmp_path / "src").mkdir()
    return conn, tmp_path


def _add_file(conn, src, name, fmt, camera=None, captured=None):
    """Insert db rows only; the caller creates the file first."""
    path = str(src / name)
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, format, camera, captured_at)"
        " VALUES (?, 1.0, ?, ?, ?, ?)",
        (path, os.path.getsize(path), fmt, camera, captured))
    fid = cur.lastrowid
    cur = conn.execute("INSERT INTO photos (primary_file_id) VALUES (?)", (fid,))
    pid = cur.lastrowid
    conn.execute("UPDATE files SET photo_id = ? WHERE id = ?", (pid, fid))
    conn.commit()
    return pid, fid, path


def test_ensure_analyzed_classifies_by_real_size(sconn):
    conn, tmp = sconn
    src = tmp / "src"
    _mk_png(src / "shot.png", 1170, 2532)
    pid, fid, path = _add_file(conn, src, "shot.png", "png")
    conn.commit()

    report = screenshots.ensure_analyzed(conn)

    assert report["found"] == 1
    assert conn.execute(
        "SELECT is_screenshot FROM photos WHERE id = ?", (pid,)).fetchone()[0] == 1
    # size cached on the row, second run does no work
    assert tuple(conn.execute(
        "SELECT width, height FROM files WHERE id = ?", (fid,)).fetchone()) == (1170, 2532)
    again = screenshots.ensure_analyzed(conn)
    assert again == {"scanned": 0, "found": 0}


def test_list_screenshots_paging_and_when(sconn):
    conn, tmp = sconn
    src = tmp / "src"
    _mk_png(src / "a.png", 1170, 2532)
    _mk_png(src / "b.png", 1080, 2340)
    pid_a, fid_a, _ = _add_file(
        conn, src, "a.png", "png", captured="2025-06-01T10:00:00")
    _add_file(conn, src, "b.png", "png", captured="2024-01-01T10:00:00")
    conn.commit()

    data = screenshots.list_screenshots(conn)
    assert data["total"] == 2
    assert data["stats"]["screenshots"] == 2

    only_2025 = screenshots.list_screenshots(conn, when="2025")
    assert only_2025["total"] == 1
    assert only_2025["items"][0]["photo_id"] == pid_a

    older = screenshots.list_screenshots(conn, when="older")
    assert older["total"] == 1
    assert older["items"][0]["name"] == "b.png"

    with pytest.raises(ValueError):
        screenshots.list_screenshots(conn, when="bogus")


def test_reject_screenshots_moves_files(sconn, monkeypatch):
    conn, tmp = sconn
    src = tmp / "src"
    _mk_png(src / "shot.png", 1170, 2532)
    pid, fid, path = _add_file(conn, src, "shot.png", "png")
    conn.commit()
    screenshots.ensure_analyzed(conn)

    rejected = tmp / "rej"
    monkeypatch.setattr(executor, "rejected_dir_for",
                        lambda c, p: str(rejected))
    result = screenshots.reject_screenshots(conn, [pid])

    assert result["moved_files"] == 1
    assert not os.path.exists(path)
    moved = list(rejected.rglob("shot.png"))
    assert moved
    # decision recorded as a user decision, undoable as a group
    row = conn.execute(
        "SELECT source, group_id FROM decisions WHERE id = ?",
        (result["decision_id"],)).fetchone()
    assert row["source"] == "user"
    restored = executor.undo_group(conn, result["group_id"])
    assert restored >= 1
    assert os.path.exists(path)


def test_reject_rejects_non_screenshots(sconn):
    conn, tmp = sconn
    src = tmp / "src"
    from PIL import Image
    Image.new("RGB", (4032, 3024), "#aa3333").save(src / "photo.jpg", "JPEG")
    pid, _, _ = _add_file(conn, src, "photo.jpg", "jpg", camera="Canon")
    conn.commit()
    screenshots.ensure_analyzed(conn)
    assert conn.execute(
        "SELECT is_screenshot FROM photos WHERE id = ?", (pid,)).fetchone()[0] == 0
    with pytest.raises(ValueError):
        screenshots.reject_screenshots(conn, [pid])
    with pytest.raises(ValueError):
        screenshots.reject_screenshots(conn, [])
