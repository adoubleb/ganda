"""B10 -- HANA raises a whole-statement internal error (129) for a SPARQL FILTER that names a
variable bound nowhere in the pattern, instead of a per-solution evaluation error.
Declaring the variable with a never-matching OPTIONAL avoids it. Run: python3 -I repro.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

G = "<urn:ganda:b10>"
P = "PREFIX c: <urn:ganda:c:> PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"


def try_sparql(cur, label, text):
    try:
        print(f"  {label}: OK  {sparql(cur, text)}")
    except Exception as e:
        print(f"  {label}: [{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e))[:90]}")


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:a> a c:t ; p:id "a" ; p:i "1"^^xsd:long . <urn:ganda:r:b> a c:t ; p:id "b" }} }}', update=True)
        # ?u comes from an OPTIONAL (sometimes unbound); the IF's else branch names ?undeclared,
        # a variable that appears in NO triple pattern.
        bad = f'{P} SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:i ?u }} FILTER (!(IF(BOUND(?u), EXISTS {{ ?n p:id ?z }}, ?undeclared))) }} }}'
        good = f'{P} SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:i ?u }} OPTIONAL {{ ?n <urn:ganda:never> ?undeclared }} FILTER (!(IF(BOUND(?u), EXISTS {{ ?n p:id ?z }}, ?undeclared))) }} }}'
        try_sparql(cur, "FILTER names an undeclared var ", bad)
        try_sparql(cur, "same, var declared (never bound)", good)
        print("\n  => the undeclared variable is an internal error (129); declaring it (never bound) is a clean result.")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
