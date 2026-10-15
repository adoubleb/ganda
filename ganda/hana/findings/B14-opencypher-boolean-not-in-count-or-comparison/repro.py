"""B14 -- openCypher refuses a BOOLEAN attribute inside COUNT(...) and a comparison of two
BOOLEAN attributes (compilation errors, code 4906), while it projects a BOOLEAN and groups
by one. Run: python3 -I repro.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, version  # noqa: E402

S = f"B14_{int(time.time())}"


def oc(cur, label: str, cypher: str):
    run(cur, label, f"SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE bw QUERY '{cypher}')")


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    cur.execute(f"CREATE SCHEMA {S}")
    cur.execute(f"SET SCHEMA {S}")
    try:
        cur.execute("CREATE COLUMN TABLE v (id NVARCHAR(256) PRIMARY KEY, b BOOLEAN, i BIGINT)")
        cur.execute("CREATE COLUMN TABLE e1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL, b BOOLEAN)")
        cur.execute("CREATE COLUMN TABLE e2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        for r in [("a", True, 1), ("b", False, 2), ("c", None, 3), ("d", True, 4)]:
            cur.execute("INSERT INTO v VALUES (?, ?, ?)", r)
        cur.execute("INSERT INTO e1 VALUES ('e0', 'a', 'b', TRUE)")
        cur.execute("INSERT INTO e1 VALUES ('e1', 'b', 'c', NULL)")
        cur.execute("INSERT INTO e2 VALUES ('f0', 'c', 'a')")
        cur.execute("CREATE GRAPH WORKSPACE bw VERTEX TABLE v KEY COLUMN id LABEL 'v' EDGE TABLE e1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES v TARGET COLUMN dst_id REFERENCES v LABEL 'e1' EDGE TABLE e2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES v TARGET COLUMN dst_id REFERENCES v LABEL 'e2'")
        oc(cur, "BOOLEAN projected                 ", "MATCH (a:v) RETURN a.ID AS ID, a.B AS B")
        oc(cur, "BOOLEAN as a group key            ", "MATCH (a:v) RETURN a.B AS K, COUNT(a.ID) AS V")
        oc(cur, "DISTINCT over a BOOLEAN           ", "MATCH (a:v) RETURN DISTINCT a.B AS B")
        oc(cur, "COUNT over a BOOLEAN attribute    ", "MATCH (a:v) RETURN COUNT(a.B) AS V")
        oc(cur, "COUNT over a BOOLEAN edge attr    ", "MATCH (a:v)-[e:e1]->(b:v) RETURN COUNT(e.B) AS V")
        oc(cur, "COUNT over a BIGINT (control)     ", "MATCH (a:v) RETURN COUNT(a.I) AS V")
        oc(cur, "two BOOLEANs compared (<>)        ", "MATCH (t:v), (m:v) WHERE (m.B <> t.B) RETURN t.ID AS ID, m.ID AS V")
        oc(cur, "two BOOLEANs compared (=)         ", "MATCH (t:v), (m:v) WHERE (m.B = t.B) RETURN t.ID AS ID, m.ID AS V")
        oc(cur, "two BIGINTs compared (control)    ", "MATCH (t:v), (m:v) WHERE (m.I > t.I) RETURN t.ID AS ID, m.ID AS V")
        oc(cur, "BOOLEAN against a constant (G10)  ", "MATCH (a:v) WHERE a.B = true RETURN a.ID AS ID")
        oc(cur, "BOOLEAN IS NULL                   ", "MATCH (a:v) WHERE a.B IS NULL RETURN a.ID AS ID")
        run(cur, "SQL COUNT(b) over the table        ", "SELECT COUNT(b) FROM v")
        print("\n  => a BOOLEAN attribute is projected, grouped by, DISTINCTed and null-tested, but COUNT(bool) and")
        print("     a comparison of two BOOLEAN attributes are compilation errors (4906); SQL counts the column.")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
