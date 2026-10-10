"""B1 -- HANA MEDIAN does not interpolate an even count of INTEGERS (returns the upper
middle), but interpolates DOUBLES. PERCENTILE_CONT(0.5) always interpolates.
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
    def vals(*xs):
        return " UNION ALL ".join(f"SELECT CAST({x} AS BIGINT) x FROM DUMMY" for x in xs)

    run(cur, "MEDIAN of BIGINT {1,2}      (mean 1.5)", f"SELECT MEDIAN(x) v FROM ({vals(1, 2)}) z")
    run(cur, "MEDIAN of BIGINT {1,10}     (mean 5.5)", f"SELECT MEDIAN(x) v FROM ({vals(1, 10)}) z")
    run(cur, "MEDIAN of BIGINT {1,2,3,10} (mean 2.5)", f"SELECT MEDIAN(x) v FROM ({vals(1, 2, 3, 10)}) z")
    run(cur, "MEDIAN of BIGINT {-3,-2}    (mean -2.5)", f"SELECT MEDIAN(x) v FROM ({vals(-3, -2)}) z")
    run(cur, "MEDIAN of DOUBLE {1,10}               ", "SELECT MEDIAN(x) v FROM (SELECT CAST(1 AS DOUBLE) x FROM DUMMY UNION ALL SELECT CAST(10 AS DOUBLE) FROM DUMMY) z")
    run(cur, "PERCENTILE_CONT(0.5) BIGINT {1,10}    ", f"SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) v FROM ({vals(1, 10)}) z")
    print("\n  => MEDIAN answers in the argument's type: over integers the mean of the two middle values")
    print("     ROUNDED half away from zero ({1,10} -> 6, not the upper middle 10); DOUBLE keeps 5.5.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
