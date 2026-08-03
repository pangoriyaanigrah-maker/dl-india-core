# DL India Core — CIO Dashboard

A personal portfolio dashboard for one Indian equity book (any mix of
large, mid, small and micro-cap):
holdings, trades, exposures, risk, performance, positions and
Excel import.

**No database.** Google Drive is the only storage.

```
browser (GitHub Pages)  index.html — fetch and render, no calculations
     │  GET /api/*
     ▼
FastAPI                 reads stored JSON, recalculates on import
     │
     ▼
Google Drive            Portfolio/  — 7 files, the whole persistence layer
     ▲
     └── scripts/daily_update.py    weekdays 18:00 IST
```

## Files

```
index.html              the dashboard (hosted on GitHub Pages)
config.js               where the backend lives — the one line to edit per deploy
backend/
  main.py               app, CORS, startup, error handling
  drive.py              Google Drive — the only place anything persists
  parser.py             Excel/CSV -> dicts, and the validation
  calculator.py         every number the dashboard shows
  api.py                the endpoints
scripts/
  daily_update.py       evening job: fetch -> recalculate -> snapshot
  build_signals.py      market data and factor maths
docs/GOOGLE_DRIVE_SETUP.md
```

## Running it

```bash
cd backend
pip install -r requirements.txt

# no Google account needed to try it:
DRIVE_LOCAL_DIR=./drive_local uvicorn main:app --port 8000
```

Open `http://localhost:8000/docs` for the API, and serve `index.html`
however you like. For real storage see
[docs/GOOGLE_DRIVE_SETUP.md](docs/GOOGLE_DRIVE_SETUP.md) — copy
`.env.example` to `.env` first.

## Endpoints

| | |
|---|---|
| `POST /api/import/holdings` | upload a holdings file |
| `POST /api/import/trades` | upload a trades file |
| `GET /api/dashboard` | summary cards |
| `GET /api/portfolio` | composition, staged actions, attribution, insights |
| `GET /api/risk` | scenarios, per-stock impacts, metrics |
| `GET /api/performance` | P&L by position, trade log |
| `GET /api/exposure` | factor tilts, sector active weights, size mix |
| `GET /api/positions` | one row per holding |
| `GET /api/signals` | market data feed |
| `GET /health` | reports whether storage is actually reachable |

GET endpoints return stored JSON. They compute nothing — everything is
recalculated when a file is imported or when the evening job runs.

## Importing

Import tab → drop a **holdings** file and a **trades** file (both required,
always uploaded together) → **Save Permanently** → confirm.

- **Holdings** are a full snapshot: they replace the holdings list —
  weights, targets, theses and each stock's held **quantity** — and
  redefine cash (`1 − sum of weights`). Trade history is never touched.
  Quantity sizes unrealized P&L; if it disagrees with what the trades
  ledger implies (bought − sold), the mismatch is flagged, not silently
  overridden.
- **Trades** append. Duplicates (same date, ticker, side, qty, price) are
  ignored. A trade for an unknown ticker creates a placeholder holding
  flagged *needs metadata*. Avg cost and realized P&L are always
  trades-derived.

Both accept `.xlsx` (sheet named `Portfolio` / `Trades`) or a flat `.csv`.
The original file is kept on Drive alongside the parsed result.

## Evening job

```bash
python scripts/daily_update.py
```

Fetches prices and recomputes every screen. If the fetch fails — or
returns nothing — **no file is written**: yesterday's data stays live and
the process exits non-zero.

## Clearing data

There is deliberately no delete button in the app.

**Do not delete the files in the Drive folder.** The service account has no
storage quota of its own — it can update a file that exists but cannot
create a new one (see `docs/GOOGLE_DRIVE_SETUP.md`). Delete
`portfolio.json` and the next startup's attempt to recreate it fails,
taking the whole app down.

Use `backend/reset_data.py` instead — it empties the file's *content*
without deleting the file itself:

```bash
python backend/reset_data.py          # clears portfolio.json, dashboard.json, metadata.json
python backend/reset_data.py --all    # also clears signals.json (market data)
```

## Backup / disaster recovery

No separate backup job. Two things already cover it, both native to Drive,
both already active — no setup needed:

- **Version history.** Every write here is a `files.update`, which Drive
  keeps a revision for automatically. A bad import, a `reset_data.py` run
  you didn't mean, a corrupted upload — right-click the file in Drive →
  **Manage versions** (or **File → Version history** when it's open) →
  restore the one before it happened.
- **Trash.** A file deleted from the Drive folder (by hand, not by this
  app) sits in Trash for 30 days before Google actually removes it.

This does not cover losing the Google account itself — that's a real gap,
knowingly accepted for a personal single-user tool rather than adding a
second storage backend and a second place your holdings data could leak
from. If that ever changes, the cheapest fix is a periodic manual export
(Drive → download the Portfolio folder) kept somewhere private.

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

Runs against a temp folder, so no Google account is needed.

## Known limitations

- No authentication. Anyone with the API URL can read and import. That is
  the stated design for a single-user tool — do not put it on a public
  URL you have shared.
