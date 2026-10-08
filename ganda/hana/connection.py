"""SAP HANA's connection layer: a server attached to a running HANA Cloud database (there
is no container: HANA Cloud is a managed service, and the Document Store and the
triple store are features of an instance, not of HANA, express edition) and one
session per database of a campaign.

A "database" of the campaign is a SCHEMA (its tables and collections) plus a NAMED
GRAPH `<urn:ganda:g:<schema>>` (its triples): the doc and rel data models use the
schema, the rdf data model the graph (graphs belong to the user, not to a schema).

    HANA_HOST / HANA_PORT (443)      the instance's SQL endpoint
    HANA_USER / HANA_PASSWORD        a user with CREATE SCHEMA and the SPARQL QUERY /
                                     SPARQL UPDATE system privileges
    HANA_ENCRYPT (true), HANA_SSL_VALIDATE (true)

The pieces a session needs that run without a server live here as plain functions
(`positional`, `decode_row`, `sparql_rows`), so the tests exercise them offline:

- a SQL text names its parameters `:name`; `positional` turns them into `?` markers
  in order, a list into `(?, ?, ...)`, and resolves the derived names `name__json`
  (the canonical JSON text of a list), `name__dec` (a number as the decimal of its
  shortest repr) and `name__<k>` (a list's element `k`);
- a result column's name is lowercased, a DECIMAL becomes an int when integral and a
  double otherwise, and a column alias with a decoding suffix (`__json`, `__bool`,
  `__int`, `__float`, `__num`) is decoded and renamed without it;
- a SPARQL text runs through `SYS.SPARQL_EXECUTE`, its JSON results decoded per term;
- a text starting with `tx.TX_MARK` is the transaction door's script: its statements
  run in one transaction, the last answers, and the transaction rolls back.
"""
from __future__ import annotations

import json
import os
import re
import threading
from decimal import Decimal

from ganda.core.provider import EngineCrash, EngineError, Server, Session
from ganda.hana.concrete_syntax import tx as _tx
from ganda.hana.dialect import GRAPH_TOKEN, child_table, from_rdf_term, graph_iri, list_text, number, record_iri

SPARQL_QUERY_HEADERS = "Accept: application/sparql-results+json\r\nContent-Type: application/sparql-query"
SPARQL_UPDATE_HEADERS = ""
# client-side codes of a lost connection (negative: the driver's own)
CONNECTION_LOST = {-10709, -10807, -10108, -10821, -10806}
TIMEOUT_CODES = {613}
# a raw failure inside the engine, a finding whatever the other variants did: a general
# error, a fatal error, a transaction rolled back by an internal error, a column store
# error (to refine against live answers: HANDOFF.md)
INTERNAL_CODES = {2, 3, 129, 2048}
DEFAULT_TIMEOUT_MS = 30000
BOOLEAN_TYPE_CODES = {28}

_TOKEN = re.compile(r"('(?:[^']|'')*')|(\"(?:[^\"]|\"\")*\")|(?<![\w:]):([A-Za-z_][A-Za-z0-9_]*)")
_SUFFIXES = ("__json", "__bool", "__int", "__float", "__num")


# --- parameters --------------------------------------------------------------------------------


def resolve(name: str, binds: dict):
    """A parameter's value, the derived names included."""
    if name in binds:
        return binds[name]
    base, sep, suffix = name.rpartition("__")
    if not sep or not base:
        raise KeyError(f"no bind variable {name!r}")
    v = resolve(base, binds)
    if suffix == "json":
        return list_text(v)
    if suffix == "dec":
        if v is None:
            return None
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    if suffix.isdigit():
        return v[int(suffix)]
    raise KeyError(f"no bind variable {name!r}")


def positional(text: str, binds: "dict | None") -> "tuple[str, list]":
    """`:name` parameters as `?` markers, in order, with their values (a list after
    `IN` becomes one marker per element; an empty one `(NULL)`); quoted strings and
    identifiers are left alone."""
    binds = binds or {}
    params: list = []

    def sub(m):
        if m.group(1) or m.group(2):
            return m.group(0)
        v = resolve(m.group(3), binds)
        if isinstance(v, list):
            if not v:
                return "(NULL)"
            params.extend(v)
            return "(" + ", ".join("?" for _ in v) + ")"
        params.append(v)
        return "?"

    return _TOKEN.sub(sub, text), params


# --- results ----------------------------------------------------------------------------------


def _plain(v):
    if isinstance(v, Decimal):
        return number(v)
    if hasattr(v, "read") and callable(v.read):
        data = v.read()
        return data.decode("utf-8") if isinstance(data, bytes) else data
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return v


def decode_column(name: str, v):
    """(the column's name, its value) with a decoding suffix applied and dropped."""
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if v is None:
                return base, None
            if suffix == "__json":
                return base, json.loads(v) if isinstance(v, str) else v
            if suffix == "__bool":
                return base, v if isinstance(v, bool) else (str(v).lower() in ("true", "1"))
            if suffix == "__int":
                return base, int(v) if not isinstance(v, str) else int(Decimal(v))
            if suffix == "__float":
                return base, float(v)
            return base, number(Decimal(str(v)))
    return name, v


def decode_row(names: list, values, booleans: "set | None" = None) -> dict:
    out = {}
    for i, (name, v) in enumerate(zip(names, values)):
        v = _plain(v)
        if booleans and i in booleans and v in (0, 1) and not isinstance(v, bool):
            v = bool(v)
        k, v = decode_column(name.lower(), v)
        out[k] = v
    return out


def sparql_rows(response) -> list:
    """The rows of a SPARQL JSON result (`application/sparql-results+json`)."""
    if response is None:
        return []
    if not isinstance(response, (str, dict)):
        response = _plain(response)
    doc = json.loads(response) if isinstance(response, str) else response
    if "boolean" in doc:
        return [{"v": bool(doc["boolean"])}]
    variables = doc.get("head", {}).get("vars", [])
    rows = []
    for b in doc.get("results", {}).get("bindings", []):
        row = {}
        for var in variables:
            k, v = decode_column(var.lower(), from_rdf_term(b.get(var)))
            row[k] = v
        rows.append(row)
    return rows


def is_sparql(text: str) -> bool:
    return text.lstrip().startswith("PREFIX")


# --- the session ------------------------------------------------------------------------------


def _error(e) -> Exception:
    code = getattr(e, "errorcode", 0) or 0
    text = getattr(e, "errortext", "") or str(e)
    message = f"[{code}] {text}"[:2000]
    if code in CONNECTION_LOST:
        return EngineCrash(message)
    if code in TIMEOUT_CODES or "timeout" in text.lower() and "abort" in text.lower():
        return EngineError(message, status=code, timeout=True)
    return EngineError(message, internal=code in INTERNAL_CODES, status=code)


def _dbapi():
    try:
        from hdbcli import dbapi
    except ImportError as e:  # pragma: no cover -- the live path
        raise EngineCrash("the SAP HANA client is not installed: pip install hdbcli") from e
    return dbapi


def connect(schema: "str | None" = None):
    """A new connection from the environment (`HANA_*`)."""
    dbapi = _dbapi()
    host = os.environ.get("HANA_HOST")
    if not host:
        raise EngineCrash("HANA_HOST is not set: ganda attaches to a running SAP HANA Cloud instance (HANA_HOST, HANA_PORT, HANA_USER, HANA_PASSWORD)")
    kw = dict(
        address=host,
        port=int(os.environ.get("HANA_PORT", "443")),
        user=os.environ.get("HANA_USER", ""),
        password=os.environ.get("HANA_PASSWORD", ""),
        encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
        sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true",
    )
    if schema:
        kw["currentSchema"] = schema.upper()
    try:
        return dbapi.connect(**kw)
    except dbapi.Error as e:
        raise EngineCrash(f"could not connect to SAP HANA at {host}: {e}") from e


class HanaSession(Session):
    """One database: a schema and a graph, through one connection (a session is used by
    one worker thread at a time)."""

    def __init__(self, conn, database: str, data_model: str, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.conn = conn
        self.database = database
        self.data_model = data_model
        self.graph = graph_iri(database)
        self.timeout_ms = timeout_ms
        self.types: dict = {}  # table -> {field: FieldType.value} (the plan's Declare)
        self.lock = threading.Lock()

    # --- statements ---------------------------------------------------------------------------

    def _bind_graph(self, text: str) -> str:
        return text.replace(GRAPH_TOKEN, self.graph)

    def _cursor(self):
        cur = self.conn.cursor()
        if self.timeout_ms and hasattr(cur, "setquerytimeout"):
            cur.setquerytimeout(max(1, self.timeout_ms // 1000))
        return cur

    def sql(self, text: str, binds: "dict | None" = None, rows: bool = True):
        """Run one SQL statement: its rows, or with `rows` False its row count."""
        dbapi = _dbapi()
        stmt, params = positional(self._bind_graph(text), binds)
        cur = self._cursor()
        try:
            cur.execute(stmt, params) if params else cur.execute(stmt)
            if not rows:
                return cur.rowcount
            if not cur.description:
                return []
            names = [d[0] for d in cur.description]
            booleans = {i for i, d in enumerate(cur.description) if d[1] in BOOLEAN_TYPE_CODES}
            return [decode_row(names, r, booleans) for r in cur.fetchall()]
        except dbapi.Error as e:
            raise _error(e) from e
        finally:
            cur.close()

    def sql_batch(self, text: str, rows: list) -> None:
        dbapi = _dbapi()
        if not rows:
            return
        stmt, _ = positional(text, rows[0])
        cur = self._cursor()
        try:
            cur.executemany(stmt, [positional(text, r)[1] for r in rows])
        except dbapi.Error as e:
            raise _error(e) from e
        finally:
            cur.close()

    def sparql(self, text: str, update: bool = False):
        """Run one SPARQL query (its rows) or update (no rows) through SPARQL_EXECUTE."""
        dbapi = _dbapi()
        cur = self._cursor()
        try:
            result = cur.callproc("SYS.SPARQL_EXECUTE", (self._bind_graph(text), SPARQL_UPDATE_HEADERS if update else SPARQL_QUERY_HEADERS, "?", "?"))
            return [] if update else sparql_rows(result[2])
        except dbapi.Error as e:
            raise _error(e) from e
        finally:
            cur.close()

    def statement(self, text: str, binds: "dict | None" = None) -> list:
        """A query text of a case (SPARQL or SQL): its rows."""
        if is_sparql(text):
            return self.sparql(text, update=not _is_sparql_query(text))
        return self.sql(text, binds)

    def _transaction(self, fn, commit: bool):
        self.conn.setautocommit(False)
        try:
            out = fn()
            (self.conn.commit if commit else self.conn.rollback)()
            return out
        except BaseException:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001 -- the first failure is the one reported
                pass
            raise
        finally:
            self.conn.setautocommit(True)

    # --- the contract -------------------------------------------------------------------------

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        if _tx.is_script(text):
            parts = _tx.parts(text)

            def run():
                for t in parts[:-1]:
                    self.statement(t, binds)
                return self.statement(parts[-1], binds)

            return self._transaction(run, commit=False)
        return self.statement(text, binds)

    def run_statements(self, steps: list, transaction: bool = False) -> list:
        """A write's statements (`concrete_syntax.write.Step`): the compared answer."""

        def run():
            answer: list = []
            captured: list = []
            rows_kept: list = []
            for s in steps:
                text = s.text
                if s.role in ("each_run", "each_count"):
                    total = 0
                    for row in rows_kept:
                        params = {**s.params, "cid": row["id"]}
                        if s.arith is not None:
                            from ganda.hana.semantics import apply_arith

                            sym, c, ftype = s.arith
                            params["cnew"] = apply_arith(row.get("v"), sym, c, ftype)
                        total += self.sql(text, params, rows=False)
                    if s.role == "each_count":
                        answer = [{"count": total}]
                    continue
                if s.role == "rows_captured":
                    if not captured:
                        # nothing matched: no row to read back (and an empty VALUES
                        # block is a parse error on some engines)
                        answer = []
                        continue
                    if is_sparql(text):
                        from ganda.hana.dialect import sparql_string

                        text = text.replace("{{captured}}", " ".join(sparql_string(x) for x in captured))
                        answer = self.sparql(text)
                    else:
                        answer = self.sql(text, {**s.params, "captured": captured})
                    continue
                if is_sparql(text):
                    is_query = _is_sparql_query(text)
                    rows = self.sparql(text, update=not is_query)
                    if s.role == "count_rows":
                        answer = [{"count": rows[0]["v"] if rows else 0}]
                    elif s.role == "rows":
                        answer = rows
                    elif s.role == "capture":
                        rows_kept = rows
                        captured = [r["id"] for r in rows]
                    continue
                if s.role == "count":
                    answer = [{"count": self.sql(text, s.params, rows=False)}]
                elif s.role == "rows":
                    answer = self.sql(text, s.params)
                elif s.role == "capture":
                    rows_kept = self.sql(text, s.params)
                    captured = [r["id"] for r in rows_kept]
                else:
                    self.sql(text, s.params, rows=False)
            return answer

        if transaction:
            return self._transaction(run, commit=True)
        return run()

    def apply_setup(self, plan: list) -> None:
        for step in plan:
            self.apply_step(step)

    def apply_step(self, step) -> None:
        from ganda.hana.mapping.common import Declare, Sparql, Sql, SqlBatch, Transaction

        if isinstance(step, Declare):
            self.types = dict(step.tables)
        elif isinstance(step, Sql):
            self.sql(step.text, step.params, rows=False)
        elif isinstance(step, SqlBatch):
            self.sql_batch(step.text, step.rows)
        elif isinstance(step, Sparql):
            self.sparql(step.text, update=True)
        elif isinstance(step, Transaction):
            self._transaction(lambda: [self.apply_step(s) for s in step.steps], commit=step.commit)
        else:
            raise TypeError(f"not a SAP HANA setup step: {step!r}")

    # --- reading the stored state back --------------------------------------------------------

    def _field_value(self, table: str, field: str, v):
        if v is None:
            return None
        if self.types.get(table, {}).get(field) == "list" and isinstance(v, str):
            return json.loads(v)
        if self.types.get(table, {}).get(field) == "bool" and not isinstance(v, bool):
            return str(v).lower() in ("true", "1")
        return v

    def _elements(self, table: str, field: str) -> dict:
        """owner -> the elements of a LIST column's child table, in position order."""
        out: dict = {}
        for r in self.sql(f"SELECT owner, pos, v FROM {child_table(table, field)} ORDER BY owner, pos"):
            out.setdefault(r["owner"], []).append(r["v"])
        return out

    def _check_lists(self, table: str, rows: dict, key_rows: dict) -> None:
        """A list whose text and child rows disagree reads as a marker the state check
        reports."""
        for f, ftype in self.types.get(table, {}).items():
            if ftype != "list":
                continue
            elements = self._elements(table, f)
            for rid, values in key_rows.items():
                text = values.get(f)
                got = elements.get(rid, [])
                if (text or []) != got:
                    values[f] = f"<list text {text!r} but child rows {got!r}>"

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        if self.data_model == "rel":
            rows = self.sql(f"SELECT * FROM {table}")
            out = {r["id"]: {k: self._field_value(table, k, v) for k, v in r.items() if k != "id"} for r in rows}
            self._check_lists(table, rows, out)
        elif self.data_model == "doc":
            out = {}
            for r in self.sql(f"SELECT * FROM {table}"):
                doc = json.loads(next(iter(r.values())))
                out[doc["id"]] = {k: v for k, v in doc.items() if k != "id"}
        else:
            out = self._rdf_records(f"c:{table}", "p:id", table)
        if ids is not None:
            wanted = set(ids)
            out = {k: v for k, v in out.items() if k in wanted}
        return out

    def _rdf_records(self, cls: str, idp: str, table: str) -> dict:
        from ganda.hana.dialect import PREFIX_TEXT

        rows = self.sparql(f"{PREFIX_TEXT} SELECT ?id ?p ?o WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?n a {cls} ; {idp} ?id ; ?p ?o . }} }}")
        out: dict = {}
        items: dict = {}
        for r in rows:
            rec = out.setdefault(r["id"], {})
            p = r["p"]
            if p.startswith("urn:ganda:p:") and p not in ("urn:ganda:p:id", "urn:ganda:p:eid", "urn:ganda:p:src", "urn:ganda:p:dst"):
                f = p[len("urn:ganda:p:"):]
                rec[f] = self._field_value(table, f, r["o"]) if f not in rec else f"<two values {rec[f]!r} and {r['o']!r}>"
            elif p.startswith("urn:ganda:i:"):
                items.setdefault((r["id"], p[len("urn:ganda:i:"):]), set()).add(r["o"])
        for (rid, f), elems in items.items():
            text = out.get(rid, {}).get(f)
            if not isinstance(text, list) or set(text) != elems:
                out[rid][f] = f"<list {text!r} but elements {sorted(elems)!r}>"
        for rid, rec in out.items():
            for f, ftype in self.types.get(table, {}).items():
                if ftype == "list" and isinstance(rec.get(f), list) and rec[f] and (rid, f) not in items:
                    rec[f] = f"<list {rec[f]!r} but no elements>"
        return out

    def count_nodes(self, table: str) -> int:
        if self.data_model == "rdf":
            from ganda.hana.dialect import PREFIX_TEXT

            rows = self.sparql(f"{PREFIX_TEXT} SELECT (COUNT(*) AS ?v) WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?n a c:{table} }} }}")
            return rows[0]["v"] if rows else 0
        return self.sql(f"SELECT COUNT(*) AS v FROM {table}")[0]["v"]

    def read_edges(self, data_model: str, instance, edge_type, sources: "list | None" = None) -> list:
        et = edge_type
        if self.data_model == "rel":
            rows = self.sql(f"SELECT * FROM {et.name}")
            attrs = {r["eid"]: {k: self._field_value(et.name, k, v) for k, v in r.items() if k not in ("eid", "src_id", "dst_id")} for r in rows}
            self._check_lists(et.name, rows, attrs)
            out = [(r["eid"], r["src_id"], r["dst_id"], attrs[r["eid"]]) for r in rows]
        elif self.data_model == "doc":
            out = []
            for r in self.sql(f"SELECT * FROM {et.from_type}"):
                doc = json.loads(next(iter(r.values())))
                for link in doc.get(et.name) or []:
                    out.append((link.get("eid"), doc["id"], link.get("to"), {k: v for k, v in link.items() if k not in ("eid", "to")}))
        else:
            out = self._rdf_edges(et)
        if sources is not None:
            wanted = set(sources)
            out = [x for x in out if x[1] in wanted]
        return out

    def _rdf_edges(self, et) -> list:
        from ganda.hana.dialect import PREFIX_TEXT

        records = self._rdf_records(f"c:{et.name}", "p:eid", et.name)
        ends = self.sparql(f"{PREFIX_TEXT} SELECT ?eid ?s ?t WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?e a c:{et.name} ; p:eid ?eid ; p:src ?a ; p:dst ?b . ?a p:id ?s . ?b p:id ?t . }} }}")
        out = [(r["eid"], r["s"], r["t"], records.get(r["eid"], {})) for r in ends]
        links = self.sparql(f"{PREFIX_TEXT} SELECT ?s ?t WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?a l:{et.name} ?b . ?a p:id ?s . ?b p:id ?t . }} }}")
        stored = {(r["s"], r["t"]) for r in links}
        expected = {(s, t) for _e, s, t, _a in out}
        for s, t in sorted(stored ^ expected):
            # a direct link without a relationship, or a relationship without its link
            out.append((f"{et.name}/link:{s}->{t}", s, t, {}))
        return out

    def count_edges(self, data_model: str, instance, edge_type) -> int:
        return len(self.read_edges(data_model, instance, edge_type))

    def ping(self):
        if self.data_model == "rdf":
            return self.sparql("PREFIX p: <urn:ganda:p:> SELECT (1 AS ?ok) WHERE {}")
        return self.sql("SELECT 1 AS ok FROM DUMMY")


def _is_sparql_query(text: str) -> bool:
    body = re.sub(r"PREFIX\s+\w*:\s*<[^>]*>", "", text).lstrip().upper()
    return body.startswith(("SELECT", "ASK", "CONSTRUCT", "DESCRIBE"))


class HanaServer(Server):
    """An attached HANA Cloud instance: every database is a schema and a graph of it."""

    external = True

    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.timeout_ms = timeout_ms
        self.admin = None
        self.sessions: dict = {}
        # the admin connection is shared by the campaign's worker threads; a driver
        # connection is not safe across threads
        self.lock = threading.Lock()

    def start(self) -> None:
        self.admin = connect()

    def stop(self) -> None:
        for s in list(self.sessions.values()):
            try:
                s.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.sessions.clear()
        if self.admin is not None:
            try:
                self.admin.close()
            except Exception:  # noqa: BLE001
                pass
            self.admin = None

    def _admin_session(self, database: str) -> HanaSession:
        return HanaSession(self.admin, database, "rel", self.timeout_ms)

    def drop_database(self, database: str) -> None:
        with self.lock:
            self._drop(database)

    def _drop(self, database: str) -> None:
        s = self._admin_session(database)
        old = self.sessions.pop(database, None)
        if old is not None:
            try:
                old.conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            s.sql(f"DROP SCHEMA {database} CASCADE", rows=False)
        except EngineError as e:
            if e.status != 362:  # invalid schema name: there was none
                raise
        s.sparql(f"DROP SILENT GRAPH {graph_iri(database)}", update=True)

    def fresh_session(self, database: str) -> HanaSession:
        with self.lock:
            self._drop(database)
            self._admin_session(database).sql(f"CREATE SCHEMA {database}", rows=False)
        data_model = database.rsplit("_", 1)[-1]
        session = HanaSession(connect(database), database, data_model, self.timeout_ms)
        self.sessions[database] = session
        return session

    def diagnostics(self) -> str:
        return f"SAP HANA at {os.environ.get('HANA_HOST')}: an attached instance, no container to inspect"


def db_name(namespace: str, data_model: str) -> str:
    return f"fz_{namespace}_{data_model}".lower()


__all__ = ["HanaServer", "HanaSession", "db_name", "positional", "record_iri"]
