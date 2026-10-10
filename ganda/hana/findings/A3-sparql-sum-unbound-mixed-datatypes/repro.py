"""A3 / H3 -- SPARQL SUM / AVG skip an unbound value only while the predicate holds ONE
datatype across the graph; once it also holds another datatype (on a resource the query
never matches), a group with an unbound value answers UNBOUND. MAX / COUNT skip it either
way.

Isolates the trigger: the same query before and after one unrelated triple, and the two
forms that keep the unbound value out of the aggregate. Run: python3 -I repro.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

# a fresh namespace per run: HANA remembers every datatype a predicate IRI has held (in any
# graph, even dropped), so a second run on the same IRIs would start "mixed"
NS = f"urn:a3:{int(time.time())}"
G = f"<{NS}:g>"
PFX = f"PREFIX p: <{NS}:p:> PREFIX c: <{NS}:c:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "


def rows(cur, text):
    doc = json.loads(sparql(cur, PFX + text))
    return [{k: v["value"] for k, v in b.items()} for b in doc["results"]["bindings"]]


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur))
    q = f"SELECT (SUM(?w) AS ?sum) (AVG(?w) AS ?avg) (MAX(?w) AS ?max) (COUNT(?w) AS ?count) WHERE {{ GRAPH {G} {{ ?n a c:n . OPTIONAL {{ ?n p:i ?w }} }} }}"
    bound = f"SELECT (SUM(?w) AS ?sum) WHERE {{ GRAPH {G} {{ ?n a c:n ; p:i ?w . }} }}"
    coalesce = f"SELECT (SUM(COALESCE(?w, 0)) AS ?sum) WHERE {{ GRAPH {G} {{ ?n a c:n . OPTIONAL {{ ?n p:i ?w }} }} }}"
    try:
        sparql(cur, PFX + f"DROP SILENT GRAPH {G}", update=True)
        sparql(cur, PFX + f'INSERT DATA {{ GRAPH {G} {{ <{NS}:a> a c:n ; p:i "3"^^xsd:long . <{NS}:b> a c:n ; p:i "4"^^xsd:long . <{NS}:c> a c:n . }} }}', update=True)
        print()
        print("  p:i holds xsd:long only        :", rows(cur, q), " want sum 7, avg 3.5")
        sparql(cur, PFX + f'INSERT DATA {{ GRAPH {G} {{ <{NS}:x> a c:other ; p:i "text" . }} }}', update=True)
        print("  p:i also holds a string        :", rows(cur, q), " the string's resource is not of class c:n")
        print("  value bound where present      :", rows(cur, bound))
        print("  SUM(COALESCE(?w, 0))           :", rows(cur, coalesce))
        print("\n  => one unrelated triple turns SUM / AVG over the same matching triples unbound;")
        print("     MAX and COUNT still skip the unbound value, and so do the two rewrites.")
    finally:
        sparql(cur, PFX + f"DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
