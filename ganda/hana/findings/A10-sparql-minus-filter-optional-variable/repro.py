"""A10 -- a SPARQL `MINUS` whose right-hand group holds an OPTIONAL and a FILTER naming
that optional variable alone removes EVERY solution from the left; the same group as a
subselect does too; the same filter carried through `BIND ((expr) AS ?t) FILTER (?t)`
answers per SPARQL 1.1, and so does the group when its filter names two optional
variables. Under SPARQL 1.1 (section 18.5, Minus; a FILTER applies to its whole group)
the forms are one query. Found by probe fact K26 (2026-10-12).
Run: python3 repro.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, sparql_values, version  # noqa: E402

G = f"<urn:ganda:a10:{int(time.time())}>"
P = "PREFIX c: <urn:ganda:c:> PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
# x: i = 1; y: no i (two rows are enough); the five-node graph of the probe follows
TWO = f'<urn:ganda:r:x> a c:two ; p:id "x" ; p:i "1"^^xsd:long . <urn:ganda:r:y> a c:two ; p:id "y" .'
FIVE = " . ".join(
    [
        '<urn:ganda:r:a> a c:five ; p:id "a" ; p:i "1"^^xsd:long ; p:s "ab"',
        '<urn:ganda:r:b> a c:five ; p:id "b" ; p:i "2"^^xsd:long',
        '<urn:ganda:r:c> a c:five ; p:id "c" ; p:s "zz"',
        '<urn:ganda:r:d> a c:five ; p:id "d" ; p:i "2"^^xsd:long ; p:s "ac"',
        '<urn:ganda:r:e> a c:five ; p:id "e"',
    ]
)


def ids(cur, cls: str, body: str) -> str:
    try:
        return str(sorted(sparql_values(cur, f"{P} SELECT ?id WHERE {{ GRAPH {G} {{ {body} }} }}", "id")))
    except Exception as e:  # noqa: BLE001
        return f"[{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e))[:90]}"


def forms(cls: str, pattern: str, pred: str) -> dict:
    base = f"?n a c:{cls} ; p:id ?id ."
    return {
        "direct FILTER": f"{base} {pattern} FILTER ({pred})",
        "MINUS group, FILTER": f"{{ ?n a c:{cls} ; p:id ?id }} MINUS {{ {base} {pattern} FILTER (!({pred})) }}",
        "MINUS subselect": f"{{ ?n a c:{cls} ; p:id ?id }} MINUS {{ SELECT ?n ?id WHERE {{ {base} {pattern} FILTER (!({pred})) }} }}",
        "MINUS group, BIND then FILTER": f"{{ ?n a c:{cls} ; p:id ?id }} MINUS {{ {base} {pattern} BIND ((!({pred})) AS ?t) FILTER (?t) }}",
    }


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        sparql(cur, f"{P} INSERT DATA {{ GRAPH {G} {{ {TWO} {FIVE} }} }}", update=True)
        print("two rows (x: i = 1; y: no i); the predicate ?i > 1 is false for x, an error for y")
        print("  expected per SPARQL 1.1: direct ['x' is out, 'y' is out] = []; MINUS removes x (where !(?i > 1) is true), keeps y: ['y']")
        for label, body in forms("two", "OPTIONAL { ?n p:i ?i }", "?i > 1").items():
            print(f"  {label:32s}: {ids(cur, 'two', body)}")
        print("\nfive rows (a: i=1 s=ab; b: i=2; c: s=zz; d: i=2 s=ac; e: nothing), two OPTIONALs")
        for pred, want in (("?i > 1", "MINUS keeps b, d"), ('?i > 1 || STRSTARTS(?s, "a")', "MINUS keeps a, b, d"), ("?i IN (1, 2)", "MINUS keeps a, b, d")):
            print(f"  predicate {pred}  ({want})")
            for label, body in forms("five", "OPTIONAL { ?n p:i ?i } OPTIONAL { ?n p:s ?s }", pred).items():
                print(f"    {label:32s}: {ids(cur, 'five', body)}")
        print("\n  => the plain MINUS group and the subselect remove every solution when the filter names")
        print("     one OPTIONAL variable alone; the BIND form, and a filter naming two, answer per the standard.")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
