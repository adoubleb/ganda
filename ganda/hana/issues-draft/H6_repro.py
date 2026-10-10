"""Minimal reproduction of H6: SAP HANA's SPARQL refuses a legal query whose FILTER (or BIND)
names a variable that no triple pattern binds -- even `FILTER(!BOUND(?x))`, which SPARQL 1.1
defines as true for every solution -- while `COALESCE(?x, 1)` over the same variable is
accepted.

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... python3 -I H6_repro.py

Creates one named graph under a fresh IRI namespace, runs the forms, drops the graph.
"""
from __future__ import annotations

import json
import os
import time

from hdbcli import dbapi

NS = f"urn:h6:{int(time.time())}"
G = f"<{NS}:g>"
PFX = f"PREFIX p: <{NS}:p:> "
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

    def q(label, where, spec):
        text = f"{PFX} SELECT ?id WHERE {{ GRAPH {G} {{ ?n p:id ?id . {where} }} }}"
        try:
            doc = json.loads(cur.callproc("SYS.SPARQL_EXECUTE", (text, HDR, "?", "?"))[2])
            rows = sorted(b["id"]["value"] for b in doc["results"]["bindings"])
            print(f"{label}: OK {rows}   (SPARQL 1.1: {spec})")
        except dbapi.Error as e:
            print(f"{label}: [{e.errorcode}] {e.errortext.strip()[:110]}   (SPARQL 1.1: {spec})")

    cur.callproc("SYS.SPARQL_EXECUTE", (f"{PFX} INSERT DATA {{ GRAPH {G} {{ <{NS}:a> p:id \"a\" . <{NS}:b> p:id \"b\" . }} }}", "", "?", "?"))
    try:
        q("FILTER(!BOUND(?x))          ", "FILTER(!BOUND(?x))", "['a', 'b']")
        q("FILTER(BOUND(?x))           ", "FILTER(BOUND(?x))", "[]")
        q("FILTER(?x = 1)              ", "FILTER(?x = 1)", "[] (an error drops the solution)")
        q("BIND(?x AS ?y)              ", "BIND(?x AS ?y)", "['a', 'b'], ?y unbound")
        q("FILTER(COALESCE(?x, 1) = 1) ", "FILTER(COALESCE(?x, 1) = 1)", "['a', 'b']")
        q("OPTIONAL declaring ?x, then FILTER(!BOUND(?x))", f"OPTIONAL {{ ?n <{NS}:never> ?x }} FILTER(!BOUND(?x))", "['a', 'b']")
    finally:
        cur.callproc("SYS.SPARQL_EXECUTE", (f"{PFX} DROP SILENT GRAPH {G}", "", "?", "?"))
    print("\nA variable no pattern binds is simply unbound in every solution (SPARQL 1.1 18.2.1 allows a")
    print("FILTER over a variable not in scope). HANA refuses the whole query for FILTER / BIND, yet")
    print("accepts the same variable inside COALESCE, and accepts it once an OPTIONAL names it.")


if __name__ == "__main__":
    main()
