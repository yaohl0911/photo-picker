# -*- coding: utf-8 -*-
"""Pure-Python service layer backing the JSON API.

Every function takes a ``sqlite3.Connection`` as its first argument and
returns plain dicts/lists ready for JSON serialization. This module must
stay importable inside DSH: it must NOT import fastapi (pydantic_core is
unloadable there). The HTTP layer in ``app.main`` is a thin adapter over
these functions.

Weight semantics
----------------
- ``effective_weights`` is the single source of truth for all ranking:
  ``learner.get_current_weights()['weights']`` (which is DEFAULT_WEIGHTS
  before any training) with per-dimension overrides from the
  ``custom_weights`` setting.
- ``list_groups``/``get_group`` re-recommend pending groups on every call
  (idempotent — ``recommend_group`` persists its result into
  ``groups.recommended_photo_id``) so stored recommendations always
  reflect the current effective weights.
"""
import json
import os

from app import executor, grouper, hashing, learner, recommender, scanner
from app.config import get_settings as get_config_settings
from app.recommender import DIMENSIONS, DIMENSION_LABELS, DEFAULT_WEIGHTS

__all__ = [
    "get_overview",
    "list_groups",
    "get_group",
    "decide_group",
    "dismiss_group_api",
    "undo_group_api",
    "add_feedback",
    "get_preferences",
    "get_settings",
    "set_preference_weights",
    "effective_weights",
    "train_now",
    "set_locked_api",
    "list_history",
    "run_full_pipeline_blocking",
]

GROUP_STATUSES = ("pending", "decided", "not_duplicate")
PAGE_LIMIT_MAX = 500
MEMBER_PREVIEW = 4
PREFERENCE_EXPLANATION = (
    "权重语义：数值越大越看重该维度；0 表示忽略该维度；"
    "负值表示反向偏好（该维度分数越低越优先）。"
)


# --- internal helpers --------------------------------------------------------

def _validate_paging(offset, limit):
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 必须是非负整数")
    if not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise ValueError("limit 必须在 1 与 {} 之间".format(PAGE_LIMIT_MAX))


def _require_group(conn, group_id):
    row = conn.execute(
        "SELECT id FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None:
        raise KeyError("group {} 不存在".format(group_id))
    return row["id"]


def _get_setting_json(conn, key, default):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def _group_photo_rows(conn, group_id):
    """Photos of a group with primary-file info and quality scores."""
    return conn.execute(
        "SELECT p.id AS photo_id, p.primary_file_id, f.format AS primary_format,"
        "       q.sharpness, q.exposure, q.noise, q.composition, q.faces"
        " FROM photos p"
        " JOIN files f ON f.id = p.primary_file_id"
        " LEFT JOIN quality_scores q ON q.photo_id = p.id"
        " WHERE p.group_id = ?"
        " ORDER BY p.id",
        (group_id,),
    ).fetchall()


def _photo_preview(entry):
    return {
        "photo_id": entry["photo_id"],
        "primary_file_id": entry["primary_file_id"],
        "is_raw": (entry["primary_format"] or "").lower() in ("cr2", "cr3",
                                                              "arw", "nef",
                                                              "raf", "dng",
                                                              "orf", "rw2"),
        "thumb_url": "/api/files/{}/thumbnail".format(entry["primary_file_id"]),
        "scores": {
            "sharpness": entry["sharpness"],
            "exposure": entry["exposure"],
            "noise": entry["noise"],
            "composition": entry["composition"],
            "faces": entry["faces"],
        },
    }


def _group_recommendation(conn, group_row, weights):
    """recommendation for one group: pending -> live recompute.

    Pending groups are re-recommended on every call so the stored
    ``recommended_photo_id`` tracks the current effective weights
    (``recommend_group`` persists its result; the call is idempotent).
    Decided / not_duplicate groups keep the stored recommendation; their
    historical reason text is not reconstructible, so reason = "".
    """
    group_id = group_row["id"]
    status = group_row["status"]
    if status == "pending":
        rec = recommender.recommend_group(conn, group_id, weights=weights)
        return rec["recommended_photo_id"], rec["reason"], \
            [r["photo_id"] for r in rec["ranked"]]
    photo_ids = [r["photo_id"] for r in _group_photo_rows(conn, group_id)]
    ranked = [r["photo_id"]
              for r in recommender.rank_photos(conn, photo_ids, weights=weights)]
    stored = group_row["recommended_photo_id"]
    if stored is not None and stored not in ranked and stored in photo_ids:
        ranked = [stored] + [pid for pid in ranked if pid != stored]
    recommended = stored if stored is not None else (
        ranked[0] if ranked else None)
    return recommended, "", ranked


# --- overview ----------------------------------------------------------------

def get_overview(conn):
    """Aggregate library statistics for the dashboard."""
    def count(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    model = learner.get_current_weights(conn)
    last_scan = conn.execute(
        "SELECT MAX(COALESCE(finished_at, started_at)) FROM scan_runs"
    ).fetchone()[0]
    return {
        "total_photos": count("SELECT COUNT(*) FROM photos"),
        "total_files": count("SELECT COUNT(*) FROM files"),
        "pending_groups": count(
            "SELECT COUNT(*) FROM groups WHERE status = 'pending'"),
        "decided_groups": count(
            "SELECT COUNT(*) FROM groups WHERE status = 'decided'"),
        "not_duplicate_groups": count(
            "SELECT COUNT(*) FROM groups WHERE status = 'not_duplicate'"),
        "decisions_count": count("SELECT COUNT(*) FROM decisions"),
        "samples": model["sample_count"],
        "model_version": model["version"],
        "last_scan_at": last_scan,
        "locked_dims": model["locked"],
    }


# --- groups ------------------------------------------------------------------

def list_groups(conn, status="pending", offset=0, limit=50):
    """List groups filtered by status with a short member preview."""
    if status not in GROUP_STATUSES:
        raise ValueError(
            "status 必须是 {} 之一".format("/".join(GROUP_STATUSES)))
    _validate_paging(offset, limit)
    weights = effective_weights(conn)
    total = conn.execute(
        "SELECT COUNT(*) FROM groups WHERE status = ?", (status,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT id, status, recommended_photo_id FROM groups"
        " WHERE status = ? ORDER BY id LIMIT ? OFFSET ?",
        (status, limit, offset),
    ).fetchall()
    groups = []
    for row in rows:
        recommended, reason, ranked = _group_recommendation(
            conn, row, weights)
        photo_rows = _group_photo_rows(conn, row["id"])
        groups.append({
            "group_id": row["id"],
            "status": row["status"],
            "recommended_photo_id": recommended,
            "reason": reason,
            "photo_count": len(photo_rows),
            "photos": [_photo_preview(e) for e in photo_rows[:MEMBER_PREVIEW]],
        })
    return {"groups": groups, "total": total, "offset": offset,
            "limit": limit}


def get_group(conn, group_id):
    """Full detail for one group, photos ordered by recommendation rank."""
    row = conn.execute(
        "SELECT id, status, recommended_photo_id FROM groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        raise KeyError("group {} 不存在".format(group_id))
    weights = effective_weights(conn)
    recommended, reason, ranked = _group_recommendation(conn, row, weights)
    photos = []
    by_rank = {pid: i for i, pid in enumerate(ranked)}
    photo_rows = sorted(_group_photo_rows(conn, group_id),
                        key=lambda e: by_rank.get(e["photo_id"], 10**9))
    file_rows = conn.execute(
        "SELECT id, path, format, photo_id FROM files"
        " WHERE photo_id IN (SELECT id FROM photos WHERE group_id = ?)"
        " ORDER BY id",
        (group_id,),
    ).fetchall()
    files_by_photo = {}
    for f in file_rows:
        files_by_photo.setdefault(f["photo_id"], []).append(f)
    totals = {r["photo_id"]: r["total"]
              for r in recommender.rank_photos(
                  conn, [e["photo_id"] for e in photo_rows], weights=weights)}
    for e in photo_rows:
        pid = e["photo_id"]
        primary = conn.execute(
            "SELECT captured_at, exif_json FROM files WHERE id = ?",
            (e["primary_file_id"],),
        ).fetchone()
        try:
            exif = json.loads(primary["exif_json"]) \
                if primary["exif_json"] else None
        except (TypeError, ValueError):
            exif = None
        fmt = (primary and e["primary_format"] or "")
        photos.append({
            "photo_id": pid,
            "primary_file_id": e["primary_file_id"],
            "files": [{
                "file_id": f["id"],
                "path": f["path"],
                "format": f["format"],
                "is_raw": (f["format"] or "").lower() in (
                    "cr2", "cr3", "arw", "nef", "raf", "dng", "orf", "rw2"),
            } for f in files_by_photo.get(pid, [])],
            "captured_at": primary["captured_at"] if primary else None,
            "exif": exif,
            "scores": {
                "sharpness": e["sharpness"],
                "exposure": e["exposure"],
                "noise": e["noise"],
                "composition": e["composition"],
                "faces": e["faces"],
                "total": totals.get(pid),
            },
            "is_recommended": pid == recommended,
            "thumb_url": "/api/files/{}/thumbnail".format(
                e["primary_file_id"]),
            "is_raw": fmt.lower() in ("cr2", "cr3", "arw", "nef", "raf",
                                      "dng", "orf", "rw2"),
        })
    return {
        "group_id": row["id"],
        "status": row["status"],
        "recommended_photo_id": recommended,
        "reason": reason,
        "ranked": ranked,
        "photos": photos,
    }


# --- decisions ---------------------------------------------------------------

def decide_group(conn, group_id, keep_photo_ids, learn=True):
    """Apply a keep decision: moves files, optionally learns from it.

    ``executor.apply_decision`` performs sample recording + retrain
    BEFORE the file moves (all under one write connection, so SQLite
    serializes the whole decision against concurrent job threads).
    Afterwards, when learning happened, every pending group's cached
    recommendation is refreshed under the new effective weights.
    """
    decision_id = executor.apply_decision(
        conn, group_id, keep_photo_ids, learn=learn)
    # The decision makes the group's winner a known fact: persist the
    # first kept photo as recommended_photo_id so decided-group listings
    # (which use the stored value) show a meaningful recommendation.
    keep_list = list(keep_photo_ids)
    if keep_list:
        conn.execute(
            "UPDATE groups SET recommended_photo_id = ? WHERE id = ?",
            (keep_list[0], group_id),
        )
    if learn:
        recommender.recompute_all(conn, weights=effective_weights(conn))
    moved = conn.execute(
        "SELECT (SELECT COUNT(*) FROM move_log WHERE decision_id = ?)"
        "     + (SELECT COUNT(*) FROM video_moves WHERE decision_id = ?)",
        (decision_id, decision_id),
    ).fetchone()[0]
    return {
        "decision_id": decision_id,
        "moved_files": moved,
        "model": learner.get_current_weights(conn),
    }


def dismiss_group_api(conn, group_id):
    """Mark a group as 'not duplicate' (kept as history, never regrouped)."""
    _require_group(conn, group_id)
    grouper.dismiss_group(conn, group_id)
    return {"ok": True, "status": "not_duplicate"}


def undo_group_api(conn, group_id):
    """Undo the newest decision of a group: restore moved files."""
    _require_group(conn, group_id)
    restored = executor.undo_group(conn, group_id)
    return {"restored_files": restored}


# --- feedback ----------------------------------------------------------------

def add_feedback(conn, photo_id, action):
    """Record a like/dislike feedback event (no immediate retrain).

    Feedback events are consumed by the learner the next time decision
    samples are recorded (liked photos count as kept, disliked as
    rejected, with extra weight).
    """
    if action not in ("like", "dislike"):
        raise ValueError("action 必须是 like 或 dislike")
    row = conn.execute(
        "SELECT group_id FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        raise KeyError("photo {} 不存在".format(photo_id))
    conn.execute(
        "INSERT INTO feedback_events (photo_id, action, group_id)"
        " VALUES (?, ?, ?)",
        (photo_id, action, row["group_id"]),
    )
    return {"ok": True}


# --- preferences ---------------------------------------------------------

def effective_weights(conn):
    """Weights actually used for ranking.

    Base is the trained model's weights (DEFAULT_WEIGHTS before any
    training); the ``custom_weights`` setting overrides individual
    dimensions on top of it.
    """
    base = dict(learner.get_current_weights(conn)["weights"])
    custom = _get_setting_json(conn, "custom_weights", {}) or {}
    for dim in DIMENSIONS:
        if dim in custom:
            base[dim] = custom[dim]
    return base


def get_preferences(conn):
    """Current preference model + weight explanation for the settings UI."""
    model = learner.get_current_weights(conn)
    custom = _get_setting_json(conn, "custom_weights", {}) or {}
    return {
        "version": model["version"],
        "bias": model["bias"],
        "sample_count": model["sample_count"],
        "locked": model["locked"],
        "weights": model["weights"],
        "custom_weights": custom,
        "effective_weights": effective_weights(conn),
        "default_weights": dict(DEFAULT_WEIGHTS),
        "labels": dict(DIMENSION_LABELS),
        "explanation": PREFERENCE_EXPLANATION,
    }


def set_preference_weights(conn, weights):
    """Store per-dimension custom weight overrides.

    Keys must be dimension names, values numeric (0 allowed = ignore the
    dimension). A partial dict merges over previously stored overrides
    (effective = base with overrides). An empty dict clears all custom
    overrides. Stored via a direct settings INSERT (config's
    update_settings whitelist does not know this key).
    """
    if not isinstance(weights, dict):
        raise ValueError("weights 必须是 {维度: 数值} 字典")
    cleaned = {}
    for key, value in weights.items():
        if key not in DIMENSIONS:
            raise ValueError(
                "未知维度 {}（允许: {}）".format(key, ", ".join(DIMENSIONS)))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("维度 {} 的权重必须是数值".format(key))
        cleaned[key] = float(value)
    if not cleaned:  # empty dict = reset to defaults
        merged = {}
    else:
        merged = _get_setting_json(conn, "custom_weights", {}) or {}
        merged.update(cleaned)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("custom_weights", json.dumps(merged)),
    )
    return get_preferences(conn)


def train_now(conn):
    """Manually trigger a retrain + recommendation refresh."""
    train_result = learner.retrain(conn)
    recomputed = recommender.recompute_all(
        conn, weights=effective_weights(conn))
    return {
        "model": learner.get_current_weights(conn),
        "train": train_result,
        "recomputed": len(recomputed),
    }


def set_locked_api(conn, dims):
    """Lock dimensions so training cannot move their weights."""
    if not isinstance(dims, (list, tuple)):
        raise ValueError("dims 必须是维度名列表")
    learner.set_locked(conn, list(dims))
    return {"ok": True, "locked": list(dims)}


# --- history -----------------------------------------------------------------

def list_history(conn, offset=0, limit=50):
    """Decision history, newest first."""
    _validate_paging(offset, limit)
    total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    rows = conn.execute(
        "SELECT d.id AS decision_id, d.group_id, g.status AS group_status,"
        "       d.kept_photo_ids, d.decided_at, d.applied_at,"
        "       (SELECT (SELECT COUNT(*) FROM move_log m"
        "                 WHERE m.decision_id = d.id)"
        "        + (SELECT COUNT(*) FROM video_moves v"
        "            WHERE v.decision_id = d.id)) AS moved_files"
        " FROM decisions d JOIN groups g ON g.id = d.group_id"
        " ORDER BY d.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    decisions = []
    for r in rows:
        raw_kept = r["kept_photo_ids"]
        try:
            kept = json.loads(raw_kept)
        except (TypeError, ValueError):
            kept = []
        decisions.append({
            "decision_id": r["decision_id"],
            "group_id": r["group_id"],
            "group_status": r["group_status"],
            # NULL kept ids = a 'not duplicate' dismissal (nothing kept)
            "kind": "dismiss" if raw_kept is None else "keep",
            "kept_photo_ids": kept,
            "decided_at": r["decided_at"],
            "applied_at": r["applied_at"],
            "moved_files": r["moved_files"],
            "undone": r["applied_at"] is None,
        })
    return {"decisions": decisions, "total": total, "offset": offset,
            "limit": limit}


# --- settings -----------------------------------------------------------------

def get_settings(conn):
    """Current persisted settings for the settings page (server-first load).

    Reads ``app.config.get_settings`` and resolves every path to absolute
    form: ``source_dirs`` entries are abspath'd (empty list stays []),
    and ``rejected_root`` returns the effective directory via
    ``executor.default_rejected_root``: an explicit setting is reported
    as the single centralized directory; an empty string means the
    default scattered mode, where each photo's rejected copy lands in
    the ``_photo_picker_rejected`` folder INSIDE its own source dir.
    Key names/types follow app.config:
    ``phash_threshold``/``time_window_sec`` are ints.
    """
    settings = get_config_settings(conn)
    return {
        "source_dirs": [os.path.abspath(d)
                        for d in (settings["source_dirs"] or [])],
        "rejected_root": executor.default_rejected_root(conn),
        "phash_threshold": settings["phash_threshold"],
        "time_window_sec": settings["time_window_sec"],
    }


# --- pipeline ----------------------------------------------------------------

_ANALYSIS_COMMIT_STEP = 50  # commit cadence to keep write-lock windows short

def run_full_pipeline_blocking(conn, progress_cb=None):
    """Synchronous scan→pair→hash→group→analyze→recommend pipeline.

    Used by run.sh for the initial import and by POST /api/pipeline/run.
    Analysis failures (undecodable images, missing native deps) are
    counted per photo instead of aborting the run.
    """
    report = {}
    scan_report = scanner.scan(conn, progress_cb=progress_cb)
    report["scan"] = {
        "new": scan_report.new_files,
        "changed": scan_report.changed_files,
        "missing": scan_report.missing_files,
        "total": scan_report.total_files,
        "pairs": scan_report.pair_count,
        "errors": list(scan_report.errors),
    }
    hash_report = hashing.compute_hashes(conn, progress_cb=progress_cb)
    report["hashing"] = dict(hash_report)
    group_report = grouper.build_groups(conn, progress_cb=progress_cb)
    report["grouping"] = dict(group_report)

    face_engine = get_config_settings(conn).get("face_engine", "auto")
    pending = conn.execute(
        "SELECT p.id AS photo_id, f.path FROM photos p"
        " JOIN files f ON f.id = p.primary_file_id"
        " LEFT JOIN quality_scores q ON q.photo_id = p.id"
        " WHERE q.photo_id IS NULL AND f.path IS NOT NULL"
        " ORDER BY p.id"
    ).fetchall()
    analyzed, failed, errors = 0, 0, []
    analysis_total = len(pending)
    from app.quality import analyze_photo
    for index, row in enumerate(pending, start=1):
        if progress_cb is not None:
            progress_cb("analysis", index, analysis_total)
        try:
            scores = analyze_photo(row["path"], face_engine)
        except Exception as exc:  # native dep unavailable, decode error, ...
            failed += 1
            errors.append("{}: {}".format(row["path"], exc))
            continue
        if scores is None:
            failed += 1
            errors.append("{}: 图像无法解码".format(row["path"]))
            continue
        conn.execute(
            "INSERT OR REPLACE INTO quality_scores "
            "(photo_id, sharpness, exposure, noise, composition, faces, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["photo_id"], scores["sharpness"], scores["exposure"],
             scores["noise"], scores["composition"], scores["faces"],
             json.dumps(scores["details"])),
        )
        analyzed += 1
        if analyzed % _ANALYSIS_COMMIT_STEP == 0:
            conn.commit()  # short write locks like scanner's batches
    conn.commit()
    report["analysis"] = {"analyzed": analyzed, "failed": failed,
                          "errors": errors}

    if progress_cb is not None:
        progress_cb("recommend", 0, 1)
    recalculated = recommender.recompute_all(
        conn, weights=effective_weights(conn))
    if progress_cb is not None:
        progress_cb("recommend", 1, 1)
    report["recommend"] = {"recalculated": len(recalculated)}
    return report


# --- filesystem browsing -------------------------------------------------------

def list_dirs(conn, path):
    """List subdirectories of ``path`` for the directory picker UI.

    Only directory names are returned (never files) — the picker is for
    choosing source roots, and a name-only listing keeps the API's
    information surface small. ``~`` expands to the user's home.
    Raises ValueError for missing paths or non-directories.
    """
    import os
    expanded = os.path.expanduser(path or "~")
    if not os.path.isdir(expanded):
        raise ValueError("路径不存在或不是文件夹：{}".format(expanded))
    parent = os.path.dirname(expanded.rstrip("/")) or None
    if parent == expanded:  # filesystem root
        parent = None
    try:
        names = [n for n in os.listdir(expanded)
                 if os.path.isdir(os.path.join(expanded, n))]
    except OSError as exc:
        raise ValueError("无法读取该文件夹：{}".format(exc)) from exc
    names.sort(key=str.lower)
    dirs = [{"name": n, "path": os.path.join(expanded, n)} for n in names]
    return {
        "path": expanded,
        "parent": parent,
        "dirs": dirs,
        "home": os.path.expanduser("~"),
        "shortcuts": {
            "主目录": os.path.expanduser("~"),
            "桌面": os.path.expanduser("~/Desktop"),
            "下载": os.path.expanduser("~/Downloads"),
            "图片": os.path.expanduser("~/Pictures"),
            "文档": os.path.expanduser("~/Documents"),
        },
    }
