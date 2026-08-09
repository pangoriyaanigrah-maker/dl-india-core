"""build_signals.py — market-data fetcher for the DL India Core dashboard.

Returns last price, market cap (Rs crore), 52-week range, cross-sectional
value / momentum / quality z-scores and descriptive risk stats for every
ticker in the book — plus a market-cap-weighted sector and size benchmark
across the whole NSE market, large-cap through micro-cap (see
BENCH_INDICES).

Division of labour: holdings are human-owned and only ever READ here. This
script never edits the book.

    python daily_update.py               # the real entry point (writes the DB)
    python build_signals.py              # fetch only, dump to signals.json
    python build_signals.py --selftest   # check the maths, no network

scripts/daily_update.py imports build() from here and writes the results
into the database. Running this file directly still dumps signals.json,
which is now a debugging artifact rather than something the app reads.

Tickers Yahoo can't resolve land in "errors" and show as NO FEED on the
dashboard. Fix one by setting a YfSymbol for that holding in the holdings
file.
"""
import csv
import io
import json
import math
import random
import re
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

OUT = "signals.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Size cuts in Rs crore. MUST match CUTS in backend/calculator.py, or the
# dashboard's own size buckets and this benchmark disagree. AMFI's official
# semi-annual large/mid/small classification cutoffs -- see calculator.py.
CUTS = {"large": 106300, "mid": 33500}

# Cross-sectional universe: every NSE-listed name from Nifty 100 down through
# Microcap 250, unioned -- the book can hold large, mid, small or micro-cap
# names (and mix them), so a stock's z-score is only meaningful against the
# tier it actually competes in, not one fixed slice of the market. NSE
# defines these four tiers as mutually exclusive by rank; the four lists
# don't overlap (checked at runtime), so the union is the whole investable
# market, each name scored against its own real peer group.
BENCH_INDICES = [
    ("https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv", "Nifty 100"),
    ("https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv", "Nifty Midcap 150"),
    ("https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv", "Nifty Smallcap 250"),
    ("https://nsearchives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv", "Nifty Microcap 250"),
]
INDEX_NAME = " + ".join(name for _, name in BENCH_INDICES)


# ---------------------------------------------------------------- maths
def winsorize(xs, lo=0.05):
    """Clip to the 5th/95th percentile. Indian small caps throw 500x P/Es; one
    of them otherwise eats the whole standard deviation and flattens every
    other name's z-score to roughly zero."""
    s = sorted(xs)
    k = int(lo * len(s))          # symmetric by construction: truncating a
    if k == 0:                    # percentile index independently at each end
        return list(xs)           # clips more from the top than the bottom,
    a, b = s[k], s[-1 - k]        # which walks the mean upward on small samples
    return [min(max(x, a), b) for x in xs]


def zscores(raw, floor=5, clip=True):
    """{key: value|None} -> {key: z|None}, clamped to +/-3.

    Fewer than `floor` real observations means there is no cross-section to
    score against, so everything returns None rather than a fake 0.00.

    clip=False for price returns: winsorizing is there to tame unbounded ratios
    like a 500x P/E. A momentum book's whole point is holding the top of the
    cross-section, so clipping the tail collapses its best names to one
    identical score. The final +/-3 clamp still catches true extremes."""
    keys = [k for k, v in raw.items() if v is not None and math.isfinite(v)]
    out = {k: None for k in raw}
    if len(keys) < floor:
        return out
    xs = [raw[k] for k in keys]
    if clip:
        xs = winsorize(xs)
    mu = sum(xs) / len(xs)
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5
    if sd == 0:
        return out
    for k, x in zip(keys, xs):
        out[k] = round(max(-3.0, min(3.0, (x - mu) / sd)), 2)
    return out


def blend(*maps):
    """Average the z-maps that have a value for a key; None if none do."""
    keys = set().union(*(m.keys() for m in maps))
    out = {}
    for k in keys:
        vals = [m[k] for m in maps if m.get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 2) if vals else None
    return out


YEAR = 252                                 # trading days
MONTH = 21


def risk(closes):
    """Descriptive risk stats for the Risk tab. All from the price series —
    no covariance model, every number reproducible.

    worstM is the worst rolling 21-day window rather than the worst calendar
    month: same question, and it doesn't need the date index carried around.
    """
    if len(closes) < MONTH + 2:
        return {}
    last = closes[-YEAR:] if len(closes) >= YEAR else closes
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    dvol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5

    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)

    worst = min(closes[i] / closes[i - MONTH] - 1
                for i in range(MONTH, len(closes)))
    return {
        "hi": round(max(last), 2),
        "lo": round(min(last), 2),          # 52-week, not the 5-year low
        "r1m": round(closes[-1] / closes[-MONTH] - 1, 4),
        "worstM": round(worst, 4),
        "mdd": round(mdd, 4),
        "dvol": round(dvol, 4),
    }


def adv(volumes, window=MONTH):
    """Average daily traded volume over the last `window` sessions -- a
    30-year average is useless for liquidity, a name's trading activity
    today is what matters. None below a minimum sample rather than a
    number computed on 2 days of data."""
    recent = volumes[-window:]
    if len(recent) < window // 2:
        return None
    return sum(recent) / len(recent)


def bench_return_series(close_df, symbols, mcap):
    """Market-cap-weighted average daily return across the given symbols --
    the beta benchmark. Same universe and the same weighting convention
    (today's full market cap, not free float, not historically reweighted)
    that bench_sect/bench_size already use for composition -- one
    consistent proxy for 'the market' this book lives in, not a second,
    different one. Nifty Smallcap 250 + Microcap 250 has no single tradable
    index/ticker to fetch directly, so this is built from the same ~500
    constituent prices already downloaded.

    Renormalizes by the weight of names that actually have a price on each
    day, so a handful of missing quotes don't quietly drag the composite
    toward zero."""
    import pandas as pd
    cols = [s for s in symbols if s in close_df.columns and mcap.get(s)]
    if not cols:
        return None
    w = pd.Series({s: mcap[s] for s in cols})
    rets = close_df[cols].pct_change()
    numer = rets.mul(w, axis=1).sum(axis=1, skipna=True)
    denom = rets.notna().mul(w, axis=1).sum(axis=1)
    return numer / denom.replace(0, float("nan"))


def bench_index_level(bench_returns):
    """Daily returns -> a dated cumulative level starting at 100 -- what a
    benchmark-relative XIRR needs (the level ON a specific historical
    contribution date), which the daily-return series used for beta can't
    answer by itself. A day with no composite return (bench_returns is NaN)
    carries the level forward flat rather than dropping the date, so a
    contribution date can always be looked up."""
    if bench_returns is None:
        return {}
    level = (1 + bench_returns.fillna(0)).cumprod() * 100
    return {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in level.items()}


def beta(close_df, sym, bench_returns):
    """Slope of the stock's daily returns against the benchmark's, on the
    dates they share. pandas aligns them; a plain list of closes could not."""
    if bench_returns is None or sym not in close_df.columns:
        return None
    try:
        import pandas as pd
        pair = pd.concat([close_df[sym].pct_change(), bench_returns], axis=1).dropna()
        if len(pair) < 60:
            return None
        var = pair.iloc[:, 1].var()
        if not var:
            return None
        return round(pair.iloc[:, 0].cov(pair.iloc[:, 1]) / var, 2)
    except Exception:
        return None


# ---------------------------------------------------------------- io
def load_book():
    """Read the ticker list from portfolio.json on Google Drive.

    Read-only: this script never writes the book. If Drive is unreachable
    it exits rather than guessing a ticker list, so a bad run cannot
    produce a feed covering the wrong stocks.
    """
    import drive

    try:
        drive.connect()
        book = drive.read_json(drive.PORTFOLIO_JSON) or {}
    except drive.DriveError as e:
        sys.exit(f"cannot reach storage ({e}) — see docs/GOOGLE_DRIVE_SETUP.md")
    holdings = book.get("holdings") or []
    if not holdings:
        # An empty book is a real state (nothing imported yet), not an
        # error -- informational, and nothing to fetch prices for.
        sys.exit("no holdings yet — import a holdings file on the dashboard, then re-run.")
    return holdings


def fetch_index_csv(url, name):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
    except Exception as e:
        sys.exit(f"could not fetch the {name} list ({e}) — signals.json left untouched")
    syms = [r["Symbol"].strip() for r in csv.DictReader(io.StringIO(raw)) if r.get("Symbol")]
    if len(syms) < 100:
        sys.exit(f"{name} list looks wrong: {len(syms)} symbols")
    return syms


def _parse_screener_sector(html):
    """Pure parsing step, split out from screener_sector() so the selftest
    can exercise it against a static fixture with no network call. Screener
    has no official API; this reads the "Peer comparison" breadcrumb
    (`<a title="Sector">NAME</a>`) straight out of the page HTML."""
    m = re.search(r'title="Sector">([^<]+)</a>', html)
    return m.group(1).strip() if m else None


def screener_sector(symbol):
    """Screener.in fallback, used ONLY when Yahoo has no sector at all for a
    stock -- rare (1 name out of ~500 in a full run), usually a just-renamed
    or recently-restructured listing Yahoo's database hasn't caught up with.

    Unlike fetch_index_csv() above, this is best-effort, not critical: no
    official Screener API exists, and _parse_screener_sector() can break
    silently if Screener redesigns that section. Returns None on ANYTHING
    going wrong -- timeout, 404, missing markup. A missing sector already
    renders as "Unclassified" today; this only ever narrows that gap,
    never widens it, and never blocks the run.
    """
    try:
        req = urllib.request.Request(
            f"https://www.screener.in/company/{symbol}/consolidated/",
            headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
        return _parse_screener_sector(html)
    except Exception:
        return None


def constituents():
    """Union of BENCH_INDICES' constituents, straight from NSE.

    No embedded fallback list on purpose: if NSE is unreachable the run aborts
    and signals.json keeps yesterday's numbers, which is honest. A stale hard-
    coded proxy would silently score the book against the wrong cross-section
    and nothing on the dashboard would say so.
    """
    out = []
    for url, name in BENCH_INDICES:
        out += fetch_index_csv(url, name)
    dupes = len(out) - len(set(out))
    if dupes:
        sys.exit(f"BENCH_INDICES overlap by {dupes} symbols — they must be disjoint "
                  f"or a stock gets double weight in the benchmark")
    return out


INFO_WORKERS = 20   # Was briefly raised to 40 on a 100-symbol benchmark that
# showed 3x speedup and zero failures -- but at the real 762-symbol scale
# Yahoo throttles, and _info() swallows that silently: names came back with
# no marketCap/PE at all, which surfaces much later as "Unclassified" size
# buckets and blank value z-scores for stocks whose data is perfectly
# available. Correctness over ~30s on a background job.

# Yahoo rate-limits .info by VOLUME, and hard: a real run measured 333 of
# 762 symbols returning nothing, with a gentle 3s retry recovering 0 of
# them -- once the limiter trips it stays tripped, so there is no pause
# short enough to be worth waiting and long enough to help. What actually
# works is not asking for 762 in a burst. The book's own holdings are the
# only names whose market cap and value score are displayed per-stock, so
# they are fetched FIRST and slowly, before the universe is allowed to
# spend the budget. Everything after is best-effort: a partially-covered
# benchmark still produces sensible sector/size weights, a holding with no
# market cap shows up as "Unclassified" on screen.
INFO_PRIORITY_WORKERS = 4     # gentle enough to survive a cold limiter
INFO_RETRY_WORKERS = 2
INFO_RETRY_PAUSE = 20.0       # only ever paid for the handful of holdings


def fetch(symbols, timeout=None, skip_info=False, period="5y", priority=()):
    """-> ({symbol: [close, ...]}, {symbol: [volume, ...]}, {symbol: info},
    close_dataframe).
    One bulk price call; the fundamentals endpoint is per-name and flaky, so
    each is isolated -- and independent, so a thread pool fetches them
    concurrently instead of one at a time (~500 sequential .info calls was
    the entire runtime of the nightly job). 5 years by default, because
    drawdown and worst-month need it — momentum anchors on an explicit
    offset rather than the start of the series. quick_prices() only needs
    52-week hi/lo, so it overrides this to 1y -- 5 years of daily OHLCV
    for even 10 symbols is real payload weight and was the actual reason
    the "fast" path measured ~10s, not the .info loop this also skips.

    Volume rides along in the same download (yfinance always returns the
    full OHLCV set) -- it used to be fetched and thrown away.

    timeout (seconds): None here means yfinance's own default -- fine for
    the nightly job, which is already expected to take minutes. quick_prices()
    passes an explicit short timeout since IT runs inline during a live
    request and must not be able to hang it indefinitely.

    skip_info: the per-ticker .info loop is the ACTUAL slow part (measured:
    ~10s for just 10 symbols, unbounded per-call, dwarfing the bulk price
    download it's paired with) -- quick_prices() only ever needed it for
    mcap, and mcap staying a few minutes stale until the next full build()
    is the same accepted tradeoff as val/momo/qual/beta. Skipping it here
    is what actually keeps the fast path fast, not the timeout above."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    px, vol = {}, {}
    data = yf.download(symbols, period=period, auto_adjust=True,
                       progress=False, group_by="column", timeout=timeout)
    close, volume = data["Close"], data["Volume"]
    for s in symbols:
        try:
            series = close[s] if hasattr(close, "columns") else close
            vals = [float(v) for v in series.dropna().tolist()]
        except (KeyError, TypeError, ValueError):
            continue
        if vals:
            px[s] = vals
        try:
            vseries = volume[s] if hasattr(volume, "columns") else volume
            vol[s] = [float(v) for v in vseries.dropna().tolist()]
        except (KeyError, TypeError, ValueError):
            vol[s] = []

    if skip_info:
        return px, vol, {}, close

    def _info(s):
        try:
            return s, (yf.Ticker(s).info or {})
        except Exception:                      # yfinance raises freely here
            return s, {}

    # The book's own holdings go first, on a small pool, while Yahoo's
    # rate limiter is still cold -- these are the only names whose market
    # cap and value z-score are shown per-stock, so they must not be
    # competing with 750 benchmark constituents for the same budget.
    info = {}
    prio = [s for s in priority if s in px]
    if prio:
        with ThreadPoolExecutor(max_workers=INFO_PRIORITY_WORKERS) as ex:
            info.update(dict(ex.map(_info, prio)))
        # A holding that still came back empty is worth a real wait; there
        # are only a handful, and "Unclassified" on screen is the cost of
        # giving up here.
        empty = [s for s in prio if not info.get(s)]
        if empty:
            print(f"  {len(empty)}/{len(prio)} holdings returned no fundamentals, waiting {INFO_RETRY_PAUSE:.0f}s...")
            time.sleep(INFO_RETRY_PAUSE)
            with ThreadPoolExecutor(max_workers=INFO_RETRY_WORKERS) as ex:
                info.update({s: v for s, v in ex.map(_info, empty) if v})
            still = [s for s in empty if not info.get(s)]
            print(f"  recovered {len(empty)-len(still)}/{len(empty)} holdings"
                  + (f"; still empty: {', '.join(still)}" if still else ""))

    rest = [s for s in px if s not in info]
    # SHUFFLED, deliberately. Yahoo throttles this hard from a datacenter
    # IP (measured: 752/752 resolve in 38s from a residential connection,
    # roughly half that from a GitHub Actions runner) and it trips at
    # about the same point every run -- so a fixed order meant the same
    # tail failed every single time, and build()'s carry-forward cache had
    # nothing new to accumulate: coverage sat at 401 then 402 across two
    # runs. Randomising which names lose the race lets the union grow run
    # over run instead.
    random.shuffle(rest)
    with ThreadPoolExecutor(max_workers=INFO_WORKERS) as ex:
        info.update(dict(ex.map(_info, rest)))
    blank = sum(1 for s in rest if not info.get(s))
    if blank:
        # Best-effort by design: the benchmark is a market-cap-weighted
        # aggregate, so partial coverage still gives sensible sector/size
        # weights. Say how partial, rather than let it look complete.
        print(f"  benchmark fundamentals: {len(rest)-blank}/{len(rest)} resolved "
              f"({blank} rate-limited by Yahoo, filled from cache where possible)")
    return px, vol, info, close


def num(d, *keys):
    """First finite number among `keys`, else None."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and math.isfinite(v) and v != 0:
            return float(v)
    return None


def market_cap(info_d, last_price=None):
    """Market cap in rupees, derived if not served directly.

    Under load Yahoo returns a PARTIAL .info -- observed on real runs with
    trailingPE and priceToBook present but marketCap simply absent, which
    is why TCS and RELIANCE sat in "Unclassified" while their value
    z-scores computed fine. Shares outstanding comes back in the same
    payload, so multiply it by the last close rather than drop a stock out
    of the size buckets over one missing field."""
    mc = num(info_d, "marketCap")
    if mc:
        return mc
    shares = num(info_d, "sharesOutstanding", "impliedSharesOutstanding")
    return shares * last_price if (shares and last_price) else None


def backfill_mcap(mcap, scored, cached):
    """Fill in symbols Yahoo dropped this run from last run's bench_cache,
    in place. Returns the set of symbols that got a cached value, so callers
    can skip re-deriving anything else (eg. sector) for them.

    A market cap barely moves day to day, and this has to run before mcap
    feeds bench_returns/signals -- otherwise a HOLDING's own mcap (not just
    the benchmark rollup) blanks out on the dashboard every time Yahoo
    throttles it for one run, even a priority-fetched one."""
    backfilled = set()
    for s in scored:
        if not mcap.get(s):
            was_mcap = (cached.get(s) or (0, None))[0]
            if was_mcap:
                mcap[s] = was_mcap
                backfilled.add(s)
    return backfilled


def positive_num(d, *keys):
    """Like num(), but for a ratio that is only meaningful when positive --
    P/E, P/B, EV/EBITDA. A loss-making company prices to a negative P/E,
    which isn't 'cheaper than a 500x P/E', it's a different thing the ratio
    can't express at all. Left in, it z-scores as an extreme outlier and
    the value factor reads a loss-maker as the deepest bargain in the book.
    Drop it from that cross-section instead -- unlike ROE or margins (see
    qual below), where a negative number is a real, meaningful signal and
    must stay in."""
    v = num(d, *keys)
    return v if (v is not None and v > 0) else None


def percentile_rank(raw, floor=5):
    """{key: value|None} -> {key: percentile 0-100|None}. A cross-sectional
    rank, not a z-score -- used where a %-scale number (an EPS revision)
    needs blending with a z-score (an outlier score) that isn't on the same
    footing otherwise. Same floor as zscores(): fewer than `floor` real
    observations means there is no real cross-section, so everything is
    None rather than a fake midpoint."""
    keys = [k for k, v in raw.items() if v is not None and math.isfinite(v)]
    out = {k: None for k in raw}
    if len(keys) < floor:
        return out
    ordered = sorted(keys, key=lambda k: raw[k])
    n = len(ordered)
    for i, k in enumerate(ordered):
        out[k] = round(i / (n - 1) * 100, 1) if n > 1 else 50.0
    return out


def is_bank(info_d):
    """A handful of factor inputs (EBIT/EV, Net Debt/EBITDA) don't mean
    anything for a lender -- a bank's 'debt' is its business, not leverage
    in the industrial sense. Detected from Yahoo's own industry/sector
    text rather than a maintained ticker list, so it covers new listings
    for free."""
    text = f"{info_d.get('industry') or ''} {info_d.get('sector') or ''}".lower()
    return "bank" in text


def ebit_ev(info_d):
    """Operating yield: how much operating profit you get per rupee of
    enterprise value. EBIT itself isn't a field Yahoo serves directly, so
    it's approximated as operating margin x revenue -- both are. For a
    bank this ratio doesn't mean anything (a bank's core business shows up
    as interest margin, not an EV multiple), so it falls back to 1/PE,
    the closest available operating-yield analogue."""
    if is_bank(info_d):
        pe = positive_num(info_d, "trailingPE")
        return (1 / pe) if pe else None
    om = num(info_d, "operatingMargins")
    rev = num(info_d, "totalRevenue")
    ev = num(info_d, "enterpriseValue")
    if om is None or rev is None or not ev:
        return None
    return (om * rev) / ev


def y_g_t_premium(info_d, t_rate=0.04):
    """Buffett's owner-earnings framing: free-cash-flow yield plus the
    growth you're not paying for, minus a risk-free rate to price against.
    T_RATE is a fixed 4% assumption, not fetched -- there's no per-stock
    risk-free rate, only a market-wide one, so it's a constant like the
    zscores() +/-3 clamp is. FCF yield and sustainable growth are each
    only meaningful together (a growth rate with no FCF context, or vice
    versa, isn't the same claim) so this is None unless all four inputs
    resolve -- never a partial sum standing in for the whole thing."""
    fcf = num(info_d, "freeCashflow")
    mcap = num(info_d, "marketCap")
    roe = num(info_d, "returnOnEquity")
    payout = num(info_d, "payoutRatio")
    if fcf is None or not mcap or roe is None or payout is None:
        return None
    return (fcf / mcap) + (roe * (1 - payout)) - t_rate


def tgt_price_ratio(info_d, cmp):
    """Analyst-implied return: mean target price vs today's price. NSE
    analyst-target coverage is known-sparse (fewer sell-side desks cover
    small/micro-caps than in US markets), so this is expected to be None
    for a large share of the universe -- zscores()'s floor=5 already
    handles that by scoring nobody rather than a thin, misleading sample."""
    tgt = num(info_d, "targetMeanPrice")
    if tgt is None or not cmp:
        return None
    return tgt / cmp - 1


def net_debt_ebitda(info_d):
    """Leverage: (debt - cash) / EBITDA, lower is safer. For a bank this
    is meaningless the same way EBIT/EV is -- deposits look like 'debt'
    but are the business, not leverage risk -- so it falls back to
    assets/equity (inverted so lower is still better), Yahoo's closest
    analogue to a bank capital-adequacy ratio."""
    if is_bank(info_d):
        equity = num(info_d, "totalStockholderEquity") or num(info_d, "bookValue")
        assets = num(info_d, "totalAssets")
        if not equity or not assets:
            return None
        return assets / equity
    debt = num(info_d, "totalDebt")
    cash = num(info_d, "totalCash")
    ebitda_v = num(info_d, "ebitda")
    if debt is None or cash is None or not ebitda_v:
        return None
    return (debt - cash) / ebitda_v


def cash_conversion(info_d):
    """CFO / EBITDA -- how much of reported profit shows up as actual
    cash. A company that's profitable on paper but not converting it to
    cash (aggressive revenue recognition, growing receivables) shows up
    here before it shows up in the P&L."""
    cfo = num(info_d, "operatingCashflow")
    ebitda_v = num(info_d, "ebitda")
    if cfo is None or not ebitda_v:
        return None
    return cfo / ebitda_v


def six_month_return(closes):
    """Raw 6-month price return -- the magnitude half of the Momentum
    factor, scored on its own rather than pre-blended into one number
    the way the old momentum() combo score was, so a name that's merely
    OK on magnitude but excellent on trend quality (six_month_sharpe)
    still shows up correctly on the parts that are strong."""
    if len(closes) < 127:
        return None
    return closes[-1] / closes[-126] - 1


def six_month_sharpe(closes):
    """Risk-adjusted trend quality: mean daily return / stdev of daily
    return over the last ~6 months. Deliberately NOT annualized -- this
    number only ever feeds a cross-sectional z-score, and annualizing is
    a constant multiplier applied to every stock alike, which can't
    change anyone's rank. Skipping it is one less place to get a
    trading-days-per-year constant wrong for no effect on the output."""
    if len(closes) < 127:
        return None
    window = closes[-126:]
    rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
    mean = sum(rets) / len(rets)
    sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
    return (mean / sd) if sd else None


def sector_momentum_map(scored, six_m, sectors):
    """Median 6-month return within each stock's own sector -- 'is the
    sub-sector at large moving,' a tailwind/headwind check independent of
    the stock's own return. Every stock in a sector gets that sector's
    median as its raw input, then it's cross-sectionally z-scored like
    any other factor input. A sector needs >=3 priced members for its
    median to mean anything; below that, a 1-stock 'sector' would just be
    that stock's own six_month_return relabeled, silently double-counting
    the magnitude factor instead of adding new information."""
    by_sector = defaultdict(list)
    for s in scored:
        sec, v = sectors.get(s), six_m.get(s)
        if sec and v is not None:
            by_sector[sec].append(v)
    medians = {sec: statistics.median(vs) for sec, vs in by_sector.items() if len(vs) >= 3}
    return {s: medians.get(sectors.get(s)) for s in scored}


def stress_resilience_24m(closes, window=504):
    """How far the current price sits below its own high of the last ~24
    months (window=504 trading days). Closer to 0 = resilient: either it
    never had a serious drawdown, or it did and has since recovered --
    both read the same way here, which is the point (recovery IS what
    resilience means, not merely 'avoided a crash'). Deeply negative =
    still sitting in whatever hole it dug, unrecovered."""
    recent = closes[-window:] if len(closes) >= window else closes
    if len(recent) < 40:
        return None
    peak = recent[0]
    for c in recent:
        peak = max(peak, c)
    return recent[-1] / peak - 1


def quick_prices(holdings):
    """Fast, book-only price refresh: just cmp/lo/hi for the current
    holdings, no benchmark universe, no per-ticker .info call. Meant to
    run inline right after an import -- fetching 750+ symbols (2+
    minutes) is too slow for a request to wait on, and even the .info
    lookup alone measured ~10s for just 10 symbols (skip_info=True below
    is what actually makes this fast, not the timeout).

    mcap/val/momo/qual/beta need either the .info call or the full
    cross-sectional universe (a z-score against 15 of your own names
    isn't a real market comparison) and are deliberately NOT touched here
    -- they stay whatever the last full build() computed until the next
    one runs. ponytail: cmp moving without those also moving is a real,
    accepted staleness window (a P/E-based value read is technically a
    few minutes behind the price it's using) -- not worth a partial
    factor recompute for a personal book checked a few times a day, not
    by a millisecond.

    -> ({ticker: {"cmp", "lo", "hi"}}, [error strings])
    Only tickers that actually resolved on Yahoo are included."""
    wanted = {h["tk"]: [h["yfSymbol"]] if h.get("yfSymbol")
                       else [f'{h["tk"]}.NS', f'{h["tk"]}.BO']
              for h in holdings}
    symbols = sorted({c for cs in wanted.values() for c in cs})
    if not symbols:
        return {}, []
    px, vol, info, close_df = fetch(symbols, timeout=8, skip_info=True, period="1y")

    resolved, errors = {}, []
    for tk, cands in wanted.items():
        hit = next((c for c in cands if c in px), None)
        if hit:
            resolved[tk] = hit
        else:
            errors.append(f"{tk}: no Yahoo data for {' or '.join(cands)}")

    out = {}
    for tk, s in resolved.items():
        r = risk(px[s])
        out[tk] = {"cmp": round(px[s][-1], 2),
                   "lo": r.get("lo", round(min(px[s]), 2)), "hi": r.get("hi")}
    return out, errors


def build(previous=None):
    holdings = load_book()
    # Yahoo symbols to try per holding. Most NSE names are TK.NS; SME and
    # recently listed names are often only on BSE, hence the .BO fallback.
    wanted = {h["tk"]: [h["yfSymbol"]] if h.get("yfSymbol")
                       else [f'{h["tk"]}.NS', f'{h["tk"]}.BO']
              for h in holdings}

    universe = [f"{t}.NS" for t in constituents()]
    book_symbols = {c for cs in wanted.values() for c in cs}
    symbols = sorted(set(universe) | book_symbols)
    print(f"fetching {len(symbols)} symbols ({len(wanted)} holdings + "
          f"{len(universe)} {INDEX_NAME})...")
    px, vol, info, close_df = fetch(symbols, priority=sorted(book_symbols))

    # Resolve each holding to the first candidate that actually returned data.
    resolved, errors = {}, []
    for tk, cands in wanted.items():
        hit = next((c for c in cands if c in px), None)
        if hit:
            resolved[tk] = hit
        else:
            errors.append(f"{tk}: no Yahoo data for {' or '.join(cands)} — "
                          f"set a Yahoo symbol for this holding on the dashboard's Import tab")

    # Score across the union of universe and holdings, so a portfolio name is
    # part of the cross-section rather than measured against a book it's absent from.
    scored = sorted(set(px) & (set(universe) | set(resolved.values())))
    mcap = {s: (market_cap(info[s], px[s][-1]) or 0) / 1e7 for s in scored}   # -> Rs crore

    cached = (previous or {}).get("bench_cache") or {}
    bench_cache_backfilled = backfill_mcap(mcap, scored, cached)

    # Value/Quality/Momentum sub-components below all follow the same
    # equal-weighted blend() pattern regardless of factor: z-score (or
    # percentile-rank) each sub-component across the cross-section, then
    # average whichever ones a stock actually has data for. Nothing here
    # is winsorized-off/on inconsistently by accident -- it's the default
    # (clip=True) everywhere except six-month figures (clip=False, same
    # reasoning as the old momentum() score: clipping the tail flattens
    # exactly the top-momentum names the factor exists to highlight).
    sectors = {s: (info.get(s) or {}).get("sector") for s in scored}
    six_m = {s: six_month_return(px[s]) for s in scored}
    momo = blend(
        zscores(six_m, clip=False),
        zscores({s: six_month_sharpe(px[s]) for s in scored}, clip=False),
        zscores(sector_momentum_map(scored, six_m, sectors), clip=False),
    )
    val = blend(
        zscores({s: ebit_ev(info[s]) for s in scored}),
        zscores({s: y_g_t_premium(info[s]) for s in scored}),
        zscores({s: tgt_price_ratio(info[s], px[s][-1]) for s in scored}),
    )
    qual = blend(
        zscores({s: num(info[s], "grossMargins") for s in scored}),
        {k: (-v if v is not None else None)
         for k, v in zscores({s: net_debt_ebitda(info[s]) for s in scored}).items()},
        zscores({s: cash_conversion(info[s]) for s in scored}),
        zscores({s: stress_resilience_24m(px[s]) for s in scored}),
        # Op margin sustainability, Compounding Score, and Receivables
        # trend all need multi-year/multi-quarter financial history this
        # script doesn't fetch yet (blocked on an FMP integration, not
        # built here) -- blend() already averages only what exists, so
        # Quality quietly improves in place once those land, no reshape.
    )
    biz_momo = blend(
        zscores({s: num(info[s], "revenueGrowth") for s in scored}),
        zscores({s: num(info[s], "earningsGrowth") for s in scored}),
        # Analyst signal, Forward visibility, and Sequential acceleration
        # are the same story as Quality above -- FMP-blocked, not missing
        # by oversight.
    )

    bench_returns = bench_return_series(close_df, universe, mcap)

    signals = {}
    for tk, s in resolved.items():
        r = risk(px[s])
        signals[tk] = {
            "cmp": round(px[s][-1], 2),
            "mcap": round(mcap[s]) or None,
            # hi/lo are 52-week even though the series is 5 years; a 5-year low
            # would stretch the price rail until the current price pinned right.
            "lo": r.get("lo", round(min(px[s]), 2)),
            "hi": r.get("hi"),
            "val": val.get(s),
            "momo": momo.get(s),
            "qual": qual.get(s),
            "bizMomo": biz_momo.get(s),
            "r1m": r.get("r1m"),
            "worstM": r.get("worstM"),
            "mdd": r.get("mdd"),
            "dvol": r.get("dvol"),
            "beta": beta(close_df, s, bench_returns),
            "adv": adv(vol.get(s, [])),
        }

    # Benchmark: market-cap weighted over the universe names that priced.
    #
    # Yahoo rate-limits hard enough that a single run typically resolves
    # only about half the 740 constituents -- but it throttles a DIFFERENT
    # random half each time, and a constituent's market cap and sector
    # barely move day to day. mcap itself was already backfilled from cache
    # above; this just does the same for sector, and skips both counting
    # and the never-resolved-at-all symbols.
    bench_cache, from_cache = {}, 0
    for s in universe:
        m = mcap.get(s, 0)
        if not m:
            continue                           # never resolved, nothing to carry
        if s in bench_cache_backfilled:
            from_cache += 1
        sector = (info.get(s) or {}).get("sector") or (cached.get(s) or (0, None))[1]
        if not sector and s not in bench_cache_backfilled:
            sector = screener_sector(s.split(".")[0])
            if sector:
                print(f"  {s}: no sector from Yahoo, found on Screener -> {sector}")
        bench_cache[s] = [round(m), sector]

    bench_sect, bench_size = defaultdict(float), defaultdict(float)
    for s, (m, sector) in bench_cache.items():
        bench_sect[sector or "Unclassified"] += m
        bench_size["Large" if m >= CUTS["large"] else
                   "Mid" if m >= CUTS["mid"] else "Small"] += m
    print(f"  benchmark coverage: {len(bench_cache)}/{len(universe)} constituents"
          + (f" ({from_cache} carried over from the previous run)" if from_cache else ""))
    tot = sum(bench_sect.values())
    share = lambda d: {k: round(v / tot, 4) for k, v in
                       sorted(d.items(), key=lambda kv: -kv[1])} if tot else {}

    return {
        "asof": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "index": INDEX_NAME,
        "note": f"z-scores are cross-sectional vs the official {INDEX_NAME} constituents "
                "(NSE list, fetched nightly); the sector/size benchmark is market-cap "
                "weighted over the same names — full mcap, not free-float. "
                "value = EBIT/EV, FCF yield + sustainable growth - 4% (Buffett premium), "
                "analyst target/price; quality = gross margin, net debt/EBITDA, cash "
                "conversion, 24-month stress resilience; momentum = 6M return, 6M Sharpe, "
                "sector momentum; biz momentum = revenue growth YoY, EPS growth YoY. "
                "Some sub-components (Quality's op-margin sustainability/compounding "
                "score/receivables trend, Biz Momentum's analyst signal/forward "
                "visibility/sequential acceleration) need financial-statement history "
                "not fetched yet and are omitted from the blend until that lands.",
        "errors": errors,
        "signals": signals,
        "bench_sect": share(bench_sect),
        "bench_size": share(bench_size),
        "bench_index_level": bench_index_level(bench_returns),
        # Last-known mcap/sector per constituent, so the next run can fill
        # whatever Yahoo rate-limits away. Not read by the dashboard.
        "bench_cache": bench_cache,
    }


def selftest():
    # Real fragment captured from screener.in/company/JSWDULUX/consolidated/,
    # not invented -- if Screener ever restyles this section, this is the
    # first thing that should fail, before a nightly run silently gets None.
    real_fragment = '''
        <a href="/market/IN02/" target="_blank" title="Broad Sector">Consumer Discretionary</a>
        <a href="/market/IN02/IN0202/" target="_blank" title="Sector">Consumer Durables</a>
        <a href="/market/IN02/IN0202/IN020201/" target="_blank" title="Broad Industry">Consumer Durables</a>
    '''
    assert _parse_screener_sector(real_fragment) == "Consumer Durables"
    assert _parse_screener_sector("<html>no breadcrumb here</html>") is None, \
        "a redesigned page must return None, not raise"

    w = winsorize(list(range(2, 22)) + [10 ** 6])          # n=21 -> clip 1 each end
    assert max(w) == 21 and min(w) == 3, w                 # 10^6 -> 21, 2 -> 3
    assert winsorize([1, 2, 3, 4, 500]) == [1, 2, 3, 4, 500], "n=5 is below the 5% cut"
    assert winsorize([1, 2]) == [1, 2], "too few points must pass through"

    z = zscores({c: v for c, v in zip("abcdefg", [1, 2, 3, 4, 5, 6, 7])})
    assert z["d"] == 0.0 and z["a"] < 0 < z["g"], z
    assert abs(z["a"]) == abs(z["g"]), "symmetric input must give symmetric z"

    z = zscores({"a": 1, "b": 2, "c": None})
    assert all(v is None for v in z.values()), "thin cross-section must not fake a score"
    assert zscores(dict.fromkeys("abcdef", 5))["a"] is None, "zero variance must be None"

    z = zscores({c: v for c, v in zip("abcdef", [1, 2, 3, 4, 5, None])})
    assert z["f"] is None and z["a"] is not None, "None must survive as None"

    tail = {c: v for c, v in zip("abcdefghijklmnopqrstu", list(range(20)) + [900])}
    assert zscores(tail, clip=False)["u"] > zscores(tail)["u"], "clip=False must keep the tail"

    assert blend({"a": 1.0}, {"a": 2.0}, {"a": None}) == {"a": 1.5}
    assert blend({"a": None}, {"a": None}) == {"a": None}

    assert six_month_return([1.0] * 100) is None, "short history must not score"
    flat400 = [10.0] * 400
    assert abs(six_month_return(flat400)) < 1e-9, "flat series has no 6M return"
    rising400 = [float(i) for i in range(1, 401)]
    assert six_month_return(rising400) > 0, "monotonic rise must be positive"

    assert six_month_sharpe([1.0] * 100) is None, "short history must not score"
    assert six_month_sharpe(flat400) is None, "zero volatility must not divide by zero"
    assert six_month_sharpe(rising400) > 0, "steady rise must have positive Sharpe"

    assert stress_resilience_24m([1.0] * 10) is None, "short history must not score"
    assert stress_resilience_24m([100.0] * 600) == 0.0, "never off its own high = fully resilient"
    crashed_unrecovered = [100.0] * 300 + [50.0] * 300
    assert stress_resilience_24m(crashed_unrecovered) == -0.5, \
        "still 50% below the 24m high = unrecovered"
    crashed_recovered = [100.0] * 200 + [50.0] * 100 + [100.0] * 200
    assert abs(stress_resilience_24m(crashed_recovered)) < 1e-9, \
        "back to the 24m high = fully recovered, same as never having crashed"

    assert percentile_rank({"a": 1, "b": 2, "c": 3, "d": 4}) == \
        {"a": None, "b": None, "c": None, "d": None}, "below floor=5 must score nobody"
    five = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
    pr = percentile_rank(five)
    assert pr["a"] == 0.0 and pr["e"] == 100.0 and pr["c"] == 50.0, pr
    assert percentile_rank({"a": 1, "b": None, "c": 2, "d": 3, "e": 4, "f": 5})["b"] is None, \
        "a missing input must stay None, not get ranked"

    assert is_bank({"industry": "Banks - Regional"}) is True
    assert is_bank({"industry": "IT Services", "sector": "Technology"}) is False

    assert risk([1.0] * 10) == {}, "too little history yields no risk stats"
    r = risk([100.0] * 300)
    assert r["mdd"] == 0 and r["dvol"] == 0 and r["worstM"] == 0, r
    down = [100.0] * 50 + [50.0] * 50                    # a clean 50% drawdown
    assert abs(risk(down)["mdd"] + 0.5) < 1e-9, risk(down)["mdd"]
    assert risk(down)["worstM"] < -0.4, "the drop must show as the worst window"
    hl = risk([float(i) for i in range(1, 401)])
    assert hl["hi"] == 400 and hl["lo"] == 149, (hl["hi"], hl["lo"])  # last 252 only

    assert num({"a": 0, "b": 3}, "a", "b") == 3.0, "zero must fall through"
    assert num({"a": float("nan")}, "a") is None

    assert positive_num({"trailingPE": -8.0}, "trailingPE") is None, \
        "a loss-maker's negative P/E must not enter the value cross-section"
    assert positive_num({"trailingPE": 15.0}, "trailingPE") == 15.0
    assert positive_num({"trailingPE": 0.0}, "trailingPE") is None

    # marketCap served directly wins; a partial payload (Yahoo does this
    # under load) falls back to shares x price rather than "Unclassified".
    assert market_cap({"marketCap": 8.7e12}, 2413.0) == 8.7e12
    assert market_cap({"sharesOutstanding": 3618087518}, 2413.0) == 3618087518 * 2413.0
    assert market_cap({"trailingPE": 16.7}, 2413.0) is None, "no shares, no derivation"
    assert market_cap({"sharesOutstanding": 3618087518}, None) is None, "no price, no derivation"

    # A holding's own mcap (TCS, RELIANCE ...) must not blank out on the
    # dashboard just because Yahoo dropped it for one run -- last known
    # value carries forward, same as the benchmark rollup already did.
    mcap = {"TCS.NS": 0, "RELIANCE.NS": 1_800_000, "NEWCO.NS": 0}
    cached = {"TCS.NS": [1_500_000, "IT"], "OLDCO.NS": [500_000, "Energy"]}
    backfilled = backfill_mcap(mcap, list(mcap), cached)
    assert mcap["TCS.NS"] == 1_500_000, "cached value must fill a fresh miss"
    assert mcap["RELIANCE.NS"] == 1_800_000, "fresh value must win over cache"
    assert mcap["NEWCO.NS"] == 0, "no cache entry means it stays unresolved, not invented"
    assert backfilled == {"TCS.NS"}, "only the symbol actually filled from cache is reported"

    # Value/Quality sub-component formulas, real-shaped inputs.
    tcs_like = {"industry": "IT Services", "sector": "Technology",
                "operatingMargins": 0.24, "totalRevenue": 2.76e12, "enterpriseValue": 8.55e12,
                "trailingPE": 17.8, "freeCashflow": 3.97e11, "marketCap": 8.87e12,
                "returnOnEquity": 0.477, "payoutRatio": 0.385, "targetMeanPrice": 2484.67,
                "grossMargins": 0.404, "totalDebt": 1.13e11, "totalCash": 4.5e11,
                "ebitda": 7.21e11, "operatingCashflow": 5.23e11}
    assert ebit_ev(tcs_like) == (0.24 * 2.76e12) / 8.55e12
    assert ebit_ev({"industry": "Banks - Regional", "trailingPE": 20.0}) == 1 / 20.0, \
        "a bank must fall back to 1/PE, not a meaningless EV multiple"
    assert ebit_ev({"industry": "IT Services"}) is None, "missing inputs, no derivation"

    ygt = y_g_t_premium(tcs_like)
    assert ygt == (3.97e11 / 8.87e12) + (0.477 * (1 - 0.385)) - 0.04
    assert y_g_t_premium({"freeCashflow": 100.0}) is None, \
        "a partial sum must not stand in for the whole premium"

    assert tgt_price_ratio(tcs_like, 2400.0) == 2484.67 / 2400.0 - 1
    assert tgt_price_ratio({}, 2400.0) is None, "no analyst coverage, no ratio"

    assert net_debt_ebitda(tcs_like) == (1.13e11 - 4.5e11) / 7.21e11
    assert net_debt_ebitda({"industry": "Banks - Regional",
                             "totalStockholderEquity": 1e11, "totalAssets": 12e11}) == 12.0, \
        "a bank must fall back to assets/equity, not a meaningless debt ratio"

    assert cash_conversion(tcs_like) == 5.23e11 / 7.21e11
    assert cash_conversion({"ebitda": 100.0}) is None, "no CFO, no ratio"

    six_m = {"A.NS": 0.10, "B.NS": 0.20, "C.NS": -0.05, "D.NS": 0.15}
    sectors = {"A.NS": "IT", "B.NS": "IT", "C.NS": "IT", "D.NS": "Energy"}
    sec_mo = sector_momentum_map(list(six_m), six_m, sectors)
    assert sec_mo["A.NS"] == sec_mo["B.NS"] == sec_mo["C.NS"] == statistics.median([0.10, 0.20, -0.05]), \
        "every IT name gets the IT sector's median"
    assert sec_mo["D.NS"] is None, "Energy has only 1 member, below the 3-name floor"

    assert adv([]) is None, "no data must not fake a zero"
    assert adv([100.0] * 5) is None, "below minimum sample must be None"
    assert adv([1000.0] * 21) == 1000.0
    assert adv([1.0] * 200 + [3000.0] * 21) == 3000.0, "only the recent window counts"

    import pandas as pd
    import random
    random.seed(0)
    n = 80
    bench_px = [100.0]
    for _ in range(n - 1):
        bench_px.append(bench_px[-1] * (1 + random.uniform(-0.02, 0.02)))
    # STOCKX moves at exactly 2x the benchmark's daily return, by construction.
    bench_rets = [bench_px[i] / bench_px[i - 1] - 1 for i in range(1, n)]
    stockx_px = [50.0]
    for r in bench_rets:
        stockx_px.append(stockx_px[-1] * (1 + 2 * r))
    df = pd.DataFrame({"BENCH1": bench_px, "BENCH2": bench_px, "STOCKX": stockx_px})

    composite = bench_return_series(df, ["BENCH1", "BENCH2"], {"BENCH1": 1000.0, "BENCH2": 1000.0})
    assert (composite.dropna() - df["BENCH1"].pct_change().dropna()).abs().max() < 1e-9, \
        "two identical, equally-weighted names must give back the same series"
    assert bench_return_series(df, ["NOPE"], {"NOPE": 100.0}) is None, "no matching symbols must not crash"

    b = beta(df, "STOCKX", composite)
    assert abs(b - 2.0) < 0.01, f"a 2x-levered series must price back to beta ~2.0, got {b}"
    assert beta(df, "STOCKX", None) is None, "no benchmark series must not crash"
    assert beta(df, "GHOST", composite) is None, "a symbol not in the price data must not crash"

    assert bench_index_level(None) == {}
    flat = pd.Series([0.0, 0.0, 0.0], index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]))
    lvl = bench_index_level(flat)
    assert lvl == {"2026-01-01": 100.0, "2026-01-02": 100.0, "2026-01-05": 100.0}, lvl
    growth = pd.Series([0.10, 0.10], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    lvl2 = bench_index_level(growth)
    assert lvl2["2026-01-01"] == 110.0 and abs(lvl2["2026-01-02"] - 121.0) < 1e-6, lvl2
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    out = build()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"{OUT}: {len(out['signals'])} priced, {len(out['errors'])} unresolved, "
          f"{len(out['bench_sect'])} benchmark sectors")
    for e in out["errors"]:
        print("  !", e)
