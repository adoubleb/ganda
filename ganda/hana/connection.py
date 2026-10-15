"""SAP HANA's connection layer: a server attached to a running HANA Cloud database (there
is no container: HANA Cloud is a managed service, and the Document Store and the
triple store are features of an instance, not of HANA, express edition) and one
session per database of a campaign.

A "database" of the campaign is a SCHEMA (its tables, collections, views and graph
workspace) plus a NAMED GRAPH `<urn:ganda:g:<schema>>` (its triples): the doc, rel and
graph data models use the schema, the rdf data model the graph (graphs belong to the
user, not to a schema). The graph data model's state is read back THROUGH its engine
(`OPENCYPHER_TABLE` over the workspace: `_node_scan`, `_edge_scan`), so a state check
compares what openCypher sees with the Instance.

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
from ganda.hana.dialect import GRAPH_TOKEN, SCHEMA_TOKEN, child_table, from_rdf_term, graph_iri, list_text, number, opencypher_table, record_iri

SPARQL_QUERY_HEADERS = "Accept: application/sparql-results+json\r\nContent-Type: application/sparql-query"
SPARQL_UPDATE_HEADERS = ""
# client-side codes of a lost connection (negative: the driver's own)
CONNECTION_LOST = {-10709, -10807, -10108, -10821, -10806}
TIMEOUT_CODES = {613}
# a raw failure inside the engine, a finding whatever the other variants did: a general
# error, a fatal error, a transaction rolled back by an internal error, a column store
# error (to refine against live answers: HANDOFF.md)
INTERNAL_CODES = {2, 3, 129, 2048}
# ... but 129 is also how the triple store and the Document Store wrap an ORDINARY refusal
# (probed 2026-10-09: a SPARQL syntax error, an undefined prefix, an unknown function, a
# non-grouped variable -- "generic. Error - ..."; MEDIAN without GROUP BY -- "Unsupported
# functionality"; a collection's MAX over a string -- "supports only numeric values"): a
# 129 carrying one of these is a rejection, compared across variants like any other
REJECTION_129 = ("generic. Error -", "Unsupported functionality", "supports only")
# message texts that mark an internal failure whatever the code
INTERNAL_TEXTS = ("internal error", "assertion", "must not reach here")
DEFAULT_TIMEOUT_MS = 30000
# dropping and creating a campaign database: not a compared statement, so a slow one is
# waited for and retried rather than reported
ADMIN_TIMEOUT_MS = 300000
ADMIN_RETRIES = 3
BOOLEAN_TYPE_CODES = {28}

# a `:name` is a bind marker -- except a SQLScript variable of an anonymous block, which
# the renderers name `:sv_*` (`concrete_syntax/select.py`, the `block` positions): the
# block's header binds the real markers (`IN sv_p BIGINT => :p`) and its body reads them
_TOKEN = re.compile(r"('(?:[^']|'')*')|(\"(?:[^\"]|\"\")*\")|(?<![\w:]):(?!sv_)([A-Za-z_][A-Za-z0-9_]*)")
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


def sql_literal(v) -> str:
    """A bind value as a SQL constant meaning the same (probe R22): an integer as its
    digits, a double as an approximate numeric literal (`repr` with an exponent: without
    one the text is an exact numeric literal, a DECIMAL), a string as `N'...'` (the quote
    doubled), a boolean `TRUE` / `FALSE`, None `NULL`, a Decimal as its digits."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        r = repr(v)
        return r if "e" in r or "E" in r else r + "E0"
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, list):
        return "N'" + list_text(v).replace("'", "''") + "'"
    return "N'" + str(v).replace("'", "''") + "'"


def inline_binds(text: str, binds: "dict | None") -> str:
    """The statement with every `:name` marker replaced by its value's SQL constant (the
    `inline` position: the same question with the values in the text, where the plan
    cache keys and the parser's typing differ from the bound statement's); a list after
    `IN` becomes `(c1, c2, ...)`, an empty one `(NULL)`. Quoted strings and identifiers
    are left alone, as `positional` leaves them."""
    binds = binds or {}

    def sub(m):
        if m.group(1) or m.group(2):
            return m.group(0)
        v = resolve(m.group(3), binds)
        if isinstance(v, list) and not m.group(3).endswith("__json"):
            return "(" + ", ".join(sql_literal(x) for x in v) + ")" if v else "(NULL)"
        return sql_literal(v)

    return _TOKEN.sub(sub, text)


_SQLSCRIPT_TYPES = {bool: "BOOLEAN", int: "BIGINT", float: "DOUBLE", Decimal: "DECIMAL", str: "NVARCHAR(5000)"}


def sqlscript_block(text: str, binds: "dict | None", noinline: bool = False) -> str:
    """The statement as a SQLScript anonymous block (the `block` positions, probe P20):
    the header declares every bound value as a typed input parameter bound by the
    original marker (`IN sv_p BIGINT => :p`; a list after `IN` one parameter per element,
    `:p__k`; an empty list `(NULL)`), the body assigns the statement -- reading `:sv_*`
    SQLScript variables, which `positional` leaves alone -- to a table variable and
    selects it; with `noinline` the assignment carries `WITH HINT (NO_INLINE)`, the
    SQLScript optimizer's statement inlining off."""
    binds = binds or {}
    header: list = []

    def declare(name: str, v) -> str:
        sql_type = _SQLSCRIPT_TYPES.get(type(v), "NVARCHAR(5000)")
        if v is None:
            sql_type = "NVARCHAR(5000)"
        entry = f"IN sv_{name} {sql_type} => :{name}"
        if entry not in header:
            header.append(entry)
        return f":sv_{name}"

    def sub(m):
        if m.group(1) or m.group(2):
            return m.group(0)
        name = m.group(3)
        v = resolve(name, binds)
        if isinstance(v, list) and not name.endswith("__json"):
            if not v:
                return "(NULL)"
            return "(" + ", ".join(declare(f"{name}__{k}", x) for k, x in enumerate(v)) + ")"
        return declare(name, v)

    body = _TOKEN.sub(sub, text)
    do = f"DO ({', '.join(header)}) BEGIN" if header else "DO BEGIN"
    return f"{do} sv_t = {body}{' WITH HINT (NO_INLINE)' if noinline else ''}; SELECT * FROM :sv_t; END"


_BLOCK = re.compile(r"^DO(?: \((?P<header>.*?)\))? BEGIN sv_t = (?P<body>.*); SELECT \* FROM :sv_t; END$", re.S)


def unwrap_block(text: str) -> str:
    """The statement a `sqlscript_block` text wraps, its binds as `:name` markers again
    (the offline stand-in has no SQLScript); any other text unchanged."""
    m = _BLOCK.match(text)
    if not m:
        return text
    return re.sub(r"(?<![\w:]):sv_", ":", m.group("body"))


def rows_differ(a: "list | None", b: "list | None") -> bool:
    """Whether two answers to one question differ as multisets of rows (the order of
    an unordered answer is the engine's to choose)."""
    key = lambda r: repr(sorted(r.items(), key=lambda kv: kv[0])) if isinstance(r, dict) else repr(r)  # noqa: E731
    return sorted(map(key, a or [])) != sorted(map(key, b or []))


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


_DECODE_ERRORS = (ValueError, TypeError, KeyError, ArithmeticError, AttributeError)


def decoded_rows(names: list, rows, booleans: "set | None" = None) -> list:
    """Every row decoded -- or, where a value is not what its column's decoding expects
    (a typed alias over a text the engine did not promise, a JSON text that does not
    parse), an INTERNAL `EngineError` `[decode] ...`: a finding of its own, never a dead
    worker thread (the worker catches `EngineError` only)."""
    try:
        return [decode_row(names, r, booleans) for r in rows]
    except _DECODE_ERRORS as e:
        raise EngineError(f"[decode] {type(e).__name__}: {str(e)[:300]} (columns {names})", internal=True) from e


def decoded_sparql(response) -> list:
    """`sparql_rows`, a decoding failure an internal `EngineError` (as `decoded_rows`)."""
    try:
        return sparql_rows(response)
    except _DECODE_ERRORS as e:
        raise EngineError(f"[decode] {type(e).__name__}: {str(e)[:300]} (a SPARQL result)", internal=True) from e


def document_of(row: dict) -> dict:
    """The JSON document a `SELECT * FROM <collection>` row holds (as `decoded_rows`, a
    text that does not parse is an internal `EngineError`)."""
    text = next(iter(row.values()))
    try:
        return json.loads(text)
    except _DECODE_ERRORS as e:
        raise EngineError(f"[decode] {type(e).__name__}: {str(e)[:300]} (a document: {str(text)[:120]!r})", internal=True) from e


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


def _looks_float(text: str) -> bool:
    """Whether a lexical form is a (non-integer) floating value: it carries a point, an
    exponent, or is an IEEE special. A plain integer text is left to its own type."""
    t = str(text).strip()
    low = t.lower()
    if low.lstrip("+-") in ("inf", "infinity", "nan"):
        return True
    return "." in t or "e" in low


# --- the session ------------------------------------------------------------------------------


def _error(e) -> Exception:
    code = getattr(e, "errorcode", 0) or 0
    text = getattr(e, "errortext", "") or str(e)
    message = f"[{code}] {text}"[:2000]
    if code in CONNECTION_LOST:
        return EngineCrash(message)
    if code in TIMEOUT_CODES or "timeout" in text.lower() and "abort" in text.lower():
        return EngineError(message, status=code, timeout=True)
    # a message confessing an internal failure under any code is one too (a compile
    # error naming an "exception <number>" is not: the Property Graph Engine words its
    # clean refusals so, probe G35) -- unless it is the triple store's 129 wrapping of an
    # ordinary refusal, which says "internal error" every time
    rejection_129 = code == 129 and any(m in text for m in REJECTION_129)
    internal = (code in INTERNAL_CODES or any(m in text.lower() for m in INTERNAL_TEXTS)) and not rejection_129
    return EngineError(message, internal=internal, status=code)


def _dbapi():
    try:
        from hdbcli import dbapi
    except ImportError as e:  # pragma: no cover -- the live path
        raise EngineCrash("the SAP HANA client is not installed: pip install hdbcli") from e
    return dbapi


def connect(schema: "str | None" = None):
    """A new connection from the environment (`HANA_*`).

    HANA Cloud's SQL-over-HTTPS endpoint occasionally resets a connect mid-handshake
    (`-10709`, "Socket closed by peer"); the attempt is retried with a short backoff
    (`HANA_CONNECT_RETRIES`, default 4) since a reset at connect time carries no state."""
    import time

    dbapi = _dbapi()
    host = os.environ.get("HANA_HOST")
    if not host:
        raise EngineCrash("HANA_HOST is not set: ganda attaches to a running SAP HANA Cloud instance (there is no Docker option). Set HANA_HOST, HANA_PORT, HANA_USER and HANA_PASSWORD in the environment or in ganda/hana/.env (copy ganda/hana/.env.example)")
    kw = dict(
        address=host,
        port=int(os.environ.get("HANA_PORT", "443")),
        user=os.environ.get("HANA_USER", ""),
        password=os.environ.get("HANA_PASSWORD", ""),
        encrypt=os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
        sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true",
    )
    # HANA Cloud's ingress routes TLS by SNI; when reached through a port-forward (an SSH
    # tunnel to a bastion that can see the instance) `address` is the local end, so the
    # real hostname must be sent as the SNI explicitly (`HANA_SNI_HOSTNAME`). HANA Cloud
    # also redirects a connection to the data node's own public address after the
    # handshake, which bypasses the tunnel (every statement then goes direct, to the very
    # route the tunnel exists to avoid): `disableCloudRedirect` keeps the session on the
    # initial connection. Defaults on whenever the SNI override says we are tunnelling.
    sni = os.environ.get("HANA_SNI_HOSTNAME")
    if sni:
        kw["sslSNIHostname"] = sni
    if os.environ.get("HANA_DISABLE_REDIRECT", "true" if sni else "false").lower() == "true":
        kw["disableCloudRedirect"] = True
    if schema:
        kw["currentSchema"] = schema.upper()
    retries = int(os.environ.get("HANA_CONNECT_RETRIES", "4"))
    last = None
    for attempt in range(retries + 1):
        try:
            return dbapi.connect(**kw)
        except dbapi.Error as e:
            last = e
            if getattr(e, "errorcode", 0) not in CONNECTION_LOST or attempt == retries:
                break
            time.sleep(0.5 * (attempt + 1))
    raise EngineCrash(f"could not connect to SAP HANA at {host}: {last}") from last


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
        self.workspace = None  # the graph data model's GraphWorkspace step (its attribute names)
        self.lock = threading.Lock()

    # --- statements ---------------------------------------------------------------------------

    def _bind_graph(self, text: str) -> str:
        return text.replace(GRAPH_TOKEN, self.graph).replace(SCHEMA_TOKEN, self.database.upper())

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
            return decoded_rows(names, cur.fetchall(), booleans)
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
            return [] if update else decoded_sparql(result[2])
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

    # the transaction door's PEER CHECK: a second connection asks the question before and
    # after this one's uncommitted junk is in; the two answers must agree (the junk is
    # invisible to it: probe R24 / D26 / K27 / G37), else the engine let a dirty read
    # through -- an internal finding. Off where there is no second connection (offline).
    peer_check = True

    def _peer(self) -> "HanaSession":
        if getattr(self, "_peer_session", None) is None:
            self._peer_session = HanaSession(connect(self.database), self.database, self.data_model, self.timeout_ms)
            self._peer_session.types, self._peer_session.workspace = self.types, self.workspace
        return self._peer_session

    def close(self) -> None:
        for conn in (getattr(getattr(self, "_peer_session", None), "conn", None), self.conn):
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._peer_session = None

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        if _tx.is_script(text):
            parts = _tx.parts(text)
            peer = self._peer() if self.peer_check else None

            def run():
                before = peer.statement(parts[-1], binds) if peer is not None else None
                for t in parts[:-1]:
                    self.statement(t, binds)
                answer = self.statement(parts[-1], binds)
                if peer is not None:
                    after = peer.statement(parts[-1], binds)
                    if rows_differ(before, after):
                        raise EngineError(f"[dirty read] another connection's answer changed while this one's transaction was open: before {str(before)[:300]} after {str(after)[:300]}", internal=True)
                return answer

            return self._transaction(run, commit=False)
        return self.statement(text, binds)

    def run_statements(self, steps: list, transaction: bool = False) -> list:
        """A write's statements (`concrete_syntax.write.Step`): the compared answer."""

        def run():
            answer: list = []
            captured: list = []
            rows_kept: list = []
            # the row counts of every counted step so far (a write on the base class of
            # the inheritance axis is one statement per subclass: `count_part`,
            # `each_count` per class, summed)
            summed = 0
            for s in steps:
                text = s.text
                if s.role in ("each_run", "each_count"):
                    for row in rows_kept:
                        params = {**s.params, "cid": row["id"]}
                        if s.arith is not None:
                            from ganda.hana.semantics import apply_arith

                            sym, c, ftype = s.arith
                            params["cnew"] = apply_arith(row.get("v"), sym, c, ftype)
                        summed += self.sql(text, params, rows=False)
                    if s.role == "each_count":
                        answer = [{"count": summed}]
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
                        # a collection filter: one cast marker per id (`:captured__k`)
                        cast = ", ".join(f"CAST(:captured__{k} AS NVARCHAR(256))" for k in range(len(captured)))
                        text = text.replace("{{captured}}", f"({cast})")
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
                if s.role in ("count", "count_part"):
                    summed = (summed if s.role == "count_part" else 0) + self.sql(text, s.params, rows=False)
                    answer = [{"count": summed}]
                elif s.role == "rows":
                    answer = self.sql(text, s.params)
                elif s.role in ("capture", "capture_more"):
                    # `capture_more`: the rows kept for the `each_*` steps that follow are
                    # this statement's; the ids read back at the end accumulate
                    rows_kept = self.sql(text, s.params)
                    captured = (captured if s.role == "capture_more" else []) + [r["id"] for r in rows_kept]
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
        from ganda.hana.mapping.common import Declare, GraphWorkspace, Sparql, Sql, SqlBatch, Transaction

        if isinstance(step, Declare):
            self.types = dict(step.tables)
        elif isinstance(step, GraphWorkspace):
            # kept: the read-back spells the attribute names the workspace declares
            self.workspace = step
            self.sql(step.sql(), rows=False)
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

    # --- the graph data model, through its engine --------------------------------------------

    def _cypher(self, query: str, params: "dict | None" = None, binds: "dict | None" = None) -> list:
        """The rows of an openCypher query over the database's workspace."""
        return self.sql(f"SELECT * FROM {opencypher_table(query, params)}", binds)

    def _scan_items(self, var: str, entry: dict, skip: tuple = ()) -> str:
        """The RETURN items reading every attribute of a workspace entry back under the
        physical column's name (uppercased: the session lowercases what it reads), a LIST
        column aliased `__JSON` so its text decodes."""
        items = []
        for col, attr in entry["columns"]:
            if col in skip:
                continue
            suffix = "__JSON" if self.types.get(entry["label"], {}).get(col) == "list" else ""
            items.append(f"{var}.{attr} AS {col.upper()}{suffix}")
        return ", ".join(items)

    def _node_scan(self, table: str) -> str:
        """`MATCH (a:n1) RETURN a.ID AS ID, a.N1_F1 AS F1, ...`: every record of a class."""
        v = self.workspace.vertex(table)
        return f"MATCH (a:{v['label']}) RETURN {self._scan_items('a', v)}"

    def _edge_scan(self, et) -> str:
        """`MATCH (a:n1)-[e:e1]->(b:n2) RETURN e.EID AS EID, a.ID AS SRC_ID, b.ID AS DST_ID,
        e.E1_W AS W, ...`: every relationship of a type, its endpoints read from the
        vertices the engine resolved."""
        e = self.workspace.edge(et.name)
        attrs = self._scan_items("e", e, skip=("eid", "src_id", "dst_id"))
        items = "e.EID AS EID, a.ID AS SRC_ID, b.ID AS DST_ID" + (f", {attrs}" if attrs else "")
        return f"MATCH (a:{et.from_type})-[e:{e['label']}]->(b:{et.to_type}) RETURN {items}"

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        if self.data_model in ("rel", "graph"):
            rows = self.sql(f"SELECT * FROM {table}") if self.data_model == "rel" else self._cypher(self._node_scan(table))
            out = {r["id"]: {k: self._field_value(table, k, v) for k, v in r.items() if k != "id"} for r in rows}
            self._check_lists(table, rows, out)
        elif self.data_model == "doc":
            out = {}
            for r in self.sql(f"SELECT * FROM {table}"):
                doc = document_of(r)
                out[doc["id"]] = {k: v for k, v in doc.items() if k != "id"}
        else:
            out = self._rdf_records(f"c:{table}", "p:id", table)
        if ids is not None:
            wanted = set(ids)
            out = {k: v for k, v in out.items() if k in wanted}
        return out

    def _rdf_records(self, cls: str, idp: str, table: str) -> dict:
        from ganda.hana.dialect import PREFIX_TEXT

        # STR(?o) carries the exact lexical form; a double term's own serialization is
        # rounded to ~6 significant figures (issues-draft/H1), so a float field is read
        # from the string, not the rounded number
        rows = self.sparql(f"{PREFIX_TEXT} SELECT ?id ?p ?o (STR(?o) AS ?ostr) WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?n a {cls} ; {idp} ?id ; ?p ?o . }} }}")
        out: dict = {}
        items: dict = {}
        for r in rows:
            rec = out.setdefault(r["id"], {})
            p = r["p"]
            if p.startswith("urn:ganda:p:") and p not in ("urn:ganda:p:id", "urn:ganda:p:eid", "urn:ganda:p:src", "urn:ganda:p:dst"):
                f = p[len("urn:ganda:p:"):]
                o = self._exact(table, f, r["o"], r.get("ostr"))
                rec[f] = self._field_value(table, f, o) if f not in rec else f"<two values {rec[f]!r} and {o!r}>"
            elif p.startswith("urn:ganda:i:"):
                f = p[len("urn:ganda:i:"):]
                items.setdefault((r["id"], f), set()).add(self._exact(table, f, r["o"], r.get("ostr")))
        for (rid, f), elems in items.items():
            text = out.get(rid, {}).get(f)
            if not isinstance(text, list) or set(text) != elems:
                out[rid][f] = f"<list {text!r} but elements {sorted(elems)!r}>"
        for rid, rec in out.items():
            for f, ftype in self.types.get(table, {}).items():
                if ftype == "list" and isinstance(rec.get(f), list) and rec[f] and (rid, f) not in items:
                    rec[f] = f"<list {rec[f]!r} but no elements>"
        return out

    def _exact(self, table: str, field: str, value, text):
        """A float field read from STR(?o)'s exact lexical form rather than the rounded
        double term (issues-draft/H1). For a LIST field whose elements are floats the
        element predicate carries the field's own name, so the type is the field's.

        A double term is re-read whatever its lexical form looks like: STR prints with
        `%.17g`, so an integral double (2^53) comes back as `9007199254740992`, no point
        and no exponent, while its term is the rounded `9.0072e+15`."""
        ftype = self.types.get(table, {}).get(field)
        if value is not None and text is not None and ftype in ("float", "list") and (isinstance(value, float) or _looks_float(text)):
            try:
                return float(text)
            except ValueError:
                return value
        return value

    def count_nodes(self, table: str) -> int:
        if self.data_model == "rdf":
            from ganda.hana.dialect import PREFIX_TEXT

            rows = self.sparql(f"{PREFIX_TEXT} SELECT (COUNT(*) AS ?v) WHERE {{ GRAPH {GRAPH_TOKEN} {{ ?n a c:{table} }} }}")
            return rows[0]["v"] if rows else 0
        if self.data_model == "graph":
            rows = self._cypher(f"MATCH (a:{self.workspace.vertex(table)['label']}) RETURN COUNT(a.ID) AS V")
            return rows[0]["v"] if rows else 0
        return self.sql(f"SELECT COUNT(*) AS v FROM {table}")[0]["v"]

    def read_edges(self, data_model: str, instance, edge_type, sources: "list | None" = None) -> list:
        et = edge_type
        if self.data_model in ("rel", "graph"):
            rows = self.sql(f"SELECT * FROM {et.name}") if self.data_model == "rel" else self._cypher(self._edge_scan(et))
            attrs = {r["eid"]: {k: self._field_value(et.name, k, v) for k, v in r.items() if k not in ("eid", "src_id", "dst_id")} for r in rows}
            self._check_lists(et.name, rows, attrs)
            out = [(r["eid"], r["src_id"], r["dst_id"], attrs[r["eid"]]) for r in rows]
        elif self.data_model == "doc":
            out = []
            for r in self.sql(f"SELECT * FROM {et.from_type}"):
                doc = document_of(r)
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
            s.close()
        self.sessions.clear()
        if self.admin is not None:
            try:
                self.admin.close()
            except Exception:  # noqa: BLE001
                pass
            self.admin = None

    def _admin_session(self, database: str) -> HanaSession:
        return HanaSession(self.admin, database, "rel", max(self.timeout_ms, ADMIN_TIMEOUT_MS))

    def _admin(self, fn) -> None:
        """An admin step (drop / create a schema and graph), retried: under another
        worker's load a `DROP GRAPH` has outrun the query timeout ([613]), and an
        exception here used to end the worker's thread for the rest of the campaign."""
        import time

        for attempt in range(ADMIN_RETRIES + 1):
            try:
                with self.lock:
                    fn()
                return
            except EngineError as e:
                if not e.timeout or attempt == ADMIN_RETRIES:
                    raise
            time.sleep(5 * (attempt + 1))

    def drop_database(self, database: str) -> None:
        self._admin(lambda: self._drop(database))

    def _drop(self, database: str) -> None:
        s = self._admin_session(database)
        old = self.sessions.pop(database, None)
        if old is not None:
            old.close()
        try:
            s.sql(f"DROP SCHEMA {database} CASCADE", rows=False)
        except EngineError as e:
            if e.status != 362:  # invalid schema name: there was none
                raise
        s.sparql(f"DROP SILENT GRAPH {graph_iri(database)}", update=True)

    def fresh_session(self, database: str) -> HanaSession:
        def reset():
            self._drop(database)
            self._admin_session(database).sql(f"CREATE SCHEMA {database}", rows=False)

        self._admin(reset)
        data_model = database.rsplit("_", 1)[-1]
        session = HanaSession(connect(database), database, data_model, self.timeout_ms)
        self.sessions[database] = session
        return session

    def diagnostics(self) -> str:
        return f"SAP HANA at {os.environ.get('HANA_HOST')}: an attached instance, no container to inspect"


def db_name(namespace: str, data_model: str) -> str:
    return f"fz_{namespace}_{data_model}".lower()


__all__ = ["HanaServer", "HanaSession", "db_name", "positional", "record_iri"]
