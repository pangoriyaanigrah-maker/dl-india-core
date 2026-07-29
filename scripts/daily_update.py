"""daily_update.py — the evening job. Run once per weekday after close.

    fetch market data  ->  signals.json  ->  recalculate  ->  history point

Everything lands on Google Drive. There is no database.

Failure policy:
  1. Never corrupt what is stored. Nothing is written until the fetch has
     returned real data; a crash leaves every file exactly as it was.
  2. Keep yesterday's data. A failed run leaves the previous signals.json
     and dashboard.json in place -- stale, but correct and clearly dated.
  3. Log the error and exit non-zero, so a CI run goes red.

An empty price fetch counts as failure, not as "nothing is priced today":
writing an empty feed would blank every price on the dashboard.

    python scripts/daily_update.py
    python scripts/daily_update.py --recalc-only    # no network
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

import calculator                                        # noqa: E402
import drive                                             # noqa: E402
from drive import (DASHBOARD_JSON, HISTORY_JSON, METADATA_JSON,   # noqa: E402
                    PORTFOLIO_JSON, SIGNALS_JSON)

log = logging.getLogger("dl.daily")


def run(fetch: bool = True, on: dt.date | None = None) -> int:
    t0 = time.perf_counter()
    try:
        drive.connect()

        feed = drive.read_json(SIGNALS_JSON) or {}
        if fetch:
            import build_signals
            log.info("fetching market data…")
            fresh = build_signals.build()
            if not (fresh.get("signals") or {}):
                raise RuntimeError("price fetch returned no signals — refusing to write an empty feed")
            # Only now is anything written. Up to this point a failure has
            # touched nothing at all.
            drive.write_json(SIGNALS_JSON, fresh)
            feed = fresh
            log.info("signals: %d tickers, %d unresolved",
                     len(feed["signals"]), len(feed.get("errors") or []))
        else:
            log.info("--recalc-only: reusing the stored feed")

        book = drive.read_json(PORTFOLIO_JSON) or {"holdings": [], "trades": [], "cash": 0.0}
        meta = drive.read_json(METADATA_JSON) or {}
        meta["signalsUpdated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

        derived = calculator.recalculate(book, feed, meta)
        drive.write_json(DASHBOARD_JSON, derived)
        drive.write_json(METADATA_JSON, meta)

        history = calculator.append_history(
            drive.read_json(HISTORY_JSON) or {}, calculator.snapshot_point(derived, on))
        drive.write_json(HISTORY_JSON, history)

        d = derived["dashboard"]
        log.info("done in %.1fs — %d holdings, value %.0f, P&L %.0f, %d history points",
                 time.perf_counter() - t0, d["holdingsCount"], d["currentValue"],
                 d["overallPL"], history["count"])
        return 0
    except Exception as e:
        log.error("daily update FAILED, stored files unchanged — yesterday's data is still live: %s", e)
        log.debug("traceback", exc_info=True)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recalc-only", action="store_true", help="skip the price fetch")
    ap.add_argument("--date", help="history date (YYYY-MM-DD), default today")
    a = ap.parse_args()
    sys.exit(run(fetch=not a.recalc_only,
                 on=dt.date.fromisoformat(a.date) if a.date else None))
