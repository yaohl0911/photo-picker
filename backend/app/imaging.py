"""Image decoding, thumbnail and preview caching for photo-picker.

Design constraints (docs/DEVENV.md): native-extension libraries (PIL,
pillow_heif, rawpy) cannot load inside DSH bash processes (AMFI library
validation). Therefore this module's top level imports ONLY the standard
library, and every third-party import is deferred to inside the function
that needs it. ``import app.imaging`` must be safe everywhere.

Cache layout (relative to cwd by default, override via CACHE_ROOT — tests
monkeypatch it)::

    {CACHE_ROOT}/thumbs/{file_id}_{size}_{int(mtime)}.webp
    {CACHE_ROOT}/previews/{file_id}_{max_side}_{int(mtime)}.jpg

The integer ``files.mtime`` embedded in the file name is the staleness key:
when a photo is re-saved its mtime changes, the computed cache path changes,
the old file is removed on the next miss, and a fresh entry is written.
``enforce_cache_limit`` bounds total cache size with LRU-by-atime eviction.
"""

import io
import sqlite3
from pathlib import Path

# Root directory for all caches ("data" by default, relative to cwd).
# Module-level on purpose: functions read it at call time so tests can
# monkeypatch it (imaging.CACHE_ROOT = tmp_path).
CACHE_ROOT = "data"

# Set once pillow_heif.register_heif_opener() has been attempted. The flag
# itself is only a bool at module top level; the actual registration call is
# deferred into _register_heif_opener().
_HEIF_REGISTERED = False

# RAW extensions routed to rawpy (full set rawpy supports in this app).
RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng"}


class DecodeError(Exception):
    """An image file could not be decoded by any available strategy."""


def decode(path, max_side=512):
    """Decode an image file into an RGB PIL.Image.

    JPG/PNG/HEIC go straight through PIL (HEIC via the pillow_heif opener
    registered once by _register_heif_opener); RAW files go through rawpy
    with a fallback to their embedded preview. The result is converted to
    RGB and downscaled proportionally (LANCZOS) so its longest side is
    <= max_side (0 or None disables scaling). Raises DecodeError when
    nothing can decode the file.

    Third-party imports are deferred into the helpers this calls so that
    importing this module stays stdlib-only.
    """
    from PIL import Image  # deferred: native extension

    ext = Path(path).suffix.lower()
    try:
        if ext in RAW_EXTENSIONS:
            img = _decode_raw(path)
        else:
            img = _open_with_pil(path)
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError(f"cannot decode {path}: {exc}") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")
    if max_side and max(img.size) > max_side:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img.thumbnail((max_side, max_side), resample)
    return img


def _register_heif_opener() -> None:
    """Register pillow_heif's HEIF opener with PIL exactly once.

    Failure (library missing or unloadable) is tolerated: HEIC files will
    fail to open later, JPG/PNG keep working. The module-level flag only
    records that an attempt happened; the call itself lives here so the
    module top level never imports pillow_heif.
    """
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif  # deferred: native extension

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    _HEIF_REGISTERED = True


def _open_with_pil(path):
    """Open a JPG/PNG/HEIC file with PIL, loading pixel data eagerly."""
    from PIL import Image  # deferred: native extension

    _register_heif_opener()
    img = Image.open(path)
    img.load()  # force decode now so corrupt data raises in this frame
    return img


def _decode_raw(path):
    """Decode a RAW file via rawpy, falling back to the embedded preview."""
    import rawpy  # deferred: native extension
    from PIL import Image  # deferred: native extension

    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
    except Exception:
        return raw_embedded_preview(path)
    return Image.fromarray(rgb)


def raw_embedded_preview(path):
    """Extract the embedded preview image of a RAW file as a PIL.Image.

    Uses rawpy's extract_thumb(): JPEG thumbs are wrapped in BytesIO and
    opened by PIL, bitmap thumbs go through Image.fromarray. Raises
    DecodeError when rawpy cannot open the file or no usable thumb exists.
    """
    import rawpy  # deferred: native extension
    from PIL import Image  # deferred: native extension

    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
    except Exception as exc:
        raise DecodeError(
            f"cannot extract embedded preview from {path}: {exc}"
        ) from exc
    fmt = str(getattr(thumb, "format", "")).upper()
    if "JPEG" in fmt:
        img = Image.open(io.BytesIO(thumb.data))
        img.load()
        return img
    if "BITMAP" in fmt:
        return Image.fromarray(thumb.data)
    raise DecodeError(f"unsupported embedded thumb format {thumb.format!r} in {path}")


def decode_for_analysis(path):
    """Decode at the standard analysis size for hashing/quality scoring."""
    return decode(path, max_side=256)


def _file_row(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row:
    """Fetch (path, mtime) for file_id; KeyError when the row is missing."""
    row = conn.execute(
        "SELECT path, mtime FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no files row with id {file_id!r}")
    return row


def _cached_bytes(
    conn: sqlite3.Connection,
    file_id: int,
    size: int,
    subdir: str,
    ext: str,
    save_format: str,
    quality: int,
) -> bytes:
    """Shared get_thumb/get_preview pipeline (see module docstring)."""
    row = _file_row(conn, file_id)
    src = row["path"]
    mtime = int(row["mtime"] or 0)
    cache_dir = Path(CACHE_ROOT) / subdir
    cache_path = cache_dir / f"{file_id}_{size}_{mtime}{ext}"
    if not cache_path.is_file():
        # Directories are created lazily on first cache miss.
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Drop stale entries for this file/size carrying an old mtime.
        for stale in cache_dir.glob(f"{file_id}_{size}_*{ext}"):
            stale.unlink(missing_ok=True)
        img = decode(src, max_side=size)
        img.save(str(cache_path), format=save_format, quality=quality)
        enforce_cache_limit()
    return cache_path.read_bytes()


def get_thumb(conn: sqlite3.Connection, file_id: int, size: int = 256) -> bytes:
    """WebP (quality 80) bytes for a file, served from/into the thumb cache."""
    return _cached_bytes(conn, file_id, size, "thumbs", ".webp", "WEBP", 80)


def get_preview(conn: sqlite3.Connection, file_id: int, max_side: int = 1600) -> bytes:
    """JPEG (quality 85) bytes for a file, served from/into the preview cache."""
    return _cached_bytes(conn, file_id, max_side, "previews", ".jpg", "JPEG", 85)


def enforce_cache_limit(root=None, max_bytes: int = 2 * 1024 ** 3) -> int:
    """Evict cache files so the total stays within budget; return bytes freed.

    Walks every file under ``root`` (default CACHE_ROOT), sorts by access
    time, and when the total exceeds ``max_bytes`` deletes the oldest files
    until at most 90% of the budget remains. Missing roots are a no-op.
    """
    if root is None:
        root = CACHE_ROOT
    root = Path(root)
    if not root.is_dir():
        return 0
    entries = []  # (atime, size, path)
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            entries.append((st.st_atime, st.st_size, p))
            total += st.st_size
    if total <= max_bytes:
        return 0
    target = int(max_bytes * 0.9)
    entries.sort(key=lambda e: e[0])
    removed = 0
    for _atime, size, p in entries:
        if total - removed <= target:
            break
        try:
            p.unlink()
            removed += size
        except OSError:
            continue
    return removed
