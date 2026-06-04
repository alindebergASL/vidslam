from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api import api_router
from .config import get_settings
from .db import SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("avs.app")

settings = get_settings()


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("initializing database at %s", settings.database_url)
        init_db()
        yield

    app = FastAPI(title="AvatarVideoStudio API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin, "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        # Back-compat alias for the existing /health probe used by smoke +
        # verify scripts. Equivalent to /healthz (process-alive).
        return {"status": "ok", "service": "avatarvideostudio-backend"}

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness probe: the process is up and serving HTTP. Cheap; should
        only fail if the worker is actually dead. Use this for k8s livenessProbe
        / load-balancer health checks that decide whether to restart the pod."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness probe: the process is *ready to serve real traffic* — DB
        reachable, ffmpeg present, data dir writable. Returns 503 (with a
        per-check breakdown) if any of them is broken so a load balancer drops
        the pod out of rotation rather than serving 500s. Use this for k8s
        readinessProbe."""
        s = get_settings()
        checks: dict[str, dict] = {}

        # DB: a SELECT 1 round-trip. Catches both "no engine" and "DB file
        # missing / locked" without touching real tables.
        t0 = time.monotonic()
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            checks["database"] = {
                "ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as e:  # noqa: BLE001
            checks["database"] = {"ok": False, "error": str(e)[:200]}

        # ffmpeg: just needs to be on PATH. The renderer fails late and ugly
        # without it; readiness should refuse traffic up front.
        checks["ffmpeg"] = {"ok": shutil.which("ffmpeg") is not None}

        # Data dir: can we actually create a file there? Catches a misconfigured
        # bind-mount / read-only volume that would otherwise blow up at render.
        try:
            target = Path(s.data_dir)
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target, prefix=".readyz-", delete=True):
                pass
            checks["data_dir_writable"] = {"ok": True, "path": str(target)}
        except OSError as e:
            checks["data_dir_writable"] = {"ok": False, "error": str(e)[:200]}

        all_ok = all(c["ok"] for c in checks.values())
        body = {"status": "ready" if all_ok else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if all_ok else 503)

    app.include_router(api_router)
    return app


app = create_app()
