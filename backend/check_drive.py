"""Check the Google Drive connection and say exactly what is wrong.

    python backend/check_drive.py

Diagnoses the three things that actually go wrong, in the order they bite:
credentials missing, Drive API not enabled, folder not shared.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    import drive

    print("1. credentials")
    if os.environ.get("DRIVE_LOCAL_DIR"):
        print(f"   using the local folder backend: {os.environ['DRIVE_LOCAL_DIR']}")
        print("   (unset DRIVE_LOCAL_DIR to test the real Google Drive)")
    try:
        creds = drive._credentials()
    except drive.DriveError as e:
        print(f"   FAIL {e}")
        return 1
    if creds is None and not os.environ.get("DRIVE_LOCAL_DIR"):
        print("   FAIL no credentials found.")
        print("        Put service-account.json next to main.py, or set")
        print("        GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_APPLICATION_CREDENTIALS.")
        return 1
    if creds is not None:
        print(f"   ok   service account: {creds.service_account_email}")

    print("2. Drive API + folder")
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    print(f"   DRIVE_FOLDER_ID = {folder_id or '(unset — the service account will own the folder)'}")
    try:
        store = drive.connect()
    except drive.DriveError as e:
        msg = str(e)
        print(f"   FAIL {msg}")
        if "accessNotConfigured" in msg or "has not been used" in msg:
            print("\n        The Drive API is not enabled on this project. Enable it at:")
            print("        https://console.cloud.google.com/apis/library/drive.googleapis.com")
        elif "404" in msg or "notFound" in msg:
            print("\n        That folder id does not exist, or is not shared with the")
            print("        service account. Share it as Editor with the email above.")
        elif "403" in msg:
            print("\n        Permission denied — share the folder as Editor with the")
            print("        service account email above.")
        return 1

    print("   ok   connected")
    print("3. files")
    for name in store.list_names():
        print(f"   - {name}")

    book = drive.read_json(drive.PORTFOLIO_JSON) or {}
    print(f"\n   holdings: {len(book.get('holdings') or [])}   trades: {len(book.get('trades') or [])}")
    print("\nconnection ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
