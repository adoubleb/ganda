"""A4 / H4 -- SQL over Document Store collections: a SUM over a join that matches NO row
fails with [314] numeric overflow when the links the join excludes hold values that
exceed BIGINT together. The same query over column tables answers NULL.

Isolates the trigger: the join's rows (none), the SUM, the SUM as DECIMAL, the link view
reading the value column alone, and the column-table form. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

S = "A4_REPRO"


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
        # a start document with no link; a middle document whose two links carry 2^63 - 1 and 1
        for c in ("st", "mid"):
            cur.execute(f"CREATE COLLECTION {c}")
        cur.execute("INSERT INTO st VALUES(?)", [json.dumps({"id": "s1", "e1": []})])
        cur.execute("INSERT INTO mid VALUES(?)", [json.dumps({"id": "m1", "e2": [{"eid": "x1", "to": "t1", "w": 9223372036854775807}, {"eid": "x2", "to": "t1", "w": 1}]})])
        common = ('WITH v_mid AS (SELECT $."id" AS id FROM mid), '
                  'v_e1 AS (SELECT $."id" AS src_id, "l"."to" AS dst_id FROM st UNNEST $."e1" AS "l"), ')
        wide = common + ('raw_v_e2 AS (SELECT "l"."eid" AS eid, $."id" AS src_id, "l"."to" AS dst_id, "l"."w" AS w FROM mid UNNEST $."e2" AS "l"), '
                         'v_e2 AS (SELECT eid, src_id, dst_id, TO_BIGINT(w) AS w FROM raw_v_e2) ')
        narrow = common + ('raw_v_e2 AS (SELECT $."id" AS src_id, "l"."w" AS w FROM mid UNNEST $."e2" AS "l"), '
                           'v_e2 AS (SELECT src_id, TO_BIGINT(w) AS w FROM raw_v_e2) ')
        paths = "SELECT j1.src_id AS strt, j2.w AS v FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id"
        print()
        run(cur, "collections: the join's rows          ", wide + paths)
        run(cur, "collections: SUM over the join        ", wide + f"SELECT SUM(p.v) FROM ({paths}) p")
        run(cur, "collections: SUM(TO_DECIMAL(...))     ", wide + f"SELECT SUM(TO_DECIMAL(p.v)) FROM ({paths}) p")
        run(cur, "collections: link view, value only    ", narrow + f"SELECT SUM(p.v) FROM ({paths}) p")
        cur.execute("CREATE COLUMN TABLE r_mid (id NVARCHAR(10))")
        cur.execute("INSERT INTO r_mid VALUES ('m1')")
        cur.execute("CREATE COLUMN TABLE r_e1 (src_id NVARCHAR(10), dst_id NVARCHAR(10))")
        cur.execute("CREATE COLUMN TABLE r_e2 (eid NVARCHAR(10), src_id NVARCHAR(10), dst_id NVARCHAR(10), w BIGINT)")
        cur.execute("INSERT INTO r_e2 VALUES ('x1', 'm1', 't1', 9223372036854775807)")
        cur.execute("INSERT INTO r_e2 VALUES ('x2', 'm1', 't1', 1)")
        rpaths = "SELECT j1.src_id AS strt, j2.w AS v FROM r_e1 j1 JOIN r_mid t1 ON t1.id = j1.dst_id JOIN r_e2 j2 ON j2.src_id = t1.id"
        run(cur, "column tables: SUM over the join      ", f"SELECT SUM(p.v) FROM ({rpaths}) p")
        print("\n  => the join matches no row (SUM is NULL), yet over the collections the SUM overflows:")
        print("     the only values that can overflow are the two links the join excludes.")
    finally:
        try:
            cur.execute(f"DROP SCHEMA {S} CASCADE")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
