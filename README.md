# DL India Core — CIO Dashboard

A personal portfolio dashboard for one Indian small/micro-cap equity book:
holdings, trades, exposures, risk, performance, positions, history and
Excel import.

**No database.** Google Drive is the only storage.

```
browser (Vercel)        index.html — fetch and render, no calculations
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
index.html              the dashboard (deploy this to Vercel)
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
| `GET /api/history` | daily snapshots |
| `GET /health` | reports whether storage is actually reachable |

GET endpoints return stored JSON. They compute nothing — everything is
recalculated when a file is imported or when the evening job runs.

## Importing

Import tab → drop a **holdings** file, a **trades** file, or both →
**Save Permanently** → confirm.

- **Holdings** are a full snapshot: they replace the holdings list and
  redefine cash (`1 − sum of weights`). Trade history is never touched.
- **Trades** append. Duplicates (same date, ticker, side, qty, price) are
  ignored. A trade for an unknown ticker creates a placeholder holding
  flagged *needs metadata*.

Both accept `.xlsx` (sheet named `Portfolio` / `Trades`) or a flat `.csv`.
The original file is kept on Drive alongside the parsed result.

## Evening job

```bash
python scripts/daily_update.py
```

Fetches prices, recomputes every screen, appends a history point. If the
fetch fails — or returns nothing — **no file is written**: yesterday's
data stays live and the process exits non-zero. A gap in the History tab
means that evening's run failed, never that figures were overwritten with
bad ones.

## Clearing data

There is deliberately no delete button in the app. Delete the files in the
Drive folder directly.

Delete `portfolio.json`, `dashboard.json` and `history.json` to clear the
book. **Leave `signals.json` alone** — that is market data, not yours to
lose, and removing it blanks every price until the evening job runs again.

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

Runs against a temp folder, so no Google account is needed.

## Known limitations

- **Never run against a real Google Drive here** — there is no service
  account in this environment. The Drive code is written to the API but
  has only been exercised through the local-folder backend. Verify with
  `GET /health` on first deploy.
- Two insight strings still hardcode names from the original mock
  (`BEL + Kaynes`, `Sun, Titan`) and fire on any book crossing their
  threshold. Pre-existing; worth fixing as its own task.
- No authentication. Anyone with the API URL can read and import. That is
  the stated design for a single-user tool — do not put it on a public
  URL you have shared.
