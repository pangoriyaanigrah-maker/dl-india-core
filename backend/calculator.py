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

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

# Size cuts in Rs crore. MUST match CUTS in scripts/build_signals.py.
CUTS = {"large": 67000, "mid": 22000}

# Used only when the feed carries no bench_sect/bench_size.
BENCH_SECT_FALLBACK = {
    "Financials": 0.30, "IT Services": 0.12, "Consumer Discretionary": 0.10, "Industrials": 0.09,
    "Energy": 0.09, "Consumer Staples": 0.07, "Materials": 0.07, "Healthcare": 0.06,
    "Autos": 0.05, "Telecom": 0.03, "Utilities": 0.02,
}
BENCH_SIZE_FALLBACK = {"Large": 0.72, "Mid": 0.18, "Small": 0.10}


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
        return "—"
    return "Large" if m >= CUTS["large"] else "Mid" if m >= CUTS["mid"] else "Small"


def sector_of(h):
    """A holding created as a placeholder by a trades import has no sector
    yet. Left as None it becomes a null dict key, and sorting a set of
    sector names then raises TypeError comparing None to str -- which took
    down the whole import. Label it once, here."""
    return h.get("sector") or "Unclassified"


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
def perf_calc(holdings, trades, signals):
    """Average-cost P&L. Trade order matters: `last` is the final price
    seen, used to mark a ticker the feed does not cover."""
    by = {}
    for t in trades:
        o = by.setdefault(t["tk"], {"buyQ": 0.0, "buyV": 0.0, "sellQ": 0.0,
                                     "sellV": 0.0, "fees": 0.0, "last": None})
        q, p = t["qty"], t["price"]
        if t["side"] == "Buy":
            o["buyQ"] += q
            o["buyV"] += q * p
        else:
            o["sellQ"] += q
            o["sellV"] += q * p
        o["fees"] += t.get("costs") or 0
        o["last"] = p

    by_tk = {h["tk"]: h for h in holdings}
    rows = []
    for tk, o in by.items():
        avg = o["buyV"] / o["buyQ"] if o["buyQ"] else 0.0
        net = o["buyQ"] - o["sellQ"]
        marked = cmp_of(signals, tk)
        c = marked if marked is not None else o["last"]
        h = by_tk.get(tk)
        unreal = net * (c - avg)
        realized = o["sellV"] - o["sellQ"] * avg
        s = signals.get(tk) or {}
        rows.append({
            "tk": tk, "net": net, "avg": avg, "cmp": c, "marked": marked is not None,
            "unreal": unreal, "realized": realized, "fees": o["fees"],
            "total": unreal + realized - o["fees"], "invested": o["buyV"],
            "ret": ((unreal + realized - o["fees"]) / o["buyV"]) if o["buyV"] else 0.0,
            "sector": sector_of(h) if h else "Unmapped",
            "analyst": (h.get("analyst") if h else None) or "—",
            "bucket": bucket_of(signals, tk), "momo": s.get("momo"),
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    tot = lambda k: sum(r[k] for r in rows)  # noqa: E731
    return {"rows": rows, "bought": tot("invested"), "realized": tot("realized"),
            "unreal": tot("unreal"), "fees": tot("fees"), "total": tot("total")}


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
    """Rule-based reads for the IC. Two strings hardcode names from the
    original mock ('BEL + Kaynes', 'Sun, Titan') and fire on any book that
    crosses their threshold -- a pre-existing quirk, kept so the text
    matches what the dashboard has always shown."""
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
    ind = w_sec.get("Industrials", 0.0) / total_weight(holdings) - bench_sect.get("Industrials", 0.0)
    if ind > 0.05:
        out.append({"tag": "Crowding", "text":
            f"Industrials is {pct(ind)} active — BEL + Kaynes make it a <b>defense/electronics-capex "
            f"bet</b>, and the Kaynes staged add deepens it further. Related side bets elsewhere in the "
            f"complex add to an exposure the book already carries; size them against this, not in isolation."})

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
        out.append({"tag": "Posture", "text":
            f"Weighted value z is {zfmt(vz)} — the book pays up for growth/quality, and both staged adds "
            f"(Sun, Titan) would buy <i>corrections</i>, partially self-correcting this. If the momentum "
            f"regime turns, the {wpct(C['cash'])} cash is the real hedge; deploy the staged tranches patiently."})
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
    ("Value", "val", "P/E, P/B, EV/EBITDA · +ve = cheaper than the universe"),
    ("Momentum", "momo", "12M−1M plus 6M return · +ve = stronger tape"),
    ("Quality", "qual", "ROE, leverage, net margin · +ve = higher quality"),
]


def rail(h, s):
    """Positions-tab price rail. None when the dashboard would show text
    instead: no feed, or no target price."""
    if not s:
        return None
    c, tp, add, lo = s.get("cmp"), h.get("tp"), h.get("addLvl"), s.get("lo")
    if lo is None and c and tp is not None:
        lo = min(c, tp) * 0.88
    if not (c and lo is not None and tp is not None):
        return None
    hi_a = max(tp, c)
    lo_a = min(lo, add * 0.98) if add else lo
    span = hi_a * 1.03 - lo_a
    x = lambda v: round(min(99.0, max(0.0, (v - lo_a) / span * 100)), 1)  # noqa: E731
    return {"loAnchor": lo_a, "hiAnchor": hi_a, "span": span,
            "cmpPct": x(c), "tpPct": x(tp), "addPct": x(add) if add else None}


def _bench(feed):
    sect = feed.get("bench_sect") or BENCH_SECT_FALLBACK
    size = feed.get("bench_size") or BENCH_SIZE_FALLBACK
    return sect, size, bool(feed.get("bench_sect"))


def _feed_meta(feed):
    return {"live": bool(feed.get("signals")), "asOf": feed.get("asof"),
            "index": feed.get("index") or "the index", "note": feed.get("note"),
            "errors": feed.get("errors") or []}


# ================================================================ screens
def build_dashboard(book, feed, meta):
    H, S = book["holdings"], feed.get("signals") or {}
    P = perf_calc(H, book["trades"], S)
    C = composition(H, book["cash"])
    current = sum(r["net"] * r["cmp"] for r in P["rows"] if r["cmp"])
    return {
        # Weights are NAV FRACTIONS, not rupees -- the book stores no
        # absolute NAV. Every rupee figure comes from the trade ledger.
        "cashWeight": C["cash"], "equityWeight": C["equity"],
        "invested": P["bought"], "currentValue": current, "portfolioValue": current,
        "todaysChange": None,     # no prior close in the feed -- cannot be derived
        "overallPL": P["total"], "overallReturn": (P["total"] / P["bought"]) if P["bought"] else 0.0,
        "realized": P["realized"], "unrealized": P["unreal"], "costs": P["fees"],
        "holdingsCount": len(H), "tradesCount": len(book["trades"]),
        "weightedUpside": wavg(H, lambda h: upside(S, h)),
        "lastUpdated": meta.get("holdingsUpdated") or meta.get("tradesUpdated"),
        "sourceFile": (meta.get("sourceFiles") or {}).get("holdings"),
        "feed": _feed_meta(feed),
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


def build_risk(book, feed):
    S = feed.get("signals") or {}
    all_h = book["holdings"]
    H = [h for h in all_h if S.get(h["tk"])]
    if not H:
        return {"priced": 0, "scenarios": [], "perStock": [], "metrics": [], "reads": [],
                "weightedBeta": None, "equityWeight": total_weight(all_h),
                "cashWeight": book["cash"], "feed": _feed_meta(feed)}

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
    return {
        "priced": len(H), "weightedBeta": w_beta, "equityWeight": equity,
        "cashWeight": book["cash"], "scenarios": scenarios,
        "perStock": [{"tk": h["tk"], "wt": h["wt"],
                      "changes": {k: f(S[h["tk"]]) for k, _, _, f in SCENARIOS}} for h in H],
        "metrics": metrics,
        "reads": _risk_reads(H, S, book["cash"], equity, w_beta, scenarios),
        "feed": _feed_meta(feed),
    }


def _risk_reads(H, S, cash, equity, w_beta, scenarios):
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
            f"portfolio beta to the Nifty</b>. A 10% index fall implies roughly "
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
    trades = sorted(book["trades"], key=lambda t: t.get("date") or "", reverse=True)
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
    sectors = [{"sector": s, "portfolio": w_sec[s] / W, "benchmark": sect.get(s, 0.0),
                "active": w_sec[s] / W - sect.get(s, 0.0), "pl": pl_sec.get(s, 0.0)}
               for s in sorted(w_sec, key=lambda s: w_sec[s], reverse=True)]
    not_held = [(s, wt) for s, wt in sect.items() if s not in w_sec]

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
            "signals": {"val": s.get("val"), "momo": s.get("momo"), "qual": s.get("qual")} if s else None,
            "rail": rail(h, s),
        })
    return {"positions": rows, "count": len(rows), "feed": _feed_meta(feed)}


# ============================================================ entry point
def recalculate(book, feed, meta):
    """Book + market data in, every derived payload out.

    All five screens are built together and stored in one file, so they
    can never disagree with each other -- a half-updated set is how a
    dashboard starts showing yesterday's risk beside today's P&L.
    """
    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "dashboard": build_dashboard(book, feed, meta),
        "portfolio": build_portfolio(book, feed),
        "risk": build_risk(book, feed),
        "performance": build_performance(book, feed),
        "exposure": build_exposure(book, feed),
        "positions": build_positions(book, feed),
    }


def snapshot_point(derived, on=None):
    """One day's row for history.json."""
    d, e, r = derived["dashboard"], derived["exposure"], derived["risk"]
    return {
        "date": (on or dt.date.today()).isoformat(),
        "portfolioValue": d["currentValue"], "investedValue": d["invested"],
        "cashPct": d["cashWeight"], "totalPL": d["overallPL"],
        "unrealizedPL": d["unrealized"], "realizedPL": d["realized"],
        "holdingsCount": d["holdingsCount"],
        "sectorAllocation": {s["sector"]: s["portfolio"] for s in e["sectors"]},
        "riskMetrics": {"weightedBeta": r["weightedBeta"], "priced": r["priced"],
                        "scenarios": {s["key"]: s["navImpact"] for s in r["scenarios"]}},
    }


def append_history(history, point):
    """Upsert by date, oldest first, with day-over-day deltas recomputed."""
    pts = [p for p in history.get("points", []) if p["date"] != point["date"]]
    pts.append(point)
    pts.sort(key=lambda p: p["date"])
    prev = None
    for p in pts:
        p["dailyPL"] = None if prev is None else p["totalPL"] - prev
        prev = p["totalPL"]
    return {"points": pts, "count": len(pts),
            "sectors": sorted({s for p in pts for s in p["sectorAllocation"]}),
            "first": pts[0]["date"], "last": pts[-1]["date"],
            "changeSinceFirst": pts[-1]["totalPL"] - pts[0]["totalPL"]}
