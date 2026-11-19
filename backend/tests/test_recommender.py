"""Tests for app.recommender — ranking, group recommendation, and reason text.

Pure stdlib (SQLite rows only, no image decoding), so these run green inside
DSH sessions; see docs/DEVENV.md.
"""

import pytest

from app.recommender import (
    DEFAULT_WEIGHTS,
    DIMENSION_LABELS,
    rank_photos,
    recommend_group,
    recompute_all,
)

UNIFORM_WEIGHTS = {
    "sharpness": 1.0,
    "exposure": 1.0,
    "noise": 1.0,
    "composition": 1.0,
    "faces": 1.0,
}


def mk_group(db, group_id, status="pending"):
    db.execute(
        "INSERT INTO groups (id, status, created_at) VALUES (?, ?, '2026-11-18 10:00:00')",
        (group_id, status),
    )
    return group_id


def mk_photo(db, photo_id, format="jpg", group_id=None, path=None,
             captured_at=None, camera=None, **scores):
    """Insert files + photos + quality_scores rows for one logical photo.

    photos/files reference each other, so insert the photo with a NULL
    primary_file_id first, then the file, then link them (foreign_keys=ON).
    Score kwargs not passed (or passed as None) stay NULL — the dimension is
    missing. With no scores at all, no quality_scores row is inserted.
    The primary file gets id photo_id * 10 + 1 so primary-file ordering is
    deterministic across tests. ``path``/``captured_at``/``camera`` feed the
    management-value bonuses (EXIF presence, filename regularity); the
    default path is an opaque long id so legacy totals stay bonus-free.
    """
    db.execute(
        "INSERT INTO photos (id, primary_file_id, group_id) VALUES (?, NULL, ?)",
        (photo_id, group_id),
    )
    file_id = photo_id * 10 + 1
    db.execute(
        "INSERT INTO files (id, path, format, photo_id, captured_at, camera)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (file_id,
         path or f"/photos/4554893{photo_id}5876129.jpg",
         format, photo_id, captured_at, camera),
    )
    db.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (file_id, photo_id))
    present = {dim: score for dim, score in scores.items() if score is not None}
    if present:
        columns = ", ".join(present)
        placeholders = ", ".join("?" for _ in present)
        db.execute(
            f"INSERT INTO quality_scores (photo_id, {columns}) VALUES (?, {placeholders})",
            (photo_id, *present.values()),
        )
    return photo_id


def test_default_weights_and_labels():
    assert DEFAULT_WEIGHTS == {
        "sharpness": 1.3,
        "exposure": 1.0,
        "noise": 0.8,
        "composition": 0.9,
        "faces": 1.2,
    }
    assert DIMENSION_LABELS == {
        "sharpness": "清晰度",
        "exposure": "曝光",
        "noise": "噪点控制",
        "composition": "构图水平",
        "faces": "人物状态",
    }


# --- management-value bonuses (EXIF + filename regularity) -------------------

def test_filename_regularity_patterns():
    from app.recommender import _filename_regularity

    assert _filename_regularity("/x/2019-05-13 103817.jpg") == 1.0
    assert _filename_regularity("/x/20190513_103817.jpg") == 1.0
    assert _filename_regularity("/x/IMG_1234.JPG") == 0.9
    assert _filename_regularity("/x/4554893058087652885.JPG") == 0.0
    assert _filename_regularity("/x/mmexport1673456789.jpeg") == 0.0
    assert _filename_regularity("/x/a3f9b2c1d4e5f6789012abcd.jpg") == 0.0


def test_identical_photos_prefer_exif_and_regular_filename(db):
    # User scenario: pixel-identical shots, equal quality scores. The copy
    # with EXIF + a dated filename must be recommended, and the reason must
    # explain both.
    mk_group(db, 901)
    mk_photo(db, 1, group_id=901,
             path="/lib/2019-05-13 103817.jpg",
             captured_at="2019-05-13T10:38:17", camera="HUAWEI ALN-AL80",
             sharpness=80.0, exposure=80.0, noise=80.0)
    mk_photo(db, 2, group_id=901,
             path="/lib/4554893058087652885.JPG",
             sharpness=80.0, exposure=80.0, noise=80.0)

    result = recommend_group(db, 901)

    assert result["recommended_photo_id"] == 1
    assert "拍摄信息" in result["reason"]
    assert "文件名" in result["reason"]
    entry = result["ranked"][0]
    assert entry["exif_bonus"] == 1.2
    assert entry["filename_bonus"] == 1.0
    assert result["ranked"][1]["exif_bonus"] == 0.0
    assert result["ranked"][1]["filename_bonus"] == 0.0


def test_bonus_does_not_override_real_quality_gap(db):
    # A 5-point quality gap (92 vs 87) must beat the maximum 2.2 bonus.
    mk_group(db, 902)
    mk_photo(db, 3, group_id=902,
             path="/lib/4554893058087652885.JPG",
             sharpness=92.0, exposure=92.0, noise=92.0)
    mk_photo(db, 4, group_id=902,
             path="/lib/2019-05-13 103817.jpg",
             captured_at="2019-05-13T10:38:17", camera="iPhone 15 Pro",
             sharpness=87.0, exposure=87.0, noise=87.0)

    result = recommend_group(db, 902)

    assert result["recommended_photo_id"] == 3
    assert "文件名" not in result["reason"]


def test_bonus_tips_sub_two_point_tie(db):
    # Within noise (85 vs 86), management value wins — that is exactly the
    # pixel-identical re-export case.
    mk_group(db, 903)
    mk_photo(db, 5, group_id=903,
             path="/lib/2019-05-13 103817.jpg",
             captured_at="2019-05-13T10:38:17", camera="HUAWEI ALN-AL80",
             sharpness=85.0, exposure=85.0, noise=85.0)
    mk_photo(db, 6, group_id=903,
             path="/lib/4554893058087652885.JPG",
             sharpness=86.0, exposure=86.0, noise=86.0)

    result = recommend_group(db, 903)

    assert result["recommended_photo_id"] == 5


def test_time_only_exif_beats_no_exif(db):
    mk_group(db, 904)
    mk_photo(db, 7, group_id=904,
             path="/lib/IMG_4471.JPG",
             captured_at="2019-05-13T10:38:17",
             sharpness=70.0)
    mk_photo(db, 8, group_id=904,
             path="/lib/4554893058087652885.JPG",
             sharpness=70.0)

    ranked = rank_photos(db, [7, 8])

    assert ranked[0]["photo_id"] == 7
    assert ranked[0]["exif_bonus"] == 0.6


def test_uniform_weights_recommend_sharper_photo(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=92, exposure=80)
    mk_photo(db, 20, group_id=1, sharpness=78, exposure=80)

    result = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    assert result["recommended_photo_id"] == 10
    reason = result["reason"]
    assert "清晰度" in reason
    assert "92" in reason
    assert "78" in reason
    assert "推荐" in reason


def test_raw_tie_break_and_reason_mentions_raw(db):
    mk_group(db, 1)
    # photo 10 is a RAW+JPG pair (primary = RAW file); photo 20 is plain JPG.
    # Identical quality_scores → tie → RAW primary wins.
    mk_photo(db, 10, format="raw", group_id=1, sharpness=80, exposure=80)
    db.execute(
        "INSERT INTO files (id, path, format, photo_id) VALUES (102, '/photos/p10.jpg', 'jpg', 10)"
    )
    mk_photo(db, 20, format="jpg", group_id=1, sharpness=80, exposure=80)

    result = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    assert result["recommended_photo_id"] == 10
    rec = result["ranked"][0]
    assert rec["is_raw"] is True
    assert rec["group_id"] == 1
    fmt = db.execute(
        "SELECT format FROM files WHERE id = ?", (rec["primary_file_id"],)
    ).fetchone()[0]
    assert fmt == "raw"
    assert "RAW" in result["reason"]
    assert "底片" in result["reason"]


def test_neutral_reason_when_scores_close(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=90, exposure=80)
    mk_photo(db, 20, group_id=1, sharpness=89, exposure=81)

    result = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    assert result["recommended_photo_id"] == 10  # tie on 85.0, stable order
    assert "接近" in result["reason"]
    assert " vs " not in result["reason"]


def test_weight_change_flips_recommendation(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=90, exposure=60)
    mk_photo(db, 20, group_id=1, sharpness=60, exposure=90)

    # uniform: 75.0 vs 75.0 tie → stable order picks photo 10
    first = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)
    assert first["recommended_photo_id"] == 10

    second = recommend_group(db, 1, weights={"exposure": 3.0, "sharpness": 1.0})
    assert second["recommended_photo_id"] == 20
    stored = db.execute(
        "SELECT recommended_photo_id FROM groups WHERE id = 1"
    ).fetchone()[0]
    assert stored == 20


def test_missing_faces_dimension_is_renormalized(db):
    # faces=None on one side must neither punish it nor credit the rival:
    # each photo's total is the weighted mean over ITS scored dimensions only.
    mk_photo(db, 10, sharpness=80, exposure=80)                 # no faces value
    mk_photo(db, 20, sharpness=80, exposure=80, faces=50)       # mediocre faces

    ranked = rank_photos(db, [10, 20], weights=UNIFORM_WEIGHTS)

    assert ranked[0]["photo_id"] == 10
    assert ranked[0]["scores"] == {"sharpness": 80.0, "exposure": 80.0}
    assert ranked[0]["total"] == pytest.approx(80.0)  # (80+80)/(1+1)
    assert ranked[1]["total"] == pytest.approx(70.0)  # (80+80+50)/(1+1+1)

    # an equal faces value must not break an otherwise tied pair either
    mk_photo(db, 30, sharpness=80, exposure=80)
    mk_photo(db, 40, sharpness=80, exposure=80, faces=80)
    tied = rank_photos(db, [30, 40], weights=UNIFORM_WEIGHTS)
    assert tied[0]["total"] == pytest.approx(80.0)
    assert tied[1]["total"] == pytest.approx(80.0)
    assert tied[0]["photo_id"] == 30  # stable tie-break: smaller primary_file_id


def test_photo_without_scores_ranks_last(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=92, exposure=80)
    mk_photo(db, 20, group_id=1)  # no quality_scores row at all

    result = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    assert result["recommended_photo_id"] == 10
    assert result["ranked"][-1]["photo_id"] == 20
    assert result["ranked"][-1]["total"] is None


def test_missing_group_raises_keyerror(db):
    with pytest.raises(KeyError):
        recommend_group(db, 999)


def test_recommend_group_writes_recommended_photo_id(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=92, exposure=80)
    mk_photo(db, 20, group_id=1, sharpness=78, exposure=80)

    recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    stored = db.execute(
        "SELECT recommended_photo_id FROM groups WHERE id = 1"
    ).fetchone()[0]
    assert stored == 10


def test_recompute_all_only_touches_pending_groups(db):
    mk_group(db, 1, status="pending")
    mk_photo(db, 10, group_id=1, sharpness=95, exposure=90)
    mk_photo(db, 20, group_id=1, sharpness=50, exposure=50)

    mk_group(db, 2, status="decided")
    mk_photo(db, 30, group_id=2, sharpness=95, exposure=90)
    mk_photo(db, 40, group_id=2, sharpness=50, exposure=50)
    db.execute("UPDATE groups SET recommended_photo_id = 40 WHERE id = 2")  # stale pick

    results = recompute_all(db, weights=UNIFORM_WEIGHTS)

    assert [r["recommended_photo_id"] for r in results] == [10]
    assert db.execute(
        "SELECT recommended_photo_id FROM groups WHERE id = 1"
    ).fetchone()[0] == 10
    # decided group untouched: stale recommendation kept
    assert db.execute(
        "SELECT recommended_photo_id FROM groups WHERE id = 2"
    ).fetchone()[0] == 40

    # but recommend_group itself is callable on a decided group
    result = recommend_group(db, 2, weights=UNIFORM_WEIGHTS)
    assert result["recommended_photo_id"] == 30
    assert db.execute(
        "SELECT recommended_photo_id FROM groups WHERE id = 2"
    ).fetchone()[0] == 30


def test_reason_lists_only_dims_with_delta_ge_10(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=90, noise=90)
    mk_photo(db, 20, group_id=1, sharpness=79, noise=85)

    reason = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)["reason"]

    assert "清晰度" in reason  # Δ = 11 ≥ 10 → listed
    assert "90 vs 79" in reason
    assert "领先 11" in reason
    assert "噪点" not in reason  # Δ = 5 < 10 → not listed
    assert "85" not in reason


def test_reason_lists_at_most_two_advantage_dims(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=95, exposure=95, noise=95)
    mk_photo(db, 20, group_id=1, sharpness=75, exposure=75, noise=75)

    reason = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)["reason"]

    assert "清晰度" in reason
    assert "曝光" in reason
    assert "噪点" not in reason  # third Δ-qualified dim is dropped


def test_single_photo_group_recommends_without_comparison(db):
    mk_group(db, 1)
    mk_photo(db, 10, group_id=1, sharpness=92, exposure=80)

    result = recommend_group(db, 1, weights=UNIFORM_WEIGHTS)

    assert result["recommended_photo_id"] == 10
    assert result["ranked"][0]["total"] == pytest.approx(86.0)
    assert result["reason"]  # some sensible single-photo reason
    assert " vs " not in result["reason"]
