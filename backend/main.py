"""DL India Core — FastAPI backend.

Google Drive is the only persistent storage. There is no database.

    uvicorn main:app --port 8000        # docs at /docs

On startup: connect to Drive, ensure the Portfolio folder exists, create
any missing JSON file. If that fails the app still starts and every
endpoint returns 503 with the reason — a dead backend that explains itself
beats one that will not boot.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import drive
from api import router

logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("dl")

app = FastAPI(
    title="DL India Core API",
    version="1.0.0",
    description=(
        "Single-user portfolio dashboard. Google Drive is the only storage.\n\n"
        "GET endpoints return stored JSON — the browser performs no calculation. "
        "Everything is recomputed when a file is imported, or by the evening job.\n\n"
        "**Weights are NAV fractions, not rupees** (0.625 = 62.5% cash)."
    ),
)

# The frontend is deployed separately (Vercel), so it is a different
# origin. Set CORS_ORIGINS to a comma-separated list in production; the
# default is permissive because this API holds one person's own data and
# has no authentication to protect.
origins = os.environ.get("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if origins == "*" else [o.strip() for o in origins.split(",")],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def startup():
    try:
        store = drive.connect()
        log.info("ready — %s", ", ".join(store.list_names()) or "folder is empty")
    except drive.DriveError as e:
        # Deliberately not fatal: a booted app can report the problem on
        # every request. One that refuses to start just looks down.
        log.error("storage unavailable at startup: %s", e)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("[%s] %s %s -> unhandled", rid, request.method, request.url.path)
        raise
    ms = (time.perf_counter() - t0) * 1000
    log.log(logging.WARNING if response.status_code >= 400 else logging.INFO,
            "[%s] %s %s -> %d in %.0fms", rid, request.method, request.url.path,
            response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(drive.DriveError)
async def _drive_down(request: Request, exc: drive.DriveError):
    log.error("storage failure on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    log.exception("unexpected failure on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/health", tags=["system"])
def health():
    """Reports whether storage is actually reachable, not just that the
    process is up."""
    try:
        names = drive.store().list_names()
        return {"status": "ok", "storage": "connected", "files": names}
    except drive.DriveError as e:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "storage": "unavailable", "detail": str(e)})
