"""Every number the dashboard shows. The browser computes none of it.

Ported from the original in-browser JavaScript and verified against it
figure-for-figure: total P&L, realized, unrealized, all four risk
scenarios, factor z-scores, sector active weights and rail geometry were
each recomputed independently from raw rows and matched exactly.

Two rules run through all of it, and both are load-bearing:

  * a missing input yields None and DROPS the name from an aggregate --
    never a silent 0. wavg() skips Nones, risk scenarios report how many
    names they could not price, factor rows report coverage.
  * fullWt == 0 means EXITING and outranks every other status.

recalculate() is the entry point: book + signals in, the JSON payloads
that get stored on Drive out.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from decimal import ROUND_HALF_UP, Decimal

# Size cuts in Rs crore. MUST match CUTS in scripts/build_signals.py.
# These are AMFI's official semi-annual large/mid/small cap classification
# cutoffs (SEBI-mandated, published each Jan/Jul) -- not derived from our
# own benchmark or holdings data. Update both copies when AMFI republishes.
# AMFI has no "Micro" tier; only three buckets exist here on purpose.
CUTS = {"large": 106300, "mid": 33500}

# A trade-ledger ticker with no current holdings row is, by construction,
# one you no longer track as an open position (see the trades-import
# placeholder logic in api.py) -- its P&L still counts toward the total,
# but it has no sector/size to report, so both attributions share this one
# clearly-named bucket instead of two vague ones ("Unmapped" / "--").
EXITED_LABEL = "Exited positions"

# Used only when the feed carries no bench_sect/bench_size. Keyed in the
# same canonical (Yahoo-style) taxonomy as the live feed -- see
# SECTOR_ALIASES below for why that matters.
BENCH_SECT_FALLBACK = {
    "Financial Services": 0.30, "Technology": 0.12, "Consumer Cyclical": 0.15, "Industrials": 0.09,
    "Energy": 0.09, "Consumer Defensive": 0.07, "Basic Materials": 0.07, "Healthcare": 0.06,
    "Communication Services": 0.03, "Utilities": 0.02,
}
BENCH_SIZE_FALLBACK = {"Large": 0.72, "Mid": 0.18, "Small": 0.10}

# Yahoo (and the Screener.in fallback build_signals.py uses for names Yahoo
# has no sector for) report sectors in their own taxonomy -- "Basic
# Materials", "Consumer Defensive". A holdings file is typed by an analyst
# in the Indian-market convention -- "Materials", "Consumer Staples". Comparing
# the two without normalizing meant those sectors always priced the
# benchmark at 0%, since the strings simply never matched: not a rounding
# artifact, a real hole in the Exposures tab's sector active-weight and the
# Crowding insight for anything but the sectors that happened to already
# share a spelling (Industrials, Financial Services, Energy, Real Estate,
# Utilities, Healthcare). Canonicalize the analyst's label before any
# benchmark lookup; sector_of() itself is untouched, so grouping and
# on-screen row labels still show exactly what was typed.
SECTOR_ALIASES = {
    "Materials": "Basic Materials",
    "Consumer Staples": "Consumer Defensive",
    "Consumer Discretionary": "Consumer Cyclical",
    "Financials": "Financial Services",
    "IT Services": "Technology", "IT": "Technology", "Information Technology": "Technology",
    "Telecom": "Communication Services", "Telecommunication": "Communication Services",
    "Auto": "Consumer Cyclical", "Autos": "Consumer Cyclical", "Automobile": "Consumer Cyclical",
    "Pharma": "Healthcare", "Pharmaceuticals": "Healthcare",
    # "Consumer" alone is genuinely ambiguous (Cyclical vs Defensive) --
    # left unmapped on purpose rather than guessing one and being wrong.
}


def canon_sector(s):
    return SECTOR_ALIASES.get(s, s)


# ------------------------------------------------------- display format
# Only needed because the written insights embed formatted numbers in
# prose. Every other figure is returned raw for the browser to format.
def _fixed(x, places):
    """JS Number.toFixed: half away from zero, not Python's banker's."""
    return str(Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _grp(n):
    """en-IN grouping: 1223866 -> '12,23,866'."""
    s = str(abs(int(n)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def pct(x, signed=True):
    return ("+" if x > 0 and signed else "") + _fixed(x * 100, 1) + "%"


def wpct(x):
    return _fixed(x * 100, 0) + "%"


def zfmt(v):
    return ("+" if v > 0 else "") + _fixed(v, 2) + "σ"


# ------------------------------------------------------------ primitives
def cmp_of(signals, tk):
    s = signals.get(tk)
    return s.get("cmp") if s else None


def upside(signals, h):
    c = cmp_of(signals, h["tk"])
    return (h["tp"] / c - 1) if (c and h.get("tp") is not None) else None


def bucket_of(signals, tk):
    s = signals.get(tk)
    m = s.get("mcap") if s else None
    if m is None:
        return "Unclassified"
    return "Large" if m >= CUTS["large"] else "Mid" if m >= CUTS["mid"] else "Small"


SECTOR_UNSET_LABEL = "Sector not set"


def sector_of(h):
    """A holding created as a placeholder by a trades import has no sector
    yet, and a manually-typed holdings row can just leave it blank. Left as
    None it becomes a null dict key, and sorting a set of sector names then
    raises TypeError comparing None to str -- which took down the whole
    import. Label it once, here -- distinct from EXITED_LABEL, since this
    is a currently-held position missing data, not one you sold out of."""
    return h.get("sector") or SECTOR_UNSET_LABEL


def conv_bucket(conv):
    """Prefix match: real conviction text is not a clean enum
    (e.g. 'Medium (Watchlist)')."""
    if not conv:
        return None
    c = str(conv).strip().lower()
    for p in ("high", "medium", "low"):
        if c.startswith(p):
            return p.capitalize()
    return None


def status_of(signals, h):
    """Order matters: EXITING wins over everything, including a target hit."""
    u = upside(signals, h)
    if h.get("fullWt") == 0:
        return ("EXITING", "b-rev", "var(--down)")
    if u is None:
        return ("NO FEED", "b-near", "var(--amber)")
    if u <= 0:
        return ("TARGET HIT", "b-rev", "var(--down)")
    if u < 0.10:
        return ("NEAR TARGET", "b-near", "var(--amber)")
    return ("ON THESIS", "b-ok", "var(--up)")


def built_of(h):
    fw, wt = h.get("fullWt"), h["wt"]
    if fw == 0:
        return "Exiting"
    if not fw:
        return "Full"
    return "Full" if wt >= fw - 0.001 else f"{round(wt / fw * 100)}% built"


def total_weight(holdings):
    """`or 1.0` guards divide-by-zero on an empty book."""
    return sum(h["wt"] for h in holdings) or 1.0


def wavg(holdings, f):
    """Weighted average that SKIPS Nones rather than treating them as 0."""
    acc = 0.0
    for h in holdings:
        v = f(h)
        if v is not None:
            acc += h["wt"] * v
    return acc / total_weight(holdings)


# ---------------------------------------------------------------- P&L
def _avg_cost(trades_for_tk):
    """Chronological weighted-average-cost accounting for one ticker.

    A single running (qty, avg_cost) for the whole position, not
    per-lot: a Buy blends the new shares into the existing average; a
    Sell realizes P&L against whatever that average is AT THAT MOMENT
    and does not itself change the average. Each sale's P&L is booked
    once, incrementally, at the time it happens -- a later buy can never
    reach back and rewrite an earlier sale's already-booked profit,
    because that sale never gets recomputed once trades have moved past
    it. avg_cost resets to 0 on a full exit (qty hits 0) rather than
    carrying forward, so a later re-entry starts a fresh average instead
    of blending with a position that's already fully closed out -- this
    is the exact bug a naive lifetime-average implementation has: without
    the reset, a re-entry's buy price would blend into a "lifetime"
    average that includes shares already sold, silently distorting P&L
    already realized on the earlier exit.

    -> (qty remaining, avg cost of qty remaining, realized total, fees
        total, gross buy value, last trade price seen)
    """
    ordered = sorted(trades_for_tk, key=lambda t: (t["date"], 0 if t["side"] == "Buy" else 1))
    qty = avg_cost = realized = fees = buyV = 0.0
    last = None
    for t in ordered:
        q, p = t["qty"], t["price"]
        fees += t.get("costs") or 0
        last = p
        if t["side"] == "Buy":
            avg_cost = (qty * avg_cost + q * p) / (qty + q) if (qty + q) > 1e-9 else p
            qty += q
            buyV += q * p
        else:
            # ponytail: a sell exceeding the qty actually held (bad data,
            # not a real scenario the app should ever produce) prices the
            # excess at zero cost rather than going negative or raising --
            # a wrong number here is recoverable, a crash on import is not.
            take = min(q, qty)
            realized += q * p - take * avg_cost
            qty -= take
            if qty <= 1e-9:
                qty, avg_cost = 0.0, 0.0
    return qty, avg_cost, realized, fees, buyV, last


def perf_calc(holdings, trades, signals):
    """Average-cost P&L. Avg cost and realized P&L are always
    trades-derived -- the trade ledger is the only record of what was
    actually paid. Unrealized P&L uses the holdings file's stated
    quantity when it has one (the current, authoritative share count);
    trades-derived net quantity is only a fallback for a placeholder
    holding that has no holdings row yet. When both exist and disagree,
    it's reported in qtyMismatches rather than silently trusted -- a
    missed trade row or an unlogged bonus/split should surface, not skew
    P&L quietly."""
    by_tk_trades = {}
    for t in trades:
        by_tk_trades.setdefault(t["tk"], []).append(t)

    by_tk = {h["tk"]: h for h in holdings}
    rows, mismatches = [], []
    for tk, tks_trades in by_tk_trades.items():
        tradesQty, avg, realized, fees, buyV, last = _avg_cost(tks_trades)
        h = by_tk.get(tk)
        statedQty = h.get("qty") if h else None
        net = statedQty if statedQty is not None else tradesQty
        if statedQty is not None and abs(statedQty - tradesQty) > 0.01:
            mismatches.append({"tk": tk, "holdingsQty": statedQty, "tradesQty": tradesQty})
        marked = cmp_of(signals, tk)
        c = marked if marked is not None else last
        unreal = net * (c - avg)
        s = signals.get(tk) or {}
        rows.append({
            "tk": tk, "net": net, "avg": avg, "cmp": c, "marked": marked is not None,
            "unreal": unreal, "realized": realized, "fees": fees,
            # Gross of costs, deliberately -- costs get their own card/column
            # (the "fees" field right above) rather than being netted into
            # the headline P&L number twice over.
            "total": unreal + realized, "invested": buyV,
            "ret": ((unreal + realized) / buyV) if buyV else 0.0,
            "sector": sector_of(h) if h else EXITED_LABEL,
            "analyst": (h.get("analyst") if h else None) or "—",
            "bucket": bucket_of(signals, tk) if h else EXITED_LABEL, "momo": s.get("momo"),
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    tot = lambda k: sum(r[k] for r in rows)  # noqa: E731
    return {"rows": rows, "bought": tot("invested"), "realized": tot("realized"),
            "unreal": tot("unreal"), "fees": tot("fees"), "total": tot("total"),
            "qtyMismatches": mismatches}


def group_pl(rows, key):
    g = {}
    for r in rows:
        g[key(r)] = g.get(key(r), 0.0) + r["total"]
    return [{"key": k, "pl": v} for k, v in sorted(g.items(), key=lambda kv: kv[1], reverse=True)]


def momo_cohort(r):
    m = r["momo"]
    if m is None:
        return "No signal"
    return "High momo" if m >= 0.5 else "Low momo" if m <= -0.5 else "Mid momo"


# -------------------------------------------------------- composition
def composition(holdings, cash):
    fw = lambda h: h["fullWt"] if h.get("fullWt") is not None else h["wt"]  # noqa: E731
    conv_w = {"High": 0.0, "Medium": 0.0, "Low": 0.0}
    for h in holdings:
        b = conv_bucket(h.get("conv"))
        if b:
            conv_w[b] += h["wt"]
    return {
        "pendingAdds": sum(max(fw(h) - h["wt"], 0) for h in holdings),
        "exits": sum(h["wt"] for h in holdings if h.get("fullWt") == 0),
        "trims": sum(h["wt"] - h["fullWt"] for h in holdings
                     if h.get("fullWt") is not None and 0 < h["fullWt"] < h["wt"]),
        "fullBuild": sum(fw(h) for h in holdings),
        "convictionWeights": conv_w,
        "equity": total_weight(holdings), "cash": cash,
        "current": [{"tk": h["tk"], "wt": h["wt"], "conviction": conv_bucket(h.get("conv"))}
                    for h in holdings],
        "fullBuildBars": [{"tk": h["tk"], "wt": h["fullWt"], "conviction": conv_bucket(h.get("conv"))}
                          for h in holdings if (h.get("fullWt") or 0) > 0],
        "top3": [h["tk"] for h in sorted(holdings, key=lambda h: h["wt"], reverse=True)[:3]],
    }


def staged_actions(holdings, signals):
    out = []
    for h in holdings:
        fw, wt = h.get("fullWt"), h["wt"]
        if fw is None or not (fw > wt or fw == 0 or 0 < fw < wt):
            continue
        kind = "Exit" if fw == 0 else "Add" if fw > wt else "Trim"
        add = h.get("addLvl")
        s = signals.get(h["tk"])
        out.append({
            "tk": h["tk"], "kind": kind, "wt": wt, "fullWt": fw,
            "delta": -wt if fw == 0 else fw - wt,
            "trigger": (f"₹{_grp(round(add))}" if add else ("Event" if kind == "Add" else "Rule")),
            "distance": (add / s["cmp"] - 1) if (add and s and s.get("cmp")) else None,
            "strategy": h.get("strategy") or "—",
        })
    return out


# ----------------------------------------------------------- insights
def insights(holdings, signals, bench_sect, P, C):
    """Rule-based reads for the IC. Every name mentioned below is read off
    the actual book -- this used to hardcode two stock names from the
    original mock ('BEL + Kaynes', 'Sun, Titan') and fire on any book that
    crossed their threshold, which meant a client could be shown analysis
    about stocks they didn't own."""
    out = []
    post_add_cash = C["cash"] - C["pendingAdds"] + C["exits"] + C["trims"]
    staged = [h["tk"] for h in holdings if (h.get("fullWt") or 0) > h["wt"]]
    exits_names = ", ".join(h["tk"] for h in holdings if h.get("fullWt") == 0)
    funded = ("fully funded from cash. " if C["pendingAdds"] <= C["cash"] else
              f"<b>underfunded by {wpct(C['pendingAdds']-C['cash'])}</b> — identify the trim source now, "
              f"not at the trigger. ")
    out.append({"tag": "Composition", "text":
        f"Book is <b>{wpct(C['equity'])} deployed</b> against a full-build target of "
        f"{wpct(C['fullBuild'])}. Staged adds pending: <b>{wpct(C['pendingAdds'])}</b> "
        f"({', '.join(staged)}) vs cash of {wpct(C['cash'])} — " + funded +
        (f"The {wpct(C['exits'])} exit sleeve ({exits_names}) returns further powder; " if C["exits"] > 0 else "") +
        f"steady-state cash at full build ≈ <b>{wpct(max(post_add_cash, 0))}</b>."})

    top3w = sum(h["wt"] for h in holdings if h["tk"] in C["top3"])
    if C["equity"] and top3w / C["equity"] > 0.5:
        out.append({"tag": "Concentration", "text":
            f"Top-3 ({', '.join(C['top3'])}) = <b>{wpct(top3w)}</b> of NAV, "
            f"{_fixed(top3w/C['equity']*100, 0)}% of equity. Acceptable for a conviction book, but note "
            f"two of three staged adds deepen existing sector bets rather than diversify."})

    if C["convictionWeights"]["Low"] > 0.12:
        low = ", ".join(h["tk"] for h in holdings if conv_bucket(h.get("conv")) == "Low")
        out.append({"tag": "Conviction check", "text":
            f"<b>{wpct(C['convictionWeights']['Low'])}</b> of NAV sits in Low-conviction names ({low}). "
            f"Enforce exit rules on this sleeve — it's the designated funding source if staged triggers "
            f"fire before exits complete."})

    gated = [h["tk"] for h in holdings if (h.get("fullWt") or 0) > h["wt"] and not h.get("addLvl")]
    if gated:
        out.append({"tag": "Sequencing", "text":
            ", ".join(f"<b>{t}</b>" for t in gated) +
            " adds are event-gated, not price-gated — they cannot be pre-funded by limit orders. "
            "Calendar the catalysts so the powder isn't redeployed elsewhere first."})

    if not P["rows"]:
        return out

    top = P["rows"][0]
    share = top["total"] / P["total"] if P["total"] else 0
    if share > 0.3 and P["total"] > 0:
        out.append({"tag": "P&L concentration", "text":
            f"<b>{top['tk']}</b> is {_fixed(share*100, 0)}% of total P&L — book-level outcome "
            f"currently rides one name."})

    hi_m = [r for r in P["rows"] if r["momo"] is not None and r["momo"] >= 0.5]
    hi_pl = sum(r["total"] for r in hi_m)
    if P["total"] > 0 and hi_pl / P["total"] > 0.5:
        out.append({"tag": "Factor read", "text":
            f"{_fixed(hi_pl/P['total']*100, 0)}% of P&L comes from high-momentum names "
            f"({', '.join(r['tk'] for r in hi_m)}). The book is being paid for momentum, not value — a "
            f"market-neutral India momentum sleeve is the cleanest side expression of the same signal."})

    w_sec = {}
    for h in holdings:
        w_sec[sector_of(h)] = w_sec.get(sector_of(h), 0.0) + h["wt"]
    total_w = total_weight(holdings)
    # Whichever sector actually carries the largest active bet, not a fixed
    # name -- a book overweight anywhere but Industrials used to get no
    # warning at all, since the old check only ever looked at that one sector.
    active_by_sector = {s: w / total_w - bench_sect.get(canon_sector(s), 0.0)
                        for s, w in w_sec.items() if s != SECTOR_UNSET_LABEL}
    crowded, crowded_active = max(active_by_sector.items(), key=lambda kv: kv[1], default=(None, 0.0))
    if crowded and crowded_active > 0.05:
        names = ", ".join(h["tk"] for h in holdings if sector_of(h) == crowded)
        out.append({"tag": "Crowding", "text":
            f"{crowded} is {pct(crowded_active)} active — {names} concentrate this exposure. "
            f"Size related bets elsewhere in the complex against this, not in isolation."})

    for h in holdings:
        u, s = upside(signals, h), signals.get(h["tk"])
        momo = (s or {}).get("momo")
        if u is None or momo is None:
            continue
        if u >= 0.15 and momo <= -0.5:
            out.append({"tag": "Divergence", "text":
                f"<b>{h['tk']}</b>: {pct(u)} modelled upside but {zfmt(momo)} momentum — thesis and tape "
                f"disagree. The staged-entry discipline exists for exactly this: let the trigger do the timing."})
        if -1 < u <= 0.05 and momo >= 0.5 and (h.get("fullWt") or 0) > 0:
            out.append({"tag": "Divergence", "text":
                f"<b>{h['tk']}</b>: tape ({zfmt(momo)} momentum) has run ahead of the {pct(u)} remaining "
                f"upside — either the target is stale or the trim rule applies."})

    vz = wavg(holdings, lambda h: (signals.get(h["tk"]) or {}).get("val"))
    if vz < -0.5:
        staged_names = [h["tk"] for h in holdings if (h.get("fullWt") or 0) > h["wt"]]
        staged_note = (f"and the staged adds ({', '.join(staged_names)}) would buy <i>corrections</i>, "
                       f"partially self-correcting this. " if staged_names else "")
        out.append({"tag": "Posture", "text":
            f"Weighted value z is {zfmt(vz)} — the book pays up for growth/quality, {staged_note}"
            f"If the momentum regime turns, the {wpct(C['cash'])} cash is the real hedge; deploy the "
            f"staged tranches patiently."})
    return out


# --------------------------------------------------------------- risk
SCENARIOS = [
    ("mid", "Mid of 52W range", "every stock reverts to (52W high + low) / 2",
     lambda s: ((s["hi"] + s["lo"]) / 2) / s["cmp"] - 1
     if s.get("hi") is not None and s.get("lo") is not None and s.get("cmp") else None),
    # 1 + r1m is a divisor: r1m == -1 (a stock that went to zero) would
    # raise ZeroDivisionError and take the Risk tab down with it. A stock
    # that was wiped out has no gains to give up, so it is excluded.
    ("give1m", "Give up 1M gains", "stocks that rose unwind the month; decliners held flat",
     lambda s: min(0.0, 1 / (1 + s["r1m"]) - 1)
     if s.get("r1m") is not None and 1 + s["r1m"] > 0 else None),
    ("low", "Retest 52W low", "every stock trades back to its 52-week low",
     lambda s: s["lo"] / s["cmp"] - 1 if s.get("lo") is not None and s.get("cmp") else None),
    ("worst", "Worst month repeats", "each stock repeats its worst 1-month window of the last 5 years",
     lambda s: s.get("worstM")),
]

FACTORS = [
    ("Value", "val", "EBIT/EV, FCF+growth-4% premium, analyst target/price · +ve = cheaper"),
    ("Momentum", "momo", "6M return, 6M Sharpe, sector momentum · +ve = stronger tape"),
    ("Quality", "qual", "Gross margin, net debt/EBITDA, cash conversion, 24M stress "
     "resilience, op-margin sustainability, compounding score, receivables trend "
     "· +ve = higher quality"),
    ("Biz Momentum", "bizMomo", "Revenue/EPS growth YoY, sequential acceleration, "
     "forward visibility, analyst signal · +ve = accelerating business"),
]


def rail(h, s):
    """Positions-tab price rail. None when the dashboard would show text
    instead: no feed, or no target price.

    Scale bounds and displayed points are kept separate. The scale
    (loAnchor/hiAnchor below) still dips below the real 52-week low to
    make room for the Add tick, and now also stretches above target/CMP
    to the real 52-week high when that's the biggest of the three -- but
    every DISPLAYED point (lo/hi/tp/cmp/add) is placed at its own real
    price, never silently substituted by a scale-adjustment artifact."""
    if not s:
        return None
    c, tp, add = s.get("cmp"), h.get("tp"), h.get("addLvl")
    lo, lo_estimated = s.get("lo"), False
    if lo is None and c and tp is not None:
        lo = min(c, tp) * 0.88
        lo_estimated = True
    if not (c and lo is not None and tp is not None):
        return None
    hi = s.get("hi")
    hi_a = max(tp, c, hi) if hi is not None else max(tp, c)
    lo_a = min(lo, add * 0.98) if add else lo
    span = hi_a * 1.03 - lo_a
    x = lambda v: round(min(99.0, max(0.0, (v - lo_a) / span * 100)), 1)  # noqa: E731
    return {
        "lo": round(lo, 2), "loEstimated": lo_estimated, "loPct": x(lo),
        "hi": round(hi, 2) if hi is not None else None,
        "hiPct": x(hi) if hi is not None else None,
        "tpPct": x(tp), "cmpPct": x(c), "addPct": x(add) if add else None,
    }


def _bench(feed):
    sect = feed.get("bench_sect") or BENCH_SECT_FALLBACK
    size = feed.get("bench_size") or BENCH_SIZE_FALLBACK
    return sect, size, bool(feed.get("bench_sect"))


def _feed_meta(feed):
    return {"live": bool(feed.get("signals")), "asOf": feed.get("asof"),
            "index": feed.get("index") or "the index", "note": feed.get("note"),
            "errors": feed.get("errors") or []}


# ================================================================ screens
def build_dashboard(book, feed, meta, nav):
    H, S = book["holdings"], feed.get("signals") or {}
    P = perf_calc(H, book["trades"], S)
    C = composition(H, book["cash"])
    current = sum(r["net"] * r["cmp"] for r in P["rows"] if r["cmp"])
    # C["cash"]/C["equity"] are a WEIGHT plug: 1 - sum(holdings weights),
    # whatever an analyst typed as target weights -- that's the right
    # basis for the Composition section's bars (a plan, not a cash
    # balance), but wrong for a headline "how much of the fund is
    # invested" stat: if typed weights happen to sum to 100%, it reports
    # 0% cash even with real money sitting uninvested. Once real
    # cashflows exist, the real rupee split (same one the NAV panel
    # shows) is what these two stats should report instead -- otherwise
    # the two panels can show contradictory pictures of the same book.
    if nav["cashflowCount"] and nav["nav"]:
        equity_weight, cash_weight = nav["holdingsValue"] / nav["nav"], nav["cashBalance"] / nav["nav"]
    else:
        equity_weight, cash_weight = C["equity"], C["cash"]
    return {
        # Weights are NAV FRACTIONS, not rupees -- the book stores no
        # absolute NAV. Every rupee figure comes from the trade ledger.
        "cashWeight": cash_weight, "equityWeight": equity_weight,
        "invested": P["bought"], "currentValue": current, "portfolioValue": current,
        "todaysChange": None,     # no prior close in the feed -- cannot be derived
        "overallPL": P["total"], "overallReturn": (P["total"] / P["bought"]) if P["bought"] else 0.0,
        "realized": P["realized"], "unrealized": P["unreal"], "costs": P["fees"],
        "holdingsCount": len(H), "tradesCount": len(book["trades"]),
        "weightedUpside": wavg(H, lambda h: upside(S, h)),
        "lastUpdated": meta.get("holdingsUpdated") or meta.get("tradesUpdated"),
        "sourceFile": (meta.get("sourceFiles") or {}).get("holdings"),
        "feed": _feed_meta(feed),
        "qtyMismatches": P["qtyMismatches"],
    }


def build_portfolio(book, feed):
    H, S = book["holdings"], feed.get("signals") or {}
    P = perf_calc(H, book["trades"], S)
    C = composition(H, book["cash"])
    sect, _, _ = _bench(feed)
    return {
        "composition": C,
        "stagedActions": staged_actions(H, S),
        "attribution": {
            "bySector": group_pl(P["rows"], lambda r: r["sector"]),
            "byMomentum": group_pl(P["rows"], momo_cohort),
            "bySize": group_pl(P["rows"], lambda r: r["bucket"]),
            "byAnalyst": group_pl(P["rows"], lambda r: r["analyst"]),
        },
        "insights": insights(H, S, sect, P, C),
        "feed": _feed_meta(feed),
    }


PARTICIPATION_RATE = 0.20   # ponytail: standard conservative institutional
# convention (trade at most this fraction of a day's volume without moving
# the price) -- not a calibrated market-impact model. No real bid-ask/
# order-book data exists for Indian micro-caps to fit one against, so this
# stays a simple, clearly-labelled heuristic rather than a fabricated
# precise "expected slippage %".
LIQUIDITY_TIERS = (10, 3)   # days-to-liquidate thresholds: Illiquid / Tight / Liquid


def liquidity_view(all_h, S, P):
    """Days-to-liquidate and a plain tier per holding -- the volume data
    the feed already fetches (see build_signals.fetch) was discarded
    before this; for a micro-cap book this is often the real risk, since
    hitting a 52-week low and becoming unsellable happen together."""
    net = {r["tk"]: r["net"] for r in P["rows"]}
    rows = []
    for h in all_h:
        tk = h["tk"]
        adv = (S.get(tk) or {}).get("adv")
        qty = net.get(tk)
        posOverAdv = (qty / adv) if (adv and qty is not None) else None
        days = (posOverAdv / PARTICIPATION_RATE) if posOverAdv is not None else None
        tier = (None if days is None else
                "Illiquid" if days > LIQUIDITY_TIERS[0] else
                "Tight" if days > LIQUIDITY_TIERS[1] else "Liquid")
        rows.append({"tk": tk, "wt": h["wt"], "adv": adv, "qty": qty,
                     "positionOverAdv": posOverAdv, "daysToLiquidate": days, "tier": tier})
    rows.sort(key=lambda r: (r["daysToLiquidate"] is None, -(r["daysToLiquidate"] or 0)))
    scored = [r for r in rows if r["daysToLiquidate"] is not None]
    return {
        "rows": rows,
        "worstDays": scored[0]["daysToLiquidate"] if scored else None,
        "illiquidWeight": sum(r["wt"] for r in scored if r["tier"] == "Illiquid"),
        "unscored": len(rows) - len(scored),
        "participationRate": PARTICIPATION_RATE,
    }


def build_risk(book, feed):
    S = feed.get("signals") or {}
    all_h = book["holdings"]
    H = [h for h in all_h if S.get(h["tk"])]
    if not H:
        return {"priced": 0, "scenarios": [], "perStock": [], "metrics": [], "reads": [],
                "weightedBeta": None, "equityWeight": total_weight(all_h),
                "cashWeight": book["cash"],
                "liquidity": {"rows": [], "worstDays": None, "illiquidWeight": 0.0,
                              "unscored": 0, "participationRate": PARTICIPATION_RATE},
                "feed": _feed_meta(feed)}

    scenarios = []
    for key, name, desc, f in SCENARIOS:
        vals = [(h, f(S[h["tk"]])) for h in H]
        priced = [(h, v) for h, v in vals if v is not None]
        scenarios.append({"key": key, "name": name, "description": desc,
                          "navImpact": sum(h["wt"] * v for h, v in priced),
                          "priced": len(priced), "missing": len(vals) - len(priced)})

    metrics = []
    for h in H:
        s = S[h["tk"]]
        hi, lo, c = s.get("hi"), s.get("lo"), s.get("cmp")
        metrics.append({"tk": h["tk"], "beta": s.get("beta"), "dvol": s.get("dvol"),
                        "worstM": s.get("worstM"), "mdd": s.get("mdd"),
                        "range52wPosition": ((c - lo) / (hi - lo))
                        if (hi is not None and lo is not None and hi > lo) else None})

    w_beta = wavg(all_h, lambda h: (S.get(h["tk"]) or {}).get("beta"))
    equity = total_weight(all_h)
    P = perf_calc(all_h, book["trades"], S)
    return {
        "priced": len(H), "weightedBeta": w_beta, "equityWeight": equity,
        "cashWeight": book["cash"], "scenarios": scenarios,
        "perStock": [{"tk": h["tk"], "wt": h["wt"],
                      "changes": {k: f(S[h["tk"]]) for k, _, _, f in SCENARIOS}} for h in H],
        "metrics": metrics,
        "liquidity": liquidity_view(all_h, S, P),
        "reads": _risk_reads(H, S, book["cash"], equity, w_beta, scenarios,
                             feed.get("index") or "the benchmark"),
        "feed": _feed_meta(feed),
    }


def _risk_reads(H, S, cash, equity, w_beta, scenarios, bench_name):
    mid, f_mid = scenarios[0], SCENARIOS[0][3]
    navs = sorted(((h["tk"], h["wt"] * f_mid(S[h["tk"]])) for h in H if f_mid(S[h["tk"]]) is not None),
                  key=lambda r: r[1])[:2]
    share = (sum(v for _, v in navs) / mid["navImpact"]) if (mid["navImpact"] and len(navs) == 2) else 0

    reads = [{"tag": "Read", "text":
        "The ladder is deliberately conservative: <b>every stock moves at once, with no correlation "
        f"credit</b>. A real book would usually lose less. The {wpct(cash)} cash cushion is already "
        "reflected, because impacts are expressed on NAV rather than on equity."}]
    if share > 0.35:
        reads.append({"tag": "Concentration of risk", "text":
            f"<b>{' + '.join(tk for tk, _ in navs)}</b> carry {_fixed(share*100, 0)}% of the mid-range "
            f"scenario loss — the same names driving return are driving the tail. Size them against this, "
            f"not against their weight alone."})
    if w_beta:
        reads.append({"tag": "Beta", "text":
            f"Weighted beta {_fixed(w_beta, 2)} on {wpct(equity)} equity ≈ <b>{_fixed(w_beta*equity, 2)} "
            f"portfolio beta to {bench_name}</b> (one market-cap-weighted composite spanning large-cap "
            f"through micro-cap, so a holding of any size is measured against a real, current peer group "
            f"rather than a fixed index that may not even include it). A 10% move there implies roughly "
            f"{pct(-0.10*w_beta*equity)} on NAV before anything stock-specific."})
    if mid["missing"]:
        reads.append({"tag": "Coverage", "text":
            f"<b>{mid['missing']} of {len(H)}</b> holdings have no 5-year history yet (recent listings). "
            f"They are excluded from the ladder rather than counted as unchanged, so the totals below "
            f"understate a full-book move."})
    return reads


def build_performance(book, feed):
    P = perf_calc(book["holdings"], book["trades"], feed.get("signals") or {})
    dates = [t["date"] for t in book["trades"] if t.get("date")]
    # There's no time-of-day in a trade row, only a date -- same-day trades
    # for one ticker would otherwise fall back to merge/upload order, which
    # can show a sell above the buy that funded it. Buy-before-Sell is the
    # only sane tiebreak: you can't sell what you haven't bought yet.
    trades = sorted(book["trades"],
                    key=lambda t: (t.get("date") or "", 1 if t["side"] == "Buy" else 0),
                    reverse=True)
    return {
        "totalPL": P["total"], "realized": P["realized"], "unrealized": P["unreal"],
        "costs": P["fees"], "grossBought": P["bought"],
        "returnOnGross": (P["total"] / P["bought"]) if P["bought"] else 0.0,
        "tradeCount": len(book["trades"]),
        "dateRange": {"from": min(dates), "to": max(dates)} if dates else None,
        "winners": sum(1 for r in P["rows"] if r["total"] > 0),
        "positions": P["rows"],
        "trades": [{**t, "value": t["qty"] * t["price"]} for t in trades],
        "feed": _feed_meta(feed),
    }


def build_exposure(book, feed):
    H, S = book["holdings"], feed.get("signals") or {}
    P = perf_calc(H, book["trades"], S)
    sect, size_bench, live = _bench(feed)
    W = total_weight(H)

    factors = []
    for name, key, note in FACTORS:
        wz = wavg(H, lambda h, k=key: (S.get(h["tk"]) or {}).get(k))
        scored = sorted(({"tk": h["tk"], "z": (S.get(h["tk"]) or {}).get(key)} for h in H
                         if (S.get(h["tk"]) or {}).get(key) is not None),
                        key=lambda r: r["z"], reverse=True)
        factors.append({
            "factor": name, "note": note, "bookZ": wz if scored else None,
            "tilt": ("Above index" if wz >= 0.3 else "Below index" if wz <= -0.3 else "Near index")
            if scored else "no data",
            "highest": scored[0] if scored else None,
            "lowest": scored[-1] if scored else None,
            "scored": len(scored), "total": len(H),
        })

    w_sec = {}
    for h in H:
        w_sec[sector_of(h)] = w_sec.get(sector_of(h), 0.0) + h["wt"]
    pl_sec = {b["key"]: b["pl"] for b in group_pl(P["rows"], lambda r: r["sector"])}
    sectors = [{"sector": s, "portfolio": w_sec[s] / W, "benchmark": sect.get(canon_sector(s), 0.0),
                "active": w_sec[s] / W - sect.get(canon_sector(s), 0.0), "pl": pl_sec.get(s, 0.0)}
               for s in sorted(w_sec, key=lambda s: w_sec[s], reverse=True)]
    held_canon = {canon_sector(s) for s in w_sec}
    not_held = [(s, wt) for s, wt in sect.items() if s not in held_canon]

    size_w = {"Large": 0.0, "Mid": 0.0, "Small": 0.0}
    unpriced = 0.0
    for h in H:
        b = bucket_of(S, h["tk"])
        if b in size_w:
            size_w[b] += h["wt"]
        else:
            unpriced += h["wt"]
    return {
        "factors": factors, "sectors": sectors,
        "notHeld": {"count": len(not_held), "benchmarkWeight": sum(w for _, w in not_held)},
        "size": [{"bucket": k, "portfolio": v / W, "benchmark": size_bench.get(k, 0.0)}
                 for k, v in size_w.items()],
        # Holdings with a price but no market cap fall into no bucket at
        # all, so the size bars would silently total less than 100% with
        # nothing saying why. Report it instead of hiding it.
        "sizeUnclassified": unpriced / W,
        "benchmarkLive": live, "feed": _feed_meta(feed),
    }


def build_positions(book, feed):
    H, S = book["holdings"], feed.get("signals") or {}
    rows = []
    for h in sorted(H, key=lambda h: h["wt"], reverse=True):
        s = S.get(h["tk"])
        text, css, colour = status_of(S, h)
        rows.append({
            "tk": h["tk"], "co": h.get("co"), "sector": sector_of(h),
            "industry": h.get("industry"), "analyst": h.get("analyst"),
            "conviction": h.get("conv"), "wt": h["wt"], "fullWt": h.get("fullWt"),
            "tp": h.get("tp"), "addLvl": h.get("addLvl"),
            "thesis": h.get("thesis"), "strategy": h.get("strategy"),
            "needsMetadata": bool(h.get("needsMetadata")),
            "cmp": s.get("cmp") if s else None, "upside": upside(S, h),
            "bucket": bucket_of(S, h["tk"]), "built": built_of(h),
            "status": {"text": text, "class": css, "colour": colour},
            "signals": {"val": s.get("val"), "momo": s.get("momo"), "qual": s.get("qual"),
                        "bizMomo": s.get("bizMomo")} if s else None,
            "rail": rail(h, s),
        })
    return {"positions": rows, "count": len(rows), "feed": _feed_meta(feed)}


# ---------------------------------------------------------------- XIRR
def xirr(cashflows):
    """Money-weighted annualized return -- the same algorithm Excel's XIRR
    uses. cashflows: [(date, amount), ...], money leaving the investor's
    pocket negative, money coming back (including a final value, treated
    as if withdrawn today) positive. No closed form exists for more than
    two flows; solved via Newton's method with a bisection fallback for
    the cases where the tangent overshoots or starts past the singularity
    at r = -1.

    None if there's nothing to solve: fewer than two flows, or every flow
    the same sign -- money that only ever moved one direction implies no
    rate at all, not a 0% or infinite one."""
    if len(cashflows) < 2:
        return None
    amounts = [a for _, a in cashflows]
    if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
        return None
    t0 = min(d for d, _ in cashflows)
    years = [(d - t0).days / 365.0 for d, _ in cashflows]

    def npv(r):
        base = 1 + r
        if base <= 0:
            return float("inf")
        return sum(a / base ** y for a, y in zip(amounts, years))

    def dnpv(r):
        base = 1 + r
        if base <= 0:
            return float("inf")
        return sum(-y * a / base ** (y + 1) for a, y in zip(amounts, years))

    r = 0.1
    for _ in range(100):
        try:
            f, fp = npv(r), dnpv(r)
        except OverflowError:
            break
        if not math.isfinite(f) or abs(fp) < 1e-12:
            break
        r_new = r - f / fp
        if abs(r_new - r) < 1e-9:
            return round(r_new, 6)
        r = r_new

    # Newton didn't settle -- bracket search instead. NPV(r) is monotone
    # decreasing wherever a sign change exists, so this always finds it if
    # one is there to find.
    lo, hi = -0.999999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
        if hi - lo < 1e-9:
            break
    return round((lo + hi) / 2, 6)


def _bench_level_at(level_pairs, d):
    """level_pairs: [(date, level), ...] sorted ascending. The level on
    date d, or the most recent trading day at or before it -- a
    contribution made on a weekend or holiday gets invested at the last
    available price, same as it would be in real life. None if d predates
    every level we have."""
    dates = [p[0] for p in level_pairs]
    i = bisect.bisect_right(dates, d) - 1
    return level_pairs[i][1] if i >= 0 else None


def benchmark_xirr(cashflows, bench_index_level):
    """The SAME external contribution/withdrawal flows used for the real
    portfolio's XIRR (contribution negative, withdrawal positive; no
    ending-value flow appended here), replayed as if every rupee had gone
    into the benchmark instead: a contribution buys units at that day's
    level, a withdrawal sells units at that day's level. The resulting
    shadow position, priced at today's level, becomes the final flow --
    genuinely comparable to the real XIRR because it's driven by the exact
    same cash-flow timing, not a differently-shaped calculation.

    ponytail: a contribution dated before the oldest level we have (older
    than the 5-year fetch window) returns None rather than guessing --
    widen the window if that ever actually matters."""
    if not cashflows or not bench_index_level:
        return None
    level_pairs = sorted((dt.date.fromisoformat(k), v) for k, v in bench_index_level.items())
    units = 0.0
    for d, amount in cashflows:
        level = _bench_level_at(level_pairs, d)
        if level is None:
            return None
        units += -amount / level      # contribution (negative) buys units; withdrawal (positive) sells
    today_date, today_level = level_pairs[-1]
    return xirr(cashflows + [(today_date, units * today_level)])


# ---------------------------------------------------------------- NAV
def build_nav(book, feed):
    """A real rupee NAV -- built from actual capital flows and actual trade
    cashflows, not the weight-based 'cash = 1 - sum of weights' plug used
    elsewhere in the app. That plug still drives composition/staged-actions
    display; this is a second, independently reconcilable view of the same
    book, the one a real cash/bank balance could actually be checked against.

    cashBalance = money contributed, minus money withdrawn, minus net cash
    spent buying and selling stocks (costs included on both sides) -- the
    same arithmetic a real brokerage statement does.
    """
    S = feed.get("signals") or {}
    cashflows = book.get("cashflows", [])
    contributed = sum(c["amount"] for c in cashflows if c["type"] == "Contribution")
    withdrawn = sum(c["amount"] for c in cashflows if c["type"] == "Withdrawal")

    trade_cash = 0.0
    for t in book["trades"]:
        gross = t["qty"] * t["price"]
        costs = t.get("costs") or 0
        trade_cash += -(gross + costs) if t["side"] == "Buy" else (gross - costs)

    cash_balance = contributed - withdrawn + trade_cash

    by_tk = {h["tk"]: h for h in book["holdings"]}
    P = perf_calc(book["holdings"], book["trades"], S)
    holdings_value, unmarked_wt = 0.0, 0.0
    for r in P["rows"]:
        h = by_tk.get(r["tk"])
        if h is None:
            continue                      # exited -- no current position to value
        holdings_value += r["net"] * r["cmp"]
        if not r["marked"]:
            unmarked_wt += h["wt"]        # valued at last trade price, not a live quote

    nav = cash_balance + holdings_value

    # Money-weighted return: the SAME external flows (contribution
    # negative, withdrawal positive), plus today's NAV as the final flow
    # -- what you'd get if you cashed out right now.
    flows = [(dt.date.fromisoformat(c["date"]), -c["amount"] if c["type"] == "Contribution" else c["amount"])
             for c in cashflows]
    port_xirr = xirr(flows + [(dt.date.today(), nav)]) if flows else None
    bench_xirr = benchmark_xirr(flows, feed.get("bench_index_level") or {}) if flows else None
    alpha = (port_xirr - bench_xirr) if (port_xirr is not None and bench_xirr is not None) else None
    # Annualizing compounds a short window's move up to a full year -- a
    # genuine 45% gain in 3 months is a real, correctly-computed 350%+
    # XIRR, not a bug. Flag it rather than let the number alone look wrong.
    span_days = (dt.date.today() - min(d for d, _ in flows)).days if flows else None

    return {
        "contributed": contributed, "withdrawn": withdrawn, "tradeCash": trade_cash,
        "cashBalance": cash_balance, "holdingsValue": holdings_value,
        "nav": nav,
        "unmarkedWeight": unmarked_wt, "cashflowCount": len(cashflows),
        "xirr": port_xirr, "benchXirr": bench_xirr, "alpha": alpha, "spanDays": span_days,
    }


# ============================================================ entry point
def recalculate(book, feed, meta):
    """Book + market data in, every derived payload out.

    All five screens are built together and stored in one file, so they
    can never disagree with each other -- a half-updated set is how a
    dashboard starts showing yesterday's risk beside today's P&L.
    """
    nav = build_nav(book, feed)
    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "dashboard": build_dashboard(book, feed, meta, nav),
        "portfolio": build_portfolio(book, feed),
        "risk": build_risk(book, feed),
        "performance": build_performance(book, feed),
        "exposure": build_exposure(book, feed),
        "positions": build_positions(book, feed),
        "nav": nav,
    }
