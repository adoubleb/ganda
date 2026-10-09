"""A1 / H1 -- SPARQL serialises an xsd:double to ~6 significant figures.

The value is STORED exactly (STR(?d) returns all 17 digits) but every SPARQL result
serialisation (JSON here) rounds the numeric term. Run: python3 -I repro.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, run, sparql, version  # noqa: E402

G = "<urn:ganda:a1>"
P = "PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
VAL = "3.141592653589793"  # math.pi, 17 significant figures


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur))
    try:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:a> p:d "{VAL}"^^xsd:double }} }}', update=True)
        print(f"\n  inserted         : {VAL}  (xsd:double)")
        print("  read as ?d       :", sparql(cur, f"{P} SELECT ?d WHERE {{ GRAPH {G} {{ ?n p:d ?d }} }}"))
        print("  read as STR(?d)  :", sparql(cur, f"{P} SELECT (STR(?d) AS ?d) WHERE {{ GRAPH {G} {{ ?n p:d ?d }} }}"))
        print("  arithmetic ?d*1e9:", sparql(cur, f"{P} SELECT ((?d * 1000000000) AS ?r) WHERE {{ GRAPH {G} {{ ?n p:d ?d }} }}"))
        print("\n  => the ?d term is 3.14159 (6 sig figs); STR(?d) is 3.1415926535897931 (exact).")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
