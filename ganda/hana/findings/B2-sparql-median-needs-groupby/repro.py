"""B2 -- SPARQL MEDIAN is an internal error (code 129) without a GROUP BY, though it works
(and interpolates) with one. Run: python3 -I repro.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql, version  # noqa: E402

G = "<urn:ganda:b2>"
P = "PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"


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
        sparql(cur, f'{P} INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:a> p:w "1"^^xsd:long . <urn:ganda:r:b> p:w "2"^^xsd:long . <urn:ganda:r:c> p:w "3"^^xsd:long }} }}', update=True)
        try_sparql(cur, "MEDIAN no GROUP BY      ", f"{P} SELECT (MEDIAN(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?e p:w ?w }} }}")
        try_sparql(cur, "MEDIAN GROUP BY const   ", f"{P} SELECT (MEDIAN(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?e p:w ?w . BIND(1 AS ?g) }} }} GROUP BY ?g")
        try_sparql(cur, "MEDIAN GROUP BY {1,2}   ", f"{P} SELECT (MEDIAN(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?e p:w ?w . BIND(1 AS ?g) FILTER(?w < 3) }} }} GROUP BY ?g")
        print("\n  => MEDIAN without GROUP BY is an internal error (129); with GROUP BY it works and interpolates.")
    finally:
        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
