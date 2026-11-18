"""Real-image perceptual-hash tests — require PIL + imagehash natives.

Skipped entirely inside DSH (docs/DEVENV.md dual-track validation); run in
the user's terminal:

    cd backend && ../.venv/bin/python -m pytest tests/test_hashing.py -v

Builds a synthetic scene, a byte-identical copy, a blurred variant, a
brightened variant and an unrelated scene, runs compute_hashes for real and
checks the pHash ordering guarantee: identical == 0, blur/brightness < 10,
different content > 20. The DSH-safe choreography tests live in
test_hashing_logic.py.
"""

import pytest

# In DSH the C extension raises a plain ImportError (AMFI library
# validation), which importorskip only skips when exc_type=ImportError.
pytest.importorskip("PIL._imaging", exc_type=ImportError)
pytest.importorskip("imagehash", exc_type=ImportError)

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

from app import hashing  # noqa: E402


def _write_scene(path):
    """Deterministic structured scene: horizontal gradient + shapes."""
    w, h = 320, 240
    img = Image.new("RGB", (w, h), (18, 24, 40))
    draw = ImageDraw.Draw(img)
    for x in range(0, w, 4):
        v = int(255 * x / w)
        draw.rectangle((x, 0, x + 3, h), fill=(v, v // 2, 255 - v))
    draw.ellipse((40, 40, 120, 130), fill=(240, 200, 60))
    draw.rectangle((160, 130, 280, 200), outline=(250, 250, 250), width=6)
    draw.polygon([(20, 210), (110, 170), (60, 235)], fill=(90, 200, 90))
    img.save(path, format="JPEG", quality=92)
    return path


def _write_blurred(src, dst):
    Image.open(src).filter(ImageFilter.GaussianBlur(2.5)).save(
        dst, format="JPEG", quality=90
    )
    return dst


def _write_brightened(src, dst):
    ImageEnhance.Brightness(Image.open(src)).enhance(1.5).save(
        dst, format="JPEG", quality=92
    )
    return dst


def _write_different(path):
    """Structurally unrelated scene: vertical gradient + different shapes."""
    w, h = 320, 240
    img = Image.new("RGB", (w, h), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    for y in range(0, h, 4):
        v = int(255 * y / h)
        draw.rectangle((0, y, w, y + 3), fill=(255 - v, v, v // 2))
    draw.ellipse((200, 120, 300, 220), fill=(60, 60, 240))
    draw.polygon([(0, 0), (120, 0), (0, 120)], fill=(210, 210, 210))
    img.save(path, format="JPEG", quality=92)
    return path


def _ins_file(db, path):
    """Insert photo + file rows for a real path; return the file id."""
    cur = db.execute(
        "INSERT INTO photos (primary_file_id, group_id) VALUES (NULL, NULL)"
    )
    pid = cur.lastrowid
    cur = db.execute(
        "INSERT INTO files (path, mtime, size, format, photo_id)"
        " VALUES (?, 1000.0, 1, 'jpg', ?)",
        (str(path), pid),
    )
    db.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?", (cur.lastrowid, pid))
    return cur.lastrowid


def test_hash_image_returns_16_char_hex_triple(tmp_path):
    p = _write_scene(tmp_path / "scene.jpg")

    ph, ah, dh = hashing._hash_image(Image.open(p))

    for hx in (ph, ah, dh):
        assert len(hx) == 16
        int(hx, 16)  # must be valid hex


def test_real_image_distance_ordering(tmp_path, db):
    a = _write_scene(tmp_path / "a.jpg")
    same = tmp_path / "a_copy.jpg"
    same.write_bytes(a.read_bytes())
    blur = _write_blurred(a, tmp_path / "a_blur.jpg")
    bright = _write_brightened(a, tmp_path / "a_bright.jpg")
    diff = _write_different(tmp_path / "b.jpg")

    ids = {_ins_file(db, p): p for p in (a, same, blur, bright, diff)}

    res = hashing.compute_hashes(db)

    assert res["hashed"] == 5
    assert res["skipped"] == 0
    assert res["errors"] == []
    hexes = {
        p: db.execute(
            "SELECT hash_phash FROM files WHERE id = ?", (fid,)
        ).fetchone()["hash_phash"]
        for fid, p in ids.items()
    }
    for hx in hexes.values():
        assert hx  # every real decode produced a hash

    assert hashing.phash_distance(hexes[a], hexes[same]) == 0
    assert hashing.phash_distance(hexes[a], hexes[blur]) < 10
    assert hashing.phash_distance(hexes[a], hexes[bright]) < 10
    assert hashing.phash_distance(hexes[a], hexes[diff]) > 20


def test_compute_hashes_second_run_skips_everything(tmp_path, db):
    a = _write_scene(tmp_path / "a.jpg")
    _ins_file(db, a)
    assert hashing.compute_hashes(db)["hashed"] == 1

    res = hashing.compute_hashes(db)

    assert res == {"hashed": 0, "skipped": 1, "errors": []}
