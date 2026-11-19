"""Background job manager for photo-picker (spec §5 jobs).

JobManager runs long operations (scan / hashing / quality analysis) off the
UI thread and reports per-job progress. Three execution models:

- scan & hashing: worker *threads*, each opening their own connection via
  app.db.get_db (check_same_thread=False + WAL + busy_timeout). DB-writing
  jobs serialize on a manager-wide write lock, so a scan and an analysis
  never write concurrently.
- analysis (production): a *process pool* — cv2/numpy burn CPU with the GIL
  held, so per-photo workers must be real processes. Each worker gets only
  (db_path, photo_id, path, face_engine): SQLite connections never cross
  the process boundary; the worker opens its own via app.db.connect and
  INSERT OR REPLACEs its quality_scores row.
- analysis (injected worker): a thread pool. submit_analysis(worker_override=…)
  lets tests (and DSH, where cv2-importing child processes crash) drive the
  exact dispatch/progress/error bookkeeping with a pure-Python worker.

Module top level imports only the standard library; app.* imports are
deferred into functions so `import app.jobs` is safe everywhere.

Error policy: exceptions inside a job never propagate to the caller — they
are captured into status(job_id)['error'] and the job's phase becomes
'failed'. submit_* always returns a job_id immediately.
"""

from __future__ import annotations

import atexit
import datetime
import os
import threading

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _analysis_worker(db_path, photo_id, path, face_engine) -> dict:
    """Process-pool worker: analyze one photo and persist its scores.

    Runs in a child process: receives only picklable args (db_path str,
    ids, path str) — NEVER a sqlite3.Connection. Opens its own connection,
    calls analyze_photo, and INSERT OR REPLACEs the quality_scores row
    before returning {photo_id, scores} to the parent.
    """
    import json

    import app.db as db_module
    from app.quality import analyze_photo

    conn = db_module.connect(db_path)
    try:
        scores = analyze_photo(path, face_engine)
        if scores is None:  # undecodable: nothing to record
            return {"photo_id": photo_id, "scores": None}
        conn.execute(
            "INSERT OR REPLACE INTO quality_scores "
            "(photo_id, sharpness, exposure, noise, composition, faces, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                photo_id,
                scores["sharpness"],
                scores["exposure"],
                scores["noise"],
                scores["composition"],
                scores["faces"],
                json.dumps(scores["details"]),
            ),
        )
        conn.commit()
        return {"photo_id": photo_id, "scores": scores}
    finally:
        conn.close()


class JobManager:
    """Runs scan/hash/analysis jobs with progress and error reporting."""

    def __init__(self, db_path: str, max_workers: int | None = None):
        if max_workers is None:
            max_workers = max(1, (os.cpu_count() or 2) - 1)
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._db_path = db_path
        self._max_workers = max_workers
        self._lock = threading.Lock()  # guards _jobs / _next_id / job dicts
        self._write_lock = threading.Lock()  # serializes DB-writing jobs
        self._jobs: dict[int, dict] = {}
        self._next_id = 1
        self._process_executor = None
        self._executor_lock = threading.Lock()
        atexit.register(self.close, wait=False)

    # -- public API ---------------------------------------------------

    def submit_scan(self, progress_cb=None) -> int:
        """Index new files from configured source_dirs in a thread."""
        job_id = self._new_job("scan")
        self._spawn(self._run_scan, job_id, progress_cb)
        return job_id

    def submit_hashing(self) -> int:
        """Run app.hashing.compute_hashes in a thread (module optional)."""
        job_id = self._new_job("hashing")
        self._spawn(self._run_hashing, job_id)
        return job_id

    def submit_analysis(self, worker_override=None) -> int:
        """Analyze all photos lacking quality_scores.

        Production: default _analysis_worker on a lazily-created
        ProcessPoolExecutor. With worker_override (tests/DSH): the injected
        worker(db_path, photo_id, path, face_engine) runs on a thread pool
        instead — identical dispatch/progress/error bookkeeping.
        """
        job_id = self._new_job("analysis")
        self._spawn(self._run_analysis, job_id, worker_override)
        return job_id

    def submit_execute(self, decision_id) -> int:
        """Placeholder until Task 8 wires the executor; fails with a note."""
        job_id = self._new_job("execute")
        self._finalize(
            job_id,
            phase="failed",
            error="execute not implemented yet (Task 8)",
            note=f"decision_id={decision_id}",
        )
        return job_id

    def submit_pipeline(self, progress_cb=None) -> int:
        """Run the full scan→pair→hash→group→analyze→recommend pipeline in a
        thread, reusing api.run_full_pipeline_blocking. The finished job's
        status dict carries the five-stage report under 'result', so the UI
        can restore it after a page switch instead of relying on the (gone)
        HTTP response."""
        job_id = self._new_job("pipeline")
        self._spawn(self._run_pipeline, job_id, progress_cb)
        return job_id

    def status(self, job_id) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(self._jobs[jid]) for jid in sorted(self._jobs)]

    def close(self, wait: bool = True) -> None:
        """Shut the process pool down (also called via atexit)."""
        with self._executor_lock:
            executor = self._process_executor
            self._process_executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)

    # -- job bookkeeping (all mutations under _lock) -------------------

    def _new_job(self, kind: str) -> int:
        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "phase": "running",
                "progress": 0.0,
                "error": None,
                "note": None,
                "result": None,
                "stage_times": None,
                "started_at": _now(),
                "finished_at": None,
            }
            return job_id

    def _update(self, job_id: int, **fields) -> None:
        with self._lock:
            self._jobs[job_id].update(fields)

    def _set_progress(self, job_id: int, current: int, total: int) -> None:
        fraction = (current / total) if total > 0 else 1.0
        self._update(job_id, progress=min(1.0, max(0.0, fraction)))

    def _finalize(self, job_id: int, phase: str, error: str | None = None, note: str | None = None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["phase"] = phase
            job["finished_at"] = _now()
            if error is not None:
                job["error"] = error
            if note is not None:
                job["note"] = note
            if phase == "finished":
                job["progress"] = 1.0

    def _spawn(self, target, job_id: int, *args) -> None:
        def guarded():
            try:
                target(job_id, *args)
            except Exception as exc:  # never let a job thread die silently
                self._finalize(job_id, phase="failed", error=str(exc))

        threading.Thread(target=guarded, daemon=True).start()

    def _call_user_cb(self, progress_cb, *args) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(*args)
        except Exception:
            pass  # user callbacks must not break the job

    # -- job bodies ----------------------------------------------------

    def _run_scan(self, job_id: int, progress_cb) -> None:
        import app.db as db
        import app.scanner as scanner

        def cb(phase: str, current: int, total: int) -> None:
            self._set_progress(job_id, current, total)
            self._call_user_cb(progress_cb, phase, current, total)

        with self._write_lock:
            conn = db.get_db(self._db_path)  # shared main-process connection
            try:
                scanner.scan(conn, cb)
            finally:
                conn.rollback()  # no-op after scan's commit; clears partial writes
        self._finalize(job_id, phase="finished")

    def _run_hashing(self, job_id: int) -> None:
        import app.db as db

        try:
            import app.hashing as hashing
        except ImportError:
            self._finalize(job_id, phase="failed", error="hashing module unavailable")
            return
        compute = getattr(hashing, "compute_hashes", None)
        if compute is None:
            self._finalize(job_id, phase="failed", error="hashing module unavailable")
            return

        def cb(phase: str, current: int, total: int) -> None:
            self._set_progress(job_id, current, total)
            self._call_user_cb(progress_cb, phase, current, total)

        with self._write_lock:
            conn = db.get_db(self._db_path)  # shared main-process connection
            try:
                compute(conn, cb)
            finally:
                conn.rollback()  # no-op after compute's commit
        self._finalize(job_id, phase="finished")

    def _run_analysis(self, job_id: int, worker_override) -> None:
        import app.db as db
        from app.config import get_settings

        worker = _analysis_worker if worker_override is None else worker_override
        use_processes = worker_override is None

        with self._write_lock:
            conn = db.get_db(self._db_path)  # shared main-process connection
            try:
                face_engine = get_settings(conn).get("face_engine", "auto")
                pending = conn.execute(
                    "SELECT p.id, f.path FROM photos p "
                    "JOIN files f ON f.id = p.primary_file_id "
                    "LEFT JOIN quality_scores q ON q.photo_id = p.id "
                    "WHERE q.photo_id IS NULL AND f.path IS NOT NULL "
                    "ORDER BY p.id"
                ).fetchall()
            finally:
                conn.rollback()  # read-only: clears any implicit txn state

            total = len(pending)
            if total == 0:
                self._finalize(job_id, phase="finished")
                return

            if use_processes:
                pool = self._get_process_pool()
            else:
                pool = ThreadPoolExecutor(max_workers=self._max_workers)
            futures = [
                pool.submit(worker, self._db_path, row["id"], row["path"], face_engine)
                for row in pending
            ]

            errors: list[str] = []
            try:
                for done_count, future in enumerate(as_completed(futures), start=1):
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(str(exc))
                    self._set_progress(job_id, done_count, total)
            finally:
                if isinstance(pool, ThreadPoolExecutor):
                    pool.shutdown(wait=True)

            self._enforce_cache_limit()
            if errors:
                self._finalize(
                    job_id,
                    phase="failed",
                    error=errors[0],
                    note=f"{len(errors)} photo(s) failed",
                )
            else:
                self._finalize(job_id, phase="finished")

    def _run_pipeline(self, job_id: int, progress_cb) -> None:
        import app.db as db
        from app import api

        # per-stage start/finish timestamps for the UI's five progress
        # bars + elapsed times; survives page reloads because it rides on
        # the job status dict
        stage_times: dict[str, dict] = {}
        current_stage = [None]

        def cb(phase: str, current: int, total: int) -> None:
            if phase != current_stage[0]:
                now = _now()
                if current_stage[0] is not None:
                    stage_times.setdefault(current_stage[0], {})["finished"] = now
                stage_times[phase] = {"started": now, "finished": None}
                current_stage[0] = phase
                self._update(job_id, note=phase, stage_times=dict(stage_times))
            self._set_progress(job_id, current, total)
            self._call_user_cb(progress_cb, phase, current, total)

        with self._write_lock:
            conn = db.get_db(self._db_path)
            try:
                report = api.run_full_pipeline_blocking(conn, progress_cb=cb)
            finally:
                conn.rollback()  # stages commit; clears residual txn state
        if current_stage[0] is not None:
            stage = stage_times.setdefault(current_stage[0], {})
            if stage.get("finished") is None:
                stage["finished"] = _now()
        self._update(job_id, result=report, stage_times=dict(stage_times))
        self._finalize(job_id, phase="finished")

    # -- helpers --------------------------------------------------------

    def _enforce_cache_limit(self) -> None:
        try:
            from app.imaging import enforce_cache_limit

            enforce_cache_limit()
        except Exception:
            pass  # cache trimming is best-effort housekeeping

    def _get_process_pool(self):
        from concurrent.futures import ProcessPoolExecutor

        with self._executor_lock:
            if self._process_executor is None:
                self._process_executor = ProcessPoolExecutor(
                    max_workers=self._max_workers
                )
            return self._process_executor
