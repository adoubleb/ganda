"""B4 -- a collection-filter LIKE right-hand side must be a constant string literal, not a
parameter. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b4_pc"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.execute(f"CREATE COLLECTION {C}")
        for d in ({"id": "a", "s": "xyz"}, {"id": "b", "s": "yz"}):
            cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps(d)])
        run(cur, "LIKE CAST(? AS NVARCHAR)   ", f"SELECT \"id\" AS id FROM {C} WHERE \"s\" LIKE CAST(? AS NVARCHAR(5000))", ["y%"])
        run(cur, "LIKE TO_NVARCHAR(?)        ", f"SELECT \"id\" AS id FROM {C} WHERE \"s\" LIKE TO_NVARCHAR(?)", ["y%"])
        run(cur, "LIKE 'y%' (constant literal)", f"SELECT \"id\" AS id FROM {C} WHERE \"s\" LIKE 'y%'")
        print("\n  => only a constant string literal (or TO_NVARCHAR/UPPER/LOWER of one) is accepted on LIKE's RHS.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
