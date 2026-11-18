"""Tests for app.learner — pairwise preference sampling, training, EMA, locking.

Pure stdlib (SQLite + JSON + math only): the learner is deliberately free of
native-library imports so these tests run green inside DSH sessions
(docs/DEVENV.md).

Weight-scale convention used throughout (also documented in app/learner.py):

* A sample's feature vector x_json is the RAW score delta in points (~±40).
* Training internally scales features to x/100; a stored weight of ~1.0 then
  means "one logit-odds step per 100 points of difference" — the same order
  of magnitude as app.recommender.DEFAULT_WEIGHTS, which is what the
  preferences panel shows on its 0–2 bars.
* preference_model.weights_json stores exactly those per-100-point weights.
"""

import json

import pytest

from app.learner import (
    ensure_tables,
    get_current_weights,
    record_decision_samples,
    retrain,
    set_locked,
)
from app.recommender import DEFAULT_WEIGHTS, DIMENSIONS


def mk_group(db, group_id, status="pending"):
    db.execute(
        "INSERT INTO groups (id, status, created_at) VALUES (?, ?, '2026-11-18 10:00:00')",
        (group_id, status),
    )
    return group_id


def mk_photo(db, photo_id, group_id=None, **scores):
    """Insert photos + quality_scores rows for one logical photo.

    ``group_id`` may be None (photo outside any group). Score kwargs not
    passed (or passed as None) stay NULL — the dimension is missing, and the
    learner fills missing dimensions with the neutral value 50.
    """
    db.execute("INSERT INTO photos (id, group_id) VALUES (?, ?)", (photo_id, group_id))
    present = {dim: score for dim, score in scores.items() if score is not None}
    if present:
        columns = ", ".join(present)
        placeholders = ", ".join("?" for _ in present)
        db.execute(
            f"INSERT INTO quality_scores (photo_id, {columns}) VALUES (?, {placeholders})",
            (photo_id, *present.values()),
        )
    return photo_id


def mk_decision(db, group_id, kept_ids):
    return db.execute(
        "INSERT INTO decisions (group_id, kept_photo_ids, decided_at)"
        " VALUES (?, ?, '2026-11-18 11:00:00')",
        (group_id, json.dumps(list(kept_ids))),
    ).lastrowid


def insert_sample(db, x, label, weight=1.0):
    # Hand-crafted samples bypass the public recording API, so create the
    # learner-owned table first via its documented entry point.
    ensure_tables(db)
    db.execute(
        "INSERT INTO preference_samples (x_json, label, weight) VALUES (?, ?, ?)",
        (json.dumps(x), label, weight),
    )


def insert_anti_sharp_pairs(db, pairs, delta=40):
    """Insert mirror-pair samples preferring the LESS sharp photo.

    Each pair is what record_decision_samples writes when a user keeps the
    softer photo: (kept - rejected) sharpness delta = -delta with label 1,
    plus its mirror with label 0.
    """
    for _ in range(pairs):
        insert_sample(db, [-delta, 0, 0, 0, 0], 1)
        insert_sample(db, [delta, 0, 0, 0, 0], 0)


def insert_old_model(db, weight=1.0, version=1):
    db.execute(
        "INSERT INTO preference_model (version, weights_json, bias, sample_count, trained_at)"
        " VALUES (?, ?, 0.0, 0, '2026-11-18 12:00:00')",
        (version, json.dumps({dim: weight for dim in DIMENSIONS})),
    )


def sample_rows(db):
    return db.execute(
        "SELECT decision_id, group_id, x_json, label, weight, created_at"
        " FROM preference_samples ORDER BY id"
    ).fetchall()


def add_feedback(db, photo_id, group_id, action):
    db.execute(
        "INSERT INTO feedback_events (photo_id, action, group_id, created_at)"
        " VALUES (?, ?, ?, '2026-11-18 11:30:00')",
        (photo_id, action, group_id),
    )


# --- record_decision_samples -------------------------------------------------


def test_record_decision_samples_pairwise_rows(db):
    mk_group(db, 7)
    mk_photo(db, 101, group_id=7, sharpness=80, exposure=60)  # faces missing
    mk_photo(db, 102, group_id=7, sharpness=60, exposure=70)
    mk_photo(db, 103, group_id=7, sharpness=50, exposure=55, faces=90)
    decision_id = mk_decision(db, 7, [101, 102])

    n = record_decision_samples(db, 7, [101, 102], decision_id=decision_id)

    assert n == 4  # 2 kept × 1 rejected, both directions
    rows = sample_rows(db)
    got = {(tuple(json.loads(r["x_json"])), r["label"], r["weight"]) for r in rows}
    # Δx = kept − rejected per dimension; missing dims fill with 50.
    assert got == {
        ((30.0, 5.0, 0.0, 0.0, -40.0), 1, 1.0),   # 101 vs 103 (faces 50−90)
        ((10.0, 15.0, 0.0, 0.0, -40.0), 1, 1.0),  # 102 vs 103
        ((-30.0, -5.0, 0.0, 0.0, 40.0), 0, 1.0),  # mirrors, label 0
        ((-10.0, -15.0, 0.0, 0.0, 40.0), 0, 1.0),
    }
    for r in rows:
        assert r["decision_id"] == decision_id
        assert r["group_id"] == 7
        assert r["created_at"]  # timestamp recorded


def test_record_decision_samples_on_decided_group(db):
    # executor.apply_decision marks the group 'decided' BEFORE recording
    # samples, so only not_duplicate is exempt from learning.
    mk_group(db, 8, status="decided")
    mk_photo(db, 111, group_id=8, sharpness=80)
    mk_photo(db, 112, group_id=8, sharpness=60)

    assert record_decision_samples(db, 8, [111]) == 2


def test_record_decision_samples_not_duplicate_returns_zero(db):
    mk_group(db, 9, status="not_duplicate")
    mk_photo(db, 201, group_id=9, sharpness=80, exposure=60)
    mk_photo(db, 202, group_id=9, sharpness=60, exposure=70)

    assert record_decision_samples(db, 9, [201]) == 0
    assert db.execute("SELECT COUNT(*) FROM preference_samples").fetchone()[0] == 0


def test_dislike_and_like_feedback_triple_pair_weight(db):
    mk_group(db, 10)
    kept = mk_photo(db, 301, group_id=10, composition=90, sharpness=55)
    rejected = mk_photo(db, 302, group_id=10, composition=40, sharpness=95)

    add_feedback(db, rejected, 10, "dislike")
    record_decision_samples(db, 10, [kept])
    rows = sample_rows(db)
    assert len(rows) == 2
    for r in rows:  # every pair the disliked photo participates in
        assert r["weight"] == 3.0

    db.execute("DELETE FROM preference_samples")
    add_feedback(db, kept, 10, "like")
    record_decision_samples(db, 10, [kept])
    for r in sample_rows(db):  # 'like' on the kept side amplifies the same way
        assert r["weight"] == 3.0


def test_disliked_photo_in_keep_list_is_treated_as_rejected(db):
    mk_group(db, 11)
    a = mk_photo(db, 401, group_id=11, sharpness=80)
    b = mk_photo(db, 402, group_id=11, sharpness=60)
    add_feedback(db, a, 11, "dislike")  # disliked despite being kept

    n = record_decision_samples(db, 11, [a, b])

    assert n == 2  # only b (kept) × a (forced rejected) pairs
    got = {(tuple(json.loads(r["x_json"])), r["label"], r["weight"]) for r in sample_rows(db)}
    assert got == {
        ((-20.0, 0.0, 0.0, 0.0, 0.0), 1, 3.0),   # b − a, kept side
        ((20.0, 0.0, 0.0, 0.0, 0.0), 0, 3.0),    # a − b, rejected side
    }


# --- training ----------------------------------------------------------------


def test_synthetic_preference_exposure_beats_sharpness(db):
    # 30 decisions: the user always keeps the high-exposure photo while
    # sharpness varies ±6 uncorrelated with the choice.
    for i in range(30):
        group_id = 100 + i
        mk_group(db, group_id)
        d = (i * 7) % 13 - 6  # −6..6, cycles with mean 0
        kept = mk_photo(db, 1000 + 2 * i, group_id=group_id, exposure=90, sharpness=55 + d)
        rejected = mk_photo(db, 1001 + 2 * i, group_id=group_id, exposure=50, sharpness=55 - d)
        decision_id = mk_decision(db, group_id, [kept])
        record_decision_samples(db, group_id, [kept], decision_id=decision_id)

    result = retrain(db)

    assert result["sample_count"] == 60
    assert result["version"] == 1
    assert result["weights"]["exposure"] > result["weights"]["sharpness"] + 1.0
    assert result["weights"]["exposure"] > 1.0
    assert result["final_loss"] >= 0.0
    assert result["iterations"] > 0


def test_ema_few_samples_stay_between_old_and_new(db):
    # Old model: every weight 1.0. Five strong anti-sharpness decision pairs
    # (10 rows) pull w_sharpness hard negative; with only a handful of
    # samples the EMA blend must stay strictly between old (1.0) and the
    # new strongly-negative weight, i.e. inside (0, 1).
    insert_old_model(db, weight=1.0)
    insert_anti_sharp_pairs(db, 5)

    result = retrain(db)

    assert result["version"] == 2
    w = result["weights"]
    assert 0 < w["sharpness"] < 1
    assert w["sharpness"] < w["exposure"]  # only sharpness is being un-learned


def test_ema_many_samples_track_new_weights(db):
    # 200 sample rows: EMA now trusts the data, w_sharpness falls below 0.3.
    insert_old_model(db, weight=1.0)
    insert_anti_sharp_pairs(db, 100)  # 100 pairs → 200 rows

    result = retrain(db)

    assert result["sample_count"] == 200
    assert result["weights"]["sharpness"] < 0.3


def test_dislike_pushes_composition_over_sharpness(db):
    # The disliked photo is razor sharp (95) but badly composed (40); the
    # kept one is softer (55) but well composed (90). The 3×-weighted
    # dislike pairs must teach composition > sharpness.
    for i in range(10):
        group_id = 200 + i
        mk_group(db, group_id)
        kept = mk_photo(db, 2000 + 2 * i, group_id=group_id, sharpness=55, composition=90)
        rejected = mk_photo(db, 2001 + 2 * i, group_id=group_id, sharpness=95, composition=40)
        add_feedback(db, rejected, group_id, "dislike")
        record_decision_samples(db, group_id, [kept])

    result = retrain(db)

    w = result["weights"]
    assert w["composition"] > w["sharpness"] + 0.5
    assert w["composition"] > 0.5
    assert w["sharpness"] < 0


def test_training_tolerates_huge_feature_deltas(db):
    # |z| > 30 is clamped inside the sigmoid so extreme score deltas cannot
    # overflow math.exp during training.
    insert_sample(db, [10000, -10000, 0, 0, 0], 1)
    insert_sample(db, [-10000, 10000, 0, 0, 0], 0)

    result = retrain(db)

    assert result["weights"]["sharpness"] > 0


# --- locking ---------------------------------------------------------------


def test_locked_dimension_is_frozen_across_retrain(db):
    # Phase 1: 15 groups teach a positive faces preference.
    for i in range(15):
        group_id = 300 + i
        mk_group(db, group_id)
        kept = mk_photo(db, 3000 + 2 * i, group_id=group_id, faces=70)
        rejected = mk_photo(db, 3001 + 2 * i, group_id=group_id, faces=40)
        record_decision_samples(db, group_id, [kept])
    v1 = retrain(db)
    assert v1["weights"]["faces"] > 0.5

    set_locked(db, ["faces"])

    # Phase 2: 15 more groups now teach the OPPOSITE (kept faces=20 vs
    # rejected faces=95) — but faces is locked, so its weight must not move.
    for i in range(15):
        group_id = 400 + i
        mk_group(db, group_id)
        kept = mk_photo(db, 4000 + 2 * i, group_id=group_id, faces=20)
        rejected = mk_photo(db, 4001 + 2 * i, group_id=group_id, faces=95)
        record_decision_samples(db, group_id, [kept])
    v2 = retrain(db)

    assert v2["version"] == v1["version"] + 1
    assert v2["weights"]["faces"] == pytest.approx(v1["weights"]["faces"])
    assert get_current_weights(db)["locked"] == ["faces"]

    # Control: once unlocked, the strongly anti-faces cumulative data DOES
    # move the faces weight down — proving the lock was doing the freezing.
    set_locked(db, [])
    v3 = retrain(db)
    assert v3["weights"]["faces"] < v1["weights"]["faces"]
    assert get_current_weights(db)["locked"] == []


def test_set_locked_rejects_unknown_dimension(db):
    with pytest.raises(ValueError, match="bogus"):
        set_locked(db, ["faces", "bogus"])
    # validation happened before any write
    assert get_current_weights(db)["locked"] == []


# --- model access -----------------------------------------------------------


def test_get_current_weights_without_model_returns_defaults(db):
    got = get_current_weights(db)

    assert got["weights"] == DEFAULT_WEIGHTS
    assert got["bias"] == 0.0
    assert got["sample_count"] == 0
    assert got["version"] == 0
    assert got["locked"] == []


def test_get_current_weights_reflects_training(db):
    insert_anti_sharp_pairs(db, 3)

    result = retrain(db)
    got = get_current_weights(db)

    assert got["version"] == result["version"] == 1
    assert got["weights"] == result["weights"]
    assert got["bias"] == pytest.approx(result["bias"])
    assert got["sample_count"] == 6
    assert got["locked"] == []


def test_empty_sample_retrain_returns_current_model_unchanged(db):
    # No samples, no model yet: version stays 0, defaults, nothing written.
    result = retrain(db)
    assert result["version"] == 0
    assert result["weights"] == DEFAULT_WEIGHTS
    assert result["sample_count"] == 0
    assert db.execute("SELECT COUNT(*) FROM preference_model").fetchone()[0] == 0

    # With an existing model: nothing new to learn → same version and weights,
    # still a single model row.
    insert_old_model(db, weight=1.0)
    result = retrain(db)
    assert result["version"] == 1
    assert result["weights"] == {dim: 1.0 for dim in DIMENSIONS}
    assert result["sample_count"] == 0
    assert db.execute("SELECT COUNT(*) FROM preference_model").fetchone()[0] == 1
