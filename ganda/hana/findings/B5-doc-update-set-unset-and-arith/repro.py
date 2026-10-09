"""B5 -- a doc UPDATE refuses a combined SET ... UNSET ... (each alone works), and SET takes
only constant expressions (no $."f" = $."f" + c). Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

C = "b5_pc"


def reset(cur):
    try:
        cur.execute(f"DELETE FROM {C}")
    except Exception:
        cur.execute(f"CREATE COLLECTION {C}")
    cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": "a", "i": 10, "s": "x"})])


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
        reset(cur); run(cur, "combined SET ... UNSET      ", f'UPDATE {C} SET "i" = 5 UNSET "s" WHERE "id" = \'a\'')
        reset(cur); run(cur, "SET alone                  ", f'UPDATE {C} SET $."i" = 5 WHERE $."id" = \'a\'')
        reset(cur); run(cur, "UNSET alone                ", f'UPDATE {C} UNSET $."s" WHERE $."id" = \'a\'')
        reset(cur); run(cur, "SET f = f + c (field arith)", f'UPDATE {C} SET $."i" = $."i" + 5 WHERE $."id" = \'a\'')
        print("\n  => combined SET/UNSET and field-referencing arithmetic are refused; SET and UNSET each alone work.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except Exception:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
