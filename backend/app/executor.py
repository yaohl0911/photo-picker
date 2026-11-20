"""Safe move executor: applies group decisions and undoes them.

Pure stdlib on purpose (shutil/os/sqlite3/json/datetime/logging) so
``import app.executor`` is safe inside DSH (see docs/DEVENV.md). The
optional learner integration is imported lazily inside
``_record_and_train`` so a missing or unimportable learner never blocks
the actual file moves.

Semantics:
- Every files row of a non-kept photo (RAW+JPG pair included) moves to
  the rejected directory for its source: by default
  ``<its source dir>/_photo_picker_rejected/<path relative to that
  source dir>`` — rejected photos stay inside the folder they came
  from, so multiple photo folders never mix. An explicit
  ``rejected_root`` setting centralizes everything under that one
  directory instead. Files outside every source dir land at
  ``<cwd>/data/_photo_picker_rejected/<basename>``.
- Destinations are never overwritten: an existing target gets a ``_1``,
  ``_2``... suffix inserted before the extension.
- Every successful move is recorded in move_log with its decision. A
  failed single file is recorded in scan_runs (phase='execute') and
  never aborts the remaining moves.
- ``undo_group`` moves the newest decision's live move_log rows back to
  their original paths (again never overwriting), marks them undone,
  clears applied_at and returns the group to 'pending'.
"""

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime

from app.config import REJECTED_DIR_NAME, get_settings

logger = logging.getLogger(__name__)

FALLBACK_REJECTED_DIR = os.path.join("data", REJECTED_DIR_NAME)


class MoveError(Exception):
    """A file could not be moved to the rejected folder (or back)."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_rejected_root(conn: sqlite3.Connection) -> str:
    """Resolve the effective rejected-root directory as an absolute path.

    An explicit ``rejected_root`` setting wins (all photos from all
    source dirs land under that one directory). Empty otherwise: the
    default mode scatters rejected photos into a ``_photo_picker_rejected``
    folder INSIDE each source dir, so photos from different folders never
    mix — there is no single root, and this returns "" to say so.
    """
    explicit = get_settings(conn).get("rejected_root") or ""
    if explicit:
        return os.path.abspath(explicit)
    return ""


def rejected_dir_for(conn: sqlite3.Connection, src: str) -> str:
    """Directory holding the rejected copy of one source file.

    Explicit ``rejected_root`` setting: that directory (centralized).
    Default: ``<the source dir containing src>/_photo_picker_rejected``,
    preserving the file's path relative to ITS source dir. Files outside
    every source dir fall back to ``<cwd>/data/_photo_picker_rejected``.
    """
    explicit = get_settings(conn).get("rejected_root") or ""
    if explicit:
        return os.path.abspath(explicit)
    src_abs = os.path.abspath(src)
    for source_dir in get_settings(conn).get("source_dirs") or []:
        try:
            rel = os.path.relpath(src_abs, os.path.abspath(source_dir))
        except ValueError:  # different drives on Windows
            continue
        if rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            return os.path.abspath(
                os.path.join(source_dir, REJECTED_DIR_NAME)
            )
    return os.path.abspath(FALLBACK_REJECTED_DIR)


def apply_decision(
    conn: sqlite3.Connection, group_id: int, keep_photo_ids, learn: bool = True,
    source: str = "user",
) -> int:
    """Apply a keep/reject decision for one group; return the decision id.

    Raises KeyError when the group does not exist and ValueError when
    keep_photo_ids contains photos outside the group; both happen before
    anything is written. Learning (best effort) runs before the moves.
    ``source`` records who made the decision ('user' | 'auto') for the
    history page; auto decisions skip learning entirely.
    """
    group = conn.execute(
        "SELECT id FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    if group is None:
        raise KeyError(f"group {group_id} does not exist")

    group_photo_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM photos WHERE group_id = ? ORDER BY id", (group_id,)
        ).fetchall()
    ]
    keep_set = set(keep_photo_ids)
    unknown = keep_set - set(group_photo_ids)
    if unknown:
        raise ValueError(
            f"keep_photo_ids not members of group {group_id}: {sorted(unknown)}"
        )
    keep_list = list(keep_photo_ids)

    decision_id = conn.execute(
        "INSERT INTO decisions (group_id, kept_photo_ids, decided_at, source) "
        "VALUES (?, ?, ?, ?)",
        (group_id, json.dumps(keep_list), _now(), source),
    ).lastrowid
    conn.execute("UPDATE groups SET status = 'decided' WHERE id = ?", (group_id,))

    if learn:
        _record_and_train(conn, group_id, keep_list, decision_id)

    for photo_id in group_photo_ids:
        if photo_id in keep_set:
            continue
        rows = conn.execute(
            "SELECT id, path FROM files WHERE photo_id = ? ORDER BY id",
            (photo_id,),
        ).fetchall()
        for row in rows:
            try:
                # per-file: each photo rejects into ITS OWN source folder
                rejected_dir = rejected_dir_for(conn, row["path"])
                dst = _move_file(
                    conn, row["id"], row["path"], rejected_dir, decision_id
                )
                _move_sidecar_files(conn, row["path"], dst, decision_id)
            except MoveError as exc:
                logger.warning("executor move failed: %s", exc)
                _record_execute_error(conn, str(exc))

    conn.execute(
        "UPDATE decisions SET applied_at = ? WHERE id = ?", (_now(), decision_id)
    )
    conn.commit()
    return decision_id


def undo_group(conn: sqlite3.Connection, group_id: int) -> int:
    """Undo the newest decision of a group; return the files restored.

    Moves every live (undone=0) move_log row of that decision back to
    its original path, in reverse order, without overwriting anything
    that reappeared there in the meantime (a ``_1`` suffix is used
    instead). The group goes back to 'pending' so the user can re-decide.
    Dismissals record a decision with NULL kept_photo_ids: undoing one
    simply flips the group back to pending. Legacy groups dismissed
    before decision rows existed fall back to the same flip.
    Returns 0 when the group has no decision yet.
    """
    decision = conn.execute(
        "SELECT id FROM decisions WHERE group_id = ? ORDER BY id DESC LIMIT 1",
        (group_id,),
    ).fetchone()
    if decision is None:
        # legacy dismissal (no decision row): still honor the undo
        status = conn.execute(
            "SELECT status FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        if status is not None and status["status"] == "not_duplicate":
            conn.execute(
                "UPDATE groups SET status = 'pending' WHERE id = ?", (group_id,)
            )
            conn.commit()
        return 0
    decision_id = decision["id"]

    rows = conn.execute(
        "SELECT id, file_id, src_path, dst_path FROM move_log "
        "WHERE decision_id = ? AND undone = 0 ORDER BY id DESC",
        (decision_id,),
    ).fetchall()
    restored = 0
    for row in rows:
        src, dst = row["src_path"], row["dst_path"]
        if dst is None or not os.path.exists(dst):
            message = f"cannot undo move {src} -> {dst}: file missing at destination"
            logger.warning("executor undo: %s", message)
            _record_execute_error(conn, message)
            continue
        target = _unique_dst(src)
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(dst, target)
        except OSError as exc:  # includes shutil.Error
            message = f"undo move {dst} -> {target} failed: {exc}"
            logger.warning("executor undo: %s", message)
            _record_execute_error(conn, message)
            continue
        conn.execute(
            "UPDATE files SET path = ? WHERE id = ?", (target, row["file_id"])
        )
        conn.execute("UPDATE move_log SET undone = 1 WHERE id = ?", (row["id"],))
        restored += 1

    video_rows = conn.execute(
        "SELECT id, src_path, dst_path FROM video_moves "
        "WHERE decision_id = ? AND undone = 0 ORDER BY id DESC",
        (decision_id,),
    ).fetchall()
    for row in video_rows:
        src, dst = row["src_path"], row["dst_path"]
        if dst is None or not os.path.exists(dst):
            message = f"cannot undo video move {src} -> {dst}: file missing at destination"
            logger.warning("executor undo: %s", message)
            _record_execute_error(conn, message)
            continue
        target = _unique_dst(src)
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(dst, target)
        except OSError as exc:  # includes shutil.Error
            message = f"undo video move {dst} -> {target} failed: {exc}"
            logger.warning("executor undo: %s", message)
            _record_execute_error(conn, message)
            continue
        conn.execute(
            "UPDATE video_moves SET undone = 1 WHERE id = ?", (row["id"],)
        )
        restored += 1

    conn.execute(
        "UPDATE decisions SET applied_at = NULL WHERE id = ?", (decision_id,)
    )
    conn.execute("UPDATE groups SET status = 'pending' WHERE id = ?", (group_id,))
    conn.commit()
    return restored


def _record_and_train(
    conn: sqlite3.Connection, group_id: int, kept_ids, decision_id=None
):
    """Feed the decision to the learner and retrain (best effort).

    app.learner is optional at runtime: when it is not importable the
    learning step is skipped silently and the moves still happen. Tests
    monkeypatch this seam so they never need a real learner module.
    """
    try:
        from app import learner
    except ImportError:
        return None
    learner.record_decision_samples(conn, group_id, kept_ids, decision_id=decision_id)
    learner.retrain(conn)
    return None


def _move_file(
    conn: sqlite3.Connection,
    file_id: int,
    src: str,
    rejected_root: str,
    decision_id: int | None = None,
) -> str:
    """Move one file into the rejected tree, record it; return the destination.

    Raises MoveError on any filesystem failure; the caller decides
    whether to continue with the remaining files.
    """
    dst = _unique_dst(os.path.join(rejected_root, _rel_path_under(conn, src)))
    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
    except OSError as exc:  # includes shutil.Error
        raise MoveError(f"move {src} -> {dst} failed: {exc}") from exc
    conn.execute(
        "INSERT INTO move_log (decision_id, file_id, src_path, dst_path, "
        "moved_at, undone) VALUES (?, ?, ?, ?, ?, 0)",
        (decision_id, file_id, src, dst, _now()),
    )
    conn.execute("UPDATE files SET path = ? WHERE id = ?", (dst, file_id))
    return dst


# Extensions of companion files that ride along with a rejected photo:
# Live Photo videos plus anything that is not an indexed image format
# (.xmp edit sidecars, DJI .SRT subtitles, thumbnails...). A sibling file
# with the same stem is never indexed as a photo itself, so keeping it
# next to its photo prevents orphans. Indexed formats (scanner.SUPPORTED
# — e.g. a same-stem PNG) are NOT sidecars: those are their own photos.
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}


def _is_sidecar_ext(ext: str) -> bool:
    """True for extensions that may ride along with a rejected photo.

    Videos always qualify. For other extensions the rule is exclusion:
    anything the scanner indexes as an image is its own photo, never a
    sidecar; every other same-stem file (.xmp, .srt, .thm, .txt...) is
    material that belongs to that photo and follows it. Hidden files
    (.DS_Store and macOS AppleDouble ._IMG_1234.HEIC metadata twins have
    a different stem) never match.
    """
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return True
    from app import scanner  # local import: scanner is stdlib-only here

    return ext not in scanner.SUPPORTED


def _find_sidecar_files(src: str) -> list[str]:
    """Same-stem companion files sitting next to src.

    Live Photo videos (IMG_1234.HEIC + IMG_1234.MOV) and edit sidecars
    (IMG_1234.ARW + IMG_1234.xmp) alike. The stem comparison is
    case-insensitive (iPhone exports IMG_1234.HEIC with IMG_1234.MOV;
    some vendors lowercase one side). Returns sorted paths; missing
    folder or permission issues yield an empty list.
    """
    stem = os.path.splitext(os.path.basename(src))[0].lower()
    folder = os.path.dirname(src) or "."
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        name_stem, name_ext = os.path.splitext(name)
        if name_stem.lower() == stem and _is_sidecar_ext(name_ext):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                found.append(candidate)
    return sorted(found)


def _move_sidecar_files(
    conn: sqlite3.Connection, src: str, photo_dst: str, decision_id: int | None
) -> int:
    """Move same-stem companion files alongside a rejected photo; return count.

    Companions land in the same rejected subfolder as the photo (they are
    not files-table rows, so their path is tracked in video_moves — the
    companion-moves ledger — instead of move_log). Failures are logged
    into scan_runs like photo move errors and never abort the decision.
    """
    moved = 0
    for video in _find_sidecar_files(src):
        video_dst = _unique_dst(
            os.path.join(os.path.dirname(photo_dst), os.path.basename(video))
        )
        try:
            parent = os.path.dirname(video_dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(video, video_dst)
        except OSError as exc:  # includes shutil.Error
            message = f"video move {video} -> {video_dst} failed: {exc}"
            logger.warning("executor: %s", message)
            _record_execute_error(conn, message)
            continue
        conn.execute(
            "INSERT INTO video_moves (decision_id, src_path, dst_path, "
            "moved_at, undone) VALUES (?, ?, ?, ?, 0)",
            (decision_id, video, video_dst, _now()),
        )
        moved += 1
    return moved


def _rel_path_under(conn: sqlite3.Connection, src: str) -> str:
    """Path of src relative to the first source dir containing it.

    Falls back to the basename when src lives outside every source dir.
    """
    src_abs = os.path.abspath(src)
    for source_dir in get_settings(conn).get("source_dirs") or []:
        try:
            rel = os.path.relpath(src_abs, os.path.abspath(source_dir))
        except ValueError:  # different drives on Windows
            continue
        if rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            return rel
    return os.path.basename(src_abs)


def _unique_dst(path: str) -> str:
    """Never overwrite: bump a ``_1``, ``_2``... suffix before the extension."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{base}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _record_execute_error(conn: sqlite3.Connection, message: str) -> None:
    """Log one executor error into scan_runs so the UI/API can surface it."""
    now = _now()
    conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, phase, progress, error) "
        "VALUES (?, ?, 'execute', NULL, ?)",
        (now, now, message),
    )
