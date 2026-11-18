"""JobManager tests — DSH-safe: no native libraries, no cv2 worker processes.

The analysis path is exercised through submit_analysis(worker_override=...)
with a pure-Python fake worker, per the delegation's testability design:
injected workers run on a thread pool, so tests never spawn cv2-importing
child processes (which crash inside DSH). Production dispatch uses the
default _analysis_worker on a ProcessPoolExecutor, covered in the user
terminal by test_quality_metrics.py + integration runs.
"""

import sqlite3
import threading
import time

import pytest

from app import jobs
from app.config import update_settings
from app.db import connect, init_db
from conftest import make_fake_file


def _db_path(conn) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if row["name"] == "main":
            return row["file"]
    raise AssertionError("main database not found")


def _wait_job(mgr, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = mgr.status(job_id)
        if st["phase"] in ("finished", "failed"):
            return st
        time.sleep(0.02)
    return mgr.status(job_id)  # timeout: return latest for assertion messages


def _fake_worker(db_path, photo_id, path, face_engine):
    """Module-level (picklable) stand-in for _analysis_worker: writes fake
    scores through its own connection, exactly like the real worker must."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO quality_scores "
            "(photo_id, sharpness, exposure, noise, composition, faces, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (photo_id, 90.0, 80.0, 70.0, 60.0, None, '{"faces": 0}'),
        )
        conn.commit()
    finally:
        conn.close()
    return {"photo_id": photo_id, "scores": {"sharpness": 90.0}}


def _insert_photo(conn, path) -> int:
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, format) VALUES (?, ?, ?, ?)",
        (path, 1.0, 10, "jpg"),
    )
    file_id = cur.lastrowid
    cur = conn.execute("INSERT INTO photos (primary_file_id) VALUES (?)", (file_id,))
    photo_id = cur.lastrowid
    conn.execute("UPDATE files SET photo_id = ? WHERE id = ?", (photo_id, file_id))
    conn.commit()
    return photo_id


@pytest.fixture()
def manager(db):
    init_db(db)
    mgr = jobs.JobManager(_db_path(db))
    yield mgr
    mgr.close()


class TestScan:
    def test_scan_indexes_files(self, manager, db, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        make_fake_file(source, "a.jpg", b"a" * 16)
        make_fake_file(source, "b.jpg", b"b" * 16)
        update_settings(db, source_dirs=[str(source)])

        job_id = manager.submit_scan()
        st = _wait_job(manager, job_id)

        assert st["phase"] == "finished", st
        assert st["error"] is None
        assert st["progress"] == 1.0
        assert st["kind"] == "scan"
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2

    def test_scan_progress_visible_mid_run(self, manager, db, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        for i in range(4):
            make_fake_file(source, f"{i}.jpg", b"x" * 8)
        update_settings(db, source_dirs=[str(source)])

        seen = threading.Event()
        release = threading.Event()

        def progress_cb(*_args):
            if not seen.is_set():
                seen.set()
                release.wait(timeout=10)

        job_id = manager.submit_scan(progress_cb=progress_cb)
        assert seen.wait(timeout=10)
        mid = manager.status(job_id)
        assert mid["phase"] == "running"
        assert mid["kind"] == "scan"
        assert 0.0 <= mid["progress"] <= 1.0

        release.set()
        st = _wait_job(manager, job_id)
        assert st["phase"] == "finished"
        assert st["progress"] == 1.0


class TestAnalysis:
    def test_analysis_with_injected_worker(self, manager, db, tmp_path):
        photo_ids = [
            _insert_photo(db, str(tmp_path / f"p{i}.jpg")) for i in range(2)
        ]

        job_id = manager.submit_analysis(worker_override=_fake_worker)
        st = _wait_job(manager, job_id)

        assert st["phase"] == "finished", st
        assert st["error"] is None
        assert st["progress"] == 1.0
        rows = db.execute(
            "SELECT photo_id, sharpness FROM quality_scores ORDER BY photo_id"
        ).fetchall()
        assert [r["photo_id"] for r in rows] == sorted(photo_ids)
        assert all(r["sharpness"] == 90.0 for r in rows)

    def test_analysis_worker_exception_recorded(self, manager, db, tmp_path):
        _insert_photo(db, str(tmp_path / "bad.jpg"))

        def bad_worker(db_path, photo_id, path, face_engine):
            raise RuntimeError("boom")

        job_id = manager.submit_analysis(worker_override=bad_worker)
        st = _wait_job(manager, job_id)

        assert st["phase"] == "failed"
        assert "boom" in st["error"]
        # manager keeps functioning after a failed job
        assert manager.list_jobs()[0]["id"] == job_id

    def test_analysis_without_pending_photos(self, manager, db):
        job_id = manager.submit_analysis(worker_override=_fake_worker)
        st = _wait_job(manager, job_id)

        assert st["phase"] == "finished"
        assert st["progress"] == 1.0


class TestHashing:
    def test_submit_hashing(self, manager, db, monkeypatch):
        try:
            import app.hashing as hashing
        except Exception:  # parallel agent may have it mid-write
            st = _wait_job(manager, manager.submit_hashing())
            assert st["phase"] == "failed"
            assert "unavailable" in st["error"]
            return

        calls = []

        def fake_compute(conn, progress_cb=None):
            calls.append(conn)
            return {"hashed": 0}

        monkeypatch.setattr(hashing, "compute_hashes", fake_compute)
        st = _wait_job(manager, manager.submit_hashing())
        assert st["phase"] == "finished", st
        assert st["error"] is None
        assert len(calls) == 1


class TestConcurrency:
    def test_scan_and_analysis_do_not_corrupt(self, manager, db, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        make_fake_file(source, "c.jpg", b"c" * 8)
        make_fake_file(source, "d.jpg", b"d" * 8)
        update_settings(db, source_dirs=[str(source)])
        for i in range(2):  # photos outside the scanned dir
            _insert_photo(db, str(tmp_path / f"q{i}.jpg"))

        scan_id = manager.submit_scan()
        analysis_id = manager.submit_analysis(worker_override=_fake_worker)

        st_scan = _wait_job(manager, scan_id)
        st_analysis = _wait_job(manager, analysis_id)

        assert st_scan["phase"] == "finished", st_scan
        assert st_analysis["phase"] == "finished", st_analysis
        assert st_scan["error"] is None
        assert st_analysis["error"] is None
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM quality_scores").fetchone()[0] >= 2


class TestExecutePlaceholder:
    def test_execute_is_placeholder_until_task_8(self, manager, db, tmp_path):
        job_id = manager.submit_execute(decision_id=123)
        st = manager.status(job_id)

        assert st["phase"] == "failed"
        assert "123" in (st.get("note") or "") + (st.get("error") or "")
        assert st["finished_at"] is not None

    def test_status_and_list_jobs_shape(self, manager, db, tmp_path):
        first = manager.submit_analysis(worker_override=_fake_worker)
        second = manager.submit_analysis(worker_override=_fake_worker)

        st = manager.status(second)
        assert st["id"] == second
        assert st["kind"] == "analysis"
        for key in ("id", "kind", "phase", "progress", "error", "started_at", "finished_at"):
            assert key in st
        assert manager.status(first)["id"] == first
        assert manager.status(999) is None

        ids = [j["id"] for j in manager.list_jobs()]
        assert ids == [first, second]


class TestPipeline:
    def test_submit_pipeline_returns_job_with_report(self, manager, db, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        make_fake_file(source, "a.jpg", b"a" * 16)
        make_fake_file(source, "b.jpg", b"b" * 16)
        update_settings(db, source_dirs=[str(source)])

        job_id = manager.submit_pipeline()
        st = _wait_job(manager, job_id)

        assert st["phase"] == "finished", st
        assert st["kind"] == "pipeline"
        assert st["progress"] == 1.0
        # the five-stage report rides on the job so page switches can't lose it
        assert set(st["result"]) == {
            "scan", "hashing", "grouping", "analysis", "recommend"
        }
        assert st["result"]["scan"]["total"] == 2
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2

    def test_pipeline_failure_recorded_not_raised(self, manager, db, monkeypatch):
        import app.api as api_mod
        from app import jobs as jobs_mod

        def boom(conn, progress_cb=None):
            raise RuntimeError("pipeline exploded")

        monkeypatch.setattr(api_mod, "run_full_pipeline_blocking", boom)
        # _run_pipeline binds api.run_full_pipeline_blocking at call time
        # through the module attribute, so patching the module works.
        job_id = jobs_mod.JobManager.__dict__["_run_pipeline"]  # sanity: exists
        job_id = manager.submit_pipeline()
        st = _wait_job(manager, job_id)
        assert st["phase"] == "failed", st
        assert "pipeline exploded" in st["error"]
        assert st["result"] is None
