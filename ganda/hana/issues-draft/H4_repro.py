"""Minimal reproduction of H4: SQL over JSON Document Store collections read through UNNEST
adds up the values of rows the JOIN excludes. A SUM over a join that matches NO row fails
with [314] numeric overflow when the excluded rows' values exceed BIGINT together; the
same query over column tables answers NULL.

    HANA_HOST=... HANA_PORT=443 HANA_USER=... HANA_PASSWORD=... python3 -I H4_repro.py

Creates a scratch schema with three collections and the same data as column tables, runs
the query on both, drops the schema.
"""
from __future__ import annotations

import json
import os

from hdbcli import dbapi

S = "H4_REPRO"


def connect():
    kw = dict(address=os.environ["HANA_HOST"], port=int(os.environ.get("HANA_PORT", "443")),
              user=os.environ["HANA_USER"], password=os.environ["HANA_PASSWORD"],
              encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
              sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true")
    if os.environ.get("HANA_SNI_HOSTNAME"):  # reached through an SSH tunnel
        kw["sslSNIHostname"] = os.environ["HANA_SNI_HOSTNAME"]
        kw["disableCloudRedirect"] = True
    return dbapi.connect(**kw)


def run(cur, label, sql):
    try:
        cur.execute(sql)
        print(f"{label}: OK {cur.fetchall()}")
    except dbapi.Error as e:
        print(f"{label}: [{e.errorcode}] {e.errortext.strip()[:90]}")


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
        # a start with no link; a middle document whose links carry 2^63 - 1 and 1
        for c in ("st", "mid"):
            cur.execute(f"CREATE COLLECTION {c}")
        cur.execute("INSERT INTO st VALUES(?)", [json.dumps({"id": "s1", "e1": []})])
        cur.execute("INSERT INTO mid VALUES(?)", [json.dumps({"id": "m1", "e2": [{"eid": "x1", "to": "t1", "w": 9223372036854775807}, {"eid": "x2", "to": "t1", "w": 1}]})])
        common = ('WITH v_mid AS (SELECT $."id" AS id FROM mid), '
                  'v_e1 AS (SELECT $."id" AS src_id, "l"."to" AS dst_id FROM st UNNEST $."e1" AS "l"), ')
        # the link read raw (its id, its ends, its value) and its value cast in a CTE of its
        # own -- how Ganda's views type a field; and the same with the value column alone
        wide = common + ('raw_v_e2 AS (SELECT "l"."eid" AS eid, $."id" AS src_id, "l"."to" AS dst_id, "l"."w" AS w FROM mid UNNEST $."e2" AS "l"), '
                         'v_e2 AS (SELECT eid, src_id, dst_id, TO_BIGINT(w) AS w FROM raw_v_e2) ')
        narrow = common + ('raw_v_e2 AS (SELECT $."id" AS src_id, "l"."w" AS w FROM mid UNNEST $."e2" AS "l"), '
                           'v_e2 AS (SELECT src_id, TO_BIGINT(w) AS w FROM raw_v_e2) ')
        paths = "SELECT j1.src_id AS strt, j2.w AS v FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id"
        run(cur, "collections: the join's rows               ", wide + paths)
        run(cur, "collections, link read whole: SUM          ", wide + f"SELECT SUM(p.v) FROM ({paths}) p")
        run(cur, "collections, link read whole: SUM(DECIMAL) ", wide + f"SELECT SUM(TO_DECIMAL(p.v)) FROM ({paths}) p")
        run(cur, "collections, value column only: SUM       ", narrow + f"SELECT SUM(p.v) FROM ({paths}) p")

        # the same data as column tables
        cur.execute("CREATE COLUMN TABLE r_mid (id NVARCHAR(10))")
        cur.execute("INSERT INTO r_mid VALUES ('m1')")
        cur.execute("CREATE COLUMN TABLE r_e1 (src_id NVARCHAR(10), dst_id NVARCHAR(10))")
        cur.execute("CREATE COLUMN TABLE r_e2 (src_id NVARCHAR(10), dst_id NVARCHAR(10), w BIGINT)")
        cur.execute("INSERT INTO r_e2 VALUES ('m1', 't1', 9223372036854775807)")
        cur.execute("INSERT INTO r_e2 VALUES ('m1', 't1', 1)")
        rpaths = "SELECT j1.src_id AS strt, j2.w AS v FROM r_e1 j1 JOIN r_mid t1 ON t1.id = j1.dst_id JOIN r_e2 j2 ON j2.src_id = t1.id"
        run(cur, "column tables: SUM over the join          ", f"SELECT SUM(p.v) FROM ({rpaths}) p")
    finally:
        cur.execute(f"DROP SCHEMA {S} CASCADE")
    print("\nThe join matches no row, so the SUM is NULL. With the link read whole, HANA raises a numeric")
    print("overflow: it adds up the two excluded links (2^63 - 1 + 1) the join discards. Summed as")
    print("DECIMAL, or with the value column read alone, the same query answers NULL.")


if __name__ == "__main__":
    main()
