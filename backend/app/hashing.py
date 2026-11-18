"""Perceptual hashing (pHash/aHash/dHash) for indexed photo files.

DSH constraint (docs/DEVENV.md): PIL and imagehash pull in native
extensions that cannot load inside DSH bash processes. This module's top
level therefore imports nothing beyond stdlib and ``app.imaging`` (itself
stdlib-only at the top), so ``import app.hashing`` is safe everywhere. The
imagehash import lives inside _hash_image(); the decode goes through
app.imaging.decode_for_analysis. Tests split accordingly:
test_hashing_logic.py monkeypatches those two seams (DSH-safe),
test_hashing.py exercises real images (skipped inside DSH).
"""

import sqlite3

from app import imaging


def _hash_image(img):
    """Compute (phash, ahash, dhash) hex strings for a decoded PIL image.

    The only place imagehash is imported (deferred native dependency);
    tests monkeypatch this seam. Returns three 16-char lowercase hex
    strings (64-bit hashes).
    """
    import imagehash  # deferred: native extension

    # average_hash 是 imagehash 4.x/5.x 全系稳定 API（ahash 短别名
    # 只在部分构建里存在，新 venv 的 4.3.2 没有）
    return (
        str(imagehash.phash(img)),
        str(imagehash.average_hash(img)),
        str(imagehash.dhash(img)),
    )


def phash_distance(hex_a, hex_b) -> int:
    """Hamming distance between two hex-encoded 64-bit hashes.

    Pure Python: int(hex, 16) XOR + popcount. Any None/empty/invalid input
    yields 64 — the maximum distance — so a missing hash is conservatively
    "not similar" to everything.
    """
    if not hex_a or not hex_b:
        return 64
    try:
        return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()
    except (ValueError, TypeError):
        return 64


def compute_hashes(conn: sqlite3.Connection, progress_cb=None) -> dict:
    """Fill hash_phash/hash_ahash/hash_dhash for the files that need them.

    A file is hashed when it belongs to a photo and has no phash yet;
    already-hashed files and files without photo attribution are counted
    in ``skipped``. Decoding goes through app.imaging.decode_for_analysis
    (256px analysis size, RAW via embedded preview). Any exception during
    decode/hash leaves the three hash columns NULL and appends
    ``"<path>: <error>"`` to ``errors``. Returns
    ``{"hashed": n, "skipped": n, "errors": [...]}``.

    progress_cb is called after every row as
    ``progress_cb(phase="hash", done=done, total=total)`` where total is
    the number of files rows examined.
    """
    rows = conn.execute(
        "SELECT id, path, photo_id, hash_phash FROM files"
    ).fetchall()
    total = len(rows)
    hashed = 0
    skipped = 0
    errors = []
    for done, row in enumerate(rows, start=1):
        if row["hash_phash"] or row["photo_id"] is None:
            skipped += 1
        else:
            try:
                img = imaging.decode_for_analysis(row["path"])
                ph, ah, dh = _hash_image(img)
                conn.execute(
                    "UPDATE files SET hash_phash = ?, hash_ahash = ?,"
                    " hash_dhash = ? WHERE id = ?",
                    (ph, ah, dh, row["id"]),
                )
                conn.commit()  # incremental: a crash never loses prior work
                hashed += 1
            except Exception as exc:
                errors.append(f"{row['path']}: {exc}")
        if progress_cb is not None:
            progress_cb(phase="hash", done=done, total=total)
    return {"hashed": hashed, "skipped": skipped, "errors": errors}
