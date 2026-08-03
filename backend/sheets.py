"""Google Sheets sync -- optional, best-effort mirrors of the live book.

Two native Google Sheets, created once by hand (same one-time-setup
constraint as everything else here -- the service account can't create new
files, only update existing ones) and looked up by name in the Portfolio
folder afterward:

    Current   overwritten on every import/recalc -- today's snapshot
    History   one row appended per day the evening job runs, deduped by
              date so a manual re-run the same day updates the row instead
              of duplicating it

Never required for the app to work: if a sheet doesn't exist yet, sync is
skipped (logged once) rather than erroring -- same reasoning as the
archival .xlsx copies in api.py. A no-op entirely under DRIVE_LOCAL_DIR
(tests, local dev without Google credentials).
"""
from __future__ import annotations

import datetime as dt
import logging

import drive

log = logging.getLogger("dl.sheets")

CURRENT_SHEET = "Current"
HISTORY_SHEET = "History"
TAB = "Sheet1"

HISTORY_HEADER = ["Date", "NAV", "Cash Balance", "Holdings Value", "Total P&L", "Realized",
                   "Unrealized", "Cash %", "Equity %", "XIRR", "Holdings Count"]

_warned = set()


def _service():
    creds = drive.store().credentials
    if not creds:
        return None                      # local/test backend -- no Google API at all
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id(name):
    fid = drive.store().file_id(name)
    if not fid and name not in _warned:
        _warned.add(name)
        log.info("%r sheet not found in the Portfolio folder -- sync skipped "
                  "(create an empty Google Sheet named %r there to enable it)", name, name)
    return fid


def _n(v, places=2):
    return round(v, places) if isinstance(v, (int, float)) else v


# --------------------------------------------------------- row building
# Pure functions, no network -- easy to check without a real Sheets call.
def current_rows(derived):
    d, nav, positions = derived["dashboard"], derived["nav"], derived["positions"]["positions"]
    rows = [
        ["DL India Core -- current snapshot", derived["generatedAt"]],
        [],
        ["NAV", _n(nav["nav"])],
        ["Cash balance", _n(nav["cashBalance"])],
        ["Holdings value", _n(nav["holdingsValue"])],
        ["Contributed", _n(nav["contributed"])],
        ["Withdrawn", _n(nav["withdrawn"])],
        ["Equity %", _n(d["equityWeight"] * 100, 1)],
        ["Cash %", _n(d["cashWeight"] * 100, 1)],
        ["Total P&L", _n(d["overallPL"])],
        ["Realized", _n(d["realized"])],
        ["Unrealized", _n(d["unrealized"])],
        ["Costs", _n(d["costs"])],
        ["XIRR", _n(nav["xirr"], 4)],
        ["Benchmark XIRR", _n(nav["benchXirr"], 4)],
        ["Alpha", _n(nav["alpha"], 4)],
        [],
        ["Ticker", "Company", "Sector", "Weight %", "CMP", "Upside %", "Status"],
    ]
    for p in positions:
        rows.append([p["tk"], p["co"], p["sector"], _n(p["wt"] * 100), p["cmp"],
                     _n(p["upside"] * 100, 1) if p["upside"] is not None else None,
                     p["status"]["text"]])
    return rows


def history_row(derived):
    d, nav = derived["dashboard"], derived["nav"]
    return [
        dt.date.today().isoformat(), _n(nav["nav"]), _n(nav["cashBalance"]),
        _n(nav["holdingsValue"]), _n(d["overallPL"]), _n(d["realized"]), _n(d["unrealized"]),
        _n(d["cashWeight"] * 100, 1), _n(d["equityWeight"] * 100, 1), _n(nav["xirr"], 4),
        d["holdingsCount"],
    ]


# ------------------------------------------------------------------ sync
def sync_current(derived):
    """Best-effort: overwrite the Current sheet with today's snapshot."""
    try:
        svc = _service()
        sid = svc and _sheet_id(CURRENT_SHEET)
        if not (svc and sid):
            return
        svc.spreadsheets().values().clear(spreadsheetId=sid, range=TAB, body={}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{TAB}!A1", valueInputOption="RAW",
            body={"values": current_rows(derived)}).execute()
    except Exception as e:
        log.warning("Current sheet sync failed (dashboard itself is unaffected): %s", e)


def append_history(derived):
    """Best-effort: add (or update) today's row in the History sheet.

    Deduped by date -- re-running the evening job twice in one day (a
    manual retrigger after a failure, say) must update that day's row, not
    grow a second one."""
    try:
        svc = _service()
        sid = svc and _sheet_id(HISTORY_SHEET)
        if not (svc and sid):
            return
        existing = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"{TAB}!A:A").execute()
        dates = [r[0] for r in existing.get("values", []) if r]
        row = history_row(derived)
        today = row[0]
        if today in dates:
            row_num = dates.index(today) + 1   # 1-indexed sheet row == list index (header is dates[0])
            svc.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{TAB}!A{row_num}", valueInputOption="RAW",
                body={"values": [row]}).execute()
            return
        if not dates:                          # brand new sheet -- lay the header down first
            svc.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{TAB}!A1", valueInputOption="RAW",
                body={"values": [HISTORY_HEADER]}).execute()
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"{TAB}!A:A", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row]}).execute()
    except Exception as e:
        log.warning("History sheet sync failed (dashboard itself is unaffected): %s", e)
