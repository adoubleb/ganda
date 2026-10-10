"""A5 / H5 -- over a Document Store field read through SQL, `f BETWEEN ? AND ?` types its
parameters BIGINT and fails on a string, while `f >= ? AND f <= ?` (what BETWEEN means)
answers. The field holds strings only. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

S = "A5_REPRO"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur))
    try:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
    except Exception:
        pass
    cur.execute(f"CREATE SCHEMA {S}")
    cur.execute(f"SET SCHEMA {S}")
    try:
        cur.execute("CREATE COLLECTION c")
        for d in ({"f": "abc"}, {"f": "xyz"}):
            cur.execute("INSERT INTO c VALUES(?)", [json.dumps(d)])
        cur.execute("CREATE COLUMN TABLE t (f NVARCHAR(5000))")
        cur.execute("INSERT INTO t VALUES ('abc')")
        v = 'WITH raw AS (SELECT $."f" AS f FROM c) '
        print()
        run(cur, "collection field: f BETWEEN ? AND ?    ", v + "SELECT f FROM raw WHERE f BETWEEN ? AND ?", ["A", "i"])
        run(cur, "collection field: f >= ? AND f <= ?    ", v + "SELECT f FROM raw WHERE f >= ? AND f <= ?", ["A", "i"])
        run(cur, "collection field: BETWEEN 'A' AND 'i'  ", v + "SELECT f FROM raw WHERE f BETWEEN 'A' AND 'i'")
        run(cur, "collection field: TO_NVARCHAR(f) BETWEEN", v + "SELECT f FROM raw WHERE TO_NVARCHAR(f) BETWEEN ? AND ?", ["A", "i"])
        run(cur, "column table:     f BETWEEN ? AND ?    ", "SELECT f FROM t WHERE f BETWEEN ? AND ?", ["A", "i"])
        print("\n  => only BETWEEN with bound parameters over the collection field fails (parameter typed BIGINT).")
    finally:
        try:
            cur.execute(f"DROP SCHEMA {S} CASCADE")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
