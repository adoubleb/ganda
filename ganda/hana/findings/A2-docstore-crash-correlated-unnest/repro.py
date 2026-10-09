"""A2 / H2 -- the Document Store crashes (internal error, code 3) on a CORRELATED scalar
subquery whose inner query reads a collection through UNNEST.

Isolates the trigger: uncorrelated is fine, correlated over a non-UNNEST view is fine,
correlated over an UNNEST view crashes. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "a2_pc"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur))
    try:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.execute(f"CREATE COLLECTION {C}")
        cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": "a", "links": [{"to": "b"}]})])
        unnest = f'WITH node AS (SELECT $."id" AS id FROM {C}), link AS (SELECT $."id" AS src FROM {C} UNNEST $."links" AS "l") '
        plain = f'WITH node AS (SELECT $."id" AS id FROM {C}), other AS (SELECT $."id" AS k FROM {C}) '
        print()
        run(cur, "uncorrelated over UNNEST view", unnest + f"SELECT s.id, (SELECT COUNT(*) FROM link) AS v FROM node s")
        run(cur, "CORRELATED  over UNNEST view", unnest + f"SELECT s.id, (SELECT COUNT(*) FROM link p WHERE p.src = s.id) AS v FROM node s")
        run(cur, "correlated, no-UNNEST view  ", plain + f"SELECT s.id, (SELECT COUNT(*) FROM other p WHERE p.k = s.id) AS v FROM node s")
        print("\n  => only the CORRELATED scalar subquery over the UNNEST view crashes (code 3).")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
