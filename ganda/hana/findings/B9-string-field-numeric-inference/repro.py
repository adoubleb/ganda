"""B9 -- CONTEXT-DEPENDENT: in a multi-view doc join the fuzzer hit
`[-10427] Conversion ... to BIGINT failed ... 'A'` on a STRING edge field (value "0")
compared with string BETWEEN bounds -- the Document Store inferred the uncast field as
numeric. It does NOT reproduce in the isolated forms below (they all succeed), so treat the
campaign bundle as the evidence, not this script. The provider casts string fields
TO_NVARCHAR defensively. Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b9_pc"


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
        cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": "s", "e5": [{"eid": "x", "to": "t", "f1": "0", "f3": 5}]})])
        raw = f'raw AS (SELECT "l"."eid" AS eid, "l"."f1" AS f1, TO_BIGINT("l"."f3") AS f3 FROM {C} UNNEST $."e5" AS "l")'
        print("  Isolated attempts (these do NOT reproduce the campaign error on this build):")
        run(cur, "uncast f1 string BETWEEN   ", f"WITH {raw}, v AS (SELECT eid, f1 FROM raw) SELECT eid FROM v t WHERE t.f1 BETWEEN 'A' AND 'i'")
        run(cur, "uncast f1 + TO_BIGINT f3   ", f"WITH {raw}, v AS (SELECT eid, f1, f3 FROM raw) SELECT eid FROM v t WHERE t.f1 BETWEEN 'A' AND 'i'")
        run(cur, "TO_NVARCHAR(f1) (the fix)  ", f"WITH {raw}, v AS (SELECT eid, TO_NVARCHAR(f1) AS f1 FROM raw) SELECT eid FROM v t WHERE t.f1 BETWEEN 'A' AND 'i'")
        print("\n  See README.md: the error was observed live in a 6-view traversal join; it does not")
        print("  minimise to these forms. Campaign evidence: bugs/<...>-src_hops_3_...distinct_ids.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
