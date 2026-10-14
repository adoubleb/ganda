"""A9 -- in one SQL statement, every OPENCYPHER_TABLE call numbers its PARAMETERS from 1
against the statement's whole `?` list: a second call's first parameter reads the
statement's first `?` (the first call's value), and its own `?` is ignored. Same types:
silently wrong rows; a plain SQL `?` before a call is read by the call too. Each call alone
binds correctly. Found by the Ganda SAP HANA fuzzer (the 12-hour campaign of 2026-10-12, as
type errors on mixed string / number parameters); the positional cause established live
2026-10-13. Run: python3 repro.py (hdbcli, HANA_* in the environment).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, version  # noqa: E402
from hdbcli import dbapi  # noqa: E402

S = f"A9_{int(time.time())}"
CALL_P = "OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY 'MATCH (n) WHERE n.I = $p RETURN n.ID AS ID' PARAMETERS ('p' = ?))"
CALL_Q = "OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY 'MATCH (n) WHERE n.I = $q RETURN n.ID AS ID' PARAMETERS ('q' = ?))"


def run(cur, label: str, text: str, values: list, want: str) -> None:
    try:
        cur.execute(text, values)
        got = f"OK {cur.fetchall()}"
    except dbapi.Error as e:
        got = f"[{e.errorcode}] {e.errortext.strip().splitlines()[0][:80]}"
    print(f"  {label:44s}: {got:42s} expected {want}")


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    cur.execute(f"CREATE SCHEMA {S}")
    cur.execute(f"SET SCHEMA {S}")
    try:
        cur.execute("CREATE COLUMN TABLE v (id NVARCHAR(10) PRIMARY KEY, i BIGINT)")
        cur.execute("CREATE COLUMN TABLE e (eid NVARCHAR(10) PRIMARY KEY, src_id NVARCHAR(10) NOT NULL, dst_id NVARCHAR(10) NOT NULL)")
        cur.execute("INSERT INTO v VALUES ('a', 1)")
        cur.execute("INSERT INTO v VALUES ('b', 2)")
        cur.execute("CREATE GRAPH WORKSPACE w VERTEX TABLE v KEY COLUMN id EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id")
        run(cur, "one call, binds (1)", f"SELECT ID FROM {CALL_P}", [1], "[('a',)]")
        run(cur, "one call, binds (2)", f"SELECT ID FROM {CALL_P}", [2], "[('b',)]")
        run(cur, "two calls UNION ALL, binds (1, 2)", f"SELECT ID FROM {CALL_P} UNION ALL SELECT ID FROM {CALL_Q}", [1, 2], "[('a',), ('b',)]")
        run(cur, "SQL ? then a call, binds ('z', 2)", f"SELECT TO_NVARCHAR(?) AS ID FROM DUMMY UNION ALL SELECT ID FROM {CALL_P}", ["z", 2], "[('z',), ('b',)]")
        print("\n  => the second call reads the statement's FIRST `?`: 1 instead of 2 (silently wrong rows),")
        print("     and a call after a SQL parameter reads that parameter's 'z' (an error converting it).")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
