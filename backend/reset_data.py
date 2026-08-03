"""reset_data.py -- wipe the book back to empty. Run by hand, never from the app.

Deleting data is deliberately NOT a button in the dashboard: one accidental
click plus one confirm() is too easy a way to erase a real book. This
script is the only way to clear it, and it lives outside the running app
on purpose.

This REPLACES each file's content rather than deleting the file. A Google
service account has no storage quota of its own (see
docs/GOOGLE_DRIVE_SETUP.md) -- it can update a file that exists but cannot
create a new one. Delete portfolio.json/dashboard.json/etc. by hand on
Drive and the next startup's attempt to recreate the missing file fails
outright, taking the whole app down with it. Emptying the content sidesteps
that entirely: the file never stops existing.

Clears the BOOK by default -- portfolio.json, dashboard.json,
metadata.json. signals.json (market data) is left alone:
prices are not yours to lose, and clearing them blanks every price on the
dashboard until the evening job runs again. Pass --all to clear that too.
The original holdings.xlsx/trades.xlsx are left on Drive as harmless
archival copies -- nothing reads them to compute anything.

    python backend/reset_data.py          # book only, asks first
    python backend/reset_data.py --all    # book + signals feed
    python backend/reset_data.py --yes    # skip the confirmation prompt
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drive  # noqa: E402

BOOK_FILES = [drive.PORTFOLIO_JSON, drive.DASHBOARD_JSON, drive.METADATA_JSON]


def main() -> int:
    clear_all = "--all" in sys.argv
    skip_confirm = "--yes" in sys.argv
    targets = BOOK_FILES + ([drive.SIGNALS_JSON] if clear_all else [])

    try:
        drive.connect()
    except drive.DriveError as e:
        print(f"cannot reach storage: {e}")
        return 1

    print("This will clear:")
    for name in targets:
        print(f"  {name}")
    if not clear_all:
        print(f"  ({drive.SIGNALS_JSON} kept -- pass --all to clear the market feed too)")

    if not skip_confirm:
        if input("\nType YES to permanently wipe the above: ") != "YES":
            print("Cancelled -- nothing changed.")
            return 0

    for name in targets:
        drive.write_json(name, drive._empty(name))
        print(f"  cleared {name}")

    print("\ndone. Upload a holdings file on the Import tab to start again.")
    print("Changed your mind? Drive keeps a version history per file --")
    print("right-click it on Drive -> Manage versions -> restore.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
