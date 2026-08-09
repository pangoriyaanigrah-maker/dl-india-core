"""fetch_fundamentals.py — FMP fundamentals fetcher for the DL India Core
dashboard.

Financial-statement-derived factor sub-components Yahoo's summary `.info`
payload can't supply: Op margin sustainability, Compounding Score,
Receivables trend (Quality); Sequential acceleration, Forward visibility,
Analyst signal (Biz Momentum). A separate script/workflow from
build_signals.py's nightly Yahoo fetch on purpose -- different provider,
different quota, and these figures only change when a company reports,
not every day the price moves.

    python scripts/fetch_fundamentals.py

Needs FMP_API_KEY in the environment. Writes fundamentals.json on Drive,
which build_signals.py's build() reads best-effort and blends in
alongside the Yahoo-sourced sub-components already there -- a stock
missing here just scores on whatever sub-components it does have, same
as every other partial-data case in this app.

fundamentals.json must exist on Drive already (created once, like the
other JSON files -- see make_starter_files.py) before this can write to
it: the service account can update an existing Drive file but cannot
create a new one.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from build_signals import constituents, is_bank, load_book  # noqa: E402

log = logging.getLogger("dl.fundamentals")

FMP_BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15
# ponytail: fixed conservative pacing, not tuned against a known rate limit
# (this key exposes no rate-limit headers to size against) -- raise once
# real nightly usage confirms there's headroom.
REQUEST_PAUSE = 0.3


def _get(path, **params):
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    params["apikey"] = key
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{FMP_BASE}/{path}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise
        return None
    except Exception:
        return None


def _parse_date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def op_margin_sustainability(annual_inc):
    """5y avg operating margin / current -- <1 means today's margin sits
    above its own 5-year average (a possible peak, mean-reversion risk),
    matching the screenshot's own "<1 = peak warning" framing. Needs a
    real multi-year sample, not 1-2 points passing as a trend."""
    margins = []
    for row in annual_inc:
        rev, op = row.get("revenue"), row.get("operatingIncome")
        if rev and op is not None:
            margins.append(op / rev)
    if len(margins) < 3:
        return None
    current = margins[0]           # FMP returns newest period first
    if not current:
        return None
    return (sum(margins) / len(margins)) / current


def compounding_score(annual_bs, annual_cf):
    """ROA x max(0, 1-(Div+Buyback)/NI) -- return on assets, discounted by
    how much of that profit already left the building as dividends or
    buybacks (the rest is what's actually being reinvested to compound).
    None for a loss-making year: 'compounding' at negative ROI isn't the
    thing this is trying to measure."""
    if not annual_bs or not annual_cf:
        return None
    assets = annual_bs[0].get("totalAssets")
    ni = annual_cf[0].get("netIncome")
    if not assets or not ni or ni <= 0:
        return None
    roa = ni / assets
    div = abs(annual_cf[0].get("commonDividendsPaid") or 0)
    buyback = abs(annual_cf[0].get("commonStockRepurchased") or 0)
    return roa * max(0.0, 1 - (div + buyback) / ni)


def receivables_trend(annual_bs, annual_inc, bank):
    """Change in Days Sales Outstanding, this year vs last -- working
    capital health. DSO rising (customers taking longer to pay) is the
    bad direction; the raw delta is returned as-is and negated at blend
    time in build_signals.py, same pattern as the debt-ratio flips
    elsewhere in this app. N/A for banks -- 'receivables' isn't a
    working-capital signal for a lender the way it is for an operating
    company. Returned as None like every other inapplicable sub-component
    in this app (drops out of blend()'s average) rather than forced to a
    literal neutral score -- consistent with how the rest of the codebase
    already handles 'this metric doesn't apply here'."""
    if bank or len(annual_bs) < 2 or len(annual_inc) < 2:
        return None

    def dso(bs_row, inc_row):
        rec, rev = bs_row.get("netReceivables"), inc_row.get("revenue")
        return (rec / rev * 365) if (rec is not None and rev) else None

    dso_t, dso_prev = dso(annual_bs[0], annual_inc[0]), dso(annual_bs[1], annual_inc[1])
    if dso_t is None or dso_prev is None:
        return None
    return dso_t - dso_prev


def sequential_acceleration(quarterly_inc):
    """Latest quarter's revenue YoY vs the quarter before that's YoY --
    is growth itself accelerating, not just positive. Needs indices
    0,1,4,5 (this Q, last Q, this Q a year ago, last Q a year ago), so a
    6-quarter fetch covers it exactly."""
    if len(quarterly_inc) < 6:
        return None
    rev = [q.get("revenue") for q in quarterly_inc[:6]]
    if any(r is None for r in (rev[0], rev[1], rev[4], rev[5])) or not rev[4] or not rev[5]:
        return None
    yoy_latest = rev[0] / rev[4] - 1
    yoy_prior = rev[1] / rev[5] - 1
    return yoy_latest - yoy_prior


def forward_visibility(annual_bs, annual_cf, annual_inc, bank):
    """Max of capex/PP&E and deferred revenue/revenue -- order-book
    visibility. Neither term means much for a bank (no PP&E-heavy capex
    cycle, no product 'deferred revenue' the way a subscription or
    order-backed business has one), so this mirrors ebit_ev/
    net_debt_ebitda's bank guard rather than returning a number that
    isn't really answering the same question for a lender."""
    if bank or not annual_bs or not annual_cf or not annual_inc:
        return None
    ppe = annual_bs[0].get("propertyPlantEquipmentNet")
    capex = abs(annual_cf[0].get("capitalExpenditure") or 0)
    capex_ppe = (capex / ppe) if ppe else None

    dr = (annual_bs[0].get("deferredRevenue") or 0) + (annual_bs[0].get("deferredRevenueNonCurrent") or 0)
    rev = annual_inc[0].get("revenue")
    dr_rev = (dr / rev) if rev else None

    candidates = [v for v in (capex_ppe, dr_rev) if v is not None]
    return max(candidates) if candidates else None


def analyst_signal(quarterly_inc, estimates):
    """eps_revision_3m: forward consensus EPS vs the company's ACTUAL
    reported EPS from the quarter closest to 90 days ago -- a real
    historical fact, always retrievable right now, not a stored estimate
    snapshot that would need three months to start existing.

    The spec's outlier_z half (most recent individual analyst estimate
    vs the consensus mean) is NOT built: FMP's analyst-estimates endpoint
    exposes aggregate mean/high/low/count only, not each analyst's latest
    individual number, so there is currently no source for 'recent_est'.
    The formula's own design already falls back to eps_revision_3m alone
    when outlier_z is unavailable, which is exactly this case."""
    if not quarterly_inc or not estimates:
        return None
    today = datetime.now(timezone.utc).date()
    target = today - timedelta(days=90)

    dated_eps = [(_parse_date(q.get("date")), q.get("eps")) for q in quarterly_inc]
    dated_eps = [(d, e) for d, e in dated_eps if d is not None and e is not None]
    if not dated_eps:
        return None
    trailing_eps_3m_ago = min(dated_eps, key=lambda de: abs((de[0] - target).days))[1]
    if not trailing_eps_3m_ago:
        return None

    future = [(_parse_date(e.get("date")), e.get("epsAvg")) for e in estimates]
    future = [(d, v) for d, v in future if d is not None and d > today and v is not None]
    if not future:
        return None
    forward_eps = min(future, key=lambda dv: dv[0])[1]

    return (forward_eps / trailing_eps_3m_ago - 1) * 100


def fetch_one(symbol):
    """Every raw (not-yet-scored) figure this needs for one symbol.
    Never raises for a single bad ticker -- _get() already swallows
    per-call failures, so a symbol FMP doesn't cover just comes back with
    every field None, same as a Yahoo miss does elsewhere in this app."""
    time.sleep(REQUEST_PAUSE)
    annual_inc = _get("income-statement", symbol=symbol, period="annual", limit=5) or []
    time.sleep(REQUEST_PAUSE)
    annual_bs = _get("balance-sheet-statement", symbol=symbol, period="annual", limit=2) or []
    time.sleep(REQUEST_PAUSE)
    annual_cf = _get("cash-flow-statement", symbol=symbol, period="annual", limit=1) or []
    time.sleep(REQUEST_PAUSE)
    quarterly_inc = _get("income-statement", symbol=symbol, period="quarter", limit=6) or []
    time.sleep(REQUEST_PAUSE)
    estimates = _get("analyst-estimates", symbol=symbol, period="quarter", limit=4) or []
    time.sleep(REQUEST_PAUSE)
    # FMP's statement endpoints carry no industry/sector field at all (Yahoo's
    # .info does) -- is_bank() needs the profile endpoint specifically, or it
    # silently never matches anything on FMP data.
    profile = _get("profile", symbol=symbol) or []
    bank = is_bank(profile[0]) if profile else False

    return {
        "opMarginSustainability": op_margin_sustainability(annual_inc),
        "compoundingScore": compounding_score(annual_bs, annual_cf),
        "receivablesTrend": receivables_trend(annual_bs, annual_inc, bank),
        "sequentialAcceleration": sequential_acceleration(quarterly_inc),
        "forwardVisibility": forward_visibility(annual_bs, annual_cf, annual_inc, bank),
        "analystSignal": analyst_signal(quarterly_inc, estimates),
    }


def run(symbols):
    """Fetch every symbol, carrying forward the previous run's value for
    anything that comes back empty this time -- same philosophy as
    build_signals.py's bench_cache: these figures barely move between
    nightly runs, so a transient FMP miss shouldn't blank a score that
    was working yesterday."""
    import drive

    drive.connect()
    previous = drive.read_json(drive.FUNDAMENTALS_JSON) or {}

    out, failures = {}, 0
    for i, sym in enumerate(symbols, 1):
        try:
            fresh = fetch_one(sym)
        except Exception as e:
            log.warning("%s: fetch failed (%s), carrying forward previous value", sym, e)
            fresh = {}
        prev_row = previous.get(sym) or {}
        merged = {k: (fresh.get(k) if fresh.get(k) is not None else prev_row.get(k))
                  for k in ("opMarginSustainability", "compoundingScore", "receivablesTrend",
                            "sequentialAcceleration", "forwardVisibility", "analystSignal")}
        if any(v is not None for v in merged.values()):
            out[sym] = merged
        else:
            failures += 1
        if i % 50 == 0:
            log.info("  %d/%d symbols fetched (%d with no data at all so far)", i, len(symbols), failures)

    drive.write_json(drive.FUNDAMENTALS_JSON, out)
    log.info("done: %d/%d symbols have at least one fundamentals sub-component (%d total misses)",
              len(out), len(symbols), failures)
    return out


def selftest():
    # op_margin_sustainability: margin steady at 20% for 5 years -> ratio
    # must land at 1.0 (current == its own 5y average, no peak warning).
    steady = [{"revenue": 100.0, "operatingIncome": 20.0} for _ in range(5)]
    assert abs(op_margin_sustainability(steady) - 1.0) < 1e-9
    # current margin (newest, index 0) spiked to 30% after 4 years at 20%
    # -> avg5 (22%) / current (30%) < 1, the screenshot's own "peak warning".
    spiked = [{"revenue": 100.0, "operatingIncome": 30.0}] + \
             [{"revenue": 100.0, "operatingIncome": 20.0} for _ in range(4)]
    assert op_margin_sustainability(spiked) < 1.0, "elevated current margin must warn, not reward"
    assert op_margin_sustainability(steady[:2]) is None, "2 points isn't a 5y trend"

    # compounding_score: ROA 10%, paid out half of NI as div+buyback ->
    # score = 0.10 * 0.5 = 0.05.
    bs = [{"totalAssets": 1000.0}]
    cf = [{"netIncome": 100.0, "commonDividendsPaid": -30.0, "commonStockRepurchased": -20.0}]
    assert abs(compounding_score(bs, cf) - 0.05) < 1e-9
    assert compounding_score(bs, [{"netIncome": -10.0}]) is None, "a loss year isn't compounding"

    # receivables_trend: DSO went from 30 days to 45 days (rec/rev*365) ->
    # delta must be +15 (worse, slower collection), and None for a bank.
    bs2 = [{"netReceivables": 45 / 365 * 100}, {"netReceivables": 30 / 365 * 100}]
    inc2 = [{"revenue": 100.0}, {"revenue": 100.0}]
    assert abs(receivables_trend(bs2, inc2, bank=False) - 15.0) < 1e-6
    assert receivables_trend(bs2, inc2, bank=True) is None, "N/A for banks"

    # sequential_acceleration: this Q grew 20% YoY, last Q grew 10% YoY ->
    # acceleration = +0.10 (growth speeding up).
    rev6 = [120.0, 110.0, 0.0, 0.0, 100.0, 100.0]   # indices 2,3 unused by the formula
    q = [{"revenue": r} for r in rev6]
    assert abs(sequential_acceleration(q) - 0.10) < 1e-9
    assert sequential_acceleration(q[:5]) is None, "needs all 6 quarters"

    # forward_visibility: capex 40/PP&E 200 = 0.20; deferred rev 10/revenue
    # 100 = 0.10 -> max of the two is 0.20. None for a bank regardless.
    bs3 = [{"propertyPlantEquipmentNet": 200.0, "deferredRevenue": 10.0,
            "deferredRevenueNonCurrent": 0.0}]
    cf3 = [{"capitalExpenditure": -40.0}]
    inc3 = [{"revenue": 100.0}]
    assert abs(forward_visibility(bs3, cf3, inc3, bank=False) - 0.20) < 1e-9
    assert forward_visibility(bs3, cf3, inc3, bank=True) is None, "N/A for banks"

    # analyst_signal: trailing EPS from ~90 days ago was 10, forward
    # consensus is 11 -> +10% revision. Dates are relative to "today" so
    # this stays correct regardless of when the selftest runs.
    today = datetime.now(timezone.utc).date()
    q_hist = [{"date": str(today - timedelta(days=d)), "eps": eps}
              for d, eps in ((10, 12.0), (92, 10.0), (183, 9.0))]
    est = [{"date": str(today + timedelta(days=45)), "epsAvg": 11.0}]
    assert abs(analyst_signal(q_hist, est) - 10.0) < 1e-6
    assert analyst_signal([], est) is None, "no reported history, no signal"
    assert analyst_signal(q_hist, []) is None, "no forward estimate, no signal"

    print("selftest ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    if not os.environ.get("FMP_API_KEY"):
        sys.exit("FMP_API_KEY not set")
    holdings = load_book()
    universe = constituents()
    wanted_symbols = sorted({(h.get("yfSymbol") or f'{h["tk"]}.NS') for h in holdings}
                            | {f"{t}.NS" for t in universe})
    log.info("fetching fundamentals for %d symbols...", len(wanted_symbols))
    run(wanted_symbols)
