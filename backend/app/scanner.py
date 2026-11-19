"""Scanner: recursive discovery, incremental indexing, EXIF, RAW+JPG pairing.

Implements the scanner module of
docs/superpowers/specs/2026-11-18-photo-dedup-design.md (§5).

The native image libraries (Pillow, pillow_heif, rawpy) cannot load in every
environment — see docs/DEVENV.md: DSH bash processes reject their C extensions
via AMFI library validation. This module therefore imports only the standard
library at top level; every third-party import is deferred into the function
that needs it, and every decode path degrades to ``{}`` instead of raising.
``import app.scanner`` is safe everywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable

from app.config import REJECTED_DIR_NAME, get_settings

#: Extension -> canonical format. The RAW set follows the rawpy/LibRaw
#: support matrix so pair_raw_jpg() sees every vendor's files.
SUPPORTED: dict[str, str] = {
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".heic": "heic",
    ".heif": "heic",
    ".png": "png",
    ".cr2": "raw",
    ".cr3": "raw",
    ".crw": "raw",
    ".nef": "raw",
    ".nrw": "raw",
    ".arw": "raw",
    ".sr2": "raw",
    ".srf": "raw",
    ".dng": "raw",
    ".raf": "raw",
    ".orf": "raw",
    ".rw2": "raw",
    ".pef": "raw",
    ".srw": "raw",
    ".x3f": "raw",
    ".raw": "raw",
    ".dcr": "raw",
    ".erf": "raw",
    ".kdc": "raw",
    ".mef": "raw",
    ".mrw": "raw",
    ".iiq": "raw",
    ".mos": "raw",
    ".ptx": "raw",
    ".rdc": "raw",
    ".rwz": "raw",
}

_MD5_BLOCK_SIZE = 8192
_PAIR_MAX_CAPTURE_GAP_SEC = 2.0
_PROGRESS_STEP = 50  # scan_runs.progress DB write every N files

# EXIF tags extracted into files.exif_json (spec: 306/271/272/33434/33437/
# 34855/37386, plus 36867 — the DateTimeOriginal found in the Exif sub-IFD).
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME = 306
_TAG_EXPOSURE_TIME = 33434
_TAG_F_NUMBER = 33437
_TAG_ISO = 34855
_TAG_DATETIME_ORIGINAL = 36867
_TAG_FOCAL_LENGTH = 37386
_EXIF_IFD_TAG = 34665  # IFD0 tag holding the offset of the Exif sub-IFD

_EXIF_TAG_NAMES: dict[int, str] = {
    _TAG_MAKE: "make",
    _TAG_MODEL: "model",
    _TAG_DATETIME: "datetime",
    _TAG_EXPOSURE_TIME: "exposure_time",
    _TAG_F_NUMBER: "f_number",
    _TAG_ISO: "iso",
    _TAG_DATETIME_ORIGINAL: "datetime_original",
    _TAG_FOCAL_LENGTH: "focal_length",
}


@dataclass
class ScanReport:
    """Outcome counters of one scan() run."""

    new_files: int = 0
    changed_files: int = 0
    missing_files: int = 0
    total_files: int = 0
    pair_count: int = 0
    errors: list[str] = field(default_factory=list)


# --- small pure helpers -------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(_MD5_BLOCK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_exif_datetime(value) -> str | None:
    """EXIF "YYYY:MM:DD HH:MM:SS[.subsec]" -> ISO, or None if unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return None


def _camera_from(make, model) -> str | None:
    parts = [
        str(part).strip()
        for part in (make, model)
        if part is not None and str(part).strip()
    ]
    return " ".join(parts) if parts else None


def _jsonable(value):
    """Normalize an EXIF value to something json.dumps accepts."""
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Fraction):  # covers PIL's IFDRational
        return float(value)
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


# --- EXIF extraction ----------------------------------------------------------

_heif_opener_registered = False


def _ensure_heif_opener() -> None:
    """Register pillow_heif's opener once per process; no-op if unavailable."""
    global _heif_opener_registered
    if _heif_opener_registered:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        # Not installed, or its native part cannot load (e.g. inside DSH):
        # HEIC files then simply fail to decode downstream.
        pass
    _heif_opener_registered = True


def _exif_fields_from_image(image) -> dict:
    """Metadata dict for a decoded PIL image (no mtime fallback here)."""
    info: dict = {
        "captured_at": None,
        "camera": None,
        "exif": {},
        "width": image.size[0],
        "height": image.size[1],
    }
    fields: dict[int, object] = {}
    try:
        exif_obj = image.getexif()
        fields.update(dict(exif_obj))
        fields.update(dict(exif_obj.get_ifd(_EXIF_IFD_TAG)))
    except Exception:
        pass  # corrupt EXIF container: keep whatever parsed so far
    if fields:
        info["exif"] = {
            name: _jsonable(fields[tag])
            for tag, name in _EXIF_TAG_NAMES.items()
            if tag in fields
        }
    info["captured_at"] = _parse_exif_datetime(
        fields.get(_TAG_DATETIME_ORIGINAL)
    ) or _parse_exif_datetime(fields.get(_TAG_DATETIME))
    info["camera"] = _camera_from(fields.get(_TAG_MAKE), fields.get(_TAG_MODEL))
    return info


def _apply_mtime_fallback(path: str, info: dict) -> dict:
    if info.get("captured_at") is None:
        try:
            mtime = os.stat(path).st_mtime
            info["captured_at"] = datetime.fromtimestamp(mtime).isoformat(
                timespec="seconds"
            )
        except OSError:
            pass
    return info


def _extract_pillow_exif(path: str) -> dict:
    from PIL import Image  # deferred: unsafe at module level in DSH

    _ensure_heif_opener()
    with Image.open(path) as image:
        info = _exif_fields_from_image(image)
    return _apply_mtime_fallback(path, info)


def _extract_raw_exif(path: str) -> dict:
    """RAW files: rawpy's embedded preview, then Pillow on the preview bytes."""
    import io

    import rawpy  # deferred: native library

    with rawpy.imread(path) as raw:
        thumb = raw.extract_thumb()
    if thumb.format == rawpy.ThumbFormat.JPEG:
        from PIL import Image

        with Image.open(io.BytesIO(thumb.data)) as image:
            info = _exif_fields_from_image(image)
    else:  # BITMAP preview: geometry only, no EXIF container to parse
        height, width = thumb.data.shape[:2]
        info = {
            "captured_at": None,
            "camera": None,
            "exif": {},
            "width": int(width),
            "height": int(height),
        }
    return _apply_mtime_fallback(path, info)


def extract_exif(path: str) -> dict:
    """Extract metadata for a supported image file.

    Returns {"captured_at", "camera", "exif", "width", "height"} on success;
    captured_at falls back to the file's mtime (ISO) when no EXIF timestamp
    exists. Returns {} when the file cannot be decoded at all — scan() still
    indexes it (format, size, md5) with empty EXIF and no image hashes, and
    records the failure in ScanReport.errors.
    """
    fmt = SUPPORTED.get(os.path.splitext(str(path))[1].lower())
    if fmt is None:
        return {}
    try:
        if fmt == "raw":
            try:
                return _extract_raw_exif(path)
            except Exception:
                return _extract_pillow_exif(path)  # some DNGs are plain TIFF
        return _extract_pillow_exif(path)
    except Exception:
        return {}


# --- discovery and indexing ---------------------------------------------------


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _discover(source_dirs: list[str], rejected_root: str) -> list[str]:
    """All supported image files under source_dirs, in deterministic order.

    Walks recursively, prunes hidden directories and the rejected_root subtree.
    """
    rejected = os.path.abspath(rejected_root) if rejected_root else ""
    found: list[str] = []
    for source in source_dirs:
        root = os.path.abspath(str(source))
        if not os.path.isdir(root):
            continue  # directory vanished since settings were saved
        if rejected and _is_within(root, rejected):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".")
                # rejected copies live inside source dirs by default;
                # never re-index them (also covers the explicit-root mode)
                and name != REJECTED_DIR_NAME
                and not (
                    rejected
                    and _is_within(os.path.join(dirpath, name), rejected)
                )
            )
            for filename in sorted(filenames):
                if os.path.splitext(filename)[1].lower() in SUPPORTED:
                    found.append(os.path.join(dirpath, filename))
    return found


def _next_photo_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM photos").fetchone()[0]


def _exif_json(info: dict) -> str | None:
    # None marks "could not decode" (fake/corrupt files); "{}" means decoded
    # but tagless — the distinction keeps downstream quality decisions honest.
    return json.dumps(info["exif"], ensure_ascii=False) if info else None


def _index_new_file(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    size: int,
    fmt: str,
    photo_id: int,
) -> dict:
    info = extract_exif(path)
    cursor = conn.execute(
        "INSERT INTO files (path, mtime, size, file_hash, format, "
        "captured_at, camera, exif_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            path,
            mtime,
            size,
            _md5(path),
            fmt,
            info.get("captured_at"),
            info.get("camera"),
            _exif_json(info),
        ),
    )
    file_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO photos (id, primary_file_id, group_id) VALUES (?, ?, NULL)",
        (photo_id, file_id),
    )
    conn.execute("UPDATE files SET photo_id = ? WHERE id = ?", (photo_id, file_id))
    return info


def _reindex_changed_file(
    conn: sqlite3.Connection,
    file_id: int,
    path: str,
    mtime: float,
    size: int,
    fmt: str,
) -> dict:
    info = extract_exif(path)
    conn.execute(
        "UPDATE files SET mtime = ?, size = ?, file_hash = ?, format = ?, "
        "captured_at = ?, camera = ?, exif_json = ?, "
        "hash_phash = NULL, hash_ahash = NULL, hash_dhash = NULL "
        "WHERE id = ?",
        (
            mtime,
            size,
            _md5(path),
            fmt,
            info.get("captured_at"),
            info.get("camera"),
            _exif_json(info),
            file_id,
        ),
    )
    return info


def _begin_scan_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO scan_runs (started_at, finished_at, phase, progress, error) "
        "VALUES (?, NULL, 'scan', 0.0, NULL)",
        (_now_iso(),),
    )
    return cursor.lastrowid


def _finish_scan_run(conn: sqlite3.Connection, run_id: int, error=None) -> None:
    if error is None:
        conn.execute(
            "UPDATE scan_runs SET finished_at = ?, progress = 1.0 WHERE id = ?",
            (_now_iso(), run_id),
        )
    else:
        conn.execute(
            "UPDATE scan_runs SET finished_at = ?, error = ? WHERE id = ?",
            (_now_iso(), str(error), run_id),
        )


def scan(
    conn: sqlite3.Connection,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> ScanReport:
    """Index every supported file under the configured source_dirs.

    Incremental: files are compared by (path, mtime, size); only new or
    changed files are re-hashed and re-extracted. Files missing from disk are
    counted but their rows and photos are kept (decided groups stay intact).
    New photos get a RAW+JPG pairing pass at the end. Per-file problems never
    raise — they are collected into ScanReport.errors so one broken file
    cannot abort a large scan.
    """
    report = ScanReport()
    settings = get_settings(conn)
    source_dirs = settings.get("source_dirs") or []
    if not source_dirs:
        return report

    run_id = _begin_scan_run(conn)
    try:
        paths = _discover(source_dirs, settings.get("rejected_root") or "")
        report.total_files = len(paths)
        if progress_cb:
            progress_cb("scan", 0, report.total_files)

        known = {
            row["path"]: row
            for row in conn.execute("SELECT id, path, mtime, size FROM files")
        }
        next_photo_id = _next_photo_id(conn)

        for index, path in enumerate(paths):
            if progress_cb:
                progress_cb("scan", index, report.total_files)
            try:
                try:
                    stat = os.stat(path)
                except OSError as exc:
                    report.errors.append(f"stat failed: {path}: {exc}")
                    continue
                fmt = SUPPORTED[os.path.splitext(path)[1].lower()]
                row = known.get(path)
                if row is None:
                    info = _index_new_file(
                        conn, path, stat.st_mtime, stat.st_size, fmt, next_photo_id
                    )
                    next_photo_id += 1
                    report.new_files += 1
                elif row["mtime"] != stat.st_mtime or row["size"] != stat.st_size:
                    info = _reindex_changed_file(
                        conn, row["id"], path, stat.st_mtime, stat.st_size, fmt
                    )
                    report.changed_files += 1
                else:
                    info = None  # unchanged: nothing to redo
                if info is not None and not info:
                    report.errors.append(f"decode failed: {path}")
            except Exception as exc:
                report.errors.append(f"index failed: {path}: {exc}")
            if (index + 1) % _PROGRESS_STEP == 0 and report.total_files:
                conn.execute(
                    "UPDATE scan_runs SET progress = ? WHERE id = ?",
                    ((index + 1) / report.total_files, run_id),
                )
                # 分批提交：把长扫描切成短事务，避免全量扫描全程持有
                # SQLite 写锁（否则并发写如 update_settings 会在
                # busy_timeout 后抛 database is locked）。增量扫描按
                # (path, mtime, size) 比对，部分提交的行下轮识别为
                # unchanged，幂等安全。
                conn.commit()

        if progress_cb:
            progress_cb("scan", report.total_files, report.total_files)

        on_disk = set(paths)
        report.missing_files = sum(1 for path in known if path not in on_disk)

        report.pair_count = pair_raw_jpg(conn)
        _finish_scan_run(conn, run_id)
        conn.commit()
    except Exception as exc:  # fatal: keep the scan_runs row honest, then raise
        _finish_scan_run(conn, run_id, error=str(exc))
        conn.commit()
        raise
    return report


# --- RAW+JPG pairing ----------------------------------------------------------


def _parse_iso(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _capture_gap_seconds(a, b) -> float | None:
    ta = _parse_iso(a["captured_at"])
    tb = _parse_iso(b["captured_at"])
    if ta is None or tb is None:
        return None
    return abs((ta - tb).total_seconds())


def _already_paired(raw_row, jpg_row) -> bool:
    photo_id = raw_row["photo_id"]
    return photo_id is not None and photo_id == jpg_row["photo_id"]


def _find_jpg_partner(raw_row, jpg_rows, claimed: set[int], photo_raw_count=None,
                      conn=None):
    """Pick the jpg to pair with a raw file: same stem first, then nearest Δt.

    Jpgs whose photo already carries a raw are excluded — they are spoken
    for, no matter which run asks. ``photo_raw_count`` maps photo_id to the
    number of raw files it carries (precomputed; a per-candidate COUNT(*)
    would rescan the whole files table once per candidate). Candidates
    whose phash disagrees with the raw (when both hashes exist) are also
    rejected: a time-proximity pairing is only a heuristic, the hashes are
    ground truth for "same photo".
    """
    candidates = [
        jpg
        for jpg in jpg_rows
        if jpg["id"] not in claimed and not _already_paired(raw_row, jpg)
        and not (photo_raw_count is not None
                 and photo_raw_count.get(jpg["photo_id"], 0) > 0)
    ]
    if not candidates:
        return None
    stem = Path(raw_row["path"]).stem.lower()
    for jpg in candidates:  # rule 1: same directory + same basename stem
        if Path(jpg["path"]).stem.lower() == stem:
            return jpg
    timed = []  # rule 2: same directory + capture time < 2s apart
    for jpg in candidates:
        gap = _capture_gap_seconds(raw_row, jpg)
        if gap is not None and gap < _PAIR_MAX_CAPTURE_GAP_SEC:
            if conn is not None:
                phash_gap = _phash_gap(conn, raw_row["id"], jpg["id"])
                if phash_gap is not None and phash_gap > 10:
                    continue  # different photos: same-second is coincidence
            timed.append((gap, jpg["id"], jpg))
    if not timed:
        return None
    timed.sort(key=lambda item: (item[0], item[1]))
    return timed[0][2]


def _merge_into_raw_photo(conn: sqlite3.Connection, raw_row, jpg_row) -> None:
    """Fold the jpg's photo (if any) into the raw's photo; primary becomes raw."""
    raw_photo_id = raw_row["photo_id"]
    jpg_photo_id = jpg_row["photo_id"]
    if raw_photo_id is None:
        if jpg_photo_id is None:
            cursor = conn.execute(
                "INSERT INTO photos (primary_file_id, group_id) VALUES (?, NULL)",
                (raw_row["id"],),
            )
            raw_photo_id = cursor.lastrowid
            conn.execute(
                "UPDATE files SET photo_id = ? WHERE id = ?",
                (raw_photo_id, raw_row["id"]),
            )
        else:
            raw_photo_id = jpg_photo_id
    elif jpg_photo_id is not None and jpg_photo_id != raw_photo_id:
        groups = {
            row["id"]: row["group_id"]
            for row in conn.execute(
                "SELECT id, group_id FROM photos WHERE id IN (?, ?)",
                (raw_photo_id, jpg_photo_id),
            )
        }
        if groups.get(raw_photo_id) is None and groups.get(jpg_photo_id) is not None:
            conn.execute(
                "UPDATE photos SET group_id = ? WHERE id = ?",
                (groups[jpg_photo_id], raw_photo_id),
            )
        conn.execute(
            "UPDATE files SET photo_id = ? WHERE photo_id = ?",
            (raw_photo_id, jpg_photo_id),
        )
        conn.execute("DELETE FROM quality_scores WHERE photo_id = ?", (jpg_photo_id,))
        conn.execute("DELETE FROM photos WHERE id = ?", (jpg_photo_id,))
    conn.execute(
        "UPDATE files SET photo_id = ? WHERE id = ?", (raw_photo_id, jpg_row["id"])
    )
    conn.execute(
        "UPDATE photos SET primary_file_id = ? WHERE id = ?", (raw_row["id"], raw_photo_id)
    )


def _phash_gap(conn, file_id_a, file_id_b):
    """Hamming distance between two files' phashes; None when either hash
    is missing (pairing runs before hashing on first scans — absence is
    "cannot tell", not "different photos")."""
    from app.hashing import phash_distance

    rows = conn.execute(
        "SELECT id, hash_phash FROM files WHERE id IN (?, ?)", (file_id_a, file_id_b)
    ).fetchall()
    hashes = {r["id"]: r["hash_phash"] for r in rows}
    a, b = hashes.get(file_id_a), hashes.get(file_id_b)
    if not a or not b:
        return None
    return phash_distance(a, b)


def repair_overmerged_photos(conn: sqlite3.Connection) -> int:
    """Split photos that accumulated more than one raw-jpg pair.

    Historical bug: pair_raw_jpg's idempotency check only excluded jpgs
    already sharing the raw's photo, while the per-call claim set reset on
    every pipeline run — so each re-run wed one more same-second jpg onto
    the same raw photo ( Huawei batch exports share one mtime second ).
    Worse, Huawei batch exports also fake the capture-time pairing itself:
    an unrelated same-second jpg gets paired with a raw. A photo now keeps
    at most one raw + one jpg, and only when their phash distance is
    within the similarity threshold (they must be the same photo); every
    extra or hash-disagreeing file becomes a standalone photo again.
    Hashes survive on the files rows; the split photos re-enter analysis
    (no quality_scores) and regrouping on the next pipeline run.
    Returns the number of photos split.
    """
    _PAIR_HASH_MAX_DISTANCE = 10  # same threshold as grouper similarity
    over = list(
        conn.execute(
            "SELECT photo_id, COUNT(*) AS n FROM files"
            " WHERE photo_id IS NOT NULL"
            " GROUP BY photo_id HAVING n > 1"
        )
    )
    if not over:
        return 0
    split = 0
    for row in over:
        photo_id = row["photo_id"]
        files = list(
            conn.execute(
                "SELECT id, format, captured_at, photo_id FROM files"
                " WHERE photo_id = ? ORDER BY id",
                (photo_id,),
            )
        )
        raws = [f for f in files if f["format"] == "raw"]
        keep_ids = {f["id"] for f in files if f["format"] != "raw" and f["format"] != "jpg"}
        if raws:
            keep_ids.add(raws[0]["id"])
            # keep the jpg closest in capture time to the raw — but only if
            # the hashes agree they are the same photo: Huawei batch exports
            # share one mtime second, so nearest-in-time is meaningless there
            jpgs = [f for f in files if f["format"] == "jpg"]
            if jpgs:
                def gap(jpg):
                    a = raws[0]["captured_at"]
                    b = jpg["captured_at"]
                    if a and b:
                        try:
                            from datetime import datetime
                            return abs(
                                (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()
                            )
                        except ValueError:
                            return float("inf")
                    return float("inf")
                best = min(jpgs, key=lambda j: (gap(j), j["id"]))
                phash_gap = _phash_gap(conn, raws[0]["id"], best["id"])
                if phash_gap is None or phash_gap <= _PAIR_HASH_MAX_DISTANCE:
                    keep_ids.add(best["id"])
        else:
            # no raw: keep the single first file only
            keep_ids.add(files[0]["id"])
        extras = [f for f in files if f["id"] not in keep_ids]
        if not extras:
            continue
        old_ids = {f["photo_id"] for f in extras if f["photo_id"] is not None}
        next_id = _next_photo_id(conn)
        for extra in extras:
            conn.execute(
                "INSERT INTO photos (id, primary_file_id, group_id)"
                " VALUES (?, ?, NULL)",
                (next_id, extra["id"]),
            )
            conn.execute(
                "UPDATE files SET photo_id = ? WHERE id = ?", (next_id, extra["id"])
            )
            next_id += 1
        # drop now-empty photo shells left behind (jpg photos the files
        # abandoned), together with their quality scores
        for old_id in old_ids:
            n = conn.execute(
                "SELECT COUNT(*) FROM files WHERE photo_id = ?", (old_id,)
            ).fetchone()[0]
            if n == 0:
                conn.execute(
                    "DELETE FROM quality_scores WHERE photo_id = ?", (old_id,)
                )
                conn.execute("DELETE FROM photos WHERE id = ?", (old_id,))
        split += 1
    conn.commit()
    return split


def pair_raw_jpg(conn: sqlite3.Connection) -> int:
    """Pair same-scene RAW+JPG files into single photos (primary stays RAW).

    Rule 1: same directory + same basename stem (IMG_1.CR3 / IMG_1.JPG).
    Rule 2: same directory + capture times less than 2 seconds apart.
    Already-paired files are left alone, so repeated calls are idempotent.
    Repairs photos over-merged by older runs before pairing.
    Returns the number of newly formed pairs.
    """
    repair_overmerged_photos(conn)
    rows = list(
        conn.execute(
            "SELECT id, path, format, photo_id, captured_at FROM files ORDER BY id"
        )
    )
    by_directory: dict[str, list] = defaultdict(list)
    for row in rows:
        by_directory[os.path.dirname(row["path"])].append(row)

    # one GROUP BY per format instead of a full-table COUNT(*) per
    # (raw, candidate-jpg) pair — Huawei batch directories hold thousands
    # of same-second files, and per-candidate scans turned quadratic
    photo_jpg_count = {
        row["photo_id"]: row["n"]
        for row in conn.execute(
            "SELECT photo_id, COUNT(*) AS n FROM files"
            " WHERE photo_id IS NOT NULL AND format = 'jpg'"
            " GROUP BY photo_id"
        )
    }
    photo_raw_count = {
        row["photo_id"]: row["n"]
        for row in conn.execute(
            "SELECT photo_id, COUNT(*) AS n FROM files"
            " WHERE photo_id IS NOT NULL AND format = 'raw'"
            " GROUP BY photo_id"
        )
    }

    claimed: set[int] = set()
    pair_count = 0
    for directory in sorted(by_directory):
        entries = by_directory[directory]
        jpgs = [row for row in entries if row["format"] == "jpg"]
        for raw_row in (row for row in entries if row["format"] == "raw"):
            if raw_row["id"] in claimed:
                continue
            # a raw whose photo already carries a jpg is paired: re-runs must
            # not wedge another same-second jpg onto it (per-call claim sets
            # reset, so composition — not memory — must enforce idempotency)
            if photo_jpg_count.get(raw_row["photo_id"], 0) >= 1:
                continue
            partner = _find_jpg_partner(
                raw_row, jpgs, claimed, photo_raw_count, conn
            )
            if partner is None:
                continue
            _merge_into_raw_photo(conn, raw_row, partner)
            claimed.add(raw_row["id"])
            claimed.add(partner["id"])
            pair_count += 1
            # keep the maps in step with the merge we just performed
            raw_photo = raw_row["photo_id"] or partner["photo_id"]
            photo_jpg_count[raw_photo] = photo_jpg_count.get(raw_photo, 0) + 1
            if partner["photo_id"] is not None and partner["photo_id"] != raw_photo:
                photo_jpg_count[partner["photo_id"]] = 0
    conn.commit()
    return pair_count
