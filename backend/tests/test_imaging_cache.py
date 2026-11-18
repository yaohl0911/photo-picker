"""Cache-layer tests for app.imaging — runnable inside DSH without PIL.

All decoding is faked: ``app.imaging.decode`` is monkeypatched to return a
FakeImage whose ``save()`` writes real bytes to disk, so the caching logic
(paths, mtime keys, LRU eviction, lazy dirs) is exercised with pure stdlib.
This file must NEVER import PIL (see docs/DEVENV.md).
"""

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import imaging


class FakeImage:
    """Minimal PIL.Image stand-in: save() writes real bytes to disk."""

    def __init__(self, payload: bytes, size=(320, 240)):
        self.payload = payload
        self.size = size

    def save(self, path, format=None, quality=None, **kwargs):
        Path(path).write_bytes(self.payload)


class FakeDecoder:
    """Counts decode() calls; each call yields a distinct payload."""

    def __init__(self):
        self.calls = []

    def __call__(self, path, max_side=512):
        self.calls.append((str(path), max_side))
        return FakeImage(f"FAKEIMG-{max_side}-{len(self.calls)}".encode())


@pytest.fixture()
def fake_decode(monkeypatch):
    decoder = FakeDecoder()
    monkeypatch.setattr(imaging, "decode", decoder)
    return decoder


@pytest.fixture()
def cache_root(monkeypatch, tmp_path):
    root = tmp_path / "cache"
    monkeypatch.setattr(imaging, "CACHE_ROOT", root)
    return root


def _insert_file(conn, tmp_path, mtime=1763000000.5, name="photo.jpg"):
    """Insert one files row pointing at a real (dummy) file on disk."""
    path = tmp_path / name
    path.write_bytes(b"dummy-bytes")
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, format) VALUES (?, ?, 2048, 'jpg')",
        (str(path), mtime),
    )
    conn.commit()
    return cur.lastrowid


def test_import_module_is_safe_without_native_libs():
    # Hard requirement: importing app.imaging must not pull PIL/rawpy/pillow_heif
    # at module top level (their C extensions crash under DSH/AMFI).
    sys.modules.pop("app.imaging", None)
    mod = importlib.import_module("app.imaging")
    assert mod.DecodeError is not None


def test_get_thumb_creates_cache_then_second_call_hits(
    db, tmp_path, fake_decode, cache_root
):
    file_id = _insert_file(db, tmp_path, mtime=1763000000.5)
    thumb_dir = cache_root / "thumbs"

    data1 = imaging.get_thumb(db, file_id, size=256)

    assert data1 == b"FAKEIMG-256-1"
    assert len(fake_decode.calls) == 1
    # decode was asked for the requested thumb size
    assert fake_decode.calls[0][1] == 256
    cache_file = thumb_dir / f"{file_id}_256_1763000000.webp"
    assert cache_file.is_file()
    assert cache_file.read_bytes() == data1

    data2 = imaging.get_thumb(db, file_id, size=256)

    assert data2 == data1
    assert len(fake_decode.calls) == 1  # cache hit: no re-decode


def test_get_thumb_mtime_change_invalidates_and_cleans_old_cache(
    db, tmp_path, fake_decode, cache_root
):
    file_id = _insert_file(db, tmp_path, mtime=1763000000.5)
    old_cache = cache_root / "thumbs" / f"{file_id}_256_1763000000.webp"

    data1 = imaging.get_thumb(db, file_id, size=256)
    assert old_cache.is_file()

    # The photo is edited: mtime moves forward.
    db.execute("UPDATE files SET mtime = ? WHERE id = ?", (1763000042.75, file_id))
    db.commit()

    data2 = imaging.get_thumb(db, file_id, size=256)

    assert data2 == b"FAKEIMG-256-2"  # regenerated, not the stale bytes
    assert len(fake_decode.calls) == 2
    assert not old_cache.exists()  # stale-mtime cache file removed
    new_cache = cache_root / "thumbs" / f"{file_id}_256_1763000042.webp"
    assert new_cache.is_file()
    assert new_cache.read_bytes() == data2


def test_get_preview_creates_lazy_dir_and_recreates_after_removal(
    db, tmp_path, fake_decode, cache_root
):
    file_id = _insert_file(db, tmp_path, name="photo2.jpg")
    preview_dir = cache_root / "previews"
    assert not preview_dir.exists()  # nothing created before first use

    data1 = imaging.get_preview(db, file_id, max_side=1600)

    assert data1 == b"FAKEIMG-1600-1"
    assert fake_decode.calls[0][1] == 1600
    preview_file = preview_dir / f"{file_id}_1600_1763000000.jpg"
    assert preview_file.is_file()
    assert preview_file.read_bytes() == data1

    data2 = imaging.get_preview(db, file_id, max_side=1600)
    assert data2 == data1
    assert len(fake_decode.calls) == 1  # hit

    # Directory wiped (e.g. user cleared the cache): it comes back lazily.
    shutil.rmtree(preview_dir)
    data3 = imaging.get_preview(db, file_id, max_side=1600)

    assert preview_dir.is_dir()
    assert preview_file.is_file()
    assert data3 == b"FAKEIMG-1600-2"
    assert len(fake_decode.calls) == 2


def test_get_preview_mtime_change_invalidates_cache(
    db, tmp_path, fake_decode, cache_root
):
    file_id = _insert_file(db, tmp_path, name="p.jpg")
    old = cache_root / "previews" / f"{file_id}_1600_1763000000.jpg"

    imaging.get_preview(db, file_id, max_side=1600)
    assert old.is_file()

    db.execute("UPDATE files SET mtime = ? WHERE id = ?", (1763000099.0, file_id))
    db.commit()
    imaging.get_preview(db, file_id, max_side=1600)

    assert not old.exists()
    assert (cache_root / "previews" / f"{file_id}_1600_1763000099.jpg").is_file()
    assert len(fake_decode.calls) == 2


def test_enforce_cache_limit_deletes_oldest_until_under_90_percent(tmp_path):
    root = tmp_path / "cache"
    thumbs = root / "thumbs"
    previews = root / "previews"
    thumbs.mkdir(parents=True)
    previews.mkdir()

    # Three 100-byte cache files with ascending atimes (oldest -> newest).
    files = [
        thumbs / "1_256_100.webp",
        thumbs / "2_256_101.webp",
        previews / "3_1600_102.jpg",
    ]
    for i, f in enumerate(files):
        f.write_bytes(b"x" * 100)
        atime = 1_000_000 + i * 1000
        os.utime(f, (atime, atime))

    removed = imaging.enforce_cache_limit(root, max_bytes=250)

    # Total 300 > 250: delete oldest until <= 225 -> only the oldest goes.
    assert removed == 100
    assert not files[0].exists()
    assert files[1].is_file()
    assert files[2].is_file()

    # Calling again at the same limit is a no-op (200 <= 250).
    assert imaging.enforce_cache_limit(root, max_bytes=250) == 0


def test_enforce_cache_limit_noop_when_under_limit_or_missing_root(tmp_path):
    root = tmp_path / "cache"
    thumbs = root / "thumbs"
    thumbs.mkdir(parents=True)
    f = thumbs / "1_256_100.webp"
    f.write_bytes(b"x" * 10)

    assert imaging.enforce_cache_limit(root, max_bytes=1024) == 0
    assert f.is_file()
    # Nonexistent root must not raise.
    assert imaging.enforce_cache_limit(tmp_path / "nope") == 0


def test_missing_file_id_raises_keyerror(db, tmp_path, fake_decode, cache_root):
    _insert_file(db, tmp_path)
    with pytest.raises(KeyError):
        imaging.get_thumb(db, 9999)
    with pytest.raises(KeyError):
        imaging.get_preview(db, 9999)
    assert fake_decode.calls == []  # failed before any decoding
