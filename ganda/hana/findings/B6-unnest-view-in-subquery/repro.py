"""B6 -- the Document Store refuses an UNNEST-based view NESTED inside a subquery (IN (...),
or an EXISTS over a double-UNNEST edge-element view). A single-UNNEST node view inside
EXISTS is accepted. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b6_n5"


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
        # a node with a scalar list, and an edge-link array whose links carry a list attr
        cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": "a", "arr": [1, 2], "e": [{"eid": "x", "to": "b", "f": [6, 7]}]})])
        node_el = f'(SELECT $."id" AS owner, "y" AS v FROM {C} UNNEST $."arr" AS "y")'
        edge_el = f'(SELECT "l"."eid" AS owner, "z" AS v FROM {C} UNNEST $."e" AS "l" UNNEST "l"."f" AS "z")'
        node = f'(SELECT $."id" AS id FROM {C})'
        edge = f'(SELECT "l"."eid" AS eid FROM {C} UNNEST $."e" AS "l")'
        run(cur, "EXISTS over single-UNNEST (node)", f"WITH el AS {node_el}, nd AS {node} SELECT nd.id FROM nd WHERE EXISTS (SELECT 1 FROM el x WHERE x.owner = nd.id AND x.v = 1)")
        run(cur, "IN (subquery over UNNEST view)  ", f"WITH el AS {node_el} SELECT owner FROM el WHERE owner IN (SELECT owner FROM el WHERE v = 1)")
        run(cur, "EXISTS over double-UNNEST (edge)", f"WITH el AS {edge_el}, eg AS {edge} SELECT eg.eid FROM eg WHERE EXISTS (SELECT 1 FROM el x WHERE x.owner = eg.eid AND x.v IN (6,7))")
        print("\n  => a single-UNNEST view inside EXISTS is fine; IN(subquery) and the double-UNNEST edge EXISTS are refused.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
