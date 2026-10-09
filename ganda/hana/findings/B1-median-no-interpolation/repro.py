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
    ints = "SELECT 1 x FROM DUMMY UNION ALL SELECT 2 FROM DUMMY"
    dbls = "SELECT 1.0 x FROM DUMMY UNION ALL SELECT 2.0 FROM DUMMY"
    run(cur, "MEDIAN of INT {1,2}         (want 1.5?)", f"SELECT MEDIAN(x) v FROM ({ints}) z")
    run(cur, "MEDIAN of INT {1,2,3,4}     (want 2.5?)", "SELECT MEDIAN(x) v FROM (SELECT 1 x FROM DUMMY UNION ALL SELECT 2 FROM DUMMY UNION ALL SELECT 3 FROM DUMMY UNION ALL SELECT 4 FROM DUMMY) z")
    run(cur, "MEDIAN of DOUBLE {1.0,2.0}  (want 1.5)", f"SELECT MEDIAN(x) v FROM ({dbls}) z")
    run(cur, "PERCENTILE_CONT(0.5) INT    (interpolates)", f"SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) v FROM ({ints}) z")
    print("\n  => MEDIAN(int) returns the upper-middle (2), not 1.5; MEDIAN(double) interpolates.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
