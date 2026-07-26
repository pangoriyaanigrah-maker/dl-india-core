"""Generate a test holdings workbook: same 15 stocks, randomised numbers.

Identity columns (Ticker/Company/Sector/Industry/Analyst/Thesis/Strategy) are
copied from book.json so the file is a drop-in for the Import tab. Only the
numbers move.

Targets are drawn around each name's real CMP from signals.json, and the weight
pattern is spread on purpose so the upload exercises the branches the live book
never reaches: trims, exits, target-hit, near-target and Low conviction.

    python make_test_xlsx.py
"""
import json
import random

import openpyxl

COLS = ["Ticker", "Company", "Sector", "Industry", "Analyst", "Weight",
        "FullWeight", "Target", "AddLevel", "Conviction", "Thesis", "Strategy"]

# (fullWeight rule, target multiple of CMP) — index i applies to holding i, so
# the mix is deterministic and every dashboard state is represented.
SHAPES = [
    ("add",  1.35), ("add",  1.22), ("full", 1.18), ("trim", 1.09),
    ("exit", 0.96), ("add",  1.41), ("full", 1.02), ("exit", 0.88),
    ("add",  1.27), ("trim", 1.15), ("full", 0.94), ("add",  1.31),
    ("full", 1.06), ("exit", 1.12), ("add",  1.19),
]
CONV = ["High", "Medium (Watchlist)", "Low"]


def build(out, seed):
    rnd = random.Random(seed)
    book = json.load(open("book.json", encoding="utf-8"))["holdings"]
    sigs = json.load(open("signals.json", encoding="utf-8"))["signals"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Portfolio"
    ws.append(COLS)

    total_w = 0.0
    for i, h in enumerate(book):
        shape, tmult = SHAPES[i % len(SHAPES)]
        wt = round(rnd.uniform(1.5, 5.5), 1)                  # percent
        full = {"add":  round(wt + rnd.uniform(1.0, 3.0), 1),
                "trim": round(wt * rnd.uniform(0.4, 0.7), 1),
                "exit": 0,
                "full": wt}[shape]
        cmp_ = (sigs.get(h["tk"]) or {}).get("cmp")
        # No price feed for this name? Fall back to its existing target, then to
        # a plain number — the sheet must never carry a blank where the test
        # needs one, or the branch it's probing silently doesn't run.
        base = cmp_ or h.get("tp") or 100
        target = round(base * tmult * rnd.uniform(0.97, 1.03), 2)
        # Price-gated adds get a trigger below spot; everything else is event-gated.
        add_lvl = round(base * rnd.uniform(0.86, 0.95), 2) if shape == "add" and i % 2 == 0 else None

        total_w += wt
        ws.append([h["tk"], h["co"], h["sector"], h.get("industry"), h["analyst"],
                   wt, full, target, add_lvl, rnd.choice(CONV),
                   h.get("thesis"), h.get("strategy")])

    for col, width in zip("ABCDEFGHIJKL", (12, 22, 20, 34, 10, 9, 11, 10, 10, 20, 60, 60)):
        ws.column_dimensions[col].width = width
    wb.save(out)
    return total_w, len(book)


OUT, SEED = "Holdings_File_TEST.xlsx", 7      # bump SEED to re-roll the numbers

if __name__ == "__main__":
    tot, n = build(OUT, SEED)
    print(f"{OUT}: {n} holdings, weights total {tot:.1f}% -> cash {100 - tot:.1f}%")
