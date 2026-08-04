"""Routes. Reads return a stored JSON payload; imports recalculate and store.

GET endpoints do no maths at all -- they hand back what the last import or
the last evening run computed. That is the whole point of storing derived
JSON on Drive: two tabs opened a second apart cannot disagree.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import calculator
import drive
import parser
import sheets
from drive import (CASHFLOWS_XLSX, DASHBOARD_JSON, HOLDINGS_XLSX, METADATA_JSON,
                    PORTFOLIO_JSON, SIGNALS_JSON, TRADES_XLSX, DriveError)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

log = logging.getLogger("dl.api")
router = APIRouter(prefix="/api")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD = 5 * 1024 * 1024        # a holdings or trades sheet is tens of KB
EMPTY_BOOK = {"holdings": [], "trades": [], "cash": 0.0, "cashflows": []}


def _book():
    b = drive.read_json(PORTFOLIO_JSON) or dict(EMPTY_BOOK)
    b.setdefault("cashflows", [])   # a book stored before this field existed
    return b


def _feed():
    return drive.read_json(SIGNALS_JSON) or {}


def _meta():
    return drive.read_json(METADATA_JSON) or {}


def _screen(name):
    """Serve one screen out of the stored derived file."""
    try:
        derived = drive.read_json(DASHBOARD_JSON) or {}
    except DriveError as e:
        raise HTTPException(503, str(e))
    if name not in derived:
        # Nothing imported yet: build an empty payload rather than 404 so a
        # fresh install renders an empty dashboard instead of an error.
        return calculator.recalculate(dict(EMPTY_BOOK), {}, {})[name]
    return derived[name]


# ------------------------------------------------------------- reads
@router.get("/dashboard", tags=["dashboard"], summary="Summary cards")
def get_dashboard():
    return _screen("dashboard")


@router.get("/portfolio", tags=["dashboard"], summary="Overview tab")
def get_portfolio():
    return _screen("portfolio")


@router.get("/risk", tags=["dashboard"], summary="Risk tab")
def get_risk():
    return _screen("risk")


@router.get("/performance", tags=["dashboard"], summary="Performance tab")
def get_performance():
    return _screen("performance")


@router.get("/exposure", tags=["dashboard"], summary="Exposures tab")
def get_exposure():
    return _screen("exposure")


@router.get("/positions", tags=["dashboard"], summary="Positions tab")
def get_positions():
    return _screen("positions")


@router.get("/nav", tags=["dashboard"], summary="Real rupee NAV and cash balance")
def get_nav():
    return _screen("nav")


@router.get("/signals", tags=["dashboard"], summary="Market data feed")
def get_signals():
    try:
        feed = _feed()
    except DriveError as e:
        raise HTTPException(503, str(e))
    sect, size, live = calculator._bench(feed)
    return {**calculator._feed_meta(feed), "benchmarkLive": live,
            "benchSector": sect, "benchSize": size,
            "signals": feed.get("signals") or {}, "count": len(feed.get("signals") or {})}


def _xlsx_download(wb, filename):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type=XLSX_MIME,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/holdings", tags=["export"], summary="Download current holdings as .xlsx")
def export_holdings():
    """Every holding currently on record, including a fully-wound-down
    position still sitting in the book with fullWt 0 -- this reads the
    same book the app itself uses, not the last raw upload, so it reflects
    every import since. Re-uploadable as-is: same columns the importer
    reads."""
    try:
        book = _book()
    except DriveError as e:
        raise HTTPException(503, str(e))
    return _xlsx_download(parser.to_holdings_xlsx(book["holdings"]), "holdings_export.xlsx")


def _in_range(row_date, from_date, to_date):
    # ISO "YYYY-MM-DD" strings sort/compare lexically same as dates.
    return (not from_date or row_date >= from_date) and (not to_date or row_date <= to_date)


@router.get("/export/trades", tags=["export"], summary="Download the trade ledger as .xlsx, optionally date-filtered")
def export_trades(from_date: str | None = None, to_date: str | None = None):
    try:
        book = _book()
    except DriveError as e:
        raise HTTPException(503, str(e))
    trades = [t for t in book["trades"] if _in_range(t["date"], from_date, to_date)]
    return _xlsx_download(parser.to_trades_xlsx(trades), "trades_export.xlsx")


@router.get("/export/cashflows", tags=["export"], summary="Download the capital flow ledger as .xlsx, optionally date-filtered")
def export_cashflows(from_date: str | None = None, to_date: str | None = None):
    try:
        book = _book()
    except DriveError as e:
        raise HTTPException(503, str(e))
    cashflows = [c for c in book["cashflows"] if _in_range(c["date"], from_date, to_date)]
    return _xlsx_download(parser.to_cashflows_xlsx(cashflows), "cashflows_export.xlsx")


@router.get("/export/history", tags=["export"], summary="Download the History Google Sheet as .xlsx")
def export_history():
    """The History sheet lives on Google Sheets, not Drive JSON/xlsx, so
    this is the one export that isn't reading straight out of the book --
    it reads the sheet back over the Sheets API and hands it back as a
    local file with the same tabs (daily log + the raw ledger mirrors)."""
    tabs = sheets.read_all_tabs(sheets.HISTORY_SHEET)
    if tabs is None:
        raise HTTPException(503, "The History Google Sheet isn't reachable right now -- check it "
                                  "exists in the Portfolio folder and the service account can read it.")
    return _xlsx_download(parser.to_multi_tab_xlsx(tabs), "history_export.xlsx")


# ----------------------------------------------------------- imports
async def _read(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(422, "Empty file.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"File too large ({len(data)} bytes, limit {MAX_UPLOAD}).")
    return data


def _run_concurrently(critical=(), best_effort=()):
    """Each Drive write costs a near-fixed ~2.5s of round-trip latency
    regardless of payload size (measured: a 200-byte file and a 40KB file
    took the same time) -- it is per-request overhead, not bandwidth. Doing
    N independent writes one after another costs N times that; since each
    thread now owns its own Drive client (see DriveStore), running them on
    a small thread pool costs roughly ONE round trip's worth of wall clock
    instead of N.

    `critical` writes must all succeed -- the first failure among them
    aborts the request, same as running sequentially. `best_effort` writes
    (an archival copy, bookkeeping metadata -- nothing else reads them to
    compute anything) are logged on failure, not raised: a flaky
    non-critical write must never make an ALREADY-SUCCEEDED critical write
    look like it failed too. That exact bug shipped once -- the archival
    cashflows.xlsx copy failed because its starter-files placeholder
    didn't exist yet, and the whole import reported failure even though
    portfolio.json had already saved the real data."""
    def _guarded(fn, must_succeed):
        try:
            fn()
        except DriveError as e:
            if must_succeed:
                raise
            log.warning("best-effort write failed (nothing critical was lost): %s", e)

    with ThreadPoolExecutor(max_workers=len(critical) + len(best_effort)) as ex:
        futures = ([ex.submit(_guarded, fn, True) for fn in critical] +
                   [ex.submit(_guarded, fn, False) for fn in best_effort])
        for f in futures:
            f.result()


def _trigger_signals_refresh():
    """Best-effort: ask the existing update-signals GitHub Actions workflow
    to run now, instead of waiting for its 18:00 IST schedule -- a ticker
    added by this import has no price/z-score until that job runs. Reuses
    the workflow's own workflow_dispatch trigger (already there for the
    manual "Run workflow" button) rather than re-fetching prices inline:
    that fetch takes ~100s against 750+ symbols, far past what a request
    handler (and Vercel's function timeout) should ever wait on.

    Needs GITHUB_TOKEN (repo-scoped, "Actions: write") and GITHUB_REPO
    ("owner/repo") set as env vars.

    -> a short status string, surfaced in the import response. This used
    to log-and-swallow, which on a serverless host means the failure is
    invisible: the workflow silently never fires, signals never refresh,
    and the dashboard just looks broken with nothing anywhere saying why.
    Still never raises -- a failed trigger must not fail the import -- but
    now it says so where someone can actually read it."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not (token and repo):
        missing = " and ".join(n for n, v in (("GITHUB_TOKEN", token), ("GITHUB_REPO", repo)) if not v)
        return f"not configured ({missing} unset) — full signal refresh will wait for the nightly job"
    ref = os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/update-signals.yml/dispatches"
    req = urllib.request.Request(url, data=json.dumps({"ref": ref}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return "queued"
    except Exception as e:
        detail = ""
        body = getattr(e, "read", None)
        if body:                        # HTTPError carries GitHub's own reason
            try:
                detail = f" — {json.loads(body()).get('message', '')}"
            except Exception:
                pass
        log.warning("could not trigger update-signals workflow (import unaffected): %s%s", e, detail)
        return f"failed: {e}{detail} (repo={repo!r}, ref={ref!r})"


def _quick_refresh_prices(book):
    """Best-effort: fetch live cmp/mcap/lo/hi for just THIS book's own
    holdings (~10-20 names, a few seconds) and merge into the stored feed,
    so P&L/NAV reflect the current price in this same import response --
    not the ~3 minutes the full cross-sectional job genuinely needs for
    750+ symbols (see _trigger_signals_refresh, which still runs that job
    in the background for val/momo/qual/beta -- unaffected by this).
    Only the fields quick_prices() actually recomputes get touched; a
    Yahoo/import hiccup here just leaves prices as stale as they already
    were, never breaks the import itself."""
    try:
        import build_signals
        updates, errors = build_signals.quick_prices(book["holdings"])
        if not updates:
            return
        feed = _feed()
        signals = feed.setdefault("signals", {})
        for tk, patch in updates.items():
            signals.setdefault(tk, {}).update(patch)
        drive.write_json(SIGNALS_JSON, feed)
    except Exception as e:
        log.warning("quick price refresh failed (import itself is unaffected): %s", e)


def _store_and_recalculate(book, meta, also=None):
    """Write the book, recompute every screen.

    `also` is an extra zero-arg write (the original uploaded .xlsx) -- a
    courtesy archival copy nothing else reads, so it's best-effort: it
    runs alongside the real write instead of waiting its turn, and its
    failure doesn't fail the request. metadata.json is the same story --
    display bookkeeping (source filenames, timestamps), not something
    P&L/NAV correctness depends on.

    The one ordering rule that stays real: portfolio.json is written
    BEFORE anything derived from it, so a failure partway through leaves
    only a stale derived set, never one that describes a book that was
    never actually saved. dashboard.json is the other genuinely critical
    write -- it's what every GET endpoint actually serves.
    """
    _run_concurrently(critical=[lambda: drive.write_json(PORTFOLIO_JSON, book)],
                      best_effort=[also] if also else [])

    derived = calculator.recalculate(book, _feed(), meta)

    _run_concurrently(critical=[lambda: drive.write_json(DASHBOARD_JSON, derived)],
                      best_effort=[lambda: drive.write_json(METADATA_JSON, meta),
                                   lambda: sheets.sync_current(derived),
                                   lambda: sheets.sync_ledgers(sheets.CURRENT_SHEET, book)])
    return derived


_PARSERS = {"holdings": parser.parse_holdings, "trades": parser.parse_trades,
            "cashflows": parser.parse_cashflows}


@router.post("/validate/{kind}", tags=["import"], summary="Check a file parses cleanly, without saving anything")
async def validate_import(kind: str, file: UploadFile = File(...)):
    """Read-only: parses the file exactly as the real import would, but
    never touches Drive -- lets the frontend confirm every file in a
    holdings+trades(+cashflows) upload is individually valid BEFORE
    committing any of them. Without this, a bad trades file uploaded
    after a good holdings file left the holdings write already saved with
    nothing to show for it but a red error message -- the exact opposite
    of the "no partial upload" the Save Permanently dialog already
    promises."""
    if kind not in _PARSERS:
        raise HTTPException(404, f"Unknown import kind {kind!r}.")
    data = await _read(file)
    try:
        _, errors = _PARSERS[kind](data, file.filename)
    except parser.ImportError_ as e:
        raise HTTPException(422, str(e))
    return {"file": file.filename, "errors": errors}


@router.post("/import/holdings", tags=["import"], summary="Upload a holdings file")
async def import_holdings(file: UploadFile = File(...)):
    """A holdings file is a full snapshot of the book: it replaces the
    holdings list and redefines cash, but never touches trade history."""
    data = await _read(file)
    try:
        holdings, errors = parser.parse_holdings(data, file.filename)
    except parser.ImportError_ as e:
        raise HTTPException(422, str(e))

    try:
        book = _book()
        book["holdings"] = holdings
        # Cash is whatever the weights do not claim.
        book["cash"] = max(0.0, 1.0 - sum(h["wt"] for h in holdings))

        meta = _meta()
        meta["holdingsUpdated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta["holdings"] = len(holdings)
        meta.setdefault("sourceFiles", {})["holdings"] = file.filename

        _quick_refresh_prices(book)
        _store_and_recalculate(book, meta, also=lambda: drive.write_bytes(HOLDINGS_XLSX, data, XLSX_MIME))
    except DriveError as e:
        raise HTTPException(503, str(e))

    refresh = _trigger_signals_refresh()
    log.info("holdings import %s: %d rows, %d rejected", file.filename, len(holdings), len(errors))
    return {"file": file.filename, "holdings": len(holdings), "trades": len(book["trades"]),
            "cash": book["cash"], "errors": errors, "signalRefresh": refresh}


@router.post("/import/trades", tags=["import"], summary="Upload a trades file")
async def import_trades(file: UploadFile = File(...)):
    """Trades are a ledger: a new file ADDS to what is recorded. Duplicate
    rows are ignored, and a trade for an unknown ticker creates a
    placeholder holding flagged needsMetadata -- unless it's already fully
    exited (net quantity zero), in which case there is no open position to
    manage and it's left as pure trade history."""
    data = await _read(file)
    try:
        trades, errors = parser.parse_trades(data, file.filename)
    except parser.ImportError_ as e:
        raise HTTPException(422, str(e))

    try:
        book = _book()
        merged, added, dupes = parser.merge_trades(book["trades"], trades)
        book["trades"] = merged

        known = {h["tk"] for h in book["holdings"]}
        seen = set()
        placeholders = []
        # Scan the rows THIS file added, not the whole merged ledger -- a
        # ticker the analyst deliberately dropped from a later holdings
        # file still has old trades on record, and re-scanning history on
        # every unrelated trades upload would resurrect it as a
        # placeholder every time.
        for t in trades:
            tk = t["tk"]
            if tk in known or tk in seen:
                continue
            seen.add(tk)
            net = sum(x["qty"] if x["side"] == "Buy" else -x["qty"]
                      for x in merged if x["tk"] == tk)
            if net == 0:
                continue
            book["holdings"].append({"tk": tk, "co": tk, "wt": 0.0, "fullWt": 0.0,
                                      "tp": None, "addLvl": None, "sector": None,
                                      "industry": None, "analyst": None, "conv": None,
                                      "thesis": None, "strategy": None, "yfSymbol": None,
                                      "needsMetadata": True})
            known.add(tk)
            placeholders.append(tk)

        meta = _meta()
        meta["tradesUpdated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta["trades"] = len(merged)
        meta.setdefault("sourceFiles", {})["trades"] = file.filename

        _quick_refresh_prices(book)   # a placeholder holding above needs a price too
        _store_and_recalculate(book, meta, also=lambda: drive.write_bytes(TRADES_XLSX, data, XLSX_MIME))
    except DriveError as e:
        raise HTTPException(503, str(e))

    refresh = _trigger_signals_refresh()
    log.info("trades import %s: %d added, %d duplicates, %d placeholders, %d rejected",
             file.filename, added, dupes, len(placeholders), len(errors))
    return {"file": file.filename, "added": added, "duplicates": dupes,
            "total": len(merged), "placeholderHoldings": placeholders, "errors": errors,
            "signalRefresh": refresh}


@router.post("/import/cashflows", tags=["import"], summary="Upload a capital contributions/withdrawals file")
async def import_cashflows(file: UploadFile = File(...)):
    """Money moving in or out of the book from outside it -- never a trade.
    A ledger, same as trades: a new file ADDS, duplicates are ignored."""
    data = await _read(file)
    try:
        cashflows, errors = parser.parse_cashflows(data, file.filename)
    except parser.ImportError_ as e:
        raise HTTPException(422, str(e))

    try:
        book = _book()
        merged, added, dupes = parser.merge_cashflows(book["cashflows"], cashflows)
        book["cashflows"] = merged

        meta = _meta()
        meta["cashflowsUpdated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta["cashflows"] = len(merged)
        meta.setdefault("sourceFiles", {})["cashflows"] = file.filename

        _store_and_recalculate(book, meta, also=lambda: drive.write_bytes(CASHFLOWS_XLSX, data, XLSX_MIME))
    except DriveError as e:
        raise HTTPException(503, str(e))

    log.info("cashflows import %s: %d added, %d duplicates, %d rejected",
             file.filename, added, dupes, len(errors))
    return {"file": file.filename, "added": added, "duplicates": dupes,
            "total": len(merged), "errors": errors}


@router.get("/metadata", tags=["system"], summary="What is stored, and when it changed")
def get_metadata():
    try:
        return {**_meta(), "files": drive.store().list_names()}
    except DriveError as e:
        raise HTTPException(503, str(e))
