"""Shared connection helper for the findings repros. Reads the `HANA_*` environment
(`set -a; source ../../../.env; set +a` from this folder, or export them yourself). Only
`hdbcli` is needed. Each repro imports `connect`, `sparql`, `sql_literal` from here.
"""
from __future__ import annotations

import json
import os

from hdbcli import dbapi

SPARQL_JSON = "Accept: application/sparql-results+json\r\nContent-Type: application/sparql-query"


def connect():
    kw = dict(
        address=os.environ["HANA_HOST"],
        port=int(os.environ.get("HANA_PORT", "443")),
        user=os.environ["HANA_USER"],
        password=os.environ["HANA_PASSWORD"],
        encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
        sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true",
    )
    if os.environ.get("HANA_SNI_HOSTNAME"):  # reached through the SSH tunnel
        kw["sslSNIHostname"] = os.environ["HANA_SNI_HOSTNAME"]
        kw["disableCloudRedirect"] = True
    return dbapi.connect(**kw)


def version(cur) -> str:
    cur.execute("SELECT VERSION FROM SYS.M_DATABASE")
    return cur.fetchone()[0]


def sparql(cur, text: str, update: bool = False):
    """Run a SPARQL query/update through SYS.SPARQL_EXECUTE; returns the raw JSON (query)
    or None (update)."""
    res = cur.callproc("SYS.SPARQL_EXECUTE", (text, "" if update else SPARQL_JSON, "?", "?"))
    return None if update else res[2]


def sparql_values(cur, text: str, var: str) -> list:
    """The values of one variable from a SPARQL SELECT."""
    doc = json.loads(sparql(cur, text))
    return [b.get(var, {}).get("value") for b in doc.get("results", {}).get("bindings", [])]


def run(cur, label: str, sql: str, params=None):
    """Run one SQL statement; print OK + rows, or the error code + message."""
    try:
        cur.execute(sql, params) if params else cur.execute(sql)
        rows = cur.fetchall() if cur.description else cur.rowcount
        print(f"  {label}: OK  {rows}")
        return rows
    except dbapi.Error as e:
        print(f"  {label}: [{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e))[:100]}")
        return None
