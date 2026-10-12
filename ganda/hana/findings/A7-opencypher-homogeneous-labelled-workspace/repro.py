"""A7 -- openCypher over a HOMOGENEOUS graph workspace (one vertex table, one edge table): a
MATCH pattern naming the VERTEX label -- `(a:v)`, declared by a LABEL clause or the default
table name -- fails with `[260] invalid column name: a.CYPHER$TABLE$LABEL`, an internal
column of the engine's rewrite leaking into the SQL it generates; over tables and over
views alike. An edge label, `LABEL(a)` in RETURN, the unlabelled pattern, and any
heterogeneous workspace (a second vertex table OR a second edge table) answer. Found by
the Ganda probe (fact G27) 2026-10-11. Run: python3 -I repro.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, version  # noqa: E402
from hdbcli import dbapi  # noqa: E402

S = f"A7_{int(time.time())}"


def oc(cur, ws: str, cypher: str) -> str:
    try:
        cur.execute(f"SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE {ws} QUERY '{cypher}')")
        return f"OK {sorted(cur.fetchall())}"
    except dbapi.Error as e:
        return f"[{e.errorcode}] {e.errortext.strip().splitlines()[0][:70]}"


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
        cur.execute("CREATE COLUMN TABLE v (id NVARCHAR(256) PRIMARY KEY, name NVARCHAR(100))")
        cur.execute("CREATE COLUMN TABLE v2 (id NVARCHAR(256) PRIMARY KEY, name NVARCHAR(100))")
        cur.execute("CREATE COLUMN TABLE e (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        cur.execute("CREATE COLUMN TABLE e2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        cur.execute("INSERT INTO v VALUES ('m', 'M')")
        cur.execute("INSERT INTO v VALUES ('n', 'N')")
        cur.execute("INSERT INTO v2 VALUES ('p', 'P')")
        cur.execute("INSERT INTO e VALUES ('f0', 'm', 'n')")
        cur.execute("INSERT INTO e2 VALUES ('f1', 'n', 'm')")
        cur.execute("CREATE VIEW gv_v AS SELECT id, name AS v_name FROM v")
        cur.execute("CREATE VIEW gv_e AS SELECT eid, src_id, dst_id FROM e")
        workspaces = {
            # homogeneous (one vertex table, one edge table)
            "tables, LABEL clauses       ": "CREATE GRAPH WORKSPACE w1 VERTEX TABLE v KEY COLUMN id LABEL 'v' EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e'",
            "tables, no LABEL clauses    ": "CREATE GRAPH WORKSPACE w2 VERTEX TABLE v KEY COLUMN id EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id",
            "views, LABEL clauses        ": "CREATE GRAPH WORKSPACE w3 VERTEX TABLE gv_v KEY COLUMN id LABEL 'v' EDGE TABLE gv_e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e'",
            "tables, vertex LABEL only   ": "CREATE GRAPH WORKSPACE w4 VERTEX TABLE v KEY COLUMN id LABEL 'v' EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id",
            "tables, edge LABEL only     ": "CREATE GRAPH WORKSPACE w5 VERTEX TABLE v KEY COLUMN id EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e'",
            # heterogeneous controls
            "two edge tables, LABELs     ": "CREATE GRAPH WORKSPACE w6 VERTEX TABLE v KEY COLUMN id LABEL 'v' EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e' EDGE TABLE e2 KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e2'",
            "two vertex tables, LABELs   ": "CREATE GRAPH WORKSPACE w7 VERTEX TABLE v KEY COLUMN id LABEL 'v' VERTEX TABLE v2 KEY COLUMN id LABEL 'v2' EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id REFERENCES v TARGET COLUMN dst_id REFERENCES v LABEL 'e'",
        }
        queries = [
            ("vertex label   ", "MATCH (a:{V}) RETURN a.ID AS ID"),
            ("edge label     ", "MATCH (a)-[x:{E}]->(b) RETURN a.ID AS ID, b.ID AS V"),
            ("both labels    ", "MATCH (a:{V})-[x:{E}]->(b:{V}) RETURN a.ID AS ID, b.ID AS V"),
            ("no label       ", "MATCH (a)-[x]->(b) RETURN a.ID AS ID, b.ID AS V"),
            ("LABEL(a)       ", "MATCH (a) RETURN a.ID AS ID, LABEL(a) AS L"),
        ]
        for i, (label, ddl) in enumerate(workspaces.items(), 1):
            cur.execute(ddl)
            # a declared label is the LABEL clause's string; without one it is the table's
            # catalog name (uppercase: the DDL is unquoted)
            v_label = "v" if "LABEL 'v'" in ddl else "V"
            e_label = "e" if "LABEL 'e'" in ddl else "E"
            print(f"w{i} {label}")
            for qlabel, q in queries:
                print(f"     {qlabel}: {oc(cur, f'w{i}', q.format(V=v_label, E=e_label))}")
        print("\n  => over a workspace with ONE vertex table (w1-w5: tables or views, LABEL clauses or")
        print("     not), a MATCH naming the vertex label fails with `invalid column name: ...CYPHER$TABLE$LABEL`")
        print("     (an internal column of the rewrite); the edge label, LABEL(a) and the unlabelled pattern")
        print("     answer, and so does every pattern over a second vertex table (w7) or edge table (w6).")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
