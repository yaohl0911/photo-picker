"""Choreography tests for app.hashing.compute_hashes — DSH-safe.

imagehash/PIL never load here: app.hashing._hash_image and
app.imaging.decode_for_analysis are monkeypatched, so what is under test is
the database logic — which rows get hashed, skip rules for already-hashed
and photo-less rows, error capture that keeps hash columns NULL, and
progress reporting — plus phash_distance's pure-Python hamming arithmetic.
Real-image tests live in test_hashing.py (skipped inside DSH).
"""

from app import hashing, imaging

PH = "1" * 16
AH = "2" * 16
DH = "3" * 16


def hex_at(base_hex, dist):
    """Hex string at exactly `dist` hamming distance from base_hex."""
    v = int(base_hex, 16) ^ ((1 << dist) - 1)
    return f"{v:016x}"


def ins_file(db, path, with_photo=True):
    """Insert a file row (+ its owning photo when with_photo); return file id."""
    photo_id = None
    if with_photo:
        cur = db.execute(
            "INSERT INTO photos (primary_file_id, group_id) VALUES (NULL, NULL)"
        )
        photo_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO files (path, mtime, size, format, photo_id)"
        " VALUES (?, 1000.0, 10, 'jpg', ?)",
        (str(path), photo_id),
    )
    file_id = cur.lastrowid
    if with_photo:
        db.execute(
            "UPDATE photos SET primary_file_id = ? WHERE id = ?",
            (file_id, photo_id),
        )
    return file_id


def _patch_pipeline(monkeypatch, hash_image=None, decode=None):
    """Replace the two native seams of compute_hashes with fakes."""
    if hash_image is None:
        def hash_image(img):
            return (PH, AH, DH)
    if decode is None:
        def decode(path):
            return object()  # opaque stand-in image

    monkeypatch.setattr(hashing, "_hash_image", hash_image)
    monkeypatch.setattr(imaging, "decode_for_analysis", decode)


# --- compute_hashes orchestration ---------------------------------------------


def test_compute_hashes_writes_three_hash_columns(db, monkeypatch):
    _patch_pipeline(monkeypatch)
    fid = ins_file(db, "dir/a.jpg")

    res = hashing.compute_hashes(db)

    assert res == {"hashed": 1, "skipped": 0, "errors": []}
    row = db.execute(
        "SELECT hash_phash, hash_ahash, hash_dhash FROM files WHERE id = ?", (fid,)
    ).fetchone()
    assert (row["hash_phash"], row["hash_ahash"], row["hash_dhash"]) == (PH, AH, DH)


def test_compute_hashes_skips_already_hashed_row(db, monkeypatch):
    _patch_pipeline(monkeypatch)
    fid = ins_file(db, "dir/a.jpg")
    assert hashing.compute_hashes(db)["hashed"] == 1

    seen = []

    def hash_image(img):
        seen.append(img)
        return ("f" * 16, "f" * 16, "f" * 16)

    _patch_pipeline(monkeypatch, hash_image=hash_image)
    res = hashing.compute_hashes(db)

    assert res == {"hashed": 0, "skipped": 1, "errors": []}
    assert seen == []  # hash never recomputed for the already-hashed row
    row = db.execute("SELECT hash_phash FROM files WHERE id = ?", (fid,)).fetchone()
    assert row["hash_phash"] == PH


def test_compute_hashes_skips_file_without_photo(db, monkeypatch):
    _patch_pipeline(monkeypatch)
    fid = ins_file(db, "dir/stray.jpg", with_photo=False)

    res = hashing.compute_hashes(db)

    assert res == {"hashed": 0, "skipped": 1, "errors": []}
    row = db.execute("SELECT hash_phash FROM files WHERE id = ?", (fid,)).fetchone()
    assert row["hash_phash"] is None


def test_decode_error_keeps_columns_null_and_records_error(db, monkeypatch):
    def boom(path):
        raise imaging.DecodeError("unreadable file")

    _patch_pipeline(monkeypatch, decode=boom)
    fid = ins_file(db, "dir/broken.jpg")

    res = hashing.compute_hashes(db)

    assert res["hashed"] == 0 and res["skipped"] == 0
    assert len(res["errors"]) == 1
    assert "dir/broken.jpg" in res["errors"][0]
    row = db.execute(
        "SELECT hash_phash, hash_ahash, hash_dhash FROM files WHERE id = ?", (fid,)
    ).fetchone()
    assert (row["hash_phash"], row["hash_ahash"], row["hash_dhash"]) == (None, None, None)


def test_unexpected_exception_is_captured_not_raised(db, monkeypatch):
    def boom(path):
        raise RuntimeError("imagehash exploded")

    _patch_pipeline(monkeypatch, decode=boom)
    fid = ins_file(db, "dir/weird.jpg")

    res = hashing.compute_hashes(db)

    assert res["hashed"] == 0 and res["skipped"] == 0
    assert len(res["errors"]) == 1
    assert "imagehash exploded" in res["errors"][0]
    row = db.execute("SELECT hash_phash FROM files WHERE id = ?", (fid,)).fetchone()
    assert row["hash_phash"] is None


def test_progress_callback_reports_every_row(db, monkeypatch):
    _patch_pipeline(monkeypatch)
    ins_file(db, "dir/a.jpg")
    ins_file(db, "dir/b.jpg")
    ins_file(db, "dir/stray.jpg", with_photo=False)
    calls = []

    res = hashing.compute_hashes(db, progress_cb=lambda *a, **kw: calls.append(kw if kw else dict(phase=a[0], done=a[1], total=a[2])))

    assert res["hashed"] == 2 and res["skipped"] == 1
    assert [c["done"] for c in calls] == [1, 2, 3]
    assert all(c["phase"] == "hash" and c["total"] == 3 for c in calls)


# --- phash_distance pure arithmetic ---------------------------------------------


def test_phash_distance_identical_is_zero():
    h = "9e3779b97f4a7c15"
    assert hashing.phash_distance(h, h) == 0
    # hex is case-insensitive
    assert hashing.phash_distance("f" * 16, "F" * 16) == 0


def test_phash_distance_counts_flipped_bits():
    base = "1234567890abcdef"
    for dist in (1, 5, 17, 40, 64):
        assert hashing.phash_distance(base, hex_at(base, dist)) == dist
        assert hashing.phash_distance(hex_at(base, dist), base) == dist


def test_phash_distance_none_or_empty_is_max_distance():
    h = "00ff00ff00ff00ff"
    for bad in (None, ""):
        assert hashing.phash_distance(bad, h) == 64
        assert hashing.phash_distance(h, bad) == 64
    assert hashing.phash_distance(None, None) == 64


def test_phash_distance_max_for_disjoint_bit_patterns():
    a = (1 << 32) - 1  # low 32 bits set
    b = ((1 << 32) - 1) << 32  # high 32 bits set
    assert hashing.phash_distance(f"{a:016x}", f"{b:016x}") == 64
