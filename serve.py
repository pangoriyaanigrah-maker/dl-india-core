"""Local server for the dashboard: serves the folder, and saves book.json.

`python -m http.server` can only read, so an upload could never reach disk and
build_signals.py never saw stocks added through the Import tab. This adds one
POST route and nothing else.

Bound to 127.0.0.1 on purpose: it writes a file, so it must not be reachable
from the rest of the network.

    python serve.py
"""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
BOOK = "book.json"


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        # One hardcoded target, so there's no path to traverse.
        if self.path.split("?")[0].lstrip("/") != BOOK:
            self.send_error(404, "Only book.json can be saved")
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            book = json.loads(self.rfile.read(size))
            if not isinstance(book.get("holdings"), list) or not book["holdings"]:
                raise ValueError("no holdings in payload")
        except Exception as e:
            # Refuse rather than truncate: a half-written book.json would take
            # the dashboard down and lose the real one.
            self.send_error(400, f"Not a valid book ({e})")
            return

        with open(BOOK, "w", encoding="utf-8") as f:
            json.dump(book, f, indent=1, ensure_ascii=False)
        print(f"saved {BOOK}: {len(book['holdings'])} holdings, "
              f"{len(book.get('trades') or [])} trades")

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
