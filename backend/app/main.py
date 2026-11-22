# -*- coding: utf-8 -*-
"""FastAPI application exposing the JSON service layer in app.api.

This module is a THIN adapter: every business function lives in
app.api (pure Python, first arg = sqlite3.Connection) so it stays
testable inside DSH, where importing fastapi fails (pydantic_core is
rejected by AMFI). Importing this module requires fastapi; the route
tests (test_main_routes.py) skip entirely when it is unavailable.

Run with the user terminal venv:
    .venv/bin/python -m app.main        # uvicorn on 127.0.0.1:8765
    .venv/bin/uvicorn app.main:app
"""
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import api, db, executor, imaging, jobs

DEFAULT_DB_PATH = os.path.join("data", "photo_picker.db")
FRONTEND_DIST = os.path.join("frontend", "dist")


# --- request bodies ----------------------------------------------------------

class DecisionBody(BaseModel):
    keep_photo_ids: List[int]
    learn: bool = True


class FeedbackBody(BaseModel):
    photo_id: int
    action: str


class WeightsBody(BaseModel):
    weights: Dict[str, float]


class LockedBody(BaseModel):
    dims: List[str]


class ScanBody(BaseModel):
    source_dirs: List[str]


# --- app factory -------------------------------------------------------------

def _resolve_db_path(db_path: Optional[str]) -> str:
    path = db_path or os.environ.get("PHOTO_PICKER_DB") or DEFAULT_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.db_path = _resolve_db_path(app.state._pending_db_path)
    # get_db returns a cached per-path connection; db.connect already
    # enables WAL + busy_timeout=5000 + foreign_keys, which is what
    # serializes job-thread writes against request-thread writes.
    conn = db.get_db(app.state.db_path)
    db.init_db(conn)
    app.state.jobs = jobs.JobManager(app.state.db_path)
    yield
    app.state.jobs.close(wait=True)


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """Build the FastAPI app. Cheap and side-effect free until startup:
    DB init and the JobManager only spin up in the lifespan handler."""
    app = FastAPI(title="photo-picker", version="1.0.0",
                  lifespan=_lifespan)
    app.state._pending_db_path = db_path

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _no_cache_code_assets(request: Request, call_next):
        # HTML/JS/CSS must always revalidate: users otherwise keep a stale
        # settings.js after an upgrade and "lose" freshly-fixed UI states
        # (e.g. pipeline resume) even though the backend is fine.
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")) or path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(KeyError)
    async def _missing(request: Request, exc: KeyError):
        detail = exc.args[0] if exc.args else str(exc)
        return JSONResponse(status_code=404, content={"detail": str(detail)})

    @app.exception_handler(ValueError)
    async def _invalid(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(executor.MoveError)
    async def _move_failed(request: Request, exc: executor.MoveError):
        return JSONResponse(
            status_code=500,
            content={"detail": "文件移动失败: {}".format(exc)})

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        """Fresh per-request connection (row_factory+WAL+busy_timeout via
        db.connect), closed afterwards — never shared across threads."""
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # --- overview / groups ---------------------------------------------------

    @app.get("/api/overview")
    def overview(conn: sqlite3.Connection = Depends(get_conn)):
        return api.get_overview(conn)

    @app.get("/api/groups")
    def groups(
        status: str = Query("pending"),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return api.list_groups(conn, status=status, offset=offset,
                               limit=limit)

    @app.get("/api/groups/{group_id}")
    def group_detail(group_id: int,
                     conn: sqlite3.Connection = Depends(get_conn)):
        return api.get_group(conn, group_id)

    @app.post("/api/groups/{group_id}/decision")
    def group_decision(group_id: int, body: DecisionBody,
                       conn: sqlite3.Connection = Depends(get_conn)):
        return api.decide_group(conn, group_id, body.keep_photo_ids,
                                learn=body.learn)

    @app.post("/api/groups/{group_id}/dismiss")
    def group_dismiss(group_id: int,
                      conn: sqlite3.Connection = Depends(get_conn)):
        return api.dismiss_group_api(conn, group_id)

    @app.post("/api/groups/{group_id}/undo")
    def group_undo(group_id: int,
                   conn: sqlite3.Connection = Depends(get_conn)):
        return api.undo_group_api(conn, group_id)

    # --- feedback / preferences ----------------------------------------------

    @app.post("/api/feedback")
    def feedback(body: FeedbackBody,
                 conn: sqlite3.Connection = Depends(get_conn)):
        return api.add_feedback(conn, body.photo_id, body.action)

    @app.patch("/api/photos/{photo_id}")
    def photo_patch(photo_id: int, payload: dict = Body(...),
                    conn: sqlite3.Connection = Depends(get_conn)):
        return api.update_photo(conn, photo_id, payload)

    @app.get("/api/preferences")
    def preferences(conn: sqlite3.Connection = Depends(get_conn)):
        return api.get_preferences(conn)

    @app.put("/api/preferences/weights")
    def preference_weights(
            body: WeightsBody,
            conn: sqlite3.Connection = Depends(get_conn)):
        return api.set_preference_weights(conn, dict(body.weights))

    @app.post("/api/preferences/train")
    def preferences_train(conn: sqlite3.Connection = Depends(get_conn)):
        return api.train_now(conn)

    @app.put("/api/preferences/locked")
    def preferences_locked(
            body: LockedBody,
            conn: sqlite3.Connection = Depends(get_conn)):
        return api.set_locked_api(conn, body.dims)

    # --- history -------------------------------------------------------------

    @app.get("/api/history")
    def history(
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        source: str = Query("all"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return api.list_history(
            conn, offset=offset, limit=limit, source=source)

    # --- settings ---------------------------------------------------------------

    @app.get("/api/settings")
    def settings(conn: sqlite3.Connection = Depends(get_conn)):
        return api.get_settings(conn)

    @app.put("/api/settings")
    def update_settings(payload: dict = Body(...),
                        conn: sqlite3.Connection = Depends(get_conn)):
        return api.update_runtime_settings(conn, **payload)

    # --- filesystem browsing (directory picker) -------------------------------

    @app.get("/api/fs/list")
    def fs_list(path: str = "~",
                conn: sqlite3.Connection = Depends(get_conn)):
        return api.list_dirs(conn, path)

    # --- screenshots ------------------------------------------------------------

    @app.get("/api/screenshots")
    def screenshots(
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=500),
        when: str = Query("all"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return api.get_screenshots(conn, offset=offset, limit=limit, when=when)

    @app.post("/api/screenshots/reject")
    def screenshots_reject(payload: dict = Body(...),
                           conn: sqlite3.Connection = Depends(get_conn)):
        photo_ids = payload.get("photo_ids")
        return api.reject_screenshots(conn, photo_ids)

    # --- jobs / scan / pipeline -----------------------------------------------

    @app.post("/api/scan")
    def scan(body: ScanBody, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)):
        from app.config import update_settings
        update_settings(conn, source_dirs=body.source_dirs)
        job_id = request.app.state.jobs.submit_scan()
        return {"job_id": job_id}

    @app.get("/api/jobs")
    def jobs_list(request: Request):
        return {"jobs": request.app.state.jobs.list_jobs()}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: int, request: Request):
        status = request.app.state.jobs.status(job_id)
        if status is None:
            raise KeyError("job {} 不存在".format(job_id))
        return status

    @app.post("/api/pipeline/run")
    def pipeline_run(request: Request):
        # Background job (JobManager thread): the HTTP request returns
        # {job_id} immediately; the UI polls /api/jobs/{id} and the finished
        # job carries the five-stage report in status['result'], which
        # survives page switches.
        job_id = request.app.state.jobs.submit_pipeline()
        return {"job_id": job_id}

    # --- images ---------------------------------------------------------------

    @app.get("/api/files/{file_id}/thumbnail")
    def thumbnail(
        file_id: int,
        size: int = Query(256, ge=32, le=2048),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        content = imaging.get_thumb(conn, file_id, size=size)
        return Response(content=content, media_type="image/webp")

    @app.get("/api/files/{file_id}/preview")
    def preview(
        file_id: int,
        max_side: int = Query(1600, ge=64, le=4096),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        content = imaging.get_preview(conn, file_id, max_side=max_side)
        return Response(content=content, media_type="image/jpeg")

    # --- static frontend ------------------------------------------------------

    dist = Path(FRONTEND_DIST)
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True),
                  name="frontend")
    else:
        @app.get("/")
        def root():
            return {"app": "photo-picker", "docs": "/docs",
                    "hint": "frontend/dist 未构建：仅 JSON API 可用"}

    return app


app = create_app()


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    threading.Timer(
        1.5, lambda: webbrowser.open("http://127.0.0.1:8765")
    ).start()
    uvicorn.run(app, host="127.0.0.1", port=8765)
