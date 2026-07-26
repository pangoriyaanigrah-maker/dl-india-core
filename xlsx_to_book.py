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


def frac(v):
    """2.5 -> 0.025, 0.025 -> 0.025. Mirrors frac() in the dashboard."""
    n = num(v)
    return None if n is None else (n / 100 if abs(n) > 1.5 else n)


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

    holdings = []
    for r in rows[1:]:
        d = dict(zip(hdr, r))
        if not text(d.get("Ticker")) and not text(d.get("Company")):
            continue  # blank row
        h = {}
        for head, key in FIELDS.items():
            v = d.get(head)
            h[key] = frac(v) if key in PCT else num(v) if key in ("tp", "addLvl") else text(v)
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
