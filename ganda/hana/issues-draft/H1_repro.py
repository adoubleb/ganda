"""Minimal, self-contained reproduction of H1: SAP HANA's triple store serializes an
`xsd:double` to ~6 significant figures in SPARQL results, though it stores the value
exactly (`STR(?d)` returns all 17 digits).

Run against a HANA Cloud instance with the Triple Store enabled:

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... \
        python3 -I ganda/hana/issues-draft/H1_repro.py

(or, through the Ganda env, `set -a; source .env; set +a` first). It creates a scratch
graph, inserts one double, reads it back three ways, prints the evidence and drops the
graph. No Ganda internals beyond the connection helper are used.
"""
from __future__ import annotations

import os

from hdbcli import dbapi

GRAPH = "<urn:ganda:h1>"
PREFIX = "PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
JSON = "Accept: application/sparql-results+json\r\nContent-Type: application/sparql-query"
VALUE = "3.141592653589793"  # math.pi, 17 significant figures


def connect():
    kw = dict(
        address=os.environ["HANA_HOST"],
        port=int(os.environ.get("HANA_PORT", "443")),
        user=os.environ["HANA_USER"],
        password=os.environ["HANA_PASSWORD"],
        encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
        sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true",
    )
    if os.environ.get("HANA_SNI_HOSTNAME"):
        kw["sslSNIHostname"] = os.environ["HANA_SNI_HOSTNAME"]
    return dbapi.connect(**kw)


def sparql(cur, text: str, update: bool):
    res = cur.callproc("SYS.SPARQL_EXECUTE", (text, "" if update else JSON, "?", "?"))
    return res[2]


def main() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT VERSION FROM SYS.M_DATABASE")
    print("HANA version:", cur.fetchone()[0])
    try:
        sparql(cur, f'{PREFIX} DROP SILENT GRAPH {GRAPH}', update=True)
        sparql(cur, f'{PREFIX} INSERT DATA {{ GRAPH {GRAPH} {{ <urn:ganda:r:a> p:d "{VALUE}"^^xsd:double }} }}', update=True)

        print(f"\ninserted  : {VALUE}  (xsd:double)")
        print("double term:", sparql(cur, f'{PREFIX} SELECT ?d WHERE {{ GRAPH {GRAPH} {{ ?n p:d ?d }} }}', update=False))
        print("STR(?d)    :", sparql(cur, f'{PREFIX} SELECT (STR(?d) AS ?d) WHERE {{ GRAPH {GRAPH} {{ ?n p:d ?d }} }}', update=False))
        print("?d * 1e9   :", sparql(cur, f'{PREFIX} SELECT ((?d * 1000000000) AS ?r) WHERE {{ GRAPH {GRAPH} {{ ?n p:d ?d }} }}', update=False))
        print("\nThe double term comes back 3.14159 (6 sig figs); STR() of the SAME binding")
        print("returns 3.1415926535897931 -- the value is stored exactly, the loss is in the")
        print("result serializer alone.")
    finally:
        sparql(cur, f'{PREFIX} DROP SILENT GRAPH {GRAPH}', update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
