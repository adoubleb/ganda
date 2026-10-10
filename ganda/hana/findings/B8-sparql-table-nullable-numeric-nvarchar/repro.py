"""B8 -- SPARQL_TABLE types a result column after the datatypes its PREDICATE has held across the
whole store: one datatype gives a typed column (OPTIONAL or not); once the predicate has held a
second datatype -- in another graph, even one since dropped -- the column is NVARCHAR (a number
as its digits, a boolean as "1" / "0"). The direct SPARQL_EXECUTE answer keeps the datatype
throughout. Uses a fresh IRI namespace per run. Run: python3 -I repro.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

NS = f"urn:b8:{int(time.time())}"
G, G2 = f"<{NS}:g>", f"<{NS}:g2>"
P = f"PREFIX c: <{NS}:c:> PREFIX p: <{NS}:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
INNER = f"{P} SELECT ?id ?a ?b WHERE {{ GRAPH {G} {{ ?n a c:n ; p:id ?id . OPTIONAL {{ ?n p:i ?a }} OPTIONAL {{ ?n p:b ?b }} }} }} ORDER BY ?id"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")

    def table(label):
        cur.execute("SELECT * FROM SPARQL_TABLE('" + INNER.replace("'", "''") + "')")
        types = [d[1] for d in cur.description]
        print(f"  {label}: SPARQL_TABLE types {types} (4 = INTEGER, 28 = BOOLEAN, 11 = NVARCHAR) rows {cur.fetchall()}")

    try:
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G} {{ <{NS}:a> a c:n ; p:id "a" ; p:i "7"^^xsd:long ; p:b true . <{NS}:b> a c:n ; p:id "b" }} }}', update=True)
        table("p:i, p:b hold one datatype each       ")
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G2} {{ <{NS}:x> a c:other ; p:i "seven" ; p:b "yes" }} }}', update=True)
        table("a string on each, in ANOTHER graph    ")
        sparql(cur, f"{P} DROP SILENT GRAPH {G2}", update=True)
        table("after that graph is dropped           ")
        doc = json.loads(sparql(cur, INNER))
        print("  direct SPARQL_EXECUTE, same query     :", [(b.get("a") or {}).get("datatype", "-").rsplit("#", 1)[-1] for b in doc["results"]["bindings"]])
        print("\n  => the column type follows the predicate's datatype history across the store (other graphs,")
        print("     dropped data), not the values the query returns; the direct answer keeps xsd:long.")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        sparql(cur, f"{P} DROP SILENT GRAPH {G2}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
