"""Recommend the best photo of each duplicate group, with a Chinese reason.

Pure Python (stdlib only): no image decoding happens here, so the module is
importable and fully testable inside DSH sessions (docs/DEVENV.md).

Scoring (docs/superpowers/specs/2026-11-18-photo-dedup-design.md §5):

    total = Σ(dim_score × w_dim) / Σ(w_dim)   over the photo's scored dims

A dimension missing (NULL column, or no quality_scores row at all) drops out
of both the numerator and the denominator — the weights renormalize, so a
photo without a faces score is neither rewarded nor punished for it. A photo
with no scores at all gets total=None and always sorts last.

Ranking ties break deterministically: RAW primary first, then the smaller
primary_file_id.
"""

import sqlite3

#: All quality dimensions, in canonical order.
DIMENSIONS = ("sharpness", "exposure", "noise", "composition", "faces")

#: Photography-commonsense prior: sharp focus and faces matter most.
DEFAULT_WEIGHTS = {
    "sharpness": 1.3,
    "exposure": 1.0,
    "noise": 0.8,
    "composition": 0.9,
    "faces": 1.2,
}

#: Chinese labels used when composing the recommendation reason.
DIMENSION_LABELS = {
    "sharpness": "清晰度",
    "exposure": "曝光",
    "noise": "噪点控制",
    "composition": "构图水平",
    "faces": "人物状态",
}

#: Minimum score lead for a dimension to be called out in the reason.
ADVANTAGE_DELTA = 10.0
#: How many advantage dimensions the reason lists at most.
MAX_REASON_DIMS = 2


def _weight_of(weights, dim):
    """Weight for one dimension: weights=None → DEFAULT_WEIGHTS, missing key → 1.0."""
    if weights is None:
        return DEFAULT_WEIGHTS[dim]
    return weights.get(dim, 1.0)


def _fmt(value):
    """Render a score without a trailing .0 (92.0 → '92')."""
    return f"{value:g}"


def _load_scores(conn, photo_id):
    """Return {dim: score} for the photo — only dimensions with a value."""
    row = conn.execute(
        "SELECT sharpness, exposure, noise, composition, faces"
        " FROM quality_scores WHERE photo_id = ?",
        (photo_id,),
    ).fetchone()
    if row is None:
        return {}
    return {dim: row[dim] for dim in DIMENSIONS if row[dim] is not None}


def _is_raw(conn, primary_file_id):
    if primary_file_id is None:
        return False
    row = conn.execute(
        "SELECT format FROM files WHERE id = ?", (primary_file_id,)
    ).fetchone()
    return row is not None and row["format"] == "raw"


def rank_photos(conn, photo_ids, weights=None):
    """Rank the given photos best-first by weighted score.

    Args:
        conn: sqlite3 connection to the app database.
        photo_ids: photo ids to rank; ids without a photos row are skipped.
        weights: dimension weights. None uses DEFAULT_WEIGHTS; dimensions
            absent from a given dict fall back to weight 1.0.

    Returns:
        List of dicts sorted best-first (ties: RAW primary first, then the
        smaller primary_file_id):

        {photo_id, total, scores, primary_file_id, is_raw, group_id}

        ``total`` is the weighted mean as a float, or None when the photo has
        no scored dimension at all (always sorted last). ``scores`` holds
        only the dimensions that have values. ``is_raw`` is True when the
        primary file's format column equals 'raw'.
    """
    entries = []
    for photo_id in photo_ids:
        photo = conn.execute(
            "SELECT id, primary_file_id, group_id FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
        if photo is None:
            continue
        scores = _load_scores(conn, photo_id)
        total = None
        if scores:
            numerator = 0.0
            denominator = 0.0
            for dim, score in scores.items():
                weight = _weight_of(weights, dim)
                numerator += score * weight
                denominator += weight
            if denominator > 0:
                total = numerator / denominator
        entries.append(
            {
                "photo_id": photo_id,
                "total": total,
                "scores": scores,
                "primary_file_id": photo["primary_file_id"],
                "is_raw": _is_raw(conn, photo["primary_file_id"]),
                "group_id": photo["group_id"],
            }
        )

    def sort_key(entry):
        total = entry["total"]
        file_id = entry["primary_file_id"]
        return (
            total is None,                             # unscored photos last
            -(total if total is not None else 0.0),    # higher total first
            not entry["is_raw"],                       # RAW preferred on ties
            file_id if file_id is not None else float("inf"),
        )

    entries.sort(key=sort_key)
    return entries


def recommend_group(conn, group_id, weights=None):
    """Choose the best photo of one group, persist it, and explain the choice.

    Callable for both pending and decided groups. A missing group raises
    KeyError. Writes groups.recommended_photo_id and returns:

        {recommended_photo_id, reason, ranked}

    where ``ranked`` is the rank_photos result and ranked[0] is the
    recommendation.
    """
    group = conn.execute("SELECT id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if group is None:
        raise KeyError(f"group {group_id} does not exist")

    photo_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM photos WHERE group_id = ? ORDER BY id", (group_id,)
        )
    ]
    ranked = rank_photos(conn, photo_ids, weights=weights)

    if not ranked:
        conn.execute("UPDATE groups SET recommended_photo_id = NULL WHERE id = ?", (group_id,))
        conn.commit()
        return {"recommended_photo_id": None, "reason": "", "ranked": []}

    recommended = ranked[0]["photo_id"]
    reason = _build_reason(ranked)
    conn.execute(
        "UPDATE groups SET recommended_photo_id = ? WHERE id = ?",
        (recommended, group_id),
    )
    conn.commit()
    return {"recommended_photo_id": recommended, "reason": reason, "ranked": ranked}


def recompute_all(conn, weights=None):
    """Re-recommend every pending group (e.g. after the learner retrains).

    decided and not_duplicate groups are left untouched. Returns the list of
    recommend_group results, one per pending group, in group-id order.
    """
    group_ids = [
        row["id"]
        for row in conn.execute("SELECT id FROM groups WHERE status = 'pending' ORDER BY id")
    ]
    return [recommend_group(conn, group_id, weights=weights) for group_id in group_ids]


def _build_reason(ranked):
    """Compose the Chinese recommendation reason for a ranked photo list."""
    rec = ranked[0]

    # recommended is RAW while the group still holds a non-RAW alternative
    raw_note = ""
    if rec["is_raw"] and any(not entry["is_raw"] for entry in ranked[1:]):
        raw_note = "保留了 RAW 底片，后期空间更大。"

    if len(ranked) == 1:
        return "组内只有一张照片，无需比较，直接推荐。"

    other = ranked[1]  # the strongest competitor
    advantages = []
    for dim in DIMENSIONS:
        rec_score = rec["scores"].get(dim)
        other_score = other["scores"].get(dim)
        if rec_score is None or other_score is None:
            continue  # not comparable on this dimension
        delta = rec_score - other_score
        if delta >= ADVANTAGE_DELTA:
            advantages.append((delta, dim, rec_score, other_score))

    if not advantages:
        neutral = "各维度表现接近（差距均在 10 分内），已按当前偏好权重排序，可自行比较后选择。"
        return neutral + raw_note

    advantages.sort(key=lambda item: -item[0])  # stable: equal Δ keeps DIMENSIONS order
    parts = []
    for index, (delta, dim, rec_score, other_score) in enumerate(
        advantages[:MAX_REASON_DIMS]
    ):
        text = f"{DIMENSION_LABELS[dim]} {_fmt(rec_score)} vs {_fmt(other_score)}"
        if index == 0:
            text += f"（领先 {_fmt(delta)} 分）"
        parts.append(text)
    return "推荐这张：" + "、".join(parts) + "；综合表现最佳。" + raw_note
