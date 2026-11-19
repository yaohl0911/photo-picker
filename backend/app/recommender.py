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

#: Management-value bonuses (added to total, NOT learned dimensions).
#: Pixel-identical re-exports (e.g. chat-app copies) score within a
#: fraction of a point of each other; these small bonuses then decide,
#: while any real quality gap (>2.2 points) still wins.
EXIF_BONUS = 1.2          # camera + capture time present
EXIF_TIME_ONLY_BONUS = 0.6  # capture time present, camera missing
FILENAME_BONUS = 1.0      # regular, sortable filename (see below)
#: File-manager copy suffix (photo(1).jpg / photo (2).JPG): the no-suffix
#: file is the original; copies are re-saves from chats/downloads.
COPY_SUFFIX_PENALTY = 1.0

import re

_DATE_SEP = re.compile(r"\d{4}[-._]\d{2}[-._]\d{2}")   # 2019-05-13 / 2019_05_13
_DATE_TIGHT = re.compile(r"^\d{8}(?:[ _-]|$)")         # 20190513 103817
_SEQISH = re.compile(
    r"^(img|dsc|p|dji|gx|mwp|mv|wp|vid|screen|screenshot)[-_ ]?\d+$", re.I
)
_LONGNUM = re.compile(r"^\d{12,}$")        # 4554893058087652885
_HEXJUNK = re.compile(r"^[0-9a-fA-F]{16,}$")  # content hashes
_MMEXPORT = re.compile(r"^mmexport\d+$", re.I)   # WeChat dumps
#: trailing "(n)" on the stem, optional leading space: 132126(1) / 132126 (2)
_COPY_SUFFIX = re.compile(r"\s?\(\d+\)$")


def _filename_regularity(path):
    """Score how manageable a filename is: 0.0 (opaque) ... 1.0 (dated).

    Dated names (2019-05-13 103817.jpg) sort by capture time and make the
    archive self-explaining; camera sequence names (IMG_1234) are almost as
    good; long numeric ids and hashes (4554893058087652885.JPG, mmexport…)
    carry zero information. Opaque patterns are checked FIRST so a long id
    can never masquerade as a date substring.
    """
    if not path:
        return 0.0
    stem = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    if _MMEXPORT.match(stem) or _LONGNUM.match(stem) or _HEXJUNK.match(stem):
        return 0.0
    if _DATE_SEP.search(stem) or _DATE_TIGHT.match(stem):
        return 1.0
    if _SEQISH.match(stem):
        return 0.9
    if len(stem) <= 12 and re.match(r"^[A-Za-z0-9 _-]+$", stem):
        return 0.5
    return 0.3


def _is_copy_suffix(path):
    """True when the stem ends in a file-manager copy marker ``(n)``.

    ``photo(1).jpg`` / ``132126 (2).JPG`` are re-saved copies; the sibling
    without the suffix is the original.
    """
    if not path:
        return False
    stem = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    return bool(_COPY_SUFFIX.search(stem))


def _management_bonuses(conn, primary_file_id):
    """(exif_bonus, filename_bonus, copy_penalty) for one photo's primary file.

    filename_bonus stays regularity-based; the copy-suffix penalty is kept
    separate so reasons can explain 'original vs copy' independently of
    filename regularity.
    """
    if primary_file_id is None:
        return 0.0, 0.0, 0.0
    row = conn.execute(
        "SELECT path, captured_at, camera FROM files WHERE id = ?",
        (primary_file_id,),
    ).fetchone()
    if row is None:
        return 0.0, 0.0, 0.0
    if row["captured_at"] and row["camera"]:
        exif = EXIF_BONUS
    elif row["captured_at"]:
        exif = EXIF_TIME_ONLY_BONUS
    else:
        exif = 0.0
    copy_penalty = COPY_SUFFIX_PENALTY if _is_copy_suffix(row["path"]) else 0.0
    return exif, FILENAME_BONUS * _filename_regularity(row["path"]), copy_penalty


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

        {photo_id, total, scores, primary_file_id, is_raw, group_id,
         exif_bonus, filename_bonus}

        ``total`` is the weighted mean as a float, or None when the photo has
        no scored dimension at all (always sorted last). ``scores`` holds
        only the dimensions that have values. ``is_raw`` is True when the
        primary file's format column equals 'raw'. Small management-value
        bonuses (EXIF presence, regular filename, and a copy-suffix
        penalty — see the constants above) are applied to a scored
        photo's total; they tip pixel-identical near-ties toward the
        better-archived original without ever overriding a real quality
        gap.
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
        exif_bonus, filename_bonus, copy_penalty = _management_bonuses(
            conn, photo["primary_file_id"]
        )
        entries.append(
            {
                "photo_id": photo_id,
                "total": total,
                "scores": scores,
                "primary_file_id": photo["primary_file_id"],
                "is_raw": _is_raw(conn, photo["primary_file_id"]),
                "group_id": photo["group_id"],
                "exif_bonus": exif_bonus,
                "filename_bonus": filename_bonus,
                "copy_penalty": copy_penalty,
                # sorting adds the small management bonuses to the quality
                # total; the bonus never changes the displayed total itself
                "bonus_total": (total if total is not None else 0.0)
                + exif_bonus
                + filename_bonus
                - copy_penalty,
            }
        )

    def sort_key(entry):
        total = entry["total"]
        file_id = entry["primary_file_id"]
        return (
            total is None,                             # unscored photos last
            -entry["bonus_total"],                    # quality + management value
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

    # Management-value notes: these matter exactly when the pixel quality
    # is a near-tie (chat-app re-exports of the same shot).
    exif_note = ""
    if rec["exif_bonus"] > other["exif_bonus"]:
        if rec["exif_bonus"] >= EXIF_BONUS:
            exif_note = "保留了完整拍摄信息（拍摄时间、相机型号），便于整理归档。"
        else:
            exif_note = "保留了拍摄时间信息，便于按时间归档。"
    filename_note = ""
    rec_reg = rec["filename_bonus"] / FILENAME_BONUS if FILENAME_BONUS else 0.0
    other_reg = other["filename_bonus"] / FILENAME_BONUS if FILENAME_BONUS else 0.0
    if rec_reg - other_reg >= 0.4 and rec_reg >= 0.9:
        filename_note = "文件名包含拍摄日期、按名排序即按时间排序，便于文件管理。"
    copy_note = ""
    if other["copy_penalty"] and not rec["copy_penalty"]:
        copy_note = "另一张的文件名带 (1) 副本后缀（复制/另存产生的副本），这张是原始文件。"

    management = raw_note + exif_note + filename_note + copy_note

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
        return neutral + management

    advantages.sort(key=lambda item: -item[0])  # stable: equal Δ keeps DIMENSIONS order
    parts = []
    for index, (delta, dim, rec_score, other_score) in enumerate(
        advantages[:MAX_REASON_DIMS]
    ):
        text = f"{DIMENSION_LABELS[dim]} {_fmt(rec_score)} vs {_fmt(other_score)}"
        if index == 0:
            text += f"（领先 {_fmt(delta)} 分）"
        parts.append(text)
    return "推荐这张：" + "、".join(parts) + "；综合表现最佳。" + management
