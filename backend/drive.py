"""Google Drive storage. The only place this app persists anything.

Four operations, which is all a single-user dashboard needs:

    ensure_folder()        the Portfolio folder exists
    read_json(name)        -> dict | None
    write_json(name, obj)  replace, atomically from Drive's point of view
    write_bytes(name, ...) replace an uploaded .xlsx

Credentials, in order:
  1. GOOGLE_SERVICE_ACCOUNT_JSON   the JSON key itself (for Vercel/Render/etc)
  2. GOOGLE_APPLICATION_CREDENTIALS or ./service-account.json  (local dev)
  3. DRIVE_LOCAL_DIR               a plain folder, no Google at all

Option 3 exists so the app runs, and the tests run, without a service
account. It is the same four operations against a directory -- about ten
lines -- and without it nothing here could be exercised at all.

Nothing is cached. Drive is the single source of truth; a stale copy in
process memory is how two tabs start disagreeing.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("dl.drive")

FOLDER_NAME = os.environ.get("DRIVE_FOLDER_NAME", "Portfolio")
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]

HOLDINGS_XLSX = "holdings.xlsx"
TRADES_XLSX = "trades.xlsx"
PORTFOLIO_JSON = "portfolio.json"
DASHBOARD_JSON = "dashboard.json"
SIGNALS_JSON = "signals.json"
METADATA_JSON = "metadata.json"
HISTORY_JSON = "history.json"

JSON_FILES = [PORTFOLIO_JSON, DASHBOARD_JSON, SIGNALS_JSON, METADATA_JSON, HISTORY_JSON]


class DriveError(RuntimeError):
    """Drive is unreachable or rejected the call. The API turns this into a
    503 -- never a partial write."""


# --------------------------------------------------------------- backends
class LocalStore:
    """DRIVE_LOCAL_DIR backend. Same four operations against a folder."""

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
    """Google Drive backend, via a service account."""

    def __init__(self, creds):
        from googleapiclient.discovery import build

        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folder_id = None
        self._ids: dict[str, str] = {}

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

    def _file_id(self, name, refresh=False):
        if not refresh and name in self._ids:
            return self._ids[name]
        found = self._q(q=f"name='{name}' and '{self.folder_id}' in parents and trashed=false")
        if not found:
            self._ids.pop(name, None)
            return None
        self._ids[name] = found[0]["id"]
        return self._ids[name]

    # -- the four operations -----------------------------------------
    def exists(self, name):
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
                # update() replaces content in place: the old version stays
                # readable until the new one lands, so a failed upload
                # leaves the previous file intact rather than a truncated one.
                self.svc.files().update(fileId=fid, media_body=media).execute()
            else:
                created = self.svc.files().create(
                    body={"name": name, "parents": [self.folder_id]},
                    media_body=media, fields="id", supportsAllDrives=True,
                ).execute()
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
    if _store is None:
        raise DriveError("storage not connected — connect() runs on startup")
    return _store


def _empty(name):
    """A missing file must still satisfy the shape its endpoint returns, so
    a fresh install renders an empty dashboard rather than a stack trace."""
    if name == METADATA_JSON:
        return {"holdingsUpdated": None, "tradesUpdated": None,
                "signalsUpdated": None, "holdings": 0, "trades": 0, "sourceFiles": {}}
    if name == HISTORY_JSON:
        return {"points": [], "count": 0, "sectors": []}
    if name == SIGNALS_JSON:
        return {"asof": None, "index": "the index", "signals": {},
                "bench_sect": {}, "bench_size": {}, "errors": []}
    return {}


# convenience passthroughs
def read_json(name):
    return store().read_json(name)


def write_json(name, obj):
    store().write_json(name, obj)


def read_bytes(name):
    return store().read_bytes(name)


def write_bytes(name, data, mime="application/octet-stream"):
    store().write_bytes(name, data, mime)
