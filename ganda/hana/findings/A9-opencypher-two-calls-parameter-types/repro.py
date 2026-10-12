"""A9 -- two OPENCYPHER_TABLE calls in ONE SQL statement: the second call's bare
PARAMETERS are typed in the wrong order when it declares a string parameter next to a
numeric one -- the string's `?` gets the number's SQL type, and the client fails to
convert the value ([-10427] Conversion of parameter/column (3) from data type UCS2 (LE)
to BIGINT failed). The same call alone, with the same parameters, answers. Found by the
Ganda SAP HANA fuzzer (the 12-hour campaign of 2026-10-12: seven error asymmetries on
the graph data model's traversal texts, whose starts call is LEFT JOINed to their paths
call). Run: python3 repro.py (hdbcli, HANA_* in the environment)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, version  # noqa: E402
from hdbcli import dbapi  # noqa: E402

S = f"A9_{int(time.time())}"
PATHS = "MATCH (t1:rv)-[j1:re1]->(s:rv) WHERE (s.RV_S >= $p0a AND s.RV_S <= $p0b) AND (j1.RE1_W = $p1) RETURN s.ID AS ID, MIN(t1.RV_I) AS V"
STARTS = "MATCH (s:rv) WHERE (s.RV_S >= $p0a AND s.RV_S <= $p0b) RETURN s.ID AS ID"


def oct(cypher: str, names: list) -> str:
    return f"OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY '{cypher}' PARAMETERS ({', '.join(chr(39) + n + chr(39) + ' = ?' for n in names)}))"


def run(cur, label: str, text: str, values: list) -> None:
    try:
        cur.execute(text, values)
        print(f"  {label:58s}: OK {sorted(cur.fetchall(), key=repr)}")
    except dbapi.Error as e:
        print(f"  {label:58s}: [{e.errorcode}] {e.errortext.strip().splitlines()[0][:110]}")


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
    except dbapi.Error:
        pass
    cur.execute(f"CREATE SCHEMA {S}")
    cur.execute(f"SET SCHEMA {S}")
    try:
        cur.execute("CREATE COLUMN TABLE rv (id NVARCHAR(256) PRIMARY KEY, s NVARCHAR(5000), i BIGINT)")
        cur.execute("CREATE COLUMN TABLE re1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL, w BIGINT)")
        cur.execute("CREATE COLUMN TABLE re2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        for r in [("a", "x", 1), ("b", "y", 2), ("c", "zz", 3)]:
            cur.execute("INSERT INTO rv VALUES (?, ?, ?)", r)
        cur.execute("INSERT INTO re1 VALUES ('e0', 'a', 'b', -23)")
        cur.execute("INSERT INTO re1 VALUES ('e1', 'b', 'c', 5)")
        cur.execute("INSERT INTO re2 VALUES ('f0', 'c', 'a')")
        cur.execute("CREATE VIEW gv_rv AS SELECT id, s AS rv_s, i AS rv_i FROM rv")
        cur.execute("CREATE VIEW gv_re1 AS SELECT eid, src_id, dst_id, w AS re1_w FROM re1")
        cur.execute("CREATE VIEW gv_re2 AS SELECT eid, src_id, dst_id FROM re2")
        cur.execute("CREATE GRAPH WORKSPACE rw VERTEX TABLE gv_rv KEY COLUMN id LABEL 'rv' EDGE TABLE gv_re1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're1' EDGE TABLE gv_re2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're2'")
        values = ["o", "z", -23]
        run(cur, "the paths call alone (p0a, p0b strings; p1 an integer)", f"SELECT * FROM {oct(PATHS, ['p0a', 'p0b', 'p1'])}", values)
        run(cur, "the starts call alone", f"SELECT * FROM {oct(STARTS, ['p0a', 'p0b'])}", values[:2])
        run(cur, "starts LEFT JOIN paths (the fuzzer's traversal text)", f"SELECT s.ID AS id, p.V AS v FROM {oct(STARTS, ['p0a', 'p0b'])} s LEFT JOIN {oct(PATHS, ['p0a', 'p0b', 'p1'])} p ON p.ID = s.ID", values[:2] + values)
        run(cur, "the same, p1 declared first in the second call", f"SELECT s.ID AS id, p.V AS v FROM {oct(STARTS, ['p0a', 'p0b'])} s LEFT JOIN {oct(PATHS, ['p1', 'p0a', 'p0b'])} p ON p.ID = s.ID", values[:2] + [-23, "o", "z"])
        run(cur, "the same with the string pivots as constants", f"SELECT s.ID AS id, p.V AS v FROM OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY 'MATCH (s:rv) WHERE (s.RV_S >= ''o'' AND s.RV_S <= ''z'') RETURN s.ID AS ID') s LEFT JOIN {oct(PATHS.replace('$p0a', chr(39) * 2 + 'o' + chr(39) * 2).replace('$p0b', chr(39) * 2 + 'z' + chr(39) * 2), ['p1'])} p ON p.ID = s.ID", [-23])
        run(cur, "two calls, both calls' parameters strings only", f"SELECT s.ID AS id, p.V AS v FROM {oct(STARTS, ['p0a', 'p0b'])} s LEFT JOIN {oct(STARTS.replace('RETURN s.ID AS ID', 'RETURN s.ID AS ID, s.RV_I AS V'), ['p0a', 'p0b'])} p ON p.ID = s.ID", values[:2] + values[:2])
        print("\n  => each call alone types its parameters by the attributes they are compared with; once a second")
        print("     call joins the statement, the second call's string parameter is typed as the integer's type")
        print("     (the client reports the conversion failure for that `?`). Constants, or parameters of one")
        print("     type per call, are the workaround the fuzzer now uses.")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
