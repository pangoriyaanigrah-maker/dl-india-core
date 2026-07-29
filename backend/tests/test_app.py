"""End-to-end against the local storage backend: import real files, check
the maths, check the stored JSON, check the failure paths.

DRIVE_LOCAL_DIR is set before importing the app so `drive.connect()` uses
a temp folder rather than reaching for Google credentials.
"""
import datetime as dt
import io
import json
import os
import tempfile

import openpyxl
import pytest

TMP = tempfile.mkdtemp(prefix="dl_test_")
os.environ["DRIVE_LOCAL_DIR"] = TMP

from fastapi.testclient import TestClient   # noqa: E402
import calculator                            # noqa: E402
import drive                                 # noqa: E402
import parser                                # noqa: E402
from main import app                         # noqa: E402

HOLDINGS = [
    ["ALPHACHEM", "Alpha Chemicals", "Materials", "Specialty", "Dev", 8.0, 10.0, 620, 480, "High", "t", "s"],
    ["BETAFIN",  "Beta Finserv",    "Financials", "NBFC",     "Dev", 7.0, 0.0,  340, None, "Low",  "t", "s"],
]
H_HDR = ["Ticker", "Company", "Sector", "Industry", "Analyst", "Weight", "FullWeight",
         "Target", "AddLevel", "Conviction", "Thesis", "Strategy"]
T_HDR = ["Date", "Ticker", "Side", "Qty", "Price", "Costs"]
TRADES = [
    [dt.date(2026, 1, 15), "ALPHACHEM", "Buy", 400, 505.0, 202.0],
    [dt.date(2026, 2, 3), "BETAFIN", "Buy", 900, 298.0, 268.2],
]
FEED = {
    "asof": "29 Jul 2026, 18:00 IST", "index": "Test Index",
    "bench_sect": {"Materials": 0.2, "Financials": 0.3}, "bench_size": {"Small": 1.0},
    "errors": [],
    "signals": {
        "ALPHACHEM": {"cmp": 542.0, "mcap": 3400, "lo": 470.0, "hi": 615.0, "val": 0.3,
                       "momo": 0.6, "qual": 0.1, "r1m": 0.04, "worstM": -0.18, "mdd": -0.3,
                       "dvol": 0.02, "beta": 0.9},
        "BETAFIN": {"cmp": 305.0, "mcap": 2100, "lo": 260.0, "hi": 355.0, "val": 0.8,
                     "momo": -0.1, "qual": 0.4, "r1m": -0.02, "worstM": -0.22, "mdd": -0.4,
                     "dvol": 0.03, "beta": 1.1},
    },
}


def xlsx(sheet, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def client():
    for f in os.listdir(os.path.join(TMP, "Portfolio")) if os.path.isdir(os.path.join(TMP, "Portfolio")) else []:
        os.remove(os.path.join(TMP, "Portfolio", f))
    with TestClient(app) as c:          # startup event runs drive.connect()
        drive.write_json(drive.SIGNALS_JSON, FEED)
        yield c


def up(c, kind, data, name="f.xlsx"):
    return c.post(f"/api/import/{kind}", files={"file": (name, data, "application/octet-stream")})


# ------------------------------------------------------------ storage
def test_startup_creates_the_portfolio_folder_and_every_json_file(client):
    names = drive.store().list_names()
    for f in drive.JSON_FILES:
        assert f in names, f"{f} missing from {names}"


def test_health_reports_storage(client):
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["storage"] == "connected"


def test_empty_install_renders_rather_than_erroring(client):
    for path in ("/api/dashboard", "/api/portfolio", "/api/risk", "/api/performance",
                  "/api/exposure", "/api/positions", "/api/signals", "/api/history"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/dashboard").json()["holdingsCount"] == 0


# ------------------------------------------------------------- import
def test_holdings_import_stores_xlsx_and_recalculates(client):
    r = up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["holdings"] == 2
    assert body["cash"] == pytest.approx(1 - 0.15)      # 1 - (0.08 + 0.07)

    assert drive.store().exists(drive.HOLDINGS_XLSX)     # original kept
    book = drive.read_json(drive.PORTFOLIO_JSON)
    assert [h["tk"] for h in book["holdings"]] == ["ALPHACHEM", "BETAFIN"]
    d = client.get("/api/dashboard").json()
    assert d["holdingsCount"] == 2 and d["cashWeight"] == pytest.approx(0.85)


def test_percent_vs_fraction_decided_per_column_not_per_cell(client):
    """A 1.2% position must not be read as 120%."""
    up(client, "holdings", xlsx("Portfolio", ["Ticker", "Company", "Weight", "Target"],
                                 [["AAA", "A", 1.2, 620], ["BBB", "B", 98.8, 340]]))
    pos = {p["tk"]: p for p in client.get("/api/positions").json()["positions"]}
    assert pos["AAA"]["wt"] == pytest.approx(0.012)


def test_trades_append_and_dedupe(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    assert up(client, "trades", xlsx("Trades", T_HDR, TRADES)).json()["added"] == 2

    again = up(client, "trades", xlsx("Trades", T_HDR, TRADES + [
        [dt.date(2026, 3, 1), "ALPHACHEM", "Buy", 50, 520.0, 26.0]])).json()
    assert again["added"] == 1 and again["duplicates"] == 2 and again["total"] == 3


def test_trade_for_unknown_ticker_creates_a_placeholder(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    body = up(client, "trades", xlsx("Trades", T_HDR,
              [[dt.date(2026, 4, 1), "ZETAENER", "Buy", 10, 640.0, 6.4]])).json()
    assert body["placeholderHoldings"] == ["ZETAENER"]
    z = next(p for p in client.get("/api/positions").json()["positions"] if p["tk"] == "ZETAENER")
    assert z["needsMetadata"] is True


def test_holdings_reimport_does_not_touch_trade_history(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    up(client, "trades", xlsx("Trades", T_HDR, TRADES))
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    assert client.get("/api/performance").json()["tradeCount"] == 2


# --------------------------------------------------------------- maths
def test_pl_matches_a_hand_calculation(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    up(client, "trades", xlsx("Trades", T_HDR, TRADES))
    d = client.get("/api/dashboard").json()

    invested = 400 * 505 + 900 * 298
    value = 400 * 542 + 900 * 305
    costs = 202.0 + 268.2
    assert d["invested"] == pytest.approx(invested)
    assert d["currentValue"] == pytest.approx(value)
    assert d["unrealized"] == pytest.approx(value - invested)
    assert d["realized"] == 0
    assert d["overallPL"] == pytest.approx(value - invested - costs)
    # the identity that must always hold
    assert d["overallPL"] == pytest.approx(d["unrealized"] + d["realized"] - d["costs"])


def test_exiting_status_beats_a_target_hit(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    pos = {p["tk"]: p for p in client.get("/api/positions").json()["positions"]}
    assert pos["BETAFIN"]["status"]["text"] == "EXITING"      # fullWt == 0 wins
    assert pos["ALPHACHEM"]["status"]["text"] == "ON THESIS"


def test_risk_has_four_scenarios_and_excludes_unpriced(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    r = client.get("/api/risk").json()
    assert [s["key"] for s in r["scenarios"]] == ["mid", "give1m", "low", "worst"]
    assert r["priced"] == 2 and r["weightedBeta"] is not None


def test_give1m_survives_a_stock_that_went_to_zero():
    g = calculator.SCENARIOS[1][3]
    assert g({"r1m": -1.0}) is None          # 1 + r1m == 0 would divide by zero
    assert g({"r1m": 0.05}) == pytest.approx(1 / 1.05 - 1)


def test_size_gap_is_reported_not_hidden(client):
    """A holding with a price but no market cap belongs to no size bucket,
    so the bars total less than 100%. Say so rather than leave a hole."""
    feed = json.loads(json.dumps(FEED))
    feed["signals"]["BETAFIN"]["mcap"] = None
    drive.write_json(drive.SIGNALS_JSON, feed)
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    e = client.get("/api/exposure").json()
    assert e["sizeUnclassified"] > 0
    assert sum(s["portfolio"] for s in e["size"]) + e["sizeUnclassified"] == pytest.approx(1.0)


# ------------------------------------------------------------- history
def test_import_writes_a_history_point(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    up(client, "trades", xlsx("Trades", T_HDR, TRADES))
    h = client.get("/api/history").json()
    assert h["count"] == 1
    p, d = h["points"][0], client.get("/api/dashboard").json()
    assert p["totalPL"] == pytest.approx(d["overallPL"])
    assert p["dailyPL"] is None                      # nothing to compare against yet
    assert sum(p["sectorAllocation"].values()) == pytest.approx(1.0)


def test_history_deltas_and_upsert_by_date():
    hist = {}
    for i, day in enumerate(["2026-07-27", "2026-07-28", "2026-07-29"]):
        hist = calculator.append_history(hist, {
            "date": day, "totalPL": 1000 + i * 250, "portfolioValue": 0, "investedValue": 0,
            "cashPct": 0, "unrealizedPL": 0, "realizedPL": 0, "holdingsCount": 0,
            "sectorAllocation": {"Materials": 1.0}, "riskMetrics": {}})
    assert [p["dailyPL"] for p in hist["points"]] == [None, 250, 250]
    assert hist["changeSinceFirst"] == 500
    # same date again must update, not append
    hist = calculator.append_history(hist, {
        "date": "2026-07-29", "totalPL": 9999, "portfolioValue": 0, "investedValue": 0,
        "cashPct": 0, "unrealizedPL": 0, "realizedPL": 0, "holdingsCount": 0,
        "sectorAllocation": {}, "riskMetrics": {}})
    assert hist["count"] == 3 and hist["points"][-1]["totalPL"] == 9999


# ------------------------------------------------------- error handling
def test_missing_sheet_gives_a_useful_422(client):
    r = up(client, "trades", xlsx("Wrong", T_HDR, TRADES))
    assert r.status_code == 422 and "No 'Trades' sheet" in r.json()["detail"]


def test_missing_required_column_gives_a_useful_422(client):
    r = up(client, "trades", xlsx("Trades", ["Date", "Ticker", "Side"], [[None, "A", "Buy"]]))
    assert r.status_code == 422 and "Ticker, Side, Qty and Price" in r.json()["detail"]


def test_holdings_without_target_column_rejected(client):
    r = up(client, "holdings", xlsx("Portfolio", ["Ticker", "Company", "Weight"], [["A", "Alpha", 5]]))
    assert r.status_code == 422 and "Target column" in r.json()["detail"]


def test_sector_read_as_ticker_is_caught(client):
    r = up(client, "holdings", xlsx("Portfolio", ["Ticker", "Company", "Weight", "Target"],
                                     [["Specialty Chemicals", "Alpha", 5, 600]]))
    assert r.status_code == 422        # every row rejected -> no valid rows


def test_bad_row_reports_its_number_without_aborting_the_batch(client):
    body = up(client, "trades", xlsx("Trades", T_HDR, [
        [dt.date(2026, 5, 1), "AAA", "Buy", -5, 500, 0],
        [dt.date(2026, 5, 2), "AAA", "Buy", 5, 500, 0]])).json()
    assert body["added"] == 1
    assert body["errors"][0]["row"] == 1 and "> 0" in body["errors"][0]["message"]


def test_empty_and_garbage_uploads_rejected(client):
    assert up(client, "trades", b"").status_code == 422
    assert up(client, "holdings", b"not a workbook").status_code == 422


def test_a_failed_import_does_not_corrupt_what_is_stored(client):
    up(client, "holdings", xlsx("Portfolio", H_HDR, HOLDINGS))
    up(client, "trades", xlsx("Trades", T_HDR, TRADES))
    before = json.dumps(drive.read_json(drive.PORTFOLIO_JSON), sort_keys=True)

    assert up(client, "holdings", b"garbage").status_code == 422
    assert up(client, "trades", xlsx("Wrong", T_HDR, TRADES)).status_code == 422

    assert json.dumps(drive.read_json(drive.PORTFOLIO_JSON), sort_keys=True) == before
    assert client.get("/api/dashboard").json()["holdingsCount"] == 2


def test_drive_failure_surfaces_as_503_not_a_crash(client, monkeypatch):
    monkeypatch.setattr(drive.store(), "read_json",
                        lambda name: (_ for _ in ()).throw(drive.DriveError("Drive is down")))
    r = client.get("/api/dashboard")
    assert r.status_code == 503 and "Drive is down" in r.json()["detail"]
