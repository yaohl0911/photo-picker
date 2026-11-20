# -*- coding: utf-8 -*-
"""Service-layer tests for app.api (pure Python, DSH-safe).

These tests cover the JSON API business logic without importing fastapi.
The HTTP layer (app.main) has its own smoke tests in test_main_routes.py
which skip entirely when fastapi cannot be imported.
"""
import json
import os

import pytest

from conftest import make_fake_file

from app import api


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _seed_file_row(conn, path, fmt, photo_id, captured_at=None, exif=None):
    st = path.stat()
    conn.execute(
        "INSERT INTO files (path, mtime, size, format, photo_id, captured_at, exif_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, fmt, photo_id, captured_at,
         json.dumps(exif) if exif else None),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_photo(conn, photo_id, group_id, paths_with_fmt, scores,
                captured_at=None, exif=None):
    """paths_with_fmt: list of (Path, format). First is the primary file."""
    conn.execute(
        "INSERT INTO photos (id, primary_file_id, group_id) VALUES (?, NULL, ?)",
        (photo_id, group_id),
    )
    primary_file_id = None
    for path, fmt in paths_with_fmt:
        fid = _seed_file_row(conn, path, fmt, photo_id,
                             captured_at=captured_at, exif=exif)
        if primary_file_id is None:
            primary_file_id = fid
    conn.execute(
        "UPDATE photos SET primary_file_id = ? WHERE id = ?",
        (primary_file_id, photo_id),
    )
    conn.execute(
        "INSERT INTO quality_scores (photo_id, sharpness, exposure, noise,"
        " composition, faces) VALUES (?,?,?,?,?,?)",
        (photo_id, scores.get("sharpness"), scores.get("exposure"),
         scores.get("noise"), scores.get("composition"), scores.get("faces")),
    )
    return photo_id


@pytest.fixture
def scene(db, tmp_path):
    """Two pending groups over real (fake) files on disk.

    Group 1 (id 1) pending, 3 photos, photo 1 is a RAW+JPG pair:
        photo1: a/IMG_1.CR3 + a/IMG_1.JPG  sharp 92 / expo 80 / noise 75 / comp 70
        photo2: a/IMG_2.JPG                sharp 78 / expo 82 / noise 74 / comp 71
        photo3: a/IMG_3.JPG                sharp 60 / expo 61 / noise 62 / comp 60
        default weights -> recommend photo1 (80.65 > 76.6 > 60.65)
    Group 2 (id 2) pending, 2 photos:
        photo4: b/IMG_4.JPG sharp 90 / expo 55 / noise 60 / comp 60
        photo5: b/IMG_5.JPG sharp 70 / expo 95 / noise 60 / comp 60
        default weights -> recommend photo5 (72.0 vs 68.5)
        custom {sharpness: 3.0} -> photo4 (74.9 vs 71.4)
    """
    src_root = tmp_path / "library"
    (src_root / "a").mkdir(parents=True)
    (src_root / "b").mkdir(parents=True)
    conn = db
    from app.config import update_settings
    update_settings(conn, source_dirs=[str(src_root)],
                    rejected_root=str(tmp_path / "rejected"))

    paths = {
        "cr3_1": make_fake_file(src_root / "a", "IMG_1.CR3", content=b"fake-cr3-1"),
        "jpg_1": make_fake_file(src_root / "a", "IMG_1.JPG", content=b"fake-jpg-1"),
        "jpg_2": make_fake_file(src_root / "a", "IMG_2.JPG", content=b"fake-jpg-2"),
        "jpg_3": make_fake_file(src_root / "a", "IMG_3.JPG", content=b"fake-jpg-3"),
        "jpg_4": make_fake_file(src_root / "b", "IMG_4.JPG", content=b"fake-jpg-4"),
        "jpg_5": make_fake_file(src_root / "b", "IMG_5.JPG", content=b"fake-jpg-5"),
    }
    conn.execute("INSERT INTO groups (id, status) VALUES (1, 'pending')")
    conn.execute("INSERT INTO groups (id, status) VALUES (2, 'pending')")
    _seed_photo(conn, 1, 1, [(paths["cr3_1"], "cr3"), (paths["jpg_1"], "jpg")],
                {"sharpness": 92, "exposure": 80, "noise": 75, "composition": 70,
                 "faces": None},
                captured_at="2026-11-01T10:00:00",
                exif={"Make": "Canon", "Model": "EOS R5"})
    _seed_photo(conn, 2, 1, [(paths["jpg_2"], "jpg")],
                {"sharpness": 78, "exposure": 82, "noise": 74, "composition": 71,
                 "faces": None},
                captured_at="2026-11-01T10:00:01", exif={"Make": "Canon"})
    _seed_photo(conn, 3, 1, [(paths["jpg_3"], "jpg")],
                {"sharpness": 60, "exposure": 61, "noise": 62, "composition": 60,
                 "faces": None},
                captured_at="2026-11-01T10:00:02")
    _seed_photo(conn, 4, 2, [(paths["jpg_4"], "jpg")],
                {"sharpness": 90, "exposure": 55, "noise": 60, "composition": 60,
                 "faces": None},
                captured_at="2026-11-02T09:00:00")
    _seed_photo(conn, 5, 2, [(paths["jpg_5"], "jpg")],
                {"sharpness": 70, "exposure": 95, "noise": 60, "composition": 60,
                 "faces": None},
                captured_at="2026-11-02T09:00:01")
    conn.commit()
    return {
        "conn": conn, "src_root": src_root,
        "rejected_root": tmp_path / "rejected",
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# get_overview
# ---------------------------------------------------------------------------

def test_get_overview_counts(scene):
    conn = scene["conn"]
    data = api.get_overview(conn)
    assert data["total_photos"] == 5
    assert data["total_files"] == 6
    assert data["pending_groups"] == 2
    assert data["decided_groups"] == 0
    assert data["not_duplicate_groups"] == 0
    assert data["decisions_count"] == 0
    assert data["samples"] == 0
    assert data["model_version"] == 0
    assert data["last_scan_at"] is None
    assert data["locked_dims"] == []


def test_get_overview_after_activity(db):
    conn = db
    conn.execute("INSERT INTO groups (id, status) VALUES (1, 'decided')")
    conn.execute("INSERT INTO groups (id, status) VALUES (2, 'not_duplicate')")
    conn.execute(
        "INSERT INTO decisions (group_id, kept_photo_ids, decided_at, applied_at)"
        " VALUES (1, '[1]', '2026-11-18T10:00:00', '2026-11-18T10:00:01')")
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at)"
        " VALUES ('2026-11-18T09:00:00', '2026-11-18T09:05:00')")
    conn.commit()
    data = api.get_overview(conn)
    assert data["decided_groups"] == 1
    assert data["not_duplicate_groups"] == 1
    assert data["decisions_count"] == 1
    assert data["last_scan_at"] == "2026-11-18T09:05:00"


# ---------------------------------------------------------------------------
# list_groups
# ---------------------------------------------------------------------------

def test_list_groups_pending_with_recommendation(scene):
    data = api.list_groups(scene["conn"])
    assert data["total"] == 2
    assert data["offset"] == 0
    assert data["limit"] == 50
    by_id = {g["group_id"]: g for g in data["groups"]}
    assert set(by_id) == {1, 2}
    g1 = by_id[1]
    assert g1["status"] == "pending"
    assert g1["recommended_photo_id"] == 1
    assert g1["reason"]
    assert "清晰度" in g1["reason"]
    assert g1["photo_count"] == 3
    assert len(g1["photos"]) == 3
    p1 = g1["photos"][0]
    assert p1["photo_id"] == 1
    assert p1["primary_file_id"] is not None
    assert p1["is_raw"] is True
    assert p1["thumb_url"] == "/api/files/{}/thumbnail".format(p1["primary_file_id"])
    assert p1["scores"]["sharpness"] == 92
    g2 = by_id[2]
    assert g2["recommended_photo_id"] == 5
    assert g2["photo_count"] == 2


def test_list_groups_status_filter_and_empty(db):
    data = api.list_groups(db, status="decided")
    assert data["groups"] == []
    assert data["total"] == 0
    with pytest.raises(ValueError):
        api.list_groups(db, status="bogus")


def test_list_groups_member_limit_and_offset(scene):
    conn = scene["conn"]
    conn.execute("INSERT INTO groups (id, status) VALUES (3, 'pending')")
    for i in range(6, 10):
        _seed_photo(conn, i, 3, [(make_fake_file(scene["src_root"], "IMG_%d.JPG" % i,
                                                 content=b"x-%d" % i), "jpg")],
                    {"sharpness": 50, "exposure": 50, "noise": 50,
                     "composition": 50, "faces": None})
    conn.commit()
    data = api.list_groups(conn)
    by_id = {g["group_id"]: g for g in data["groups"]}
    assert by_id[3]["photo_count"] == 4
    assert len(by_id[3]["photos"]) == 4  # capped at 4
    data = api.list_groups(conn, offset=1, limit=1)
    assert len(data["groups"]) == 1
    assert data["total"] == 3
    with pytest.raises(ValueError):
        api.list_groups(conn, limit=0)
    with pytest.raises(ValueError):
        api.list_groups(conn, offset=-1)


def test_list_groups_decided_filter(scene):
    conn = scene["conn"]
    api.decide_group(conn, 1, [1], learn=False)
    data = api.list_groups(conn, status="decided")
    assert data["total"] == 1
    assert data["groups"][0]["group_id"] == 1
    assert data["groups"][0]["recommended_photo_id"] == 1
    assert data["groups"][0]["photo_count"] == 3


# ---------------------------------------------------------------------------
# get_group
# ---------------------------------------------------------------------------

def test_get_group_detail(scene):
    data = api.get_group(scene["conn"], 1)
    assert data["group_id"] == 1
    assert data["status"] == "pending"
    assert data["recommended_photo_id"] == 1
    assert data["ranked"] == [1, 2, 3]
    photos = {p["photo_id"]: p for p in data["photos"]}
    assert len(photos) == 3
    p1 = photos[1]
    assert p1["is_recommended"] is True
    assert p1["captured_at"] == "2026-11-01T10:00:00"
    assert p1["exif"] == {"Make": "Canon", "Model": "EOS R5"}
    assert p1["scores"]["sharpness"] == 92
    assert p1["scores"]["total"] == pytest.approx(80.65, abs=0.3)
    assert len(p1["files"]) == 2
    fmts = {f["format"] for f in p1["files"]}
    assert fmts == {"cr3", "jpg"}
    assert p1["files"][0]["file_id"] == p1["primary_file_id"]
    assert p1["files"][0]["path"].endswith("IMG_1.CR3")
    assert p1["is_raw"] is True
    p2 = photos[2]
    assert p2["is_recommended"] is False
    assert p2["exif"] == {"Make": "Canon"}
    p3 = photos[3]
    assert p3["exif"] is None
    assert 5 not in photos  # group 2 photo not leaked


def test_get_group_missing(scene):
    with pytest.raises(KeyError):
        api.get_group(scene["conn"], 999)


# ---------------------------------------------------------------------------
# decide_group
# ---------------------------------------------------------------------------

def test_decide_group_full_integration(scene):
    conn = scene["conn"]
    rejected = scene["rejected_root"]
    result = api.decide_group(conn, 1, [1], learn=True)
    # decision row + group status
    assert result["decision_id"] == 1
    assert result["moved_files"] == 2
    assert result["model"]["version"] == 1
    assert result["model"]["sample_count"] == 4
    row = conn.execute("SELECT status, recommended_photo_id FROM groups WHERE id=1").fetchone()
    assert row["status"] == "decided"
    assert row["recommended_photo_id"] == 1
    dec = conn.execute("SELECT kept_photo_ids, applied_at FROM decisions WHERE id=1").fetchone()
    assert json.loads(dec["kept_photo_ids"]) == [1]
    assert dec["applied_at"]
    # files physically moved
    assert (rejected / "a" / "IMG_2.JPG").exists()
    assert (rejected / "a" / "IMG_3.JPG").exists()
    assert not scene["paths"]["jpg_2"].exists()
    # recompute refreshed group 2's recommendation with the new model
    # weights: the trained model (sharpness-dominant after keeping the
    # sharp photo) flips group 2 from photo5 (exposure-strong, the
    # default-weight winner) to photo4 (sharpness-strong).
    g2 = api.get_group(conn, 2)
    assert g2["recommended_photo_id"] == 4
    from app.learner import get_current_weights
    from app.recommender import rank_photos
    w = get_current_weights(conn)["weights"]
    expected = rank_photos(conn, [4, 5], weights=w)[0]["photo_id"]
    assert expected == 4
    assert g2["recommended_photo_id"] == expected


def test_decide_group_without_learn(scene):
    conn = scene["conn"]
    result = api.decide_group(conn, 1, [2], learn=False)
    # keeping photo2 rejects photo1 (CR3 + JPG = 2 files) and photo3 (1 file)
    assert result["moved_files"] == 3
    assert result["model"]["version"] == 0
    assert result["model"]["sample_count"] == 0
    # decided group persists the kept photo as its recommendation
    row = conn.execute(
        "SELECT status, recommended_photo_id FROM groups WHERE id=1").fetchone()
    assert row["status"] == "decided"
    assert row["recommended_photo_id"] == 2


def test_decide_group_photo_not_in_group(scene):
    with pytest.raises(ValueError):
        api.decide_group(scene["conn"], 1, [1, 5])


def test_decide_group_missing_group(scene):
    with pytest.raises(KeyError):
        api.decide_group(scene["conn"], 999, [1])


# ---------------------------------------------------------------------------
# dismiss / undo
# ---------------------------------------------------------------------------

def test_dismiss_group_api(scene):
    conn = scene["conn"]
    result = api.dismiss_group_api(conn, 2)
    assert result == {"ok": True, "status": "not_duplicate"}
    row = conn.execute("SELECT status FROM groups WHERE id=2").fetchone()
    assert row["status"] == "not_duplicate"
    data = api.list_groups(conn, status="not_duplicate")
    assert data["total"] == 1
    assert data["groups"][0]["group_id"] == 2


def test_dismiss_group_api_missing(scene):
    with pytest.raises(KeyError):
        api.dismiss_group_api(scene["conn"], 999)


def test_undo_group_api(scene):
    conn = scene["conn"]
    api.decide_group(conn, 1, [1], learn=True)
    result = api.undo_group_api(conn, 1)
    assert result == {"restored_files": 2}
    assert scene["paths"]["jpg_2"].exists()
    assert not (scene["rejected_root"] / "a" / "IMG_2.JPG").exists()
    row = conn.execute("SELECT status FROM groups WHERE id=1").fetchone()
    assert row["status"] == "pending"
    hist = api.list_history(conn)
    assert hist["total"] == 1
    assert hist["decisions"][0]["undone"] is True


def test_undo_group_api_missing(scene):
    with pytest.raises(KeyError):
        api.undo_group_api(scene["conn"], 999)


# ---------------------------------------------------------------------------
# add_feedback
# ---------------------------------------------------------------------------

def test_add_feedback_like_dislike(scene):
    conn = scene["conn"]
    result = api.add_feedback(conn, 2, "like")
    assert result == {"ok": True}
    result = api.add_feedback(conn, 3, "dislike")
    assert result == {"ok": True}
    rows = conn.execute(
        "SELECT photo_id, action, group_id FROM feedback_events ORDER BY id").fetchall()
    assert [(r["photo_id"], r["action"], r["group_id"]) for r in rows] == \
        [(2, "like", 1), (3, "dislike", 1)]
    # no immediate retrain
    model = conn.execute("SELECT COUNT(*) AS c FROM preference_model").fetchone()
    assert model["c"] == 0


def test_add_feedback_invalid_action(scene):
    with pytest.raises(ValueError):
        api.add_feedback(scene["conn"], 1, "meh")


def test_add_feedback_missing_photo(scene):
    with pytest.raises(KeyError):
        api.add_feedback(scene["conn"], 999, "like")


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------

def test_get_preferences_default(scene):
    prefs = api.get_preferences(scene["conn"])
    assert prefs["version"] == 0
    assert prefs["sample_count"] == 0
    assert prefs["locked"] == []
    assert prefs["weights"] == {
        "sharpness": 1.3, "exposure": 1.0, "noise": 0.8,
        "composition": 0.9, "faces": 1.2}
    assert prefs["default_weights"] == prefs["weights"]
    assert prefs["custom_weights"] == {}
    assert prefs["effective_weights"] == prefs["weights"]
    assert prefs["labels"]["sharpness"] == "清晰度"
    assert set(prefs["labels"]) == {"sharpness", "exposure", "noise",
                                    "composition", "faces"}
    assert prefs["explanation"]


def test_set_preference_weights_and_effect(scene):
    conn = scene["conn"]
    prefs = api.set_preference_weights(conn, {"sharpness": 3.0})
    assert prefs["custom_weights"] == {"sharpness": 3.0}
    assert prefs["effective_weights"]["sharpness"] == 3.0
    assert prefs["effective_weights"]["exposure"] == 1.0  # untouched dims default
    # recommendation flips: photo5 -> photo4
    data = api.list_groups(conn)
    by_id = {g["group_id"]: g for g in data["groups"]}
    assert by_id[2]["recommended_photo_id"] == 4


def test_set_preference_weights_partial_and_zero(db):
    conn = db
    api.set_preference_weights(conn, {"sharpness": 0.1, "faces": 0})
    prefs = api.get_preferences(conn)
    assert prefs["effective_weights"]["sharpness"] == 0.1
    assert prefs["effective_weights"]["faces"] == 0
    assert prefs["effective_weights"]["noise"] == 0.8


def test_set_preference_weights_invalid(db):
    conn = db
    with pytest.raises(ValueError):
        api.set_preference_weights(conn, {"bogus": 1.0})
    with pytest.raises(ValueError):
        api.set_preference_weights(conn, {"sharpness": "high"})
    with pytest.raises(ValueError):
        api.set_preference_weights(conn, {"sharpness": True})


def test_effective_weights_after_training(scene):
    conn = scene["conn"]
    api.decide_group(conn, 1, [1], learn=True)
    # model trained: base becomes model weights
    prefs = api.get_preferences(conn)
    assert prefs["version"] == 1
    assert prefs["weights"] != prefs["default_weights"]
    # custom overrides model on a single dim
    api.set_preference_weights(conn, {"faces": 0.5})
    prefs = api.get_preferences(conn)
    assert prefs["effective_weights"]["faces"] == 0.5
    assert prefs["effective_weights"]["sharpness"] == prefs["weights"]["sharpness"]


# ---------------------------------------------------------------------------
# train_now / set_locked_api
# ---------------------------------------------------------------------------

def test_train_now_empty(db):
    result = api.train_now(db)
    assert result["model"]["version"] == 0
    assert result["recomputed"] == 0


def test_train_now_after_samples(scene):
    conn = scene["conn"]
    api.decide_group(conn, 1, [1], learn=True)  # trains to version 1
    result = api.train_now(conn)
    assert result["model"]["version"] == 2
    assert result["recomputed"] == 1  # group 2 still pending


def test_set_locked_api(scene):
    conn = scene["conn"]
    result = api.set_locked_api(conn, ["sharpness"])
    assert result["ok"] is True
    assert result["locked"] == ["sharpness"]
    with pytest.raises(ValueError):
        api.set_locked_api(conn, ["bogus"])


# ---------------------------------------------------------------------------
# list_history
# ---------------------------------------------------------------------------

def test_list_history(db, tmp_path):
    conn = db
    make_fake_file(tmp_path, "f.jpg", content=b"f")
    conn.execute("INSERT INTO groups (id, status) VALUES (1, 'decided')")
    _seed_photo(conn, 1, 1, [(tmp_path / "f.jpg", "jpg")],
                {"sharpness": 50, "exposure": 50, "noise": 50,
                 "composition": 50, "faces": None})
    conn.execute(
        "INSERT INTO decisions (id, group_id, kept_photo_ids, decided_at, applied_at)"
        " VALUES (1, 1, '[1]', '2026-11-18T10:00:00', '2026-11-18T10:00:01')")
    conn.execute(
        "INSERT INTO move_log (decision_id, file_id, src_path, dst_path, undone)"
        " VALUES (1, 1, 'src', 'dst', 0)")
    conn.execute(
        "INSERT INTO move_log (decision_id, file_id, src_path, dst_path, undone)"
        " VALUES (1, 1, 'src', 'dst', 1)")
    conn.commit()
    hist = api.list_history(conn)
    assert hist["total"] == 1
    entry = hist["decisions"][0]
    assert entry["decision_id"] == 1
    assert entry["group_id"] == 1
    assert entry["group_status"] == "decided"
    assert entry["kept_photo_ids"] == [1]
    assert entry["decided_at"] == "2026-11-18T10:00:00"
    assert entry["applied_at"] == "2026-11-18T10:00:01"
    assert entry["moved_files"] == 2
    assert entry["undone"] is False


def test_list_history_validation(db):
    with pytest.raises(ValueError):
        api.list_history(db, limit=0)
    with pytest.raises(ValueError):
        api.list_history(db, offset=-5)


# ---------------------------------------------------------------------------
# get_settings
# ---------------------------------------------------------------------------

def test_get_settings_defaults(db):
    data = api.get_settings(db)
    assert data["source_dirs"] == []
    # empty rejected_root = scattered per-source mode (no single root)
    assert data["rejected_root"] == ""
    assert data["phash_threshold"] == 10
    assert data["time_window_sec"] == 60


def test_get_settings_after_update(db, tmp_path):
    from app.config import update_settings
    conn = db
    update_settings(conn, source_dirs=[str(tmp_path / "lib")],
                    rejected_root=str(tmp_path / "rej"))
    data = api.get_settings(conn)
    assert data["source_dirs"] == [os.path.abspath(tmp_path / "lib")]
    assert data["rejected_root"] == os.path.abspath(tmp_path / "rej")


def test_get_settings_types_and_derived_rejected_root(db, tmp_path):
    from app.config import update_settings
    conn = db
    update_settings(conn, source_dirs=[str(tmp_path / "photos"), str(tmp_path / "raw")])
    data = api.get_settings(conn)
    assert isinstance(data["source_dirs"], list)
    assert all(isinstance(d, str) for d in data["source_dirs"])
    assert isinstance(data["rejected_root"], str)
    # 无显式设置：分散模式——每个照片文件夹内一个 _photo_picker_rejected
    assert data["rejected_root"] == ""
    assert isinstance(data["phash_threshold"], int)
    assert isinstance(data["time_window_sec"], int)


# ---------------------------------------------------------------------------
# run_full_pipeline_blocking
# ---------------------------------------------------------------------------

def test_run_full_pipeline_blocking(db, tmp_path):
    conn = db
    src = tmp_path / "lib"
    src.mkdir()
    for i in range(3):
        make_fake_file(src, "IMG_%d.JPG" % i, content=b"fake-%d" % i)
    from app.config import update_settings
    update_settings(conn, source_dirs=[str(src)],
                    rejected_root=str(tmp_path / "rejected"))
    report = api.run_full_pipeline_blocking(conn)
    # fake images: hashed 0, errors 3 (decode fails in DSH and user terminal)
    assert report["scan"]["total"] == 3
    assert report["scan"]["new"] == 3
    assert report["hashing"]["hashed"] == 0
    assert len(report["hashing"]["errors"]) == 3
    assert report["grouping"]["groups"] == 0
    assert "analyzed" in report["analysis"]
    assert "recalculated" in report["recommend"]
    # scan recorded
    assert api.get_overview(conn)["last_scan_at"] is not None
    # no crash, all phases present (auto stage added by auto-resolve)
    assert set(report) == {"scan", "hashing", "grouping", "analysis",
                           "recommend", "auto"}


def test_run_full_pipeline_blocking_reports_all_five_stages(db, tmp_path):
    """progress_cb must fire for every stage (scan/hash/group/analysis/
    recommend/auto) with positional (phase, done, total) args — the UI's
    per-stage progress list depends on it."""
    conn = db
    src = tmp_path / "lib"
    src.mkdir()
    for i in range(3):
        make_fake_file(src, "IMG_%d.JPG" % i, content=b"fake-%d" % i)
    from app.config import update_settings
    update_settings(conn, source_dirs=[str(src)],
                    rejected_root=str(tmp_path / "rejected"))

    calls = []
    cb = lambda *a, **kw: calls.append(a if a else tuple(kw.values()))
    api.run_full_pipeline_blocking(conn, progress_cb=cb)

    phases = {c[0] for c in calls}
    assert phases == {"scan", "hash", "group", "analysis", "recommend",
                      "auto"}, phases
    # every stage reports progress within 1..total, and finishes at total
    by_phase = {}
    for c in calls:
        by_phase.setdefault(c[0], []).append(c)
    for phase, entries in by_phase.items():
        # (group may legitimately be 0/0 when nothing was hashed yet)
        assert all(0 <= e[1] <= e[2] and e[2] >= 0 for e in entries), (phase, entries)
        assert entries[-1][1] == entries[-1][2], (phase, entries[-1])


def test_decide_group_zero_keep_rejects_whole_group(scene):
    # 保留 0 张 = 整组淘汰：所有文件移动、组 decided、无偏好样本、
    # recommended_photo_id 不被改写（无保留者）
    conn = scene["conn"]
    rejected = scene["rejected_root"]
    before = conn.execute(
        "SELECT recommended_photo_id FROM groups WHERE id=2"
    ).fetchone()["recommended_photo_id"]

    result = api.decide_group(conn, 2, [], learn=True)

    assert result["moved_files"] == 2
    assert result["model"]["sample_count"] == 0
    row = conn.execute("SELECT status, recommended_photo_id FROM groups WHERE id=2").fetchone()
    assert row["status"] == "decided"
    assert row["recommended_photo_id"] == before  # 空保留集不写推荐
    dec = conn.execute(
        "SELECT kept_photo_ids FROM decisions WHERE group_id=2"
    ).fetchone()
    assert json.loads(dec["kept_photo_ids"]) == []
    # both photos of group 2 physically moved
    for p in scene["paths"].values():
        if "IMG_4" in str(p) or "IMG_5" in str(p):
            assert not p.exists()


# --- filesystem browsing (directory picker) ------------------------------------

class TestListDirs:
    def test_lists_only_subdirectories_sorted(self, db, tmp_path):
        (tmp_path / "b_dir").mkdir()
        (tmp_path / "a_dir").mkdir()
        (tmp_path / "z_dir").mkdir()
        (tmp_path / "note.txt").write_text("not a dir")

        res = api.list_dirs(db, str(tmp_path))

        assert res["path"] == str(tmp_path)
        assert [d["name"] for d in res["dirs"]] == ["a_dir", "b_dir", "z_dir"]
        assert res["parent"] is not None

    def test_parent_of_root_is_none(self, db):
        res = api.list_dirs(db, "/")
        assert res["parent"] is None
        assert isinstance(res["dirs"], list)

    def test_shortcuts_and_home_present(self, db):
        res = api.list_dirs(db, "~")
        assert res["path"] == res["home"]
        assert "下载" in res["shortcuts"]
        assert res["shortcuts"]["主目录"] == res["home"]

    def test_missing_path_raises_value_error(self, db, tmp_path):
        with pytest.raises(ValueError):
            api.list_dirs(db, str(tmp_path / "nope"))

    def test_file_path_raises_value_error(self, db, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError):
            api.list_dirs(db, str(f))


# ---------------------------------------------------------------------------
# decide_group / list_history count companion videos in moved_files
# ---------------------------------------------------------------------------

def test_decide_group_moved_files_counts_live_photo_videos(scene):
    conn = scene["conn"]
    rejected = scene["rejected_root"]
    # IMG_2.JPG is rejected by keeping photo 1; give it a Live Photo MOV
    video = scene["src_root"] / "a" / "IMG_2.MOV"
    video.write_bytes(b"live-video")

    result = api.decide_group(conn, 1, [1], learn=False)

    # 2 moved photo files + 1 companion video
    assert result["moved_files"] == 3
    assert (rejected / "a" / "IMG_2.MOV").exists()

    history = api.list_history(conn)
    assert history["decisions"][0]["moved_files"] == 3
