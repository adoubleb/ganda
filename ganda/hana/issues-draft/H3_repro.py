"""Minimal reproduction of H3: SAP HANA's SPARQL SUM / AVG skip an unbound value only while
the predicate holds ONE datatype across the graph. Once the predicate also holds a value
of another datatype -- on an unrelated resource the query never matches -- a group that
contains an unbound value answers UNBOUND. MAX and COUNT skip the unbound value either way.

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... python3 -I H3_repro.py

Creates one named graph under a fresh IRI namespace, runs the same query before and after
inserting one unrelated triple, then shows the triple need not even be in the same graph
and that dropping it does not undo the effect, and drops the graphs.
"""
from __future__ import annotations

import json
import os

import time

from hdbcli import dbapi

# a fresh namespace per run: HANA remembers every datatype a predicate IRI has held, in any
# graph, even after the graph is dropped, so a second run on the same IRIs starts "mixed"
NS = f"urn:h3:{int(time.time())}"
G = f"<{NS}:g>"
G2 = f"<{NS}:g2>"
PFX = f"PREFIX p: <{NS}:p:> PREFIX c: <{NS}:c:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
HDR = "Accept: application/sparql-results+json\r\nContent-Type: application/sparql-query"


def connect():
    kw = dict(address=os.environ["HANA_HOST"], port=int(os.environ.get("HANA_PORT", "443")),
              user=os.environ["HANA_USER"], password=os.environ["HANA_PASSWORD"],
              encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
              sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true")
    if os.environ.get("HANA_SNI_HOSTNAME"):  # reached through an SSH tunnel
        kw["sslSNIHostname"] = os.environ["HANA_SNI_HOSTNAME"]
        kw["disableCloudRedirect"] = True
    return dbapi.connect(**kw)


def main() -> None:
    cur = connect().cursor()
    cur.execute("SELECT VERSION FROM SYS.M_DATABASE")
    print("HANA version:", cur.fetchone()[0])

    def update(text):
        cur.callproc("SYS.SPARQL_EXECUTE", (PFX + text, "", "?", "?"))

    def query(text):
        doc = json.loads(cur.callproc("SYS.SPARQL_EXECUTE", (PFX + text, HDR, "?", "?"))[2])
        return [{k: v["value"] for k, v in b.items()} for b in doc["results"]["bindings"]]

    q = f"SELECT (SUM(?w) AS ?sum) (AVG(?w) AS ?avg) (MAX(?w) AS ?max) (COUNT(?w) AS ?count) WHERE {{ GRAPH {G} {{ ?n a c:n . OPTIONAL {{ ?n p:i ?w }} }} }}"
    update(f"DROP SILENT GRAPH {G}")
    try:
        # three resources of class c:n: two with p:i (3, 4), one without
        update(f'INSERT DATA {{ GRAPH {G} {{ <{NS}:a> a c:n ; p:i "3"^^xsd:long . <{NS}:b> a c:n ; p:i "4"^^xsd:long . <{NS}:c> a c:n . }} }}')
        print("p:i holds only xsd:long      :", query(q), " (expected sum 7, avg 3.5)")
        # one unrelated resource, of another class, gives p:i a second datatype
        update(f'INSERT DATA {{ GRAPH {G} {{ <{NS}:x> a c:other ; p:i "text" . }} }}')
        print("p:i also holds a string      :", query(q), " (the query never matches the string's resource)")
        # the same in a second graph: the string lives in the FIRST graph only
        q2 = q.replace(G, G2)
        update(f'INSERT DATA {{ GRAPH {G2} {{ <{NS}:a> a c:n ; p:i "3"^^xsd:long . <{NS}:b> a c:n ; p:i "4"^^xsd:long . <{NS}:c> a c:n . }} }}')
        print("another graph, longs only    :", query(q2))
        update(f"DROP SILENT GRAPH {G}")
        print("after the string's graph is dropped:", query(q2))
    finally:
        update(f"DROP SILENT GRAPH {G}")
        update(f"DROP SILENT GRAPH {G2}")
    print("\nThe same query over the same matching triples answers sum 7 / avg 3.5, then nothing:")
    print("SUM and AVG turn unbound once the predicate IRI has held a second datatype anywhere in")
    print("the store -- in another graph, and still after that graph is dropped.")


if __name__ == "__main__":
    main()
