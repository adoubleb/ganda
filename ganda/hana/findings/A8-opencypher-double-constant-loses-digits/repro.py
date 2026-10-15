"""A8 -- a DOUBLE constant written in an openCypher query is parsed to about fifteen
significant digits: a constant one or two ulps away from a stored DOUBLE compares `=` to
it, while the same value as a bound parameter, or as TOFLOAT of a string constant, does
not (exact, as HANA SQL's comparison of the column). Found by the Ganda SAP HANA fuzzer
(the 12-hour campaign of 2026-10-12: ten `graph:literal` mismatches on float pivots).
Run: python3 repro.py (hdbcli, HANA_* in the environment)."""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, version  # noqa: E402
from hdbcli import dbapi  # noqa: E402

S = f"A8_{int(time.time())}"
STORED = -985.195
ONE = math.nextafter(STORED, -math.inf)  # one ulp below
TWO = math.nextafter(ONE, -math.inf)  # two ulps below


def oc(cur, cypher: str, params=None) -> str:
    clause = " PARAMETERS (" + ", ".join(f"'{k}' = ?" for k in params) + ")" if params else ""
    try:
        cur.execute(f"SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY '{cypher}'{clause})", list(params.values()) if params else None)
        return f"{sorted(r[0] for r in cur.fetchall())}"
    except dbapi.Error as e:
        return f"[{e.errorcode}] {e.errortext.strip().splitlines()[0][:90]}"


def sql(cur, text: str, params=None) -> str:
    cur.execute(text, params or [])
    return f"{sorted(r[0] for r in cur.fetchall())}"


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
        cur.execute("CREATE COLUMN TABLE rv (id NVARCHAR(256) PRIMARY KEY, d DOUBLE)")
        cur.execute("CREATE COLUMN TABLE re1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        cur.execute("CREATE COLUMN TABLE re2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
        cur.execute("INSERT INTO rv VALUES ('b', ?)", [STORED])
        cur.execute("INSERT INTO rv VALUES ('h', 0.5)")
        cur.execute("CREATE VIEW gv_rv AS SELECT id, d AS rv_d FROM rv")
        cur.execute("CREATE VIEW gv_re1 AS SELECT eid, src_id, dst_id FROM re1")
        cur.execute("CREATE VIEW gv_re2 AS SELECT eid, src_id, dst_id FROM re2")
        cur.execute("CREATE GRAPH WORKSPACE rw VERTEX TABLE gv_rv KEY COLUMN id LABEL 'rv' EDGE TABLE gv_re1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're1' EDGE TABLE gv_re2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're2'")
        print(f"stored d = {STORED!r}; one ulp below = {ONE!r}; two ulps below = {TWO!r}\n")
        rows = [
            ("SQL      d = one ulp below        ", sql(cur, "SELECT id FROM rv WHERE d = ?", [ONE])),
            ("SQL      d <= one ulp below       ", sql(cur, "SELECT id FROM rv WHERE d <= ?", [ONE])),
            ("cypher   d = $p, one ulp below    ", oc(cur, "MATCH (a:rv) WHERE a.RV_D = $p RETURN a.ID AS ID", {"p": ONE})),
            ("cypher   d <= $p, one ulp below   ", oc(cur, "MATCH (a:rv) WHERE a.RV_D <= $p RETURN a.ID AS ID", {"p": ONE})),
            ("cypher   d <> $p, one ulp below   ", oc(cur, "MATCH (a:rv) WHERE a.RV_D <> $p RETURN a.ID AS ID", {"p": ONE})),
            ("cypher   d = $p, two ulps below   ", oc(cur, "MATCH (a:rv) WHERE a.RV_D = $p RETURN a.ID AS ID", {"p": TWO})),
            ("cypher   d = $p, exactly          ", oc(cur, "MATCH (a:rv) WHERE a.RV_D = $p RETURN a.ID AS ID", {"p": STORED})),
            (f"cypher   d = TOFLOAT(''{ONE!r}'')", oc(cur, f"MATCH (a:rv) WHERE a.RV_D = TOFLOAT(''{ONE!r}'') RETURN a.ID AS ID")),
            (f"cypher   d = {ONE!r} (constant)", oc(cur, f"MATCH (a:rv) WHERE a.RV_D = {ONE!r} RETURN a.ID AS ID")),
            (f"cypher   d = {TWO!r} (constant)", oc(cur, f"MATCH (a:rv) WHERE a.RV_D = {TWO!r} RETURN a.ID AS ID")),
            ("cypher   d <= two ulps below as TOFLOAT  ", oc(cur, f"MATCH (a:rv) WHERE a.RV_D <= TOFLOAT(''{TWO!r}'') RETURN a.ID AS ID")),
            ("cypher   0.5 = 0.5000000000000001 (constant, 1 ulp)", oc(cur, "MATCH (a:rv) WHERE a.RV_D = 0.5000000000000001 RETURN a.ID AS ID")),
            ("cypher   0.5 = 0.5000000000000002 (constant, 2 ulps)", oc(cur, "MATCH (a:rv) WHERE a.RV_D = 0.5000000000000002 RETURN a.ID AS ID")),
            ("cypher   0.5 = $p, 0.5000000000000002 (2 ulps)", oc(cur, "MATCH (a:rv) WHERE a.RV_D = $p RETURN a.ID AS ID", {"p": 0.5000000000000002})),
            ("cypher   TOFLOAT(''-985.1950000000002'') projected", oc(cur, "MATCH (a:rv) WHERE a.ID = ''b'' RETURN TOFLOAT(''-985.1950000000002'') AS V")),
        ]
        for label, got in rows:
            print(f"  {label:52s}: {got}")
        print("\n  => SQL, a bound cypher parameter and TOFLOAT of a string constant compare the DOUBLE exactly (no")
        print("     row for a neighbour one or two ulps away); a DOUBLE constant written in the query text matches")
        print("     one AND two ulps away: the parser keeps about fifteen significant digits of it.")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
