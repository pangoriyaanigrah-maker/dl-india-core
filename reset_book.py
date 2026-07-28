"""reset_book.py -- wipe book.sqlite back to empty. Run by hand, never from the app.

Deleting data is deliberately NOT a button in the dashboard: one accidental
click plus one confirm() is too easy a way to erase a real book. This script
is the only way to clear it, and it lives outside the running app on purpose
-- clearing data is a conscious, separate action, not a feature of the site.

    python reset_book.py              # asks for confirmation, then wipes it
    python reset_book.py --selftest   # checks the schema logic, no prompt, no real file touched

Only touches the local book.sqlite file. If GitHub is your permanent store,
commit and push the change afterwards like any other file:
    git add book.sqlite && git commit -m "reset book" && git push
"""
import os
import sqlite3
import sys
import tempfile

BOOK = "book.sqlite"

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE holdings(tk TEXT PRIMARY KEY, co TEXT, sector TEXT, industry TEXT, analyst TEXT,
  wt REAL, fullWt REAL, tp REAL, addLvl REAL, yfSymbol TEXT, conv TEXT, thesis TEXT, strategy TEXT);
CREATE TABLE trades(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, tk TEXT, side TEXT,
  qty REAL, price REAL, costs REAL);
"""


def write_empty(path):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO meta VALUES ('cash', '0')")
    con.commit()
    con.close()


def counts(path):
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM holdings").fetchone()[0]
    m = con.execute("SELECT count(*) FROM trades").fetchone()[0]
    con.close()
    return n, m


def selftest():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "test.sqlite")
        write_empty(p)
        assert counts(p) == (0, 0)
        con = sqlite3.connect(p)
        con.execute("INSERT INTO holdings(tk) VALUES ('X')")
        con.commit(); con.close()
        assert counts(p) == (1, 0)
        write_empty(p)                       # a second wipe must still work cleanly
        assert counts(p) == (0, 0)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    if not os.path.exists(BOOK):
        sys.exit(f"{BOOK} doesn't exist here -- nothing to reset.")
    n, m = counts(BOOK)
    print(f"{BOOK} currently has {n} holdings and {m} trades.")
    if input("Type YES to permanently wipe it: ") != "YES":
        sys.exit("Cancelled -- nothing changed.")
    write_empty(BOOK)
    print(f"{BOOK} reset to empty.")
