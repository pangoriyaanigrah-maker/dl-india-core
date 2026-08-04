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
import sys
import time
import uuid

from pathlib import Path

# `uvicorn main:app` run from inside backend/ puts this directory on
# sys.path for free; a serverless platform importing this file from a
# different working directory (Vercel's Python runtime, notably) doesn't
# necessarily give the same guarantee. Without this, `import drive` below
# fails at module load -- before any route runs, so every single request
# 500s identically. Same defensive line scripts/daily_update.py already
# needed for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import drive
from api import router

REPO_ROOT = Path(__file__).resolve().parent.parent

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

# The frontend is hosted separately (GitHub Pages), so it is a different
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


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    """Every /api/* response is live financial data with no version or
    timestamp of its own. With no Cache-Control at all, a browser is free
    to apply heuristic freshness (RFC 7234 4.2.2) and quietly answer a
    later fetch() from its own cache instead of asking again -- indistin-
    guishable from the dashboard just not updating. Be explicit instead of
    hoping a default is conservative enough."""
    response = await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path == "/health":
        response.headers["Cache-Control"] = "no-store"
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


class NoCacheStaticFiles(StaticFiles):
    """index.html and config.js are redeployed by overwriting the same
    path, with no cache-busting hash in the URL -- the only signal a
    browser has that the file changed is asking again. Default StaticFiles
    sends Last-Modified/ETag but no Cache-Control, so browsers fall back
    to heuristic freshness and can serve a stale copy for a while with no
    revalidation at all: exactly what "the new trade doesn't show up"
    looks like from the outside, even though the backend is correct.
    no-cache (not no-store) keeps the fast path -- a 304 on an unchanged
    file -- while still forcing a check on every load."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Serve the dashboard too, so `uvicorn main:app` is the whole app locally --
# one origin, so no CORS and no API_BASE to set. In production GitHub Pages
# (or a separate Vercel static build) serves index.html and this mount goes
# unused. Mounted LAST: the routes above win, this catches everything else.
#
# StaticFiles raises at construction if the directory doesn't exist -- and
# this runs at import time, not inside a request handler, so on a platform
# that doesn't preserve REPO_ROOT's layout (a serverless bundler that
# packages files differently than the git checkout) that exception used to
# take the ENTIRE app down before a single route could run: every request
# crashed identically, API included, not just the pages this mount serves.
# The API must survive that regardless of whether static serving does.
try:
    app.mount("/", NoCacheStaticFiles(directory=REPO_ROOT, html=True), name="dashboard")
except RuntimeError as e:
    log.warning("static dashboard mount skipped (%s) -- /api/* still works, "
                "the frontend must be hosted separately", e)
