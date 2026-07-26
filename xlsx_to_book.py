"""Holdings_File.xlsx (Portfolio sheet) -> book.json, the dashboard's editable store.

Human-owned data only: weights, targets, conviction, thesis, strategy.
Prices / z-scores stay in signals.json, written by build_signals.py.

    python xlsx_to_book.py [Holdings_File.xlsx] [book.json]
"""
import json
import sys
from datetime import date

import openpyxl

FIELDS = {  # sheet header -> book.json key
    "Ticker": "tk", "Company": "co",
    # Sector is coarse and drives the active-weight table, so its values must
    # match the bench_sect keys build_signals.py emits. Industry is the
    # analyst's thematic label and is display-only — an end-market view
    # (MANINDS as "Oil & Gas Pipes") that no index taxonomy agrees with.
    "Sector": "sector", "Industry": "industry",
    "Analyst": "analyst",
    "Weight": "wt", "FullWeight": "fullWt", "Target": "tp", "AddLevel": "addLvl",
    "Conviction": "conv", "Thesis": "thesis", "Strategy": "strategy",
}
PCT = ("wt", "fullWt")


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", "").replace("%", "").strip())
    except ValueError:
        return None


def divisor(values):
    """Is this weight column written as percents (2.5) or fractions (0.025)?

    The whole column shares one convention, so decide once from the TOTAL: book
    weights sum to ~1 as fractions and ~100 as percents. Judging each cell
    against a 1.5 cutoff instead reads a legitimate 1.2% position as 120%.
    Mirrors the same rule in the dashboard's holdingsFromCSV.
    """
    total = sum(n for n in (num(v) for v in values) if n is not None)
    return 100.0 if total > 1.5 else 1.0


def text(v):
    s = str(v).strip() if v is not None else ""
    return s or None


def build(xlsx):
    ws = openpyxl.load_workbook(xlsx, data_only=True)["Portfolio"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [text(c) for c in rows[0]]
    missing = [h for h in FIELDS if h not in hdr]
    if missing:
        raise SystemExit(f"Portfolio sheet is missing columns: {', '.join(missing)}")

    body = [dict(zip(hdr, r)) for r in rows[1:]]
    body = [d for d in body if text(d.get("Ticker")) or text(d.get("Company"))]
    div = {"wt": divisor([d.get("Weight") for d in body]),
           "fullWt": divisor([d.get("FullWeight") for d in body])}

    holdings = []
    for d in body:
        h = {}
        for head, key in FIELDS.items():
            v = d.get(head)
            if key in PCT:
                n = num(v)
                h[key] = None if n is None else n / div[key]
            else:
                h[key] = num(v) if key in ("tp", "addLvl") else text(v)
        h["tk"] = (h["tk"] or h["co"]).upper()
        h["co"] = h["co"] or h["tk"]
        h["wt"] = h["wt"] or 0.0
        if h["fullWt"] is None:
            h["fullWt"] = h["wt"]
        holdings.append(h)

    equity = round(sum(h["wt"] for h in holdings), 6)
    return {
        "updated": date.today().isoformat(),
        "source": f"{xlsx} (Portfolio sheet)",
        "cash": round(1 - equity, 6),
        "holdings": holdings,
        "trades": [],
    }


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "Holdings_File.xlsx"
    dst = sys.argv[2] if len(sys.argv) > 2 else "book.json"
    book = build(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(book, f, indent=1, ensure_ascii=False)
    print(f"{dst}: {len(book['holdings'])} holdings, "
          f"equity {(1 - book['cash']) * 100:.1f}%, cash {book['cash'] * 100:.1f}%")
    for h in book["holdings"]:
        if h["tp"] is None:
            print(f"  ! {h['tk']}: no target — shows 'no target set' until one is entered")
