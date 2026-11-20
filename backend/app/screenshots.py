"""Screenshot detection and bulk rejection.

A photo counts as a screenshot when its metadata says so — we never
guess from pixels:

* exact screen resolution (incl. rotated) of common iPhone / HUAWEI /
  iPad / generic Android panels,
* file name starting with ``Screenshot_`` (HUAWEEI/Android convention),
* PNG without any camera EXIF in a phone aspect ratio band with a
  short edge of at least 720 px.

Analysis is lazy and incremental: it runs on the first API call and
only looks at rows not yet classified (new files after the next scan
get classified then). Results are cached on ``photos.is_screenshot``
and ``files.width`` / ``files.height``.
"""

from __future__ import annotations

import os
import sqlite3

# (width, height) pairs of common device screens; rotated use is
# covered by _screen_pairs adding the swapped variant.
_BASE_SCREENS = [
    # iPhone
    (1170, 2532), (1170, 2533), (1179, 2556), (1284, 2778),
    (1178, 2556), (1242, 2688), (1125, 2436), (828, 1792),
    # HUAWEI / generic Android
    (1080, 2340), (1080, 2400), (720, 1600), (1080, 2316),
    (1224, 2688), (1228, 2700), (1080, 2248), (1440, 3200),
    (1080, 1920), (1080, 2160), (720, 1280), (720, 1520),
    # iPad
    (1640, 2360), (1668, 2388), (2048, 2732), (1536, 2048),
]
SCREEN_SIZES = frozenset(
    wh for base in _BASE_SCREENS for wh in (base, (base[1], base[0])))

_PHONE_RATIO_LO = 0.40   # portrait height/width around 2.1; band is wide
_PHONE_RATIO_HI = 0.53
_MIN_SHORT_EDGE = 720

_WHEN_FILTERS = ("all", "30d", "2025", "older")


def _open_size(path: str):
    """Lazy (width, height) via PIL header read; None when unreadable."""
    try:
        from app import imaging  # noqa: F401  (registers HEIF opener)
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _classify(name: str, fmt: str, camera, size) -> tuple[bool, str]:
    """Return (is_screenshot, why-text)."""
    if name.startswith("Screenshot_"):
        return True, "文件名 Screenshot_ 前缀"
    if size is not None and size in SCREEN_SIZES:
        return True, "屏幕尺寸 {}×{}".format(*size)
    if size is not None and (fmt or "").lower() == "png" and not camera:
        w, h = size
        short, long = (w, h) if w <= h else (h, w)
        if short >= _MIN_SHORT_EDGE and _PHONE_RATIO_LO <= short / long <= _PHONE_RATIO_HI:
            return True, "屏幕比例 · 无相机信息"
    return False, ""


def ensure_analyzed(conn: sqlite3.Connection) -> dict:
    """Classify every photo not yet marked; idempotent per row."""
    _ensure_columns(conn)
    rows = conn.execute(
        "SELECT f.id AS fid, f.path, f.format, f.camera, p.id AS pid"
        " FROM photos p JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot IS NULL"
    ).fetchall()
    if not rows:
        return {"scanned": 0, "found": 0}
    found = 0
    for r in rows:
        cached = conn.execute(
            "SELECT width, height FROM files WHERE id = ?",
            (r["fid"],)).fetchone()
        if cached is not None and cached[0] is not None:
            size = (cached[0], cached[1])
        else:
            size = _open_size(r["path"])
            if size is not None:
                conn.execute(
                    "UPDATE files SET width = ?, height = ? WHERE id = ?",
                    (size[0], size[1], r["fid"]))
        ok, _why = _classify(
            os.path.basename(r["path"]), r["format"] or "", r["camera"], size)
        conn.execute(
            "UPDATE photos SET is_screenshot = ? WHERE id = ?",
            (1 if ok else 0, r["pid"]))
        if ok:
            found += 1
    conn.commit()
    return {"scanned": len(rows), "found": found}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for stmt in (
        "ALTER TABLE files ADD COLUMN width INTEGER",
        "ALTER TABLE files ADD COLUMN height INTEGER",
        "ALTER TABLE photos ADD COLUMN is_screenshot INTEGER",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def list_screenshots(
    conn: sqlite3.Connection, offset: int = 0, limit: int = 60,
    when: str = "all",
) -> dict:
    """Paged screenshot list with a time filter.

    ``when``: all | 30d | 2025 | older (based on captured_at, falling
    back to nothing — screenshots without a timestamp are listed under
    'all' only).
    """
    if when not in _WHEN_FILTERS:
        raise ValueError("when 必须是 all/30d/2025/older 之一")
    _ensure_columns(conn)
    ensure_analyzed(conn)

    cond, params = "", []
    if when == "30d":
        cond = " AND f.captured_at >= datetime('now', '-30 day')"
    elif when == "2025":
        cond = " AND f.captured_at LIKE '2025-%'"
    elif when == "older":
        cond = " AND (f.captured_at IS NULL OR f.captured_at < '2025-')"

    total = conn.execute(
        "SELECT COUNT(*) FROM photos p"
        " JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot = 1" + cond).fetchone()[0]
    rows = conn.execute(
        "SELECT p.id AS pid, f.id AS fid, f.path, f.format, f.width,"
        "       f.height, f.camera, f.captured_at, f.size"
        " FROM photos p JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot = 1" + cond +
        " ORDER BY f.captured_at DESC, p.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()

    items = []
    for r in rows:
        _ok, why = _classify(
            os.path.basename(r["path"]), r["format"] or "", r["camera"],
            (r["width"], r["height"]) if r["width"] is not None else None)
        items.append({
            "photo_id": r["pid"],
            "file_id": r["fid"],
            "name": os.path.basename(r["path"]),
            "width": r["width"],
            "height": r["height"],
            "why": why,
            "captured_at": r["captured_at"],
            "size": r["size"],
            "thumb_url": "/api/files/{}/thumbnail".format(r["fid"]),
        })
    stats = _stats(conn)
    return {"items": items, "total": total, "offset": offset,
            "limit": limit, "stats": stats}


def _stats(conn: sqlite3.Connection) -> dict:
    total_photos = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE is_screenshot = 1").fetchone()[0]
    all_photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    bytes_total = conn.execute(
        "SELECT COALESCE(SUM(f.size), 0) FROM photos p"
        " JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot = 1").fetchone()[0]
    latest = conn.execute(
        "SELECT MAX(f.captured_at) FROM photos p"
        " JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot = 1").fetchone()[0]
    return {
        "screenshots": total_photos,
        "percent": round(100.0 * total_photos / all_photos, 1) if all_photos else 0,
        "bytes": bytes_total,
        "latest": latest,
    }


def reject_screenshots(
    conn: sqlite3.Connection, photo_ids: list,
) -> dict:
    """Bulk-reject screenshots through the normal decision pipeline.

    Photos are gathered into one transient group and decided with an
    empty keep-set, reusing executor.apply_decision (file moves,
    sidecar moves, move_log, undo, history). source is 'user' — this
    is a deliberate manual action, not an automatic rule.
    """
    if not isinstance(photo_ids, list) or not photo_ids:
        raise ValueError("photo_ids 不能为空")
    from app import executor
    ids = []
    for pid in photo_ids:
        row = conn.execute(
            "SELECT is_screenshot FROM photos WHERE id = ?",
            (pid,)).fetchone()
        if row is None:
            raise ValueError("photo {} 不存在".format(pid))
        if row[0] != 1:
            raise ValueError("photo {} 不是截图".format(pid))
        ids.append(pid)

    cur = conn.execute(
        "INSERT INTO groups (status, created_at) VALUES ('pending', ?)",
        (executor._now(),))
    gid = cur.lastrowid
    for pid in ids:
        conn.execute(
            "UPDATE photos SET group_id = ? WHERE id = ?", (gid, pid))
    conn.commit()
    decision_id = executor.apply_decision(
        conn, gid, [], learn=False, source="user")
    return {"decision_id": decision_id, "group_id": gid,
            "moved_files": _moved_count(conn, decision_id)}


def _moved_count(conn: sqlite3.Connection, decision_id: int) -> int:
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM move_log m WHERE m.decision_id = ?)"
        " + (SELECT COUNT(*) FROM video_moves v"
        "     WHERE v.decision_id = ?)",
        (decision_id, decision_id)).fetchone()[0]
