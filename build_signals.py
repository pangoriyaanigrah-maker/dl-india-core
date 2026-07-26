"""build_signals.py — nightly machine-data feed for the DL India Core dashboard.

Writes signals.json: last price, market cap (Rs crore), 52-week low, and
cross-sectional value / momentum / quality z-scores for every ticker in
book.json — plus a market-cap-weighted sector and size benchmark computed
across an NSE universe proxy.

Division of labour: book.json is human-owned (weights, targets, theses) and is
only ever READ here. signals.json is machine-owned and only ever WRITTEN here.
Nothing in this script may edit the book.

    python build_signals.py              # fetch and write signals.json
    python build_signals.py --selftest   # check the maths, no network

Tickers that Yahoo can't resolve land in signals.json's "errors" and simply
show as NO FEED on the dashboard. Fix one by adding a "yfSymbol" to that
holding in book.json — e.g. "yfSymbol": "MACFOS.NS".
"""
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BOOK = "book.json"
OUT = "signals.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Size cuts in Rs crore. MUST match CUTS in index.html or the dashboard's size
# buckets and this benchmark disagree.
CUTS = {"large": 67000, "mid": 22000}

# Cross-sectional universe: a liquid NSE proxy, not the official Nifty 500.
# Deliberately spans large through small cap. A megacap-only list is worse than
# useless here — this book is entirely small/SME names, so they'd all sit above
# the 95th percentile, winsorize to the same value, and report identical
# momentum. It also makes bench_size honest; 65 megacaps price the market at
# 99.8% large. Portfolio names are appended automatically, so every holding is
# part of the cross-section it is scored against.
UNIVERSE = """
RELIANCE HDFCBANK ICICIBANK INFY TCS ITC LT SBIN BHARTIARTL KOTAKBANK AXISBANK
HINDUNILVR BAJFINANCE MARUTI SUNPHARMA TITAN ULTRACEMCO NESTLEIND WIPRO ONGC
NTPC POWERGRID TATASTEEL JSWSTEEL ADANIENT ADANIPORTS COALINDIA HCLTECH TECHM
ASIANPAINT BAJAJFINSV GRASIM CIPLA DRREDDY DIVISLAB EICHERMOT HEROMOTOCO
BRITANNIA HINDALCO INDUSINDBK M&M SHRIRAMFIN TATACONSUM APOLLOHOSP BEL BPCL IOC
SBILIFE HDFCLIFE PIDILITIND DABUR GODREJCP HAVELLS SIEMENS ABB CUMMINSIND
DIXON PERSISTENT MPHASIS TRENT DMART ETERNAL TMPV
FEDERALBNK AUBANK BANKBARODA CANBK PNB UNIONBANK INDIANB IDFCFIRSTB JIOFIN LICI
PFC RECLTD IRFC HUDCO CDSL BSE MCX ANGELONE CAMS KFINTECH POLICYBZR PAYTM
TATAELXSI KPITTECH COFORGE OFSS SONATSOFTW CYIENT TATATECH NYKAA DELHIVERY IRCTC
HAL BDL MAZDOCK COCHINSHIP GRSE DATAPATTNS ZENTEC KAYNES SYRMA
SUZLON RVNL IRCON NBCC NHPC SJVN IEX TITAGARH THERMAX AIAENG GRINDWELL
CARBORUNIV SKFINDIA TIMKEN ELGIEQUIP KSB KIRLOSBROS SHAKTIPUMP
AMBER VOLTAS BLUESTARCO CROMPTON VGUARD POLYCAB KEI FINCABLES RRKABEL
ENDURANCE SUNDRMFAST BALKRISIND APOLLOTYRE MRF CEATLTD ESCORTS ASHOKLEY TVSMOTOR
SUPREMEIND ASTRAL FINPIPE APLAPOLLO RATNAMANI MAHSEAMLES JINDALSAW WELCORP
DEEPAKNTR AARTIIND NAVINFLUOR SRF ATUL VINATIORGA FINEORG ALKYLAMINE CLEAN
LAURUSLABS GLENMARK TORNTPHARM ZYDUSLIFE IPCALAB AJANTPHARM ERIS JBCHEPHARM
FORTIS MAXHEALTH NH KIMS RAINBOW METROPOLIS LALPATHLAB
JUBLFOOD DEVYANI SAPPHIRE WESTLIFE VBL RADICO UBL PGHH MARICO EMAMILTD BAJAJCON
KANSAINER BERGEPAINT INDIGO GESHIP SCI
""".split()


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


def momentum(closes):
    """12M-1M return plus 6M return — the blend the dashboard's factor rail
    describes. Needs a full year of history, so recent IPOs score None rather
    than being compared on a stub."""
    if len(closes) < 200:
        return None
    return (closes[-22] / closes[0] - 1) + (closes[-1] / closes[-126] - 1)


# ---------------------------------------------------------------- io
def load_book():
    try:
        with open(BOOK, encoding="utf-8") as f:
            holdings = json.load(f)["holdings"]
    except FileNotFoundError:
        sys.exit(f"{BOOK} not found — upload a holdings file on the dashboard's Import tab.")
    except (KeyError, json.JSONDecodeError) as e:
        sys.exit(f"{BOOK} is not a valid book: {e}")
    if not holdings:
        sys.exit(f"{BOOK} has no holdings.")
    return holdings


def fetch(symbols):
    """-> ({symbol: [close, ...]}, {symbol: info}). One bulk price call; the
    fundamentals endpoint is per-name and flaky, so each is isolated."""
    import yfinance as yf

    px = {}
    data = yf.download(symbols, period="1y", auto_adjust=True,
                       progress=False, group_by="column")
    close = data["Close"]
    for s in symbols:
        try:
            series = close[s] if hasattr(close, "columns") else close
            vals = [float(v) for v in series.dropna().tolist()]
        except (KeyError, TypeError, ValueError):
            continue
        if vals:
            px[s] = vals

    info = {}
    for s in px:
        try:
            info[s] = yf.Ticker(s).info or {}
        except Exception:                      # yfinance raises freely here
            info[s] = {}
    return px, info


def num(d, *keys):
    """First finite number among `keys`, else None."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and math.isfinite(v) and v != 0:
            return float(v)
    return None


def build():
    holdings = load_book()
    # Yahoo symbols to try per holding. Most NSE names are TK.NS; SME and
    # recently listed names are often only on BSE, hence the .BO fallback.
    wanted = {h["tk"]: [h["yfSymbol"]] if h.get("yfSymbol")
                       else [f'{h["tk"]}.NS', f'{h["tk"]}.BO']
              for h in holdings}

    universe = [f"{t}.NS" for t in UNIVERSE]
    symbols = sorted(set(universe) | {c for cs in wanted.values() for c in cs})
    print(f"fetching {len(symbols)} symbols ({len(wanted)} holdings + "
          f"{len(universe)} universe)...")
    px, info = fetch(symbols)

    # Resolve each holding to the first candidate that actually returned data.
    resolved, errors = {}, []
    for tk, cands in wanted.items():
        hit = next((c for c in cands if c in px), None)
        if hit:
            resolved[tk] = hit
        else:
            errors.append(f"{tk}: no Yahoo data for {' or '.join(cands)} — "
                          f"set \"yfSymbol\" on this holding in book.json")

    # Score across the union of universe and holdings, so a portfolio name is
    # part of the cross-section rather than measured against a book it's absent from.
    scored = sorted(set(px) & (set(universe) | set(resolved.values())))
    mcap = {s: (num(info[s], "marketCap") or 0) / 1e7 for s in scored}   # -> Rs crore

    momo = zscores({s: momentum(px[s]) for s in scored}, clip=False)
    val = blend(*[{k: (-v if v is not None else None)
                   for k, v in zscores({s: num(info[s], *ks) for s in scored}).items()}
                  for ks in (("forwardPE", "trailingPE"),
                             ("priceToBook",),
                             ("enterpriseToEbitda",))])
    qual = blend(
        zscores({s: num(info[s], "returnOnEquity") for s in scored}),
        {k: (-v if v is not None else None)
         for k, v in zscores({s: num(info[s], "debtToEquity") for s in scored}).items()},
        zscores({s: num(info[s], "profitMargins") for s in scored}),
    )

    signals = {}
    for tk, s in resolved.items():
        signals[tk] = {
            "cmp": round(px[s][-1], 2),
            "mcap": round(mcap[s]) or None,
            "lo": round(min(px[s]), 2),
            "val": val.get(s),
            "momo": momo.get(s),
            "qual": qual.get(s),
        }

    # Benchmark: market-cap weighted over the universe names that priced.
    bench_sect, bench_size = defaultdict(float), defaultdict(float)
    for s in universe:
        m = mcap.get(s, 0)
        if not m:
            continue
        bench_sect[info[s].get("sector") or "Unclassified"] += m
        bench_size["Large" if m >= CUTS["large"] else
                   "Mid" if m >= CUTS["mid"] else "Small"] += m
    tot = sum(bench_sect.values())
    share = lambda d: {k: round(v / tot, 4) for k, v in
                       sorted(d.items(), key=lambda kv: -kv[1])} if tot else {}

    return {
        "asof": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "note": "z-scores are cross-sectional vs an NSE proxy universe, not the "
                "official Nifty 500. value = cheapness on P/E, P/B, EV/EBITDA; "
                "quality = ROE, leverage, net margin (no accruals or margin-stability "
                "term); momentum = (12M-1M) + 6M price return, needs 1y of history.",
        "errors": errors,
        "signals": signals,
        "bench_sect": share(bench_sect),
        "bench_size": share(bench_size),
    }


def selftest():
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

    assert momentum([1.0] * 199) is None, "short history must not score"
    flat = [10.0] * 250
    assert abs(momentum(flat)) < 1e-9, "flat series has no momentum"
    rising = [float(i) for i in range(1, 251)]
    assert momentum(rising) > 0, "monotonic rise must be positive momentum"

    assert num({"a": 0, "b": 3}, "a", "b") == 3.0, "zero must fall through"
    assert num({"a": float("nan")}, "a") is None
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
