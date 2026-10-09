"""B7 -- a GROUP BY inside a subquery over a collection is refused ("GROUP BY clause after
GROUP BY clause on collection tables"). The top-level GROUP BY form works.
Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b7_pc"


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
        for v in (1, 1, 2, 3):
            cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": f"r{v}{id(v)%97}", "f1": v})])
        run(cur, "COUNT(*) over (GROUP BY sub)", f"SELECT COUNT(*) AS v FROM (SELECT \"f1\" AS x FROM {C} GROUP BY \"f1\") z")
        run(cur, "COUNT(DISTINCT f1) top-level", f"SELECT COUNT(DISTINCT \"f1\") AS v FROM {C}")
        print("\n  => a GROUP BY inside a subquery over a collection is refused; the top-level distinct count works.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
