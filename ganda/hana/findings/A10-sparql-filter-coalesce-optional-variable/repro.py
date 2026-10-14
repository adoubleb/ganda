"""A10 -- a SPARQL FILTER whose COALESCE falls back for an unbound OPTIONAL variable is
evaluated wrongly: `FILTER (COALESCE(?i, 0) = 0)` drops the solution where ?i is unbound,
and `FILTER (COALESCE(?i > 1, true))` keeps one where ?i > 1 is false. The same
expression is right as a SELECT expression and through `BIND ((expr) AS ?t) FILTER (?t)`,
and right when ?i is required. Found by probe fact K26 (2026-10-12) as a MINUS partition
`FILTER (COALESCE(IF(?i > 1, 1, 0), 2) = 2)` that removed every solution; narrowed
2026-10-13.
Run: python3 repro.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

G = f"<urn:ganda:a10:{int(time.time())}>"
P = "PREFIX p: <urn:ex10:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
DATA = '<urn:ex10:x> p:id "x" ; p:i "1"^^xsd:long . <urn:ex10:y> p:id "y" .'
OPT = "?n p:id ?id . OPTIONAL { ?n p:i ?i }"


def rows(cur, body: str, sel: str = "?id") -> str:
    try:
        doc = json.loads(sparql(cur, f"{P} SELECT {sel} WHERE {{ GRAPH {G} {{ {body} }} }} ORDER BY ?id"))
        vs = doc["head"]["vars"]
        out = [tuple(b.get(v, {}).get("value") for v in vs) for b in doc["results"]["bindings"]]
        return str([o[0] if len(o) == 1 else o for o in out])
    except Exception as e:  # noqa: BLE001
        return f"[{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e))[:90]}"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        sparql(cur, f"{P} INSERT DATA {{ GRAPH {G} {{ {DATA} }} }}", update=True)
        print("data: x has i = 1; y has no i\n")
        print("expression values:", rows(cur, OPT, "?id (COALESCE(?i, 0) AS ?a) (COALESCE(?i > 1, true) AS ?b)"),
              " (expected [('x', '1', 'false'), ('y', '0', 'true')])\n")
        for f, want in (("COALESCE(?i, 0) = 0", "['y']"), ("COALESCE(?i > 1, true)", "['y']"),
                        ("COALESCE(IF(?i > 1, 1, 0), 2) = 2", "['y']")):
            print(f"{f}   expected {want}")
            print(f"  FILTER (expr)                : {rows(cur, f'{OPT} FILTER ({f})')}")
            print(f"  BIND ((expr) AS ?t) FILTER   : {rows(cur, f'{OPT} BIND (({f}) AS ?t) FILTER (?t)')}")
        print("\nrequired ?i: FILTER (COALESCE(?i > 1, true))   expected []")
        print("  ", rows(cur, "?n p:id ?id ; p:i ?i . FILTER (COALESCE(?i > 1, true))"))
        print("\nas found (K26): every solution MINUS the COALESCE partition   expected ['x']")
        print("  FILTER :", rows(cur, f"{{ ?n p:id ?id }} MINUS {{ {OPT} FILTER (COALESCE(IF(?i > 1, 1, 0), 2) = 2) }}"))
        print("  BIND   :", rows(cur, f"{{ ?n p:id ?id }} MINUS {{ {OPT} BIND ((COALESCE(IF(?i > 1, 1, 0), 2) = 2) AS ?t) FILTER (?t) }}"))
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
