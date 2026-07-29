"""Routes. Reads return a stored JSON payload; imports recalculate and store.

GET endpoints do no maths at all -- they hand back what the last import or
the last evening run computed. That is the whole point of storing derived
JSON on Drive: two tabs opened a second apart cannot disagree.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

import calculator
import drive
import parser
from drive import (DASHBOARD_JSON, HISTORY_JSON, HOLDINGS_XLSX, METADATA_JSON,
                    PORTFOLIO_JSON, SIGNALS_JSON, TRADES_XLSX, DriveError)

log = logging.getLogger("dl.api")
router = APIRouter(prefix="/api")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD = 5 * 1024 * 1024        # a holdings or trades sheet is tens of KB
EMPTY_BOOK = {"holdings": [], "trades": [], "cash": 0.0}


def _book():
    return drive.read_json(PORTFOLIO_JSON) or dict(EMPTY_BOOK)


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


@router.get("/history", tags=["dashboard"], summary="History tab")
def get_history():
    try:
        return drive.read_json(HISTORY_JSON) or {"points": [], "count": 0, "sectors": []}
    except DriveError as e:
        raise HTTPException(503, str(e))


# ----------------------------------------------------------- imports
async def _read(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(422, "Empty file.")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"File too large ({len(data)} bytes, limit {MAX_UPLOAD}).")
    return data


def _store_and_recalculate(book, meta):
    """Write the book, recompute every screen, refresh today's history
    point. Order matters: the raw book lands first, so a failure halfway
    leaves derived files stale rather than describing a book that was
    never saved."""
    drive.write_json(PORTFOLIO_JSON, book)
    derived = calculator.recalculate(book, _feed(), meta)
    drive.write_json(DASHBOARD_JSON, derived)
    drive.write_json(METADATA_JSON, meta)
    history = drive.read_json(HISTORY_JSON) or {}
    drive.write_json(HISTORY_JSON,
                      calculator.append_history(history, calculator.snapshot_point(derived)))
    return derived


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

        drive.write_bytes(HOLDINGS_XLSX, data, XLSX_MIME)   # keep the original
        _store_and_recalculate(book, meta)
    except DriveError as e:
        raise HTTPException(503, str(e))

    log.info("holdings import %s: %d rows, %d rejected", file.filename, len(holdings), len(errors))
    return {"file": file.filename, "holdings": len(holdings), "trades": len(book["trades"]),
            "cash": book["cash"], "errors": errors}


@router.post("/import/trades", tags=["import"], summary="Upload a trades file")
async def import_trades(file: UploadFile = File(...)):
    """Trades are a ledger: a new file ADDS to what is recorded. Duplicate
    rows are ignored, and a trade for an unknown ticker creates a
    placeholder holding flagged needsMetadata."""
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
        placeholders = []
        for t in merged:
            if t["tk"] not in known:
                book["holdings"].append({"tk": t["tk"], "co": t["tk"], "wt": 0.0, "fullWt": 0.0,
                                          "tp": None, "addLvl": None, "sector": None,
                                          "industry": None, "analyst": None, "conv": None,
                                          "thesis": None, "strategy": None, "yfSymbol": None,
                                          "needsMetadata": True})
                known.add(t["tk"])
                placeholders.append(t["tk"])

        meta = _meta()
        meta["tradesUpdated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta["trades"] = len(merged)
        meta.setdefault("sourceFiles", {})["trades"] = file.filename

        drive.write_bytes(TRADES_XLSX, data, XLSX_MIME)
        _store_and_recalculate(book, meta)
    except DriveError as e:
        raise HTTPException(503, str(e))

    log.info("trades import %s: %d added, %d duplicates, %d placeholders, %d rejected",
             file.filename, added, dupes, len(placeholders), len(errors))
    return {"file": file.filename, "added": added, "duplicates": dupes,
            "total": len(merged), "placeholderHoldings": placeholders, "errors": errors}


@router.get("/metadata", tags=["system"], summary="What is stored, and when it changed")
def get_metadata():
    try:
        return {**_meta(), "files": drive.store().list_names()}
    except DriveError as e:
        raise HTTPException(503, str(e))
