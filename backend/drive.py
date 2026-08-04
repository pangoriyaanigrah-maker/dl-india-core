"""Google Drive storage. The only place this app persists anything.

Four operations, which is all a single-user dashboard needs:

    ensure_folder()        the Portfolio folder exists
    read_json(name)        -> dict | None
    write_json(name, obj)  replace, atomically from Drive's point of view
    write_bytes(name, ...) replace an uploaded .xlsx

Credentials, in order:
  1. GOOGLE_SERVICE_ACCOUNT_JSON   the JSON key itself (for a hosted backend, e.g. Render)
  2. GOOGLE_APPLICATION_CREDENTIALS or ./service-account.json  (local dev)
  3. DRIVE_LOCAL_DIR               a plain folder, no Google at all

Option 3 exists so the app runs, and the tests run, without a service
account. It is the same four operations against a directory -- about ten
lines -- and without it nothing here could be exercised at all.

Reads are memoised for a few seconds, and every write clears it. This is
not a caching layer: one page load asks six endpoints for the SAME
dashboard.json, and downloading 47KB six times over made the page take ten
seconds. The window is short enough that Drive stays the source of truth --
after a write the next read always goes back to Drive.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("dl.drive")

# Load .env here rather than in main.py: check_drive.py and daily_update.py
# import this module directly and need the same settings, and one of them
# forgetting to load it is exactly how "works for me" bugs start.
try:
    from dotenv import load_dotenv

    for _p in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if _p.exists():
            load_dotenv(_p)
            break
except ImportError:      # optional: real env vars work on their own
    pass

FOLDER_NAME = os.environ.get("DRIVE_FOLDER_NAME", "Portfolio")
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]

HOLDINGS_XLSX = "holdings.xlsx"
TRADES_XLSX = "trades.xlsx"
CASHFLOWS_XLSX = "cashflows.xlsx"
PORTFOLIO_JSON = "portfolio.json"
DASHBOARD_JSON = "dashboard.json"
SIGNALS_JSON = "signals.json"
METADATA_JSON = "metadata.json"

JSON_FILES = [PORTFOLIO_JSON, DASHBOARD_JSON, SIGNALS_JSON, METADATA_JSON]


class DriveError(RuntimeError):
    """Drive is unreachable or rejected the call. The API turns this into a
    503 -- never a partial write."""


# --------------------------------------------------------------- backends
class LocalStore:
    """DRIVE_LOCAL_DIR backend. Same four operations against a folder."""

    credentials = None   # no Google API here -- sheets.py checks this to skip Sheets sync

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve() / FOLDER_NAME
        self.root.mkdir(parents=True, exist_ok=True)
        log.info("storage: local folder %s", self.root)

    def _path(self, name):
        return self.root / name

    def exists(self, name):
        return self._path(name).exists()

    def read_json(self, name):
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise DriveError(f"{name} is not valid JSON ({e})")

    def read_bytes(self, name):
        p = self._path(name)
        return p.read_bytes() if p.exists() else None

    def write_bytes(self, name, data, mime=None):
        # temp file + replace, so a crash mid-write cannot truncate the
        # existing file -- the same guarantee Drive's own upload gives.
        tmp = self._path(name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self._path(name))

    def write_json(self, name, obj):
        self.write_bytes(name, json.dumps(obj, indent=1, ensure_ascii=False).encode("utf-8"))

    def list_names(self):
        return sorted(p.name for p in self.root.iterdir() if p.is_file())


class DriveStore:
    """Google Drive backend, via a service account.

    One client PER THREAD, not one shared client behind a lock. The first
    version of this class built a single googleapiclient Resource and
    guarded every call with a threading.Lock, because the underlying
    httplib2.Http transport is not thread-safe and FastAPI runs sync
    endpoints in a threadpool -- sharing one connection across threads
    corrupted it and surfaced as "[SSL: WRONG_VERSION_NUMBER]". The lock
    fixed the crash but serialised every Drive call the app ever makes,
    including calls touching completely unrelated files, turning a save
    (five or six real network round trips) into a ~20s wait. build() does
    no network I/O -- it is a local object built from a static discovery
    document -- so giving each thread its own costs one extra object, not
    an extra request, and lets unrelated files transfer in parallel again.
    """

    def __init__(self, creds):
        self._creds = creds
        self._local = threading.local()
        self.folder_id = None
        self._ids: dict[str, str] = {}
        self._ids_lock = threading.Lock()   # guards the dict only, never held during I/O

    @property
    def svc(self):
        if not hasattr(self._local, "svc"):
            from googleapiclient.discovery import build
            self._local.svc = build("drive", "v3", credentials=self._creds, cache_discovery=False)
        return self._local.svc

    # -- helpers -----------------------------------------------------
    def _q(self, **kw):
        try:
            return self.svc.files().list(
                spaces="drive", fields="files(id,name)", pageSize=50, **kw
            ).execute().get("files", [])
        except Exception as e:
            raise DriveError(f"Google Drive is unreachable ({e})")

    def ensure_folder(self):
        shared = os.environ.get("DRIVE_FOLDER_ID")
        if shared:
            self.folder_id = shared
            log.info("storage: Google Drive folder id %s (from DRIVE_FOLDER_ID)", shared)
            return
        found = self._q(q=f"name='{FOLDER_NAME}' and mimeType='{FOLDER_MIME}' and trashed=false")
        if found:
            self.folder_id = found[0]["id"]
        else:
            try:
                self.folder_id = self.svc.files().create(
                    body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME}, fields="id"
                ).execute()["id"]
            except Exception as e:
                raise DriveError(f"could not create the {FOLDER_NAME} folder ({e})")
            log.info("created Drive folder %s", FOLDER_NAME)
        log.info("storage: Google Drive folder %s (%s)", FOLDER_NAME, self.folder_id)

    def prime(self):
        """One listing instead of one query per file. Startup used to make
        six round trips before serving anything, which on a serverless host
        is paid again on every cold start."""
        ids = {f["name"]: f["id"]
               for f in self._q(q=f"'{self.folder_id}' in parents and trashed=false")}
        with self._ids_lock:
            self._ids = ids
            self._primed = True
        return set(ids)

    @property
    def credentials(self):
        return self._creds

    def file_id(self, name):
        """Public wrapper -- sheets.py needs the Drive file id of a native
        Google Sheet (same id space as spreadsheetId) without reaching into
        the underlying cache directly."""
        return self._file_id(name)

    def _file_id(self, name, refresh=False):
        if not refresh:
            with self._ids_lock:
                if name in self._ids:
                    return self._ids[name]
                # After prime(), absent from the cache means absent from
                # the folder -- no point asking Drive again.
                if getattr(self, "_primed", False):
                    return None
        found = self._q(q=f"name='{name}' and '{self.folder_id}' in parents and trashed=false")
        with self._ids_lock:
            if not found:
                self._ids.pop(name, None)
                return None
            self._ids[name] = found[0]["id"]
            return self._ids[name]

    # -- the four operations -----------------------------------------
    def exists(self, name):
        if getattr(self, "_primed", False):
            with self._ids_lock:
                return name in self._ids
        return self._file_id(name, refresh=True) is not None

    def read_bytes(self, name):
        from googleapiclient.http import MediaIoBaseDownload

        fid = self._file_id(name)
        if fid is None:
            return None
        buf = io.BytesIO()
        try:
            dl = MediaIoBaseDownload(buf, self.svc.files().get_media(fileId=fid))
            done = False
            while not done:
                _, done = dl.next_chunk()
        except Exception as e:
            raise DriveError(f"could not read {name} from Drive ({e})")
        return buf.getvalue()

    def read_json(self, name):
        raw = self.read_bytes(name)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise DriveError(f"{name} on Drive is not valid JSON ({e})")

    def write_bytes(self, name, data, mime="application/octet-stream"):
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        fid = self._file_id(name)
        try:
            if fid:
                # update() replaces content in place: the old version
                # stays readable until the new one lands, so a failed
                # upload leaves the previous file intact, not truncated.
                self.svc.files().update(fileId=fid, media_body=media).execute()
            else:
                created = self.svc.files().create(
                    body={"name": name, "parents": [self.folder_id]},
                    media_body=media, fields="id", supportsAllDrives=True,
                ).execute()
                with self._ids_lock:
                    self._ids[name] = created["id"]
        except Exception as e:
            # The one failure worth naming precisely. A service account has
            # no storage of its own, so it can modify a file you own but
            # cannot create one. Shared drives are the documented fix and
            # they need Google Workspace; on personal Gmail the answer is
            # to create the files yourself, once.
            if "storageQuotaExceeded" in str(e) or "do not have storage quota" in str(e):
                raise DriveError(
                    f"cannot create {name}: a Google service account has no storage quota of its "
                    f"own, so it can only update files that already exist. Run "
                    f"`python backend/make_starter_files.py` and upload the results to your "
                    f"Portfolio folder once — after that everything works normally."
                )
            raise DriveError(f"could not write {name} to Drive ({e})")

    def write_json(self, name, obj):
        self.write_bytes(name, json.dumps(obj, indent=1, ensure_ascii=False).encode("utf-8"),
                          mime="application/json")

    def list_names(self):
        return sorted(f["name"] for f in self._q(q=f"'{self.folder_id}' in parents and trashed=false"))


# ------------------------------------------------------------------ init
_store = None
_connect_lock = threading.Lock()


def _credentials():
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise DriveError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON ({e})")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Look next to this file as well as in the working directory: uvicorn
    # gets launched from the repo root as often as from backend/, and a
    # credential found only half the time is worse than one never found.
    candidates = [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                  "service-account.json",
                  str(Path(__file__).resolve().parent / "service-account.json")]
    for path in filter(None, candidates):
        if os.path.exists(path):
            log.info("credentials: %s", path)
            return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return None


def connect():
    """Called once on startup. Picks a backend, ensures the folder, and
    creates any missing JSON file so every GET has something to return."""
    global _store
    local = os.environ.get("DRIVE_LOCAL_DIR")
    if local:
        _store = LocalStore(local)
    else:
        creds = _credentials()
        if creds is None:
            raise DriveError(
                "no Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON, or put "
                "service-account.json next to main.py, or set DRIVE_LOCAL_DIR to run "
                "against a plain folder. See docs/GOOGLE_DRIVE_SETUP.md."
            )
        _store = DriveStore(creds)
        _store.ensure_folder()
        _store.prime()          # one listing, not one query per file

    missing = [n for n in JSON_FILES if not _store.exists(n)]
    for name in missing:
        try:
            _store.write_json(name, _empty(name))
            log.info("created missing %s", name)
        except DriveError as e:
            # Don't take the whole app down for this: reads of the files
            # that DO exist still work, and the message says what to do.
            log.error("%s", e)
            raise
    return _store


def store():
    """connect() is meant to run once at startup (see main.py's startup
    event), but that only actually fires on platforms whose ASGI runtime
    implements the lifespan protocol -- several serverless Python runtimes
    (Vercel's included) invoke the app per-request without ever sending a
    lifespan.startup message, so the event handler silently never runs and
    _store stays None forever. Connecting lazily here means the first real
    request pays connect()'s cost instead of every request failing with a
    generic "not connected" -- and whatever connect() actually raises
    (missing credentials, Drive API disabled, ...) surfaces as the real
    reason instead of being masked by that message."""
    global _store
    if _store is None:
        # Double-checked: FastAPI runs sync endpoints in a threadpool, so
        # a cold start can see several requests hit this at once. Only the
        # first should pay for ensure_folder()/prime(); the rest just wait
        # and reuse what it built instead of each redoing that work.
        with _connect_lock:
            if _store is None:
                connect()
    return _store


def _empty(name):
    """A missing file must still satisfy the shape its endpoint returns, so
    a fresh install renders an empty dashboard rather than a stack trace."""
    if name == METADATA_JSON:
        return {"holdingsUpdated": None, "tradesUpdated": None,
                "signalsUpdated": None, "holdings": 0, "trades": 0, "sourceFiles": {}}
    if name == SIGNALS_JSON:
        return {"asof": None, "index": "the index", "signals": {},
                "bench_sect": {}, "bench_size": {}, "bench_index_level": {}, "errors": []}
    return {}


# --------------------------------------------------------------- memo
# A page load asks six endpoints for the same dashboard.json within a few
# hundred milliseconds. Without this that is six 47KB downloads, serialised
# behind the lock, and the page took ~10s. TTL is deliberately tiny and any
# write clears the whole memo, so Drive remains the source of truth.
MEMO_TTL = float(os.environ.get("DRIVE_MEMO_TTL", "5"))
_memo: dict[str, tuple[float, object]] = {}
_memo_lock = threading.Lock()


_inflight: dict[str, threading.Lock] = {}


def _memo_get(name):
    with _memo_lock:
        hit = _memo.get(name)
        if hit and (time.monotonic() - hit[0]) < MEMO_TTL:
            return hit[1]
        return None


def invalidate():
    with _memo_lock:
        _memo.clear()


def read_json(name):
    """Single-flight: eight endpoints ask for dashboard.json at once on
    every page load. Without this they all miss the memo (none has been
    populated yet), all eight hit Drive, and serialised behind the API lock
    that took ~10s and blew the client's timeout. Now the first caller
    fetches and the rest wait on it, then read the memo it filled."""
    hit = _memo_get(name)
    if hit is None:
        with _memo_lock:
            gate = _inflight.setdefault(name, threading.Lock())
        with gate:
            hit = _memo_get(name)          # filled while we waited?
            if hit is None:
                hit = store().read_json(name)
                if hit is not None:
                    with _memo_lock:
                        _memo[name] = (time.monotonic(), hit)
    if hit is None:
        return None
    return json.loads(json.dumps(hit))     # a copy, never the memo itself


def write_json(name, obj):
    store().write_json(name, obj)
    invalidate()          # the next read must see what was just written


def read_bytes(name):
    return store().read_bytes(name)


def write_bytes(name, data, mime="application/octet-stream"):
    store().write_bytes(name, data, mime)
    invalidate()
