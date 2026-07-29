# Google Drive setup

Storage is one Drive folder. No database, no migrations, nothing to back
up separately — Drive already versions files for you.

```
Portfolio/
    holdings.xlsx      the last holdings file you uploaded, untouched
    trades.xlsx        the last trades file you uploaded, untouched
    portfolio.json     the book: holdings, trades, cash
    dashboard.json     every computed screen, in one file
    signals.json       market data from the evening job
    metadata.json      what changed and when
    history.json       one snapshot per day
```

## Why the files are split this way

**`portfolio.json` is the source of truth.** Imports write it; everything
else is derived from it.

**`dashboard.json` holds all six screens in one object** — dashboard,
portfolio, risk, performance, exposure, positions. One file means one
write, so the tabs can never disagree with each other. A half-updated set
is how a dashboard ends up showing yesterday's risk beside today's P&L.

**The original `.xlsx` files are kept** so you can always see exactly what
was uploaded, independent of how it was parsed.

## Creating the service account

1. **Google Cloud Console** → create or pick a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Name it anything; no roles needed.
4. Open the service account → **Keys → Add key → Create new key → JSON**.
   Save the file.

### Point it at a folder in *your* Drive (recommended)

A service account has its own Drive, which you cannot browse. To keep the
files somewhere you can actually see:

1. Create a folder called **Portfolio** in your own Google Drive.
2. Share it with the service account's email (`...@....iam.gserviceaccount.com`)
   as **Editor**.
3. Copy the folder ID from the URL —
   `drive.google.com/drive/folders/`**`1AbCdEf...`** — and set
   `DRIVE_FOLDER_ID` to it.

Skip this and the backend creates its own `Portfolio` folder in the
service account's Drive. That works, but the files are invisible to you.

## Configuring credentials

Copy `.env.example` to `.env` and pick one:

| Where | Variable |
|---|---|
| Hosted | `GOOGLE_SERVICE_ACCOUNT_JSON` — the whole key JSON on one line |
| Local | `GOOGLE_APPLICATION_CREDENTIALS`, or just drop `service-account.json` next to `main.py` |
| No Google | `DRIVE_LOCAL_DIR=./drive_local` — a plain folder |

`service-account.json` and `.env` are gitignored. Never commit either.

### The local option

`DRIVE_LOCAL_DIR` runs the same four storage operations against a
directory. It is what the test suite uses, and it means you can develop
without a Google account at all. Everything else behaves identically.

## Running

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8000       # docs at /docs
```

On startup the backend connects to Drive, ensures the folder exists, and
creates any missing JSON file. If storage is unreachable it **still
starts** and every endpoint returns 503 with the reason — a backend that
explains itself beats one that just refuses to boot. `GET /health` reports
real storage state, not just that the process is alive.

## Frontend

`index.html` is static; deploy the repo root to Vercel. Because the API
lives elsewhere, tell the page where:

```html
<script>window.API_BASE = "https://your-api.onrender.com";</script>
```

Add that line before the main `<script>` block. Leave it unset for
same-origin (a local `uvicorn` serving both).

Set `CORS_ORIGINS` on the backend to your Vercel URL once deployed.

## The evening job

```bash
python scripts/daily_update.py                # fetch, recalculate, snapshot
python scripts/daily_update.py --recalc-only  # no network
```

Fetches market data, writes `signals.json`, recomputes every screen and
appends a history point.

**Failure policy:** nothing is written until the fetch returns real data.
A crash, a network failure or an empty feed leaves every file exactly as
it was — yesterday's data stays live, the error is logged, and the process
exits non-zero. An empty feed counts as failure, not as "nothing is priced
today", because writing it would blank every price on the dashboard.

## If Drive goes down

- Reads return **503** with the reason. The dashboard shows its
  backend-unreachable banner.
- Imports return **503** *before* writing anything. The uploaded file is
  discarded rather than half-applied.
- Writes go through Drive's `update()`, which replaces content in place —
  the old version stays readable until the new one lands, so a failed
  upload leaves the previous file intact rather than truncated.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no Google credentials found` | None of the three options set. See the table above. |
| `could not create the Portfolio folder` | Drive API not enabled on the project. |
| Files upload but you cannot find them | No `DRIVE_FOLDER_ID` — they are in the service account's own Drive. Share a folder and set the ID. |
| `403` from Drive | The folder is not shared with the service account's email, or not as Editor. |
| CORS errors in the browser | `CORS_ORIGINS` does not include your Vercel URL. |
