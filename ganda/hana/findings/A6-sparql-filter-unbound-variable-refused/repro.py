"""A6 / H6 -- SPARQL: a FILTER or BIND over a variable no pattern binds is refused (code 129),
even FILTER(!BOUND(?x)), which SPARQL 1.1 makes true for every solution; COALESCE(?x, 1) and a
never-matching OPTIONAL that names ?x are accepted. Run: python3 -I repro.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

NS = f"urn:a6:{int(time.time())}"
G = f"<{NS}:g>"
PFX = f"PREFIX p: <{NS}:p:> "


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur))

    def q(label, where, spec):
        try:
            doc = json.loads(sparql(cur, f"{PFX} SELECT ?id WHERE {{ GRAPH {G} {{ ?n p:id ?id . {where} }} }}"))
            print(f"  {label}: OK  {sorted(b['id']['value'] for b in doc['results']['bindings'])}   (standard: {spec})")
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: [{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e)).strip()[:90]}   (standard: {spec})")

    sparql(cur, f"{PFX} INSERT DATA {{ GRAPH {G} {{ <{NS}:a> p:id \"a\" . <{NS}:b> p:id \"b\" . }} }}", update=True)
    try:
        print()
        q("FILTER(!BOUND(?x))         ", "FILTER(!BOUND(?x))", "a, b")
        q("FILTER(?x = 1)             ", "FILTER(?x = 1)", "none")
        q("BIND(?x AS ?y)             ", "BIND(?x AS ?y)", "a, b")
        q("FILTER(COALESCE(?x, 1) = 1)", "FILTER(COALESCE(?x, 1) = 1)", "a, b")
        q("OPTIONAL names ?x, !BOUND  ", f"OPTIONAL {{ ?n <{NS}:never> ?x }} FILTER(!BOUND(?x))", "a, b")
        print("\n  => a never-bound variable is refused in FILTER / BIND, accepted in COALESCE or once declared.")
    finally:
        sparql(cur, f"{PFX} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
