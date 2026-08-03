"""Generate the starter files to upload into your Drive Portfolio folder.

Why this exists: a Google service account has NO storage quota of its own.
It can read and modify files you own, but it cannot create one -- Drive
rejects the upload with "Service Accounts do not have storage quota".
Shared drives are the documented fix, and they are a Google Workspace
feature; on a personal Gmail account there are none.

So the files get created once, by you, and the backend only ever updates
them after that. Updating a file you own is charged to your quota, not the
service account's, and works fine.

    python backend/make_starter_files.py
    -> writes ./drive_starter/, upload the contents to your Portfolio folder

You only ever do this once.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drive  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "drive_starter"


def main():
    OUT.mkdir(exist_ok=True)
    for name in drive.JSON_FILES:
        (OUT / name).write_text(
            json.dumps(drive._empty(name), indent=1, ensure_ascii=False), encoding="utf-8")

    # The uploaded workbooks hit the same wall: the backend REPLACES them on
    # every import, and replacing needs the file to already exist. A real
    # empty workbook, so anything that opens it sees a valid file rather
    # than a zero-byte one.
    import openpyxl
    for name, sheet in ((drive.HOLDINGS_XLSX, "Portfolio"), (drive.TRADES_XLSX, "Trades"),
                        (drive.CASHFLOWS_XLSX, "Cashflows")):
        wb = openpyxl.Workbook()
        wb.active.title = sheet
        wb.active["A1"] = "placeholder — replaced on your first import"
        wb.save(OUT / name)

    names = drive.JSON_FILES + [drive.HOLDINGS_XLSX, drive.TRADES_XLSX, drive.CASHFLOWS_XLSX]
    print(f"wrote {len(names)} files to {OUT}\n")
    for n in names:
        print(f"  {n}")
    print("\nUpload ALL of them into your Drive 'Portfolio' folder (drag the")
    print("whole folder contents in one go), then run:")
    print("  python backend/check_drive.py")
    print("\nYou only ever do this once. From then on the backend replaces the")
    print("contents in place, which a service account is allowed to do.")


if __name__ == "__main__":
    main()
