"""Scanner logic tests: discovery, incremental index, RAW+JPG pairing.

Pure-Python layer per docs/DEVENV.md: this file never touches Pillow image
operations so it stays green inside DSH (where PIL._imaging cannot load).
All fixture files are fake byte blobs — they walk the decode-failure branch:
files must still be indexed (md5, format), EXIF stays empty, and the failure
lands in ScanReport.errors without aborting the scan.
"""

import hashlib
import os

from app.config import update_settings
from app.scanner import (
    SUPPORTED,
    _camera_from,
    _parse_exif_datetime,
    pair_raw_jpg,
    scan,
)

from conftest import make_fake_file


def _md5_of(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _scan(db, *dirs):
    update_settings(db, source_dirs=[str(d) for d in dirs])
    return scan(db)


def _files(db) -> dict:
    return {row["path"]: row for row in db.execute("SELECT * FROM files")}


def _photos(db) -> list:
    return list(db.execute("SELECT * FROM photos"))


# --- discovery and formats ---------------------------------------------------


def test_scan_with_no_source_dirs_returns_empty_report(db):
    report = scan(db)
    assert report.new_files == 0
    assert report.changed_files == 0
    assert report.missing_files == 0
    assert report.total_files == 0
    assert report.pair_count == 0
    assert report.errors == []
    assert _files(db) == {}
    assert _photos(db) == []


def test_supported_extension_map():
    assert SUPPORTED[".jpg"] == "jpg"
    assert SUPPORTED[".jpeg"] == "jpg"
    assert SUPPORTED[".heic"] == "heic"
    assert SUPPORTED[".heif"] == "heic"
    assert SUPPORTED[".png"] == "png"
    for ext in (".cr2", ".cr3", ".nef", ".arw", ".dng"):
        assert SUPPORTED[ext] == "raw"
    assert ".txt" not in SUPPORTED


def test_scan_indexes_supported_files_and_skips_others(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "a.jpg", b"fake-jpg-bytes")
    make_fake_file(src, "b.DNG", b"fake-raw-bytes")  # upper-case extension
    make_fake_file(src, "c.png", b"fake-png-bytes")
    make_fake_file(src, "notes.txt", b"hello")  # not supported: never indexed

    report = _scan(db, src)

    assert report.new_files == 3
    assert report.total_files == 3
    assert report.missing_files == 0
    rows = _files(db)
    assert len(rows) == 3
    formats = {os.path.basename(path): row["format"] for path, row in rows.items()}
    assert formats["a.jpg"] == "jpg"
    assert formats["b.DNG"] == "raw"
    assert formats["c.png"] == "png"
    # fake bytes: file still indexed with md5 hash, empty EXIF, errors recorded
    assert rows[str(src / "a.jpg")]["file_hash"] == _md5_of(b"fake-jpg-bytes")
    for row in rows.values():
        assert row["captured_at"] is None
        assert row["camera"] is None
        assert row["exif_json"] is None
        assert row["hash_phash"] is None
    assert len(report.errors) == 3
    assert all("decode failed" in err for err in report.errors)
    # one photo per new file
    assert len(_photos(db)) == 3
    assert all(row["photo_id"] is not None for row in rows.values())


def test_scan_walks_recursively_but_skips_hidden_and_rejected(db, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / ".hidden").mkdir()
    (src / "rej").mkdir()
    make_fake_file(src / "sub", "ok.jpg")
    make_fake_file(src / ".hidden", "secret.jpg")
    make_fake_file(src / "rej", "moved.jpg")
    make_fake_file(src, "top.jpg")

    update_settings(db, source_dirs=[str(src)], rejected_root=str(src / "rej"))
    report = scan(db)

    assert report.total_files == 2
    assert report.new_files == 2
    paths = {os.path.basename(path) for path in _files(db)}
    assert paths == {"ok.jpg", "top.jpg"}


def test_scan_skips_named_rejected_dir_even_without_explicit_root(db, tmp_path):
    # Default scattered mode: each source holds its own
    # _photo_picker_rejected; those copies must never be re-indexed,
    # and pruning happens at any nesting depth.
    src = tmp_path / "src"
    (src / "sub" / "_photo_picker_rejected").mkdir(parents=True)
    (src / "_photo_picker_rejected").mkdir()
    make_fake_file(src, "top.jpg")
    make_fake_file(src / "sub", "ok.jpg")
    make_fake_file(src / "_photo_picker_rejected", "moved1.jpg")
    make_fake_file(src / "sub" / "_photo_picker_rejected", "moved2.jpg")

    update_settings(db, source_dirs=[str(src)], rejected_root="")
    report = scan(db)

    paths = {os.path.basename(path) for path in _files(db)}
    assert paths == {"ok.jpg", "top.jpg"}
    assert report.total_files == 2


# --- incremental behavior ----------------------------------------------------


def test_scan_is_incremental_unchanged_files_not_reprocessed(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.jpg", "b.dng", "c.png"):
        make_fake_file(src, name, mtime=1_000_000)

    first = _scan(db, src)
    assert first.new_files == 3

    second = scan(db)  # settings persist in the DB
    assert second.new_files == 0
    assert second.changed_files == 0
    assert second.missing_files == 0
    # unchanged files are not re-decoded, so no fresh decode errors
    assert second.errors == []
    assert len(_files(db)) == 3
    assert len(_photos(db)) == 3


def test_scan_detects_content_change_and_rehashes(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p = make_fake_file(src, "a.jpg", b"v1-content", mtime=1_000_000)
    _scan(db, src)
    old_hash = _files(db)[str(p)]["file_hash"]
    assert old_hash == _md5_of(b"v1-content")

    p.write_bytes(b"v2-content-with-different-size")
    os.utime(p, (2_000_000, 2_000_000))
    report = scan(db)

    assert report.changed_files == 1
    assert report.new_files == 0
    row = _files(db)[str(p)]
    assert row["file_hash"] == _md5_of(b"v2-content-with-different-size")
    assert row["file_hash"] != old_hash
    assert row["mtime"] == 2_000_000
    assert len(_photos(db)) == 1  # photo kept, not duplicated
    assert row["photo_id"] is not None


def test_scan_detects_mtime_only_change(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p = make_fake_file(src, "same.jpg", b"same-bytes", mtime=1_000_000)
    _scan(db, src)

    os.utime(p, (1_000_500, 1_000_500))  # same size, new mtime
    report = scan(db)

    assert report.changed_files == 1
    assert report.new_files == 0
    assert _files(db)[str(p)]["mtime"] == 1_000_500


def test_scan_missing_file_keeps_row_and_photo(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    a = make_fake_file(src, "a.jpg")
    make_fake_file(src, "b.jpg")
    _scan(db, src)

    a.unlink()
    report = scan(db)

    assert report.missing_files == 1
    assert report.new_files == 0
    assert report.changed_files == 0
    assert str(a) in _files(db)  # row kept, not deleted
    assert len(_photos(db)) == 2  # photos untouched


# --- scan_runs bookkeeping ---------------------------------------------------


def test_scan_runs_row_written_and_progress_reaches_one(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "a.jpg")
    make_fake_file(src, "b.jpg")
    _scan(db, src)

    callbacks = []
    scan(db, progress_cb=lambda *args: callbacks.append(args))

    row = db.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert row["phase"] == "scan"
    assert row["progress"] == 1.0
    assert row["error"] is None
    assert callbacks[0] == ("scan", 0, 2)
    assert callbacks[-1] == ("scan", 2, 2)
    assert all(call[0] == "scan" for call in callbacks)


# --- RAW+JPG pairing ---------------------------------------------------------


def test_scan_pairs_raw_jpg_with_same_basename(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "IMG_1.dng", b"fake-raw")
    make_fake_file(src, "IMG_1.jpg", b"fake-jpg")

    report = _scan(db, src)

    assert report.new_files == 2
    assert report.pair_count == 1
    photos = _photos(db)
    assert len(photos) == 1
    photo = photos[0]
    rows = _files(db)
    raw_row = rows[str(src / "IMG_1.dng")]
    jpg_row = rows[str(src / "IMG_1.jpg")]
    assert jpg_row["photo_id"] == raw_row["photo_id"] == photo["id"]
    assert photo["primary_file_id"] == raw_row["id"]  # RAW is the primary


def test_pair_raw_jpg_by_capture_time_proximity(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "frame_a.dng", b"fake-raw")
    make_fake_file(src, "snap_0001.jpg", b"fake-jpg")
    report = _scan(db, src)
    assert report.pair_count == 0  # no shared stem, no captured_at yet

    db.execute(
        "UPDATE files SET captured_at = ? WHERE path = ?",
        ("2026-05-01T10:00:00", str(src / "frame_a.dng")),
    )
    db.execute(
        "UPDATE files SET captured_at = ? WHERE path = ?",
        ("2026-05-01T10:00:01.5", str(src / "snap_0001.jpg")),
    )
    db.commit()

    assert pair_raw_jpg(db) == 1
    rows = _files(db)
    raw_row = rows[str(src / "frame_a.dng")]
    jpg_row = rows[str(src / "snap_0001.jpg")]
    assert jpg_row["photo_id"] == raw_row["photo_id"]
    photos = _photos(db)
    assert len(photos) == 1
    assert photos[0]["primary_file_id"] == raw_row["id"]

    assert pair_raw_jpg(db) == 0  # idempotent: already paired


def test_pair_raw_jpg_far_apart_capture_times_do_not_pair(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "frame_a.dng", b"fake-raw")
    make_fake_file(src, "snap_0001.jpg", b"fake-jpg")
    _scan(db, src)
    db.execute(
        "UPDATE files SET captured_at = ? WHERE path = ?",
        ("2026-05-01T10:00:00", str(src / "frame_a.dng")),
    )
    db.execute(
        "UPDATE files SET captured_at = ? WHERE path = ?",
        ("2026-05-01T10:00:30", str(src / "snap_0001.jpg")),
    )
    db.commit()

    assert pair_raw_jpg(db) == 0
    assert len(_photos(db)) == 2


def test_pair_raw_jpg_one_partner_per_raw(db, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_fake_file(src, "IMG_1.dng", b"raw")
    make_fake_file(src, "IMG_1.jpg", b"jpg")
    make_fake_file(src, "extra.jpg", b"other")  # same dir, no stem/time match
    report = _scan(db, src)  # scan() pairs at its end

    assert report.pair_count == 1  # IMG_1 pair; extra.jpg stays alone
    assert len(_photos(db)) == 2
    rows = _files(db)
    raw_row = rows[str(src / "IMG_1.dng")]
    img_jpg = rows[str(src / "IMG_1.jpg")]
    extra_row = rows[str(src / "extra.jpg")]
    assert img_jpg["photo_id"] == raw_row["photo_id"]
    assert extra_row["photo_id"] != raw_row["photo_id"]

    assert pair_raw_jpg(db) == 0  # idempotent: nothing left to pair


# --- pure EXIF helpers (logic verified here, decode in the exif layer) -------


def test_parse_exif_datetime():
    assert _parse_exif_datetime("2020:01:02 03:04:05") == "2020-01-02T03:04:05"
    assert _parse_exif_datetime("2020:01:02 03:04:05.123") == "2020-01-02T03:04:05"
    assert _parse_exif_datetime("") is None
    assert _parse_exif_datetime(None) is None
    assert _parse_exif_datetime("not a date") is None
    assert _parse_exif_datetime("2020:13:45 99:99:99") is None


def test_camera_from():
    assert _camera_from("Canon", "EOS R5") == "Canon EOS R5"
    assert _camera_from("Canon", None) == "Canon"
    assert _camera_from(None, "EOS R5") == "EOS R5"
    assert _camera_from(None, None) is None
    assert _camera_from("Canon", "") == "Canon"


# --- short transactions: concurrent writers must not starve ------------------


def test_scan_commits_in_batches_to_keep_lock_windows_short(db, tmp_path):
    """回归：扫描大目录必须分批提交（每 _PROGRESS_STEP 个文件）。

    历史 bug：scan() 从头到尾单一大事务，全量扫描全程持有 SQLite
    写锁，并发写（如 POST /api/scan 里的 update_settings）在
    busy_timeout=5s 后抛 database is locked → HTTP 500。
    """
    src = tmp_path / "src"
    src.mkdir()
    for i in range(150):  # 3 × _PROGRESS_STEP(50)
        make_fake_file(src, f"p{i:03d}.jpg")

    commits = []
    db.set_trace_callback(
        lambda sql: commits.append(sql)
        if sql.strip().upper().startswith("COMMIT")
        else None
    )
    try:
        report = _scan(db, str(src))
    finally:
        db.set_trace_callback(None)

    assert report.total_files == 150
    # 150 / 50 = 3 次批提交（另有收尾与 helper 内 settings 写入的 commit）
    assert len(commits) >= 3
