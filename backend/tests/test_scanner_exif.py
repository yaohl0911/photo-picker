"""Scanner EXIF-layer tests — require native Pillow (PIL._imaging).

Inside DSH, AMFI library validation rejects PIL._imaging (docs/DEVENV.md),
so this whole module skips there. Run it in the user terminal:

    .venv/bin/python -m pytest backend/tests/test_scanner_exif.py -v
"""

import json
import os
from datetime import datetime

import pytest

# exc_type=ImportError: the DSH failure is a plain ImportError (AMFI dlopen),
# not a ModuleNotFoundError, and pytest >= 8.2 only skips the latter by default.
pytest.importorskip("PIL._imaging", exc_type=ImportError)

from PIL import Image  # noqa: E402  (imported after the skip guard on purpose)

from app.config import update_settings  # noqa: E402
from app.scanner import extract_exif, scan  # noqa: E402


def _make_jpeg(path, size=(64, 48), color=(80, 120, 160), tags=None):
    """Write a small real JPEG, optionally carrying EXIF tags in IFD0."""
    image = Image.new("RGB", size, color)
    if tags:
        exif = Image.Exif()
        for tag, value in tags.items():
            exif[tag] = value
        image.save(path, format="JPEG", exif=exif)
    else:
        image.save(path, format="JPEG")
    return path


def test_extract_exif_datetime_make_model(tmp_path):
    p = _make_jpeg(
        tmp_path / "photo.jpg",
        tags={271: "TestCam", 272: "ModelX", 306: "2020:01:02 03:04:05"},
    )
    info = extract_exif(str(p))
    assert info["captured_at"] == "2020-01-02T03:04:05"
    assert info["camera"] == "TestCam ModelX"
    assert info["exif"]["make"] == "TestCam"
    assert info["exif"]["model"] == "ModelX"
    assert info["width"] == 64
    assert info["height"] == 48


def test_extract_exif_prefers_datetime_original(tmp_path):
    p = _make_jpeg(
        tmp_path / "photo.jpg",
        tags={
            271: "TestCam",
            272: "ModelX",
            306: "2001:01:01 00:00:00",
            36867: "2020:06:07 08:09:10",
        },
    )
    info = extract_exif(str(p))
    assert info["captured_at"] == "2020-06-07T08:09:10"


def test_extract_exif_records_shooting_params(tmp_path):
    p = _make_jpeg(tmp_path / "photo.jpg", tags={34855: 400})
    info = extract_exif(str(p))
    assert info["exif"]["iso"] == 400
    assert info["camera"] is None


def test_extract_exif_without_exif_falls_back_to_mtime(tmp_path):
    p = _make_jpeg(tmp_path / "plain.jpg")
    os.utime(p, (1735689600, 1735689600))
    info = extract_exif(str(p))
    assert info["captured_at"] == datetime.fromtimestamp(1735689600).isoformat(
        timespec="seconds"
    )
    assert info["camera"] is None
    assert info["exif"] == {}
    assert (info["width"], info["height"]) == (64, 48)


def test_extract_exif_fake_file_returns_empty(tmp_path):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"not-a-real-image")
    assert extract_exif(str(p)) == {}

    p2 = tmp_path / "broken.dng"
    p2.write_bytes(b"not-a-real-raw")
    assert extract_exif(str(p2)) == {}


def test_scan_indexes_real_jpeg_exif_into_db(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_jpeg(
        src / "with_exif.jpg",
        tags={
            271: "TestCam",
            272: "ModelX",
            306: "2020:01:02 03:04:05",
            34855: 200,
        },
    )
    _make_jpeg(src / "plain.jpg")
    update_settings(db, source_dirs=[str(src)])

    report = scan(db)

    assert report.new_files == 2
    assert report.errors == []  # real images decode cleanly
    rows = {
        os.path.basename(row["path"]): row
        for row in db.execute("SELECT * FROM files")
    }
    assert rows["with_exif.jpg"]["captured_at"] == "2020-01-02T03:04:05"
    assert rows["with_exif.jpg"]["camera"] == "TestCam ModelX"
    exif = json.loads(rows["with_exif.jpg"]["exif_json"])
    assert exif["iso"] == 200
    # no-exif image: mtime fallback, still indexed with decoded exif "{}"
    assert rows["plain.jpg"]["captured_at"] is not None
    assert rows["plain.jpg"]["camera"] is None
    assert json.loads(rows["plain.jpg"]["exif_json"]) == {}
    assert len(db.execute("SELECT * FROM photos").fetchall()) == 2
