"""Minimal reproduction of H2: SAP HANA's Document Store crashes (internal error, code 3,
"Execution flow must not reach here") on a CORRELATED scalar subquery whose inner query
reads a COLLECTION through UNNEST.

Run against a HANA Cloud instance with the Document Store enabled:

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... \
        python3 -I ganda/hana/issues-draft/H2_repro.py

(or `set -a; source .env; set +a` first for the Ganda env). Creates one collection with a
single document, runs four queries that isolate the trigger, drops the collection.
"""
from __future__ import annotations

import json
import os

from hdbcli import dbapi

C = "h2_pc"


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
    if os.environ.get("HANA_SNI_HOSTNAME"):
        kw["disableCloudRedirect"] = True
    return dbapi.connect(**kw)


def run(cur, label, sql):
    try:
        cur.execute(sql)
        print(f"{label}: OK {cur.fetchall()}")
    except dbapi.Error as e:
        print(f"{label}: {getattr(e, 'errorcode', '?')} {getattr(e, 'errortext', e)[:90]}")


def main() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT VERSION FROM SYS.M_DATABASE")
    print("HANA version:", cur.fetchone()[0])
    try:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except dbapi.Error:
            pass
        cur.execute(f"CREATE COLLECTION {C}")
        cur.execute(f"INSERT INTO {C} VALUES(?)", [json.dumps({"id": "a", "links": [{"to": "b"}]})])

        # a view that reads the collection with UNNEST, and one that reads it plainly
        unnest = f'WITH node AS (SELECT $."id" AS id FROM {C}), link AS (SELECT $."id" AS src FROM {C} UNNEST $."links" AS "l") '
        plain = f'WITH node AS (SELECT $."id" AS id FROM {C}), other AS (SELECT $."id" AS k FROM {C}) '

        run(cur, "uncorrelated over UNNEST view ", unnest + "SELECT s.id, (SELECT COUNT(*) FROM link) AS v FROM node s")
        run(cur, "CORRELATED over UNNEST view   ", unnest + "SELECT s.id, (SELECT COUNT(*) FROM link p WHERE p.src = s.id) AS v FROM node s")
        run(cur, "correlated, no UNNEST view    ", plain + "SELECT s.id, (SELECT COUNT(*) FROM other p WHERE p.k = s.id) AS v FROM node s")
        print("\nOnly the CORRELATED scalar subquery over the UNNEST-based view crashes (code 3);")
        print("the uncorrelated form and the correlated form over a non-UNNEST view both answer.")
    finally:
        try:
            cur.execute(f"DROP COLLECTION {C}")
        except dbapi.Error:
            pass
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
