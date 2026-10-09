"""B11 -- START is a reserved word and cannot be used as a column alias (syntax error 257).
Run: python3 -I repro.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    run(cur, "SELECT 1 AS start FROM DUMMY", "SELECT 1 AS start FROM DUMMY")
    run(cur, "SELECT 1 AS strt  FROM DUMMY", "SELECT 1 AS strt FROM DUMMY")
    run(cur, 'SELECT 1 AS "start" (quoted)', 'SELECT 1 AS "start" FROM DUMMY')
    print("\n  => bare `start` as an alias is a syntax error; `strt` (or a quoted identifier) works.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
