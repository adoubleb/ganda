"""B8 -- SPARQL_TABLE loses the datatype of a maybe-unbound numeric variable: it surfaces as
an NVARCHAR column ("7"), while the SAME query run directly through SPARQL_EXECUTE types the
binding as xsd:long (decodes to int 7). Run: python3 -I repro.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

G = "<urn:ganda:b8>"
P = "PREFIX c: <urn:ganda:c:> PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
# the "keys" shape the fuzzer flagged: a nullable integer field, ordered
INNER = f'{P} SELECT ?a WHERE {{ GRAPH {G} {{ ?n a c:n ; p:id ?id . OPTIONAL {{ ?n p:f1 ?a }} }} }} ORDER BY ASC(?a)'


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:a> a c:n ; p:id "a" ; p:f1 "7"^^xsd:long . <urn:ganda:r:b> a c:n ; p:id "b" }} }}', update=True)

        # direct through SPARQL_EXECUTE: the bound ?a carries datatype xsd:long
        doc = json.loads(sparql(cur, INNER))
        print("  direct SPARQL_EXECUTE binding:", [b.get("a") for b in doc["results"]["bindings"]])

        # the same query wrapped in SPARQL_TABLE: ?a is an NVARCHAR column, value '7'
        sql = "SELECT * FROM SPARQL_TABLE('" + INNER.replace("'", "''") + "')"
        cur.execute(sql)
        print(f"  SPARQL_TABLE column type code {cur.description[0][1]} (11 = NVARCHAR), rows {cur.fetchall()}")
        print("\n  => direct: ?a has datatype xsd:long (-> int 7). SPARQL_TABLE: NVARCHAR column -> string '7'.")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
