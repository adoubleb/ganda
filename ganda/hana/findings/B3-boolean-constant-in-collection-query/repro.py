"""B3 -- the Document Store rejects a BOOLEAN constant (CASE ... THEN TRUE, = TO_JSON_BOOLEAN
comparisons producing a boolean) in any query that reads a collection; the cast must live
one CTE removed from the collection read. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b3_pc"


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
        for d in ({"id": "a", "b": True}, {"id": "b", "b": False}):
            cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps(d)])
        # CASE producing a BOOLEAN constant in a scope that reads the collection -> rejected
        run(cur, "CASE..THEN TRUE over collection", f"SELECT \"id\" AS id, CASE \"b\" WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS bb FROM {C}")
        run(cur, "same, in a subquery over coll ", f"SELECT id, CASE b WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS bb FROM (SELECT \"id\" AS id, \"b\" AS b FROM {C}) z")
        # the cast one CTE removed from the collection -> accepted
        run(cur, "CASE one CTE removed (two CTEs)", f"WITH raw AS (SELECT \"id\" AS id, \"b\" AS b FROM {C}), v AS (SELECT id, CASE b WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS b FROM raw) SELECT * FROM v ORDER BY id")
        print("\n  => a BOOLEAN constant is refused while the query still reads the collection; a CTE boundary fixes it.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
