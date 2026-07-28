"""Local server for the dashboard: serves the folder, and saves book.sqlite.

`python -m http.server` can only read, so an upload could never reach disk and
build_signals.py never saw stocks added through the Import tab. This adds one
POST route and nothing else.

Bound to 127.0.0.1 on purpose: it writes a file, so it must not be reachable
from the rest of the network.

    python serve.py
"""
import os
import sqlite3
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
BOOK = "book.sqlite"


def replace_with_retry(src, dst, tries=10, delay=0.1):
    """os.replace(), retried briefly on WinError 5.

    Windows can hold a transient lock on a just-written file (Defender's
    on-write scan is the usual cause) that POSIX rename never has to worry
    about, so the very first save after start can fail here even though
    nothing is actually wrong. A few retries clear it; this is the ceiling —
    if it's still locked after ~1s, something else is genuinely wrong.
    """
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        # One hardcoded target, so there's no path to traverse.
        if self.path.split("?")[0].lstrip("/") != BOOK:
            self.send_error(404, f"Only {BOOK} can be saved")
            return
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size)

        # Validate structurally, not by row count: an intentionally empty
        # book (0 holdings, before anyone has uploaded anything) is a normal
        # state now, not something to reject. What must never land on disk is
        # a truncated or corrupt write, which would take the dashboard down.
        # Row counts are read here too, from tmp, before the replace — not by
        # reopening BOOK afterwards. sqlite3.connect used as a context manager
        # only commits/rolls back on exit, it does NOT close the connection,
        # so a reopen-after-replace here left a dangling handle on BOOK that
        # made every save after the first fail to lock it.
        tmp = tempfile.NamedTemporaryFile(dir=".", suffix=".sqlite", delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            con = sqlite3.connect(tmp.name)
            n = con.execute("SELECT count(*) FROM holdings").fetchone()[0]
            m = con.execute("SELECT count(*) FROM trades").fetchone()[0]
            con.close()
        except Exception as e:
            os.unlink(tmp.name)
            self.send_error(400, f"Not a valid book database ({e})")
            return

        try:
            replace_with_retry(tmp.name, BOOK)
        except PermissionError as e:
            os.unlink(tmp.name)
            self.send_error(500, f"{BOOK} is locked by another process ({e})")
            return
        print(f"saved {BOOK}: {n} holdings, {m} trades")

        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass                                    # quiet: only saves are worth printing


if __name__ == "__main__":
    print(f"Dashboard: http://localhost:{PORT}   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
