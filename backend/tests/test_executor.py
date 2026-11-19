"""Tests for app.executor: safe move of rejected photos and undo.

Pure stdlib throughout (shutil/os/sqlite3/json/datetime), so the whole file
runs inside DSH. Learning integration is exercised via monkeypatching the
``_record_and_train`` seam and a fake ``app.learner`` module, so these tests
never depend on the real learner.py existing.
"""

import json
import os
import sys
import types
from datetime import datetime

import pytest

from conftest import make_fake_file

from app import executor
from app.config import update_settings

_FORMATS = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".cr3": "raw", ".dng": "raw"}


def _fmt(rel_path):
    return _FORMATS.get(os.path.splitext(rel_path)[1].lower(), "jpg")


def _now():
    return datetime.now().isoformat(timespec="seconds")


@pytest.fixture()
def env(db, tmp_path):
    """A source tree wired into settings, plus the rejected root next to it."""
    src_root = tmp_path / "library"
    (src_root / "a" / "b").mkdir(parents=True)
    rejected_root = tmp_path / "rejected"
    update_settings(
        db, source_dirs=[str(src_root)], rejected_root=str(rejected_root)
    )
    return {"src_root": src_root, "rejected_root": rejected_root}


def mk_group(db, src_root, photo_specs):
    """Create one pending group whose photos have real files under src_root.

    photo_specs: list of photos; each photo is a list of relative file paths,
    e.g. ``[["a/IMG_1.CR3", "a/IMG_1.JPG"], ["a/IMG_2.JPG"]]``. Every path
    becomes a files row (first one is the primary asset) backed by a fake
    file on disk with per-path unique content.
    Returns (group_id, [photo_id, ...]).
    """
    group_id = db.execute(
        "INSERT INTO groups (status, created_at) VALUES ('pending', ?)", (_now(),)
    ).lastrowid
    photo_ids = []
    for rel_paths in photo_specs:
        file_ids = []
        for rel in rel_paths:
            (src_root / rel).parent.mkdir(parents=True, exist_ok=True)
            path = make_fake_file(src_root, rel, content=f"content-of-{rel}".encode())
            cur = db.execute(
                "INSERT INTO files (path, format, mtime, size) VALUES (?, ?, ?, ?)",
                (str(path), _fmt(rel), path.stat().st_mtime, path.stat().st_size),
            )
            file_ids.append(cur.lastrowid)
        photo_id = db.execute(
            "INSERT INTO photos (primary_file_id, group_id) VALUES (?, ?)",
            (file_ids[0], group_id),
        ).lastrowid
        db.executemany(
            "UPDATE files SET photo_id = ? WHERE id = ?",
            [(photo_id, fid) for fid in file_ids],
        )
        photo_ids.append(photo_id)
    db.commit()
    return group_id, photo_ids


# --- default rejected root -------------------------------------------------


def test_default_rejected_root_without_explicit_setting_is_scattered(db):
    # No explicit setting: rejected photos scatter into per-source
    # folders — there is no single root, so this reports "".
    update_settings(db, source_dirs=["/photos/2026", "/photos/2025"])
    assert executor.default_rejected_root(db) == ""


def test_default_rejected_root_without_source_dirs(db):
    assert executor.default_rejected_root(db) == ""


def test_default_rejected_root_prefers_explicit_setting(db):
    update_settings(
        db, source_dirs=["/photos/2026"], rejected_root="/tmp/custom_rejected"
    )
    assert executor.default_rejected_root(db) == os.path.abspath(
        "/tmp/custom_rejected"
    )


def test_rejected_dir_for_scatters_per_source_dir(db):
    update_settings(db, source_dirs=["/photos/2026", "/photos/2025"])
    assert executor.rejected_dir_for(db, "/photos/2026/trip/a.jpg") == os.path.abspath(
        "/photos/2026/_photo_picker_rejected"
    )
    assert executor.rejected_dir_for(db, "/photos/2025/x/b.jpg") == os.path.abspath(
        "/photos/2025/_photo_picker_rejected"
    )
    # outside every source dir -> cwd/data fallback
    assert executor.rejected_dir_for(db, "/elsewhere/c.jpg") == os.path.abspath(
        os.path.join("data", "_photo_picker_rejected")
    )


def test_rejected_dir_for_explicit_setting_is_central(db):
    update_settings(
        db, source_dirs=["/photos/2026", "/photos/2025"],
        rejected_root="/tmp/custom_rejected",
    )
    for src in ("/photos/2026/a.jpg", "/photos/2025/b.jpg"):
        assert executor.rejected_dir_for(db, src) == os.path.abspath(
            "/tmp/custom_rejected"
        )


def test_apply_decision_scatters_rejects_into_own_source(db, tmp_path):
    # Two sources, one photo in each: rejected copies must land in their
    # OWN source's _photo_picker_rejected — never mixed into the other.
    src_a = tmp_path / "srcA"
    src_b = tmp_path / "srcB"
    src_a.mkdir()
    src_b.mkdir()
    update_settings(db, source_dirs=[str(src_a), str(src_b)], rejected_root="")

    gid_a, photos_a = mk_group(db, src_a, [["IMG_1.JPG"], ["IMG_2.JPG"]])
    gid_b, photos_b = mk_group(db, src_b, [["IMG_1.JPG"], ["IMG_2.JPG"]])

    executor.apply_decision(db, gid_a, keep_photo_ids=[photos_a[0]])
    executor.apply_decision(db, gid_b, keep_photo_ids=[photos_b[0]])

    assert (src_a / "_photo_picker_rejected" / "IMG_2.JPG").exists()
    assert (src_b / "_photo_picker_rejected" / "IMG_2.JPG").exists()
    # each folder holds ONLY its own rejects
    assert sorted(p.name for p in (src_a / "_photo_picker_rejected").iterdir()) == ["IMG_2.JPG"]
    assert sorted(p.name for p in (src_b / "_photo_picker_rejected").iterdir()) == ["IMG_2.JPG"]
    # no centralized folder appeared anywhere
    assert not (tmp_path / "_photo_picker_rejected").exists()


# --- apply_decision --------------------------------------------------------


def test_apply_decision_moves_raw_and_jpg_of_unkept_photo(db, env):
    group_id, photo_ids = mk_group(
        db,
        env["src_root"],
        [["a/IMG_1.CR3", "a/IMG_1.JPG"], ["a/IMG_2.CR3", "a/IMG_2.JPG"]],
    )
    kept, rejected = photo_ids

    decision_id = executor.apply_decision(db, group_id, [kept])

    assert isinstance(decision_id, int)
    # both assets of the rejected photo moved, directory layout kept
    for rel in ("a/IMG_2.CR3", "a/IMG_2.JPG"):
        assert not (env["src_root"] / rel).exists()
        dst = env["rejected_root"] / rel
        assert dst.exists()
        assert dst.read_bytes() == f"content-of-{rel}".encode()
    # kept photo untouched
    assert (env["src_root"] / "a/IMG_1.CR3").exists()
    assert (env["src_root"] / "a/IMG_1.JPG").exists()
    # files.path now points at the destinations
    paths = [
        r["path"]
        for r in db.execute("SELECT path FROM files WHERE photo_id = ?", (rejected,))
    ]
    assert sorted(paths) == sorted(
        [
            str(env["rejected_root"] / "a/IMG_2.CR3"),
            str(env["rejected_root"] / "a/IMG_2.JPG"),
        ]
    )
    # move_log: 2 rows for this decision, all live
    log = db.execute(
        "SELECT src_path, dst_path, undone FROM move_log WHERE decision_id = ?",
        (decision_id,),
    ).fetchall()
    assert len(log) == 2
    assert all(r["undone"] == 0 for r in log)
    # decision + group state recorded
    row = db.execute(
        "SELECT * FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["group_id"] == group_id
    assert json.loads(row["kept_photo_ids"]) == [kept]
    assert row["decided_at"] is not None
    assert row["applied_at"] is not None
    status = db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"]
    assert status == "decided"


def test_apply_decision_keep_all_moves_nothing(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )

    executor.apply_decision(db, group_id, photo_ids)

    assert db.execute("SELECT COUNT(*) c FROM move_log").fetchone()["c"] == 0
    for rel in ("a/IMG_1.JPG", "a/IMG_2.JPG"):
        assert (env["src_root"] / rel).exists()
    assert not env["rejected_root"].exists()
    status = db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"]
    assert status == "decided"


def test_apply_decision_preserves_nested_subdirectory(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/b/IMG_2.JPG"]]
    )

    executor.apply_decision(db, group_id, [photo_ids[0]])

    dst = env["rejected_root"] / "a" / "b" / "IMG_2.JPG"
    assert dst.exists()
    assert dst.read_bytes() == b"content-of-a/b/IMG_2.JPG"
    assert not (env["src_root"] / "a" / "b" / "IMG_2.JPG").exists()


def test_apply_decision_conflict_gets_numeric_suffix(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )
    # pre-place a different file at the destination name
    (env["rejected_root"] / "a").mkdir(parents=True)
    (env["rejected_root"] / "a" / "IMG_2.JPG").write_bytes(b"pre-existing-original")

    executor.apply_decision(db, group_id, [photo_ids[0]])

    # original target untouched; the source landed beside it with a _1 suffix
    assert (
        env["rejected_root"] / "a" / "IMG_2.JPG"
    ).read_bytes() == b"pre-existing-original"
    suffixed = env["rejected_root"] / "a" / "IMG_2_1.JPG"
    assert suffixed.read_bytes() == b"content-of-a/IMG_2.JPG"
    row = db.execute(
        "SELECT path FROM files WHERE photo_id = ?", (photo_ids[1],)
    ).fetchone()
    assert row["path"] == str(suffixed)


def test_apply_decision_file_outside_source_dirs_uses_basename(db, env, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    group_id, photo_ids = mk_group(db, env["src_root"], [["a/IMG_1.JPG"]])
    stray = make_fake_file(outside, "stray.JPG", content=b"stray-content")
    file_id = db.execute(
        "INSERT INTO files (path, format) VALUES (?, 'jpg')", (str(stray),)
    ).lastrowid
    photo_id = db.execute(
        "INSERT INTO photos (primary_file_id, group_id) VALUES (?, ?)",
        (file_id, group_id),
    ).lastrowid
    db.execute("UPDATE files SET photo_id = ? WHERE id = ?", (photo_id, file_id))
    db.commit()

    executor.apply_decision(db, group_id, [photo_ids[0]])

    dst = env["rejected_root"] / "stray.JPG"
    assert dst.exists()
    assert dst.read_bytes() == b"stray-content"
    assert not stray.exists()


def test_apply_decision_continues_when_one_file_fails(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.CR3", "a/IMG_2.JPG"]]
    )
    # the RAW asset vanished from disk (external deletion)
    os.remove(env["src_root"] / "a" / "IMG_2.CR3")

    decision_id = executor.apply_decision(db, group_id, [photo_ids[0]])

    # the healthy JPG still moved and is the only move_log row
    assert (env["rejected_root"] / "a" / "IMG_2.JPG").exists()
    assert not (env["src_root"] / "a" / "IMG_2.JPG").exists()
    rows = db.execute(
        "SELECT dst_path FROM move_log WHERE decision_id = ?", (decision_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["dst_path"] == str(env["rejected_root"] / "a" / "IMG_2.JPG")
    # the failure is recorded in scan_runs under phase='execute'
    errors = db.execute(
        "SELECT error FROM scan_runs WHERE phase = 'execute' AND error IS NOT NULL"
    ).fetchall()
    assert len(errors) == 1
    assert "IMG_2.CR3" in errors[0]["error"]
    # decision still applied, missing file's row left untouched
    applied_at = db.execute(
        "SELECT applied_at FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()["applied_at"]
    assert applied_at is not None
    raw_path = db.execute(
        "SELECT path FROM files WHERE photo_id = ? AND format = 'raw'",
        (photo_ids[1],),
    ).fetchone()["path"]
    assert raw_path == str(env["src_root"] / "a" / "IMG_2.CR3")


def test_apply_decision_unknown_group_raises_keyerror(db):
    with pytest.raises(KeyError):
        executor.apply_decision(db, 999, [1])


def test_apply_decision_rejects_photo_outside_group(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )
    _, other_photos = mk_group(db, env["src_root"], [["a/OTHER.JPG"]])

    with pytest.raises(ValueError):
        executor.apply_decision(db, group_id, photo_ids + other_photos)

    # validation happened before any write
    assert db.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM move_log").fetchone()["c"] == 0
    status = db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"]
    assert status == "pending"


# --- learning seam ---------------------------------------------------------


def test_apply_decision_learns_when_enabled(db, env, monkeypatch):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )
    calls = []
    monkeypatch.setattr(
        executor,
        "_record_and_train",
        lambda conn, gid, kept, did: calls.append((gid, list(kept), did)),
    )

    decision_id = executor.apply_decision(db, group_id, [photo_ids[0]], learn=True)

    assert calls == [(group_id, [photo_ids[0]], decision_id)]
    # learning happens, and the move still happened
    assert (env["rejected_root"] / "a" / "IMG_2.JPG").exists()


def test_apply_decision_no_learning_when_disabled(db, env, monkeypatch):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )
    calls = []
    monkeypatch.setattr(
        executor, "_record_and_train", lambda *args: calls.append(args)
    )

    executor.apply_decision(db, group_id, [photo_ids[0]], learn=False)

    assert calls == []


def test_record_and_train_delegates_to_learner(db, monkeypatch):
    seen = {}
    fake = types.ModuleType("app.learner")

    def record_decision_samples(conn, group_id, kept_ids, decision_id=None):
        seen["samples"] = (group_id, list(kept_ids), decision_id)
        return 1

    def retrain(conn):
        seen["retrain"] = conn

    fake.record_decision_samples = record_decision_samples
    fake.retrain = retrain
    monkeypatch.setitem(sys.modules, "app.learner", fake)
    monkeypatch.setattr("app.learner", fake, raising=False)

    executor._record_and_train(db, 7, [11], decision_id=42)

    assert seen["samples"] == (7, [11], 42)
    assert seen["retrain"] is db


def test_record_and_train_silent_when_learner_missing(db, monkeypatch):
    import app as app_pkg

    # simulate "app.learner is not installed" no matter what is on disk
    monkeypatch.setitem(sys.modules, "app.learner", None)
    monkeypatch.delattr(app_pkg, "learner", raising=False)

    # must not raise even though the learner is unavailable
    assert executor._record_and_train(db, 1, [1], 1) is None


# --- undo ------------------------------------------------------------------


def test_undo_group_restores_everything(db, env):
    group_id, photo_ids = mk_group(
        db,
        env["src_root"],
        [["a/IMG_1.CR3", "a/IMG_1.JPG"], ["a/IMG_2.CR3", "a/IMG_2.JPG"]],
    )
    decision_id = executor.apply_decision(db, group_id, [photo_ids[0]])

    restored = executor.undo_group(db, group_id)

    assert restored == 2
    for rel in ("a/IMG_2.CR3", "a/IMG_2.JPG"):
        src = env["src_root"] / rel
        dst = env["rejected_root"] / rel
        assert src.exists()
        assert src.read_bytes() == f"content-of-{rel}".encode()
        assert not dst.exists()
    undone = db.execute(
        "SELECT COUNT(*) c FROM move_log WHERE decision_id = ? AND undone = 1",
        (decision_id,),
    ).fetchone()["c"]
    assert undone == 2
    status = db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"]
    assert status == "pending"
    applied_at = db.execute(
        "SELECT applied_at FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()["applied_at"]
    assert applied_at is None
    paths = [
        r["path"]
        for r in db.execute("SELECT path FROM files WHERE photo_id = ?", (photo_ids[1],))
    ]
    assert sorted(paths) == sorted(
        [
            str(env["src_root"] / "a/IMG_2.CR3"),
            str(env["src_root"] / "a/IMG_2.JPG"),
        ]
    )


def test_undo_group_without_decision_is_noop(db, env):
    group_id, _ = mk_group(db, env["src_root"], [["a/IMG_1.JPG"]])

    assert executor.undo_group(db, group_id) == 0


def test_undo_dismiss_flips_group_back_to_pending(db, env):
    from app.grouper import dismiss_group

    group_id, _ = mk_group(db, env["src_root"], [["a/IMG_1.JPG"]])
    dismiss_group(db, group_id)
    assert db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"] == "not_duplicate"

    restored = executor.undo_group(db, group_id)

    assert restored == 0  # nothing was moved
    assert db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"] == "pending"


def test_undo_legacy_dismiss_without_decision_row_still_works(db, env):
    # dismissals made before decision rows existed left no trace — undo
    # must still flip a not_duplicate group back to pending
    group_id, _ = mk_group(db, env["src_root"], [["a/IMG_1.JPG"]])
    db.execute(
        "UPDATE groups SET status = 'not_duplicate' WHERE id = ?", (group_id,)
    )
    db.commit()

    assert executor.undo_group(db, group_id) == 0
    assert db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"] == "pending"


def test_undo_group_suffixes_when_source_occupied(db, env):
    group_id, photo_ids = mk_group(
        db, env["src_root"], [["a/IMG_1.JPG"], ["a/IMG_2.JPG"]]
    )
    executor.apply_decision(db, group_id, [photo_ids[0]])
    # a brand-new photo arrives at the original location before undo
    make_fake_file(env["src_root"], "a/IMG_2.JPG", content=b"new-arrival")

    restored = executor.undo_group(db, group_id)

    assert restored == 1
    assert (env["src_root"] / "a" / "IMG_2.JPG").read_bytes() == b"new-arrival"
    back = env["src_root"] / "a" / "IMG_2_1.JPG"
    assert back.read_bytes() == b"content-of-a/IMG_2.JPG"
    row = db.execute(
        "SELECT path, undone FROM move_log JOIN files ON files.id = move_log.file_id "
        "WHERE files.photo_id = ?",
        (photo_ids[1],),
    ).fetchone()
    assert row["undone"] == 1
    assert db.execute(
        "SELECT path FROM files WHERE photo_id = ?", (photo_ids[1],)
    ).fetchone()["path"] == str(back)


def test_apply_decision_zero_keep_rejects_whole_group(db, env):
    # 保留 0 张：整组淘汰（全部文件进淘汰目录），可撤销
    group_id, photo_ids = mk_group(
        db,
        env["src_root"],
        [["a/IMG_1.JPG"], ["a/IMG_2.CR3", "a/IMG_2.JPG"]],
    )

    decision_id = executor.apply_decision(db, group_id, [], learn=True)

    assert isinstance(decision_id, int)
    # every file of every photo moved
    for rel in ("a/IMG_1.JPG", "a/IMG_2.CR3", "a/IMG_2.JPG"):
        assert not (env["src_root"] / rel).exists()
        assert (env["rejected_root"] / rel).exists()
    # decision row records the empty keep list; group is decided
    kept_json = db.execute(
        "SELECT kept_photo_ids FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()["kept_photo_ids"]
    assert json.loads(kept_json) == []
    assert db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"] == "decided"
    # no kept photo → no preference pairs recorded
    n = db.execute(
        "SELECT COUNT(*) FROM preference_samples"
    ).fetchone()[0]
    assert n == 0
    # undo brings everything back
    restored = executor.undo_group(db, group_id)
    assert restored == 3
    for rel in ("a/IMG_1.JPG", "a/IMG_2.CR3", "a/IMG_2.JPG"):
        assert (env["src_root"] / rel).exists()
    assert db.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["status"] == "pending"
