"""An offline stand-in for SAP HANA, for the tests: the provider's OWN session code
(`HanaSession`: parameters, the transaction door, the write steps, the state read-back,
the result decoding) over SQLite for the rel and doc data models and over rdflib for the
rdf data model.

It checks the LOGIC of every rendering against the reference -- a join that loses a
path, a filter in the wrong place, a SPARQL aggregate over the wrong group -- not HANA's
syntax or semantics, which only the live probe (`ganda hana probe`) and a live campaign
can check. What the translation does:

- rel: the HANA DDL as SQLite DDL (`CREATE COLUMN TABLE`, partitions, inverted hash
  indexes, `MERGE DELTA`), `UPSERT ... WHERE` as an UPDATE then an INSERT, `MERGE INTO
  ... USING` as an UPDATE over the source, a `DUMMY` table, a MEDIAN aggregate,
  case-sensitive LIKE;
- doc: a collection is a table of JSON texts; a path `"f"` is `json_extract(doc,
  '$.f')`, `UNNEST "e" AS "l"` a `json_each`, `FOR ANY ... SATISFIES ... END` an
  EXISTS over `json_each`, `UPDATE ... SET / UNSET` a `json_set` / `json_remove`;
- rdf: an rdflib Dataset; a SPARQL_TABLE text runs its SPARQL; a transaction is a
  snapshot of the graph restored on rollback. The answers are shaped as HANA's live
  ones are (`_as_hana`): a double term is serialized to six significant figures (H1),
  and through SPARQL_TABLE a stored value (long, double, boolean) comes back as its
  text, a boolean as "1" / "0" -- HANA does so for a predicate holding several
  datatypes across the graph; the stand-in assumes every predicate does, so every value
  projection must carry its decode suffix (or read a double through STR);
- graph: the rel translation over the same tables, the `gv_` views as SQLite views, the
  workspace step recorded (not run), and every `OPENCYPHER_TABLE(...)` of a text
  rewritten into a SQLite subquery by `opencypher_sqlite` -- a translator of the
  openCypher subset the renderers emit, mirroring the engine's documented semantics
  (one relationship per edge variable within a clause, both orientations of an
  undirected edge, a trail for a variable-length path, SUM 0 over nothing, NULL first
  ascending), so the state read back through openCypher and every graph variant are
  checked against the other data models and the reference.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from decimal import Decimal

from ganda.core.provider import EngineError, Server
from ganda.hana.concrete_syntax import tx as _tx
from ganda.hana.connection import HanaSession, decode_row, positional, sparql_rows
from ganda.hana.dialect import GRAPH_TOKEN


# SQLite has no boolean: a boolean is stored and bound as one of two strings no
# generated string holds, ordered false < true, and read back as a bool
TRUE, FALSE = "\x01T", "\x01F"
sqlite3.register_adapter(bool, lambda b: TRUE if b else FALSE)


def _bools(v):
    return True if v == TRUE else False if v == FALSE else v


_MARK_STEP = re.compile(r"^(.*?) INSERT \{ GRAPH (<[^>]+>) \{ \?n <urn:ganda:w> true \} \} WHERE (\{.*\})$", re.S)
_XSD = "http://www.w3.org/2001/XMLSchema#"


def _as_hana(response: str, table: bool) -> str:
    """rdflib's SPARQL JSON results as HANA answers them: an `xsd:double` term printed
    `%g` (H1: STR carries the exact form); with `table` (SPARQL_TABLE) a value of a
    datatype the graph stores (long, double, boolean) as plain text, a boolean as "1" /
    "0" -- HANA types a column after the datatypes its predicate holds across the whole
    graph, text when they differ, and a campaign's field predicates (`p:f1`) are shared by
    classes of different types; a computed count (`xsd:integer`) stays typed."""
    doc = json.loads(response)
    for b in doc.get("results", {}).get("bindings", []):
        for term in b.values():
            dt = term.get("datatype")
            if dt in (_XSD + "double", _XSD + "float"):
                term["value"] = "%g" % float(term["value"])
            if table and dt in (_XSD + "long", _XSD + "double", _XSD + "float", _XSD + "boolean"):
                if dt == _XSD + "boolean":
                    term["value"] = "1" if term["value"] in ("true", "1") else "0"
                del term["datatype"]
    return json.dumps(doc)


class _Avg:
    """AVG as the exact quotient (SQLite's is a double sum, inexact above 2^53)."""

    def __init__(self):
        self.values = []

    def step(self, v):
        if v is not None:
            self.values.append(v)

    def finalize(self):
        if not self.values:
            return None
        total, n = sum(self.values), len(self.values)
        if all(isinstance(v, int) for v in self.values) and total % n == 0:
            return total // n
        return total / n


class _Median:
    def __init__(self):
        self.values = []

    def step(self, v):
        if v is not None:
            self.values.append(v)

    def finalize(self):
        return statistics.median(self.values) if self.values else None


def _sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA case_sensitive_like = ON")
    conn.execute("CREATE TABLE DUMMY (dummy TEXT)")
    conn.execute("INSERT INTO DUMMY VALUES ('X')")
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("AVG", 1, _Avg)
    conn.create_function("TO_BIGINT", 1, lambda x: None if x is None else int(Decimal(str(x))))
    conn.create_function("TO_DOUBLE", 1, lambda x: None if x is None else float(x))
    conn.create_function("TO_NVARCHAR", 1, lambda x: None if x is None else str(x))
    return conn


# --- rel ----------------------------------------------------------------------------------------


def _rel_sql(text: str) -> "str | None":
    text = re.sub(r"\bCREATE COLUMN TABLE\b", "CREATE TABLE", text)
    text = re.sub(r" PARTITION BY HASH \(id\) PARTITIONS \d+", "", text)
    text = text.replace("CREATE INVERTED HASH INDEX", "CREATE INDEX")
    # a boolean is one of the two strings (`TRUE`, `FALSE`): a cast keeps it one
    text = text.replace("AS BOOLEAN)", "AS TEXT)")
    if text.startswith("MERGE DELTA OF"):
        return None
    return text


_UPSERT = re.compile(r"^UPSERT (\w+) \(([^)]*)\) VALUES \((.*)\) WHERE (\w+) = (:\w+)$", re.S)
_MERGE = re.compile(r"^MERGE INTO (\w+) USING \((.*)\) s ON \1\.(\w+) = s\.\3 WHEN MATCHED THEN UPDATE SET (.*)$", re.S)


def _split_top(text: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + [cur.strip()] if cur.strip() else out


# --- doc ----------------------------------------------------------------------------------------

_FOR_ANY = re.compile(r'FOR ANY "e" IN (\$\."\w+"|"l"\."\w+") SATISFIES (.*?) END')
_EDGE_PATH = re.compile(r'"l"\."(\w+)"')
_DOC_PATH = re.compile(r'\$\."(\w+)"')


def _json_path(expr: str) -> "tuple[str, str]":
    m = _EDGE_PATH.fullmatch(expr)
    if m:
        return '"l".value', m.group(1)
    return "doc", _DOC_PATH.fullmatch(expr).group(1)


def _doc_expr(text: str) -> str:
    """A collection query's expressions as SQLite JSON1."""
    def for_any(m):
        src, key = _json_path(m.group(1))
        cond = m.group(2).replace('"e"', "fa.value")
        return f"EXISTS (SELECT 1 FROM json_each({src}, '$.{key}') AS fa WHERE {cond})"

    text = _FOR_ANY.sub(for_any, text)
    text = re.sub(r'UNNEST \$\."(\w+)" AS "(\w)"', lambda m: f", json_each(doc, '$.{m.group(1)}') AS \"{m.group(2)}\"", text)
    text = re.sub(r'UNNEST "l"\."(\w+)" AS "y"', lambda m: f", json_each(\"l\".value, '$.{m.group(1)}') AS \"y\"", text)
    text = re.sub(r'(?<!AS )"([xy])"(?!\.value)', r'"\1".value', text)
    text = _EDGE_PATH.sub(lambda m: f"json_extract(\"l\".value, '$.{m.group(1)}')", text)
    text = _DOC_PATH.sub(lambda m: f"json_extract(doc, '$.{m.group(1)}')", text)
    text = text.replace("TO_JSON_BOOLEAN(TRUE)", "1").replace("TO_JSON_BOOLEAN(FALSE)", "0")
    text = re.sub(r"PARSE_JSON\(", "json(", text)
    text = text.replace("AS DECIMAL)", "AS REAL)")
    return text


def _literal_to_json(text: str):
    """The object expression an INSERT names (`{"id": 'a', "f": 1}`) as a value."""
    pos = 0

    def ws():
        nonlocal pos
        while pos < len(text) and text[pos] in " \n\t":
            pos += 1

    def value():
        nonlocal pos
        ws()
        ch = text[pos]
        if ch == "{":
            pos += 1
            out = {}
            ws()
            if text[pos] == "}":
                pos += 1
                return out
            while True:
                ws()
                end = text.index('"', pos + 1)
                key = text[pos + 1:end]
                pos = end + 1
                ws()
                pos += 1  # ':'
                out[key] = value()
                ws()
                if text[pos] == ",":
                    pos += 1
                    continue
                pos += 1
                return out
        if ch == "[":
            pos += 1
            out = []
            ws()
            if text[pos] == "]":
                pos += 1
                return out
            while True:
                out.append(value())
                ws()
                if text[pos] == ",":
                    pos += 1
                    continue
                pos += 1
                return out
        if ch == "'":
            buf = []
            pos += 1
            while True:
                if text[pos] == "'" and text[pos + 1:pos + 2] == "'":
                    buf.append("'")
                    pos += 2
                elif text[pos] == "'":
                    pos += 1
                    return "".join(buf)
                else:
                    buf.append(text[pos])
                    pos += 1
        m = re.compile(r"TRUE|FALSE|NULL|-?[0-9][0-9.eE+-]*").match(text, pos)
        tok = m.group(0)
        pos = m.end()
        if tok == "TRUE":
            return True
        if tok == "FALSE":
            return False
        if tok == "NULL":
            return None
        return float(tok) if any(c in tok for c in ".eE") else int(tok)

    return value()


_SET_ITEM = re.compile(r'^\$\."(\w+)" = (.*)$', re.S)


def _doc_update(text: str) -> "tuple[str, list] | str":
    """`UPDATE c SET ... UNSET ... WHERE ...` as an UPDATE of the JSON text."""
    m = re.match(r"^UPDATE (\w+) (.*?)(?: (WHERE .*))?$", text, re.S)
    table, body, where = m.group(1), m.group(2), m.group(3) or ""
    if body.startswith(f"SET {table}.$ = "):
        expr = body[len(f"SET {table}.$ = "):]
        return f"UPDATE {table} SET doc = {_doc_expr(expr)} {_doc_expr(where)}"
    sets, unsets = "", ""
    if " UNSET " in " " + body:
        parts = (" " + body).split(" UNSET ")
        sets, unsets = parts[0].strip(), parts[1].strip()
    else:
        sets = body
    sets = sets[len("SET "):] if sets.startswith("SET ") else sets
    expr = "doc"
    if sets:
        args = []
        for item in _split_top(sets):
            im = _SET_ITEM.match(item)
            key, value = im.group(1), im.group(2)
            if value.startswith("TO_JSON_BOOLEAN("):
                value = "json('true')" if "TRUE" in value else "json('false')"
            elif value == "NULL":
                value = "json('null')"
            else:
                value = _doc_expr(value)
            args.append(f"'$.{key}', {value}")
        expr = f"json_set({expr}, {', '.join(args)})"
    if unsets:
        keys = [k.strip().removeprefix("$.").strip('"') for k in unsets.split(",")]
        expr = f"json_remove({expr}, {', '.join(repr('$.' + k) for k in keys)})"
    return f"UPDATE {table} SET doc = {expr} {_doc_expr(where)}"


def _doc_sql(text: str, collections: set) -> "str | None":
    if text.startswith("CREATE COLLECTION"):
        name = text.split()[-1]
        collections.add(name)
        return f"CREATE TABLE {name} (doc TEXT)"
    if text.startswith("CREATE HASH INDEX"):
        return None
    m = re.match(r"^INSERT INTO (\w+) VALUES\((:\w+)\)$", text)
    if m:
        return f"INSERT INTO {m.group(1)} (doc) VALUES (json({m.group(2)}))"
    m = re.match(r"^INSERT INTO (\w+) VALUES\((\{.*\})\)$", text, re.S)
    if m:
        return ("literal", m.group(1), _literal_to_json(m.group(2)))
    if text.startswith("UPDATE "):
        return _doc_update(text)
    m = re.match(r"^SELECT \* FROM (\w+)$", text)
    if m and m.group(1) in collections:
        return f"SELECT doc AS {m.group(1)} FROM {m.group(1)}"
    if text.startswith("WITH "):
        return _with_views(text)
    if text.startswith("DELETE FROM ") or text.startswith("SELECT "):
        if any(re.search(rf"\bFROM {c}\b", text) for c in collections):
            return _doc_expr(text)
    return text


def _with_views(text: str) -> str:
    """A view-form text: the views' collection queries translated, the SQL on top kept."""
    out, i = "WITH ", len("WITH ")
    while True:
        m = re.compile(r"((?:raw_)?v_\w+) AS \(").match(text, i)
        if not m:
            break
        depth, j = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        body = text[m.end():j - 1]
        body = body.replace("WHEN 'true' THEN TRUE", f"WHEN 1 THEN '{TRUE}'").replace("WHEN 'false' THEN FALSE", f"WHEN 0 THEN '{FALSE}'")
        out += f"{m.group(1)} AS ({_doc_expr(body)})"
        i = j
        if text.startswith(", ", i):
            out += ", "
            i += 2
        else:
            break
    return out + text[i:]


# --- the session ------------------------------------------------------------------------------


def _params(values: list) -> list:
    return [float(v) if isinstance(v, Decimal) else v for v in values]


class OfflineSession(HanaSession):
    def __init__(self, database: str, data_model: str):
        super().__init__(None, database, data_model, timeout_ms=0)
        self.collections: set = set()
        self.cypher_ws = None  # the graph data model's workspace, for the translator
        if data_model == "rdf":
            import rdflib
            from rdflib.plugins import sparql as _sparql

            _sparql.SPARQL_LOAD_GRAPHS = False
            self.ds = rdflib.Dataset()
            self.db = None
        else:
            self.db = _sqlite()

    def apply_step(self, step) -> None:
        from ganda.hana.mapping.common import GraphWorkspace
        from tests.hana import opencypher_sqlite

        if isinstance(step, GraphWorkspace):
            # no CREATE GRAPH WORKSPACE in SQLite: the session keeps the step (the
            # read-back spells its attribute names) and the translator its workspace
            self.workspace = step
            self.cypher_ws = opencypher_sqlite.Workspace.from_step(step)
            return
        super().apply_step(step)

    # the three primitives the real session sends to the driver

    def sql(self, text: str, binds=None, rows: bool = True):
        text = self._bind_graph(text)
        if self.data_model == "rdf":
            m = re.match(r"^SELECT \* FROM SPARQL_TABLE\('(.*)'\)$", text, re.S)
            if not m:
                raise EngineError(f"the offline rdf session runs SPARQL_TABLE texts only: {text[:200]}")
            return self.sparql(m.group(1).replace("''", "'"), table=True)
        if self.data_model in ("rel", "graph"):
            if self.cypher_ws is not None and "OPENCYPHER_TABLE(" in text:
                from tests.hana import opencypher_sqlite

                try:
                    text = opencypher_sqlite.rewrite_sql(text, self.cypher_ws)
                except opencypher_sqlite.UnsupportedCypher as e:
                    raise EngineError(f"offline openCypher: {e}\n{text[:500]}") from e
            m = _UPSERT.match(text)
            if m:
                return self._upsert(m, binds)
            m = _MERGE.match(text)
            if m:
                return self._merge(m, binds, rows)
            text = _rel_sql(text)
        else:
            text = _doc_sql(text, self.collections)
            if isinstance(text, tuple):
                _op, table, doc = text
                self.db.execute(f"INSERT INTO {table} (doc) VALUES (json(?))", [json.dumps(doc)])
                return [] if rows else 1
        if text is None:
            return [] if rows else 0
        stmt, params = positional(text, binds)
        try:
            cur = self.db.execute(stmt, _params(params))
        except sqlite3.Error as e:
            raise EngineError(f"sqlite: {e}\n{stmt}") from e
        if not rows:
            return cur.rowcount
        if not cur.description:
            return []
        names = [d[0] for d in cur.description]
        return [decode_row(names, [_bools(x) for x in r]) for r in cur.fetchall()]

    def _upsert(self, m, binds):
        table, cols, vals, key, kparam = m.groups()
        cols = [c.strip() for c in cols.split(",")]
        vals = [v.strip() for v in _split_top(vals)]
        sets = ", ".join(f"{c} = {v}" for c, v in zip(cols, vals) if c != key)
        n = self.sql(f"UPDATE {table} SET {sets} WHERE {key} = {kparam}", binds, rows=False)
        if n == 0:
            self.sql(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(vals)})", binds, rows=False)
        return 1

    def _merge(self, m, binds, rows):
        table, source, key, sets = m.groups()
        assigns = []
        for item in _split_top(sets):
            col, expr = item.split(" = ", 1)
            col = col.split(".", 1)[1]
            expr = re.sub(r"\bs\.(\w+)", lambda x: f"(SELECT s.{x.group(1)} FROM ({source}) s WHERE s.{key} = {table}.{key})", expr)
            expr = re.sub(rf"\b{table}\.(\w+)", r"\1", expr)
            assigns.append(f"{col} = {expr}")
        return self.sql(f"UPDATE {table} SET {', '.join(assigns)} WHERE {key} IN (SELECT s.{key} FROM ({source}) s)", binds, rows)

    def sql_batch(self, text: str, rows: list) -> None:
        for r in rows:
            self.sql(text, r, rows=False)

    def sparql(self, text: str, update: bool = False, table: bool = False):
        text = self._bind_graph(text)
        try:
            m = _MARK_STEP.match(text) if update else None
            if m:
                # rdflib evaluates an EXISTS inside GRAPH in an update's WHERE against
                # the default graph (it does not in a query): the marked records are
                # selected by a query, then marked
                prefixes, graph, where = m.groups()
                rows = self.ds.query(f"{prefixes} SELECT DISTINCT ?n WHERE {where}")
                for (n,) in rows:
                    self.ds.update(f"INSERT DATA {{ GRAPH {graph} {{ <{n}> <urn:ganda:w> true }} }}")
                return []
            if update:
                self.ds.update(text)
                return []
            result = self.ds.query(text)
            return sparql_rows(_as_hana(result.serialize(format="json").decode("utf-8"), table))
        except Exception as e:  # noqa: BLE001 -- rdflib raises many kinds
            raise EngineError(f"rdflib: {type(e).__name__}: {e}\n{text[:3000]}") from e

    def _transaction(self, fn, commit: bool):
        if self.data_model == "rdf":
            snapshot = list(self.ds.quads((None, None, None, None)))
            try:
                out = fn()
            except BaseException:
                self._restore(snapshot)
                raise
            if not commit:
                self._restore(snapshot)
            return out
        self.db.execute("BEGIN")
        try:
            out = fn()
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT" if commit else "ROLLBACK")
        return out

    def _restore(self, quads: list) -> None:
        for g in list(self.ds.graphs()):
            self.ds.remove_graph(g)
        for s, p, o, g in quads:
            self.ds.add((s, p, o, g))


class OfflineServer(Server):
    def __init__(self):
        self.sessions: dict = {}

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.sessions.clear()

    def fresh_session(self, database: str) -> OfflineSession:
        s = OfflineSession(database, database.rsplit("_", 1)[-1])
        self.sessions[database] = s
        return s

    def drop_database(self, database: str) -> None:
        self.sessions.pop(database, None)


__all__ = ["GRAPH_TOKEN", "OfflineServer", "OfflineSession", "_tx"]
