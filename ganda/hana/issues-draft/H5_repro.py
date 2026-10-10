"""Minimal reproduction of H5: over a JSON Document Store field read through SQL, `f BETWEEN ?
AND ?` types its PARAMETERS as BIGINT, so binding strings fails converting them -- while the
very comparisons BETWEEN is defined as, `f >= ? AND f <= ?`, answer. The field holds strings
only.

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... python3 -I H5_repro.py

Creates a scratch schema with one collection (and a column table for comparison), runs the
forms, drops the schema.
"""
from __future__ import annotations

import json
import os

from hdbcli import dbapi

S = "H5_REPRO"


def connect():
    kw = dict(address=os.environ["HANA_HOST"], port=int(os.environ.get("HANA_PORT", "443")),
              user=os.environ["HANA_USER"], password=os.environ["HANA_PASSWORD"],
              encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
              sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true")
    if os.environ.get("HANA_SNI_HOSTNAME"):  # reached through an SSH tunnel
        kw["sslSNIHostname"] = os.environ["HANA_SNI_HOSTNAME"]
        kw["disableCloudRedirect"] = True
    return dbapi.connect(**kw)


def run(cur, label, sql, params=None):
    try:
        cur.execute(sql, params) if params else cur.execute(sql)
        print(f"{label}: OK {cur.fetchall()}")
    except dbapi.Error as e:
        print(f"{label}: [{e.errorcode}] {e.errortext.strip()[:110]}")


def main() -> None:
    cur = connect().cursor()
    cur.execute("SELECT VERSION FROM SYS.M_DATABASE")
    print("HANA version:", cur.fetchone()[0])
    try:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
    except dbapi.Error:
        pass
    cur.execute(f"CREATE SCHEMA {S}")
    cur.execute(f"SET SCHEMA {S}")
    try:
        cur.execute("CREATE COLLECTION c")
        for d in ({"f": "abc"}, {"f": "xyz"}):
            cur.execute("INSERT INTO c VALUES(?)", [json.dumps(d)])
        cur.execute("CREATE COLUMN TABLE t (f NVARCHAR(5000))")
        cur.execute("INSERT INTO t VALUES ('abc')")
        cur.execute("INSERT INTO t VALUES ('xyz')")
        v = 'WITH raw AS (SELECT $."f" AS f FROM c) '
        run(cur, "collection field: f BETWEEN ? AND ?      ", v + "SELECT f FROM raw WHERE f BETWEEN ? AND ?", ["A", "i"])
        run(cur, "collection field: f >= ? AND f <= ?      ", v + "SELECT f FROM raw WHERE f >= ? AND f <= ?", ["A", "i"])
        run(cur, "collection field: f BETWEEN 'A' AND 'i'  ", v + "SELECT f FROM raw WHERE f BETWEEN 'A' AND 'i'")
        run(cur, "collection field: TO_NVARCHAR(f) BETWEEN ", v + "SELECT f FROM raw WHERE TO_NVARCHAR(f) BETWEEN ? AND ?", ["A", "i"])
        run(cur, "column table:     f BETWEEN ? AND ?      ", "SELECT f FROM t WHERE f BETWEEN ? AND ?", ["A", "i"])
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
    print("\n`f BETWEEN ? AND ?` is `f >= ? AND f <= ?` (SQL); over the collection field the former")
    print("types its parameters BIGINT and fails on 'A', the latter answers ['abc'].")


if __name__ == "__main__":
    main()
