"""The concrete syntax catalog of the select AST: HOW each SAP HANA data model asks a
`SelectQuery`. A concrete syntax is three independent choices; every applicable
combination is an equivalent text.

    axis      choice        the text
    filter    plain         the filter as drawn
              rw            an equivalent tree (`rewrites.rewrite`: three-valued De
                            Morgan, `IN [x]` for `= x`, the complement under NOT, ...)
              rev           the reversible leaves' operands swapped (`:p > f`)
              arith         `(f <sym> :c) op :pc`, exact on every stored value
    position  table         rel: `FROM t`; doc: the COLLECTION queried directly (its
                            filter evaluated by the document store); rdf: a `GRAPH`
                            block
              scan          rel, doc views: `FROM (SELECT * FROM t) t`
              nest          rel, doc views: the filter inside the derived table, the
                            kind's projection, aggregate, grouping or ordering outside;
                            rdf: the filter in a subquery
              view          doc: the question in SQL over the views of the collections
                            (`document`), the filter evaluated by SQL
              from          rdf: the graph named by `FROM <g>`
              table_fn      rdf: the SPARQL text inside SQL's `SPARQL_TABLE`
              native        rdf: the filter's unknowns left to SPARQL's error
                            propagation instead of spelled out
              match         graph: the filter in openCypher (`graph.scan`), the kind
                            in SQL over what it projected
              literal       graph: the native text with its constants inlined
                            instead of bound (`graph.literal`)
    form      base          the kind's own text
              order         `max` / `min` as the first row of an ordering
              fn            a distinct count as `COUNT(*)` over `SELECT DISTINCT`
              jscan         a join's inner relation as a derived table
              jrev          a join's correlation with its operands swapped

A variant is `<data model>:<tag>`, the tag the non-default choices joined by `+` (form,
filter, position). The base (plain, table, base) is asked of every data model; on the
doc data model it is the collection query where the document store can ask the
question (no semi-join, join, method or median there) and the views otherwise; on the
graph data model it is openCypher where the language states the question
(`_cypher_asks`: no method, avg, median or COUNT(DISTINCT); aggregates over numbers)
and SQL over the `g_` CTEs otherwise (`concrete_syntax.graph`), which `scan` / `nest`
/ `view` ask too.

An edge-type target reads the junction table (`eid AS id`), the doc data model's link
objects (`UNNEST "e" AS "l"` on the source class's collection) and the rdf data model's
relationship resources.

The transaction door (`tx`) wraps every concrete syntax alike (`tx.script`).
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.ast.predicates import MIRROR, Predicate, fields_of
from ganda.core.ast.select import SelectQuery, column, notnull_fields
from ganda.core.model import FieldType, Instance, Schema
from ganda.hana import rewrites as _rewrites
from ganda.hana.concrete_syntax import document, graph, tx as _tx
from ganda.hana.concrete_syntax.methods import METHODS, method_fn, result_type, sparql_text, sql_text  # noqa: F401
from ganda.hana.concrete_syntax.predicate import REL, Names, sparql_filter, sql
from ganda.hana.concrete_syntax.traversal import agg_proj, guarded, sparql_table
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT

FILTERS = ("plain", "rw", "rev", "arith")
POSITIONS = ("table", "scan", "nest", "view", "from", "table_fn", "native", "match", "literal")
FORMS = ("base", "order", "fn", "jscan", "jrev")
DATA_MODELS = ("doc", "rel", "graph", "rdf")
_POSITIONS_OF = {
    "rel": ("table", "scan", "nest"),
    "doc": ("table", "scan", "nest", "view"),
    "graph": ("table", "scan", "nest", "view", "match", "literal"),
    "rdf": ("table", "nest", "from", "table_fn", "native"),
}
# the largest LIMIT, where an ordering has an OFFSET and no limit of its own
NO_LIMIT = 2147483647
# every group of a GROUP BY holds a solution: a no-op by the standard, which keeps an
# engine from answering an empty group over no solutions (rdflib does)
HAVING = "HAVING (COUNT(*) > 0)"


@dataclass(frozen=True)
class SelectSyntax:
    filter: str = "plain"
    position: str = "table"
    form: str = "base"

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, "base"), (self.filter, "plain"), (self.position, "table")) if x != default]
        return "+".join(parts)

    @property
    def axes(self) -> int:
        return len([x for x in (self.form != "base", self.filter != "plain", self.position != "table") if x])


BASE = SelectSyntax()


@dataclass
class Rewrites:
    rw: object = None
    rev: object = None
    arith: object = None
    present: list = field(default_factory=list)

    def filter_pred(self, q: SelectQuery, name: str):
        return q.where if name == "plain" else getattr(self, name)


def all_syntaxes() -> list:
    return [SelectSyntax(f, p, m) for m, f, p in itertools.product(FORMS, FILTERS, POSITIONS)]


# --- what the doc data model's collection query can ask ---------------------------------------


def collection_asks(q: SelectQuery, schema: Schema, pred) -> bool:
    """Whether the document store asks the question itself: no semi-join, no join, no
    method, no median, and a filter whose leaves it renders."""
    if q.kind in ("join", "method") or (q.kind == "agg" and q.fn == "median"):
        return False
    if _string_extremum(q, schema):
        return False
    return document.doc_filter_ok(pred)


def _string_extremum(q: SelectQuery, schema: "Schema | None") -> bool:
    """A MAX / MIN over a string: the collection query takes them over numbers only ("max
    supports only numeric values", an error code 129); the views answer it in SQL. Without
    a schema (`base_data_models`) the type is unknown and every MAX / MIN counts as one,
    so a case the views cannot ask is marked partial rather than missing its doc base."""
    if q.fn not in ("max", "min") or q.kind not in ("agg", "group"):
        return False
    return schema is None or _ftype(schema, q, q.field if q.kind == "agg" else q.arg) is FieldType.STRING


def _views_ask(q: SelectQuery, pred) -> bool:
    if not document.views_ask(pred):
        return False
    # a LIST-membership (CONTAINS*) on an EDGE type renders as EXISTS over the element
    # view, which for an edge is a double UNNEST (the link array, then the attribute
    # list); the Document Store refuses that doubly-nested UNNEST view inside a subquery
    # ("unsupported form of subquery with UNNEST"). A node's single-UNNEST element view
    # is fine. The collection's own FOR ANY filter covers the edge shape.
    if q.edge and pred is not None and any(leaf.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL") for leaf in pred.leaves()):
        return False
    return True


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if sp.position not in _POSITIONS_OF[data_model]:
        return False
    if sp.filter != "plain" and (q.where is None or getattr(rewrites, sp.filter) is None):
        return False
    pred = rewrites.filter_pred(q, sp.filter)
    if sp.form in ("jscan", "jrev") and q.kind != "join":
        return False
    if sp.form == "order" and not (q.kind == "agg" and q.fn in ("max", "min") and rewrites.present):
        return False
    if sp.form == "fn" and not (q.kind == "agg" and q.fn == "distinct_count"):
        return False
    if sp.position == "nest" and q.where is None:
        return False
    if data_model == "doc":
        if sp.filter == "arith" and pred is not None and any(leaf.ftype == "float" for leaf in pred.leaves()):
            # a DECIMAL-stored double does not scale like a double (0.30000000000000004 * 2)
            return False
        if sp.form == "fn" and sp.position == "table" and collection_asks(q, schema, pred):
            # the `fn` distinct count nests a GROUP BY in a subquery; over a COLLECTION the
            # Document Store refuses it ("GROUP BY clause after GROUP BY clause on collection
            # tables") -- the base COUNT(DISTINCT) form and the views cover it
            return False
        if sp.position == "table":
            # the base is the collection query, or the views where the store cannot ask
            if sp == BASE:
                return collection_asks(q, schema, pred) or _views_ask(q, pred)
            return collection_asks(q, schema, pred)
        return _views_ask(q, pred)
    if data_model == "graph":
        if q.tx and not graph.TX_VISIBLE:
            return False
        if sp.position == "literal":
            return pred is not None and graph.binds_parameters(pred) and graph.literal_safe(pred) and _cypher_asks(q, schema, sp, pred)
        if sp.position == "match":
            return pred is not None and graph.expressible(pred, schema, q.table)
        if sp.position == "view":
            # the views text differs from the base only where the base is native
            return _cypher_asks(q, schema, sp, pred)
        return True
    if data_model == "rdf" and sp.position == "nest" and q.kind == "join":
        return False
    if sp.position == "native" and pred is None:
        return False
    return True


def base_data_models(q: SelectQuery) -> tuple:
    """The data models the question has a base concrete syntax on: the doc data model
    lacks one where neither its collection query nor its views can ask it (a semi-join
    or a negated membership next to a whole-list equality); the graph data model lacks
    one for a transaction door its engine cannot see into (`graph.TX_VISIBLE`)."""
    models = DATA_MODELS if collection_asks(q, None, q.where) or _views_ask(q, q.where) else ("rel", "graph", "rdf")
    if q.tx and not graph.TX_VISIBLE:
        models = tuple(m for m in models if m != "graph")
    return models


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes() if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering --------------------------------------------------------------------------------


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    pred = rewrites.filter_pred(q, sp.filter)
    if data_model == "rel":
        text = _sql(q, schema, sp, pred, REL)
    elif data_model == "doc":
        if sp.position == "table" and collection_asks(q, schema, pred):
            text = _collection(q, schema, sp, pred)
        else:
            text = document.with_views(schema, _sql(q, schema, sp, pred, document.NAMES))
    elif data_model == "graph":
        if sp.position in ("table", "literal") and _cypher_asks(q, schema, sp, pred):
            text = _cypher_select(q, schema, sp, pred, literal=sp.position == "literal")
        elif sp.position == "match":
            text = _sql(q, schema, sp, None, graph.NAMES, source=f"(SELECT * FROM {graph.scan(schema, q.table, pred)})")
        else:
            text = _sql(q, schema, sp, pred, graph.NAMES)
        text = graph.with_views(schema, text)
    elif sp.position == "table_fn":
        text = sparql_table(_sparql(q, schema, sp, pred, "table"))
    else:
        text = _sparql(q, schema, sp, pred, sp.position)
    if q.tx:
        return _tx.script(_tx.statements(q.tx, schema, data_model), text)
    return text


def _ftype(schema: Schema, q: SelectQuery, f: str) -> FieldType:
    return column(schema, q, f).type


def _json_alias(schema: Schema, q: SelectQuery, f: str, alias: str) -> str:
    """A projected column's alias: `__json` for a LIST (its text, decoded by the session)."""
    return f"{alias}__json" if _ftype(schema, q, f) is FieldType.LIST else alias


def _sparql_proj(ftype: "FieldType | None", var: str, alias: str) -> str:
    """A value variable `?var` as a SPARQL SELECT-list item for a value whose field type is
    `ftype` (None when unknown -- a plain projection). A FLOAT is read through `STR(?var)`
    aliased `?alias__float`: HANA serializes an `xsd:double` term to ~6 significant figures
    (issues-draft/H1), but `STR` carries the exact lexical form, which the session parses
    (`__float`). An INT is aliased `?alias__int`: SPARQL_TABLE surfaces a possibly-unbound
    numeric variable as an NVARCHAR column (`"7"` not `7`), which the suffix decodes back; it
    is a no-op on the JSON-term path. A BOOL is aliased `?alias__bool` for the same reason
    (SPARQL_TABLE answers it as `"0"` / `"1"`). A LIST stays its JSON text (`__json`)."""
    if ftype is FieldType.FLOAT:
        return f"(STR(?{var}) AS ?{alias}__float)"
    if ftype is FieldType.INT:
        return f"(?{var} AS ?{alias}__int)"
    if ftype is FieldType.BOOL:
        return f"(?{var} AS ?{alias}__bool)"
    if ftype is FieldType.LIST:
        return f"?{alias}__json"
    return f"?{var}" if var == alias else f"(?{var} AS ?{alias})"


# --- SQL (rel, and the doc data model's views) -------------------------------------------------


def _sql(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, names: Names, source: "str | None" = None) -> str:
    """The SQL text over the relation `names` gives the target (`source` instead: a
    derived table already filtered, the graph data model's `match` position)."""
    rid = "eid" if q.edge else "id"
    key = rid
    rel = names.table(q.table)
    conds = [sql(pred, "t", q.table, key, names)] if pred is not None else []
    conds += [f"t.{f} IS NOT NULL" for f in notnull_fields(q, "order" if sp.form == "order" else "base")]
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        conds.append(f"t.{q.field} IS NOT NULL")
    where = f" WHERE {' AND '.join(dict.fromkeys(conds))}" if conds else ""
    if source is not None:
        frm, after = f"FROM {source} t", where
    elif sp.position == "scan":
        frm, after = f"FROM (SELECT * FROM {rel}) t", where
    elif sp.position == "nest":
        frm, after = f"FROM (SELECT * FROM {rel} t{where}) t", ""
    else:
        frm, after = f"FROM {rel} t", where
    src = frm + after
    kind = q.kind
    if kind == "ids":
        return f"SELECT t.{rid} AS id {src}"
    if kind == "count":
        return f"SELECT COUNT(*) AS v {src}"
    if kind == "field":
        return f"SELECT t.{rid} AS id, t.{q.field} AS {_json_alias(schema, q, q.field, 'v')} {src}"
    if kind == "method":
        return f"SELECT t.{rid} AS id, {sql_text(q.fn, _ftype(schema, q, q.field), 't.' + q.field)} AS v {src}"
    if kind == "agg":
        f = f"t.{q.field}"
        if sp.form == "order":
            return f"SELECT {f} AS v {src} ORDER BY {f} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
        if q.fn == "distinct_count":
            if sp.form == "fn":
                return f"SELECT COUNT(*) AS v FROM (SELECT DISTINCT {f} {src}) z"
            return f"SELECT COUNT(DISTINCT {f}) AS v {src}"
        if q.fn == "set":
            return f"SELECT DISTINCT {f} AS v {src}"
        if q.fn == "list":
            return f"SELECT {f} AS v {src}"
        if q.fn == "median":
            # HANA's MEDIAN answers in the argument's type: over integers the mean of the two
            # middle values ROUNDED to an integer ({1, 10} -> 6, half away from zero);
            # PERCENTILE_CONT(0.5) is the statistical median the reference computes, and the
            # scalar subquery answers one NULL row over no present value (MEDIAN would,
            # PERCENTILE_CONT ... WITHIN GROUP alone answers no row)
            return f"SELECT (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {f}) {src}) AS v FROM DUMMY"
        return f"SELECT {q.fn.upper()}({f}) AS v {src}"
    if kind == "group":
        keys = ", ".join(f"t.{k} AS {name}" for k, name in zip(q.fields, ("k", "k2")))
        value = "COUNT(*)" if q.fn == "count_star" else f"{q.fn.upper()}(t.{q.arg})"
        return f"SELECT {keys}, {value} AS v {src} GROUP BY {', '.join('t.' + k for k in q.fields)}"
    if kind == "distinct":
        cols = ", ".join(f"t.{f} AS {name}" for f, name in zip(q.fields, ("v", "w")))
        return f"SELECT DISTINCT {cols} {src}"
    if kind == "page":
        d = q.directions[0]
        nulls = "NULLS FIRST" if d == "ASC" else "NULLS LAST"
        return f"SELECT t.{rid} AS id {src} ORDER BY t.{q.field} {d} {nulls}, t.{rid} ASC LIMIT {q.limit} OFFSET {q.skip}"
    if kind == "keys":
        cols = ", ".join(f"t.{k} AS {'ab'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"t.{k} {d} {'NULLS FIRST' if d == 'ASC' else 'NULLS LAST'}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + _limit(q)
    if kind == "join":
        f, op = q.field, q.fn
        inner = f"(SELECT * FROM {rel})" if sp.form == "jscan" else rel
        cond = f"t.{f} {MIRROR[op]} m.{f}" if sp.form == "jrev" else f"m.{f} {op} t.{f}"
        if q.arg == "count":
            return f"SELECT t.id AS id, (SELECT COUNT(*) FROM {inner} m WHERE {cond}) AS v {src}"
        return f"SELECT t.id AS id, m.id AS v {frm} JOIN {inner} m ON {cond}{after}"
    raise ValueError(kind)


def _limit(q: SelectQuery) -> str:
    if q.limit:
        return f" LIMIT {q.limit}" + (f" OFFSET {q.skip}" if q.skip else "")
    return f" LIMIT {NO_LIMIT} OFFSET {q.skip}" if q.skip else ""


# --- the doc data model's collection query -----------------------------------------------------

# how the session reads a projected text back (`connection.decode_column`)
_DECODE = {FieldType.INT: "__int", FieldType.FLOAT: "__float", FieldType.BOOL: "__bool", FieldType.LIST: "__json", FieldType.STRING: ""}


def _agg_suffix(fn: str, ftype: FieldType) -> str:
    """An aggregate's answer: the column's own type for `max` / `min`, a number (an int
    when integral) for `sum` / `avg`."""
    if ftype is FieldType.STRING:
        return ""
    return _DECODE[ftype] if fn in ("max", "min") else "__num"


def _collection(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred) -> str:
    """The question asked of the collection itself (an edge type: of its source class's
    collection, the link array UNNESTed). A projected field is the document store's own
    text, the alias telling the session how to read it (`__int`, `__float`, ...)."""
    alias = "l" if q.edge else ""
    if q.edge:
        et = schema.edge_type(q.table)
        source = f"{et.from_type} UNNEST {document.path(et.name)} AS \"l\""
        rid = document.path("eid", "l")
    else:
        source = q.table
        rid = document.path("id")

    def p(f: str) -> str:
        return document.path(f, alias)

    def out(f: str, name: str) -> str:
        return f"{p(f)} AS {name}{_DECODE[_ftype(schema, q, f)]}"

    conds = [document.doc_pred(pred, alias)] if pred is not None else []
    notnull = list(notnull_fields(q, "order" if sp.form == "order" else "base"))
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        notnull.append(q.field)
    conds += [f"{p(f)} IS NOT NULL" for f in dict.fromkeys(notnull)]
    src = f"FROM {source}" + (f" WHERE {' AND '.join(conds)}" if conds else "")
    kind = q.kind
    if kind == "ids":
        return f"SELECT {rid} AS id {src}"
    if kind == "count":
        return f"SELECT COUNT(*) AS v {src}"
    if kind == "field":
        return f"SELECT {rid} AS id, {out(q.field, 'v')} {src}"
    if kind == "agg":
        f = q.field
        ftype = _ftype(schema, q, f)
        if sp.form == "order":
            return f"SELECT {out(f, 'v')} {src} ORDER BY {p(f)} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
        if q.fn == "count":
            return f"SELECT COUNT({p(f)}) AS v {src}"
        if q.fn == "distinct_count":
            if sp.form == "fn":
                return f"SELECT COUNT(*) AS v FROM (SELECT {p(f)} AS x {src} GROUP BY {p(f)}) z"
            return f"SELECT COUNT(DISTINCT {p(f)}) AS v {src}"
        if q.fn == "set":
            return f"SELECT {out(f, 'v')} {src} GROUP BY {p(f)}"
        if q.fn == "list":
            return f"SELECT {out(f, 'v')} {src}"
        return f"SELECT {q.fn.upper()}({document.typed(ftype, p(f))}) AS v{_agg_suffix(q.fn, ftype)} {src}"
    if kind == "group":
        keys = ", ".join(out(k, name) for k, name in zip(q.fields, ("k", "k2")))
        if q.fn == "count_star":
            value = "COUNT(*) AS v"
        elif q.fn == "count":
            value = f"COUNT({p(q.arg)}) AS v"
        else:
            atype = _ftype(schema, q, q.arg)
            value = f"{q.fn.upper()}({document.typed(atype, p(q.arg))}) AS v{_agg_suffix(q.fn, atype)}"
        return f"SELECT {keys}, {value} {src} GROUP BY {', '.join(p(k) for k in q.fields)}"
    if kind == "distinct":
        cols = ", ".join(out(f, name) for f, name in zip(q.fields, ("v", "w")))
        return f"SELECT {cols} {src} GROUP BY {', '.join(p(f) for f in q.fields)}"
    if kind == "page":
        d = q.directions[0]
        return f"SELECT {rid} AS id {src} ORDER BY {p(q.field)} {d}, {rid} ASC LIMIT {q.limit} OFFSET {q.skip}"
    if kind == "keys":
        cols = ", ".join(out(k, "ab"[i]) for i, k in enumerate(q.fields))
        order = ", ".join(f"{p(k)} {d}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + _limit(q)
    raise ValueError(kind)


# --- the graph data model's openCypher ---------------------------------------------------------


def _cypher_asks(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred) -> bool:
    """Whether openCypher states the question itself (the native family): the filter
    has a cypher form, and the kind is one its RETURN expresses -- no method, no avg or
    median, COUNT(DISTINCT) only as the `fn` form's COUNT over a DISTINCT, MAX / MIN /
    SUM over numbers, an ordering only where the engine orders as the semantics do
    (`graph.NULLS_LIKE_SQL`, `graph.STRING_ORDER_NATIVE`; never a boolean key), a join's
    inner relation never a derived table (`jscan`)."""
    if sp.form == "jscan" or q.kind == "method":
        return False
    if pred is not None and not graph.expressible(pred, schema, q.table):
        return False

    def numeric(f: str) -> bool:
        return _ftype(schema, q, f) in (FieldType.INT, FieldType.FLOAT)

    def orderable(f: str, nulls_excluded: bool = False) -> bool:
        ft = _ftype(schema, q, f)
        if ft is FieldType.BOOL or (ft is FieldType.STRING and not graph.STRING_ORDER_NATIVE):
            return False
        return nulls_excluded or graph.NULLS_LIKE_SQL

    if q.kind == "agg":
        if q.fn in ("avg", "median"):
            return False
        if q.fn == "distinct_count":
            return sp.form == "fn"
        if q.fn in ("max", "min"):
            # the `order` form filters the nulls out first, so their placement is moot
            return orderable(q.field, nulls_excluded=True) if sp.form == "order" else numeric(q.field)
        if q.fn == "sum":
            return numeric(q.field)
        return True
    if q.kind == "group":
        return q.fn in ("count_star", "count") or numeric(q.arg)
    if q.kind in ("page", "keys"):
        return all(orderable(f) for f in q.fields)
    return True


def _cypher_limit(limit: int, skip: int) -> str:
    """`LIMIT n [SKIP m]`: the grammar puts SKIP after LIMIT, so a skip alone rides on
    the largest LIMIT (as `_limit` does with OFFSET)."""
    if not limit and not skip:
        return ""
    return f" LIMIT {limit or NO_LIMIT}" + (f" SKIP {skip}" if skip else "")


def _cypher_select(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, literal: bool) -> str:
    """The native text: `MATCH (t:n1) WHERE ... RETURN ...` inside `OPENCYPHER_TABLE`,
    the aliases uppercase. SQL wraps only what the language lacks: a sum's NULL over no
    present value (cypher answers 0: the COUNT travels along), an aggregate over no
    match where the engine answers no row (`graph.AGG_EMPTY_ROW`), COUNT(*) over a
    DISTINCT, and a join's count per record (a LEFT JOIN to the records, as the
    traversal's aggregates)."""
    params = None if literal else graph.Params()
    rid = "t.EID" if q.edge else "t.ID"

    def a(f: str) -> str:
        return f"t.{graph.attr(schema, q.table, f)}"

    conds = [graph.cypher_filter(pred, "t", q.table, schema, params)] if pred is not None else []
    conds += [f"NOT ({a(f)} IS NULL)" for f in notnull_fields(q, "order" if sp.form == "order" else "base")]
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        conds.append(f"NOT ({a(q.field)} IS NULL)")
    conds = list(dict.fromkeys(conds))
    head = graph.match_clause(schema, q.table, "t")
    where = f" WHERE {' AND '.join(conds)}" if conds else ""

    def oct(ret: str, tail: str = "", clauses: str = head, cond: str = where) -> str:
        return graph.table_fn(f"{clauses}{cond} RETURN {ret}{tail}", params)

    def one(ret: str, tail: str = "") -> str:
        return f"SELECT * FROM {oct(ret, tail)}"

    kind = q.kind
    if kind == "ids":
        return one(f"{rid} AS ID")
    if kind == "count":
        if graph.AGG_EMPTY_ROW:
            return one(f"COUNT({rid}) AS V")
        return f"SELECT COUNT(*) AS v FROM {oct(f'{rid} AS ID')} z"
    if kind == "field":
        return one(f"{rid} AS ID, {a(q.field)} AS {_json_alias(schema, q, q.field, 'v').upper()}")
    if kind == "agg":
        f = a(q.field)
        if sp.form == "order":
            return one(f"{f} AS V", f" ORDER BY {f} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1")
        if q.fn == "distinct_count":
            return f"SELECT COUNT(*) AS v FROM {oct(f'DISTINCT {f} AS V')} z"
        if q.fn == "set":
            return one(f"DISTINCT {f} AS V")
        if q.fn == "list":
            return one(f"{f} AS V")
        if q.fn == "sum":
            return f"SELECT (SELECT CASE WHEN z.N = 0 THEN NULL ELSE z.S END FROM {oct(f'SUM({f}) AS S, COUNT({f}) AS N')} z) AS v FROM DUMMY"
        item = f"COUNT({f}) AS V" if q.fn == "count" else f"{q.fn.upper()}({f}) AS V"
        if graph.AGG_EMPTY_ROW:
            return one(item)
        return f"SELECT (SELECT z.V FROM {oct(item)} z) AS v FROM DUMMY"
    if kind == "group":
        names = ("K", "K2")[: len(q.fields)]
        keys = ", ".join(f"{a(k)} AS {n}" for k, n in zip(q.fields, names))
        if q.fn == "count_star":
            return one(f"{keys}, COUNT({rid}) AS V")
        if q.fn == "count":
            return one(f"{keys}, COUNT({a(q.arg)}) AS V")
        if q.fn == "sum":
            outer = ", ".join(f"z.{n} AS {n.lower()}" for n in names)
            return f"SELECT {outer}, CASE WHEN z.N = 0 THEN NULL ELSE z.S END AS v FROM {oct(f'{keys}, SUM({a(q.arg)}) AS S, COUNT({a(q.arg)}) AS N')} z"
        return one(f"{keys}, {q.fn.upper()}({a(q.arg)}) AS V")
    if kind == "distinct":
        return one("DISTINCT " + ", ".join(f"{a(f)} AS {n}" for f, n in zip(q.fields, ("V", "W"))))
    if kind == "page":
        return one(f"{rid} AS ID", f" ORDER BY {a(q.field)} {q.directions[0]}, {rid} ASC{_cypher_limit(q.limit, q.skip)}")
    if kind == "keys":
        cols = ", ".join(f"{a(k)} AS {'AB'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"{a(k)} {d}" for k, d in zip(q.fields, q.directions))
        return one(cols, f" ORDER BY {order}{_cypher_limit(q.limit, q.skip)}")
    if kind == "join":
        f, op = q.field, q.fn
        m = f"m.{graph.attr(schema, q.table, f)}"
        corr = f"({a(f)} {MIRROR[op]} {m})" if sp.form == "jrev" else f"({m} {op} {a(f)})"
        # the two classes as two patterns of ONE clause (the Cartesian product, filtered)
        clauses = f"{head}, (m:{graph.label(q.table)})"
        cond = f" WHERE {' AND '.join(conds + [corr])}"
        if q.arg == "count":
            paths = oct("t.ID AS ID, COUNT(m.ID) AS V", clauses=clauses, cond=cond)
            return f"SELECT s.ID AS id, COALESCE(p.V, 0) AS v FROM {oct('t.ID AS ID')} s LEFT JOIN {paths} p ON p.ID = s.ID"
        return f"SELECT * FROM {oct('t.ID AS ID, m.ID AS V', clauses=clauses, cond=cond)}"
    raise ValueError(kind)


# --- SPARQL -----------------------------------------------------------------------------------


def _sparql(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, position: str) -> str:
    cls, idp = (f"c:{q.table}", "p:eid") if q.edge else (f"c:{q.table}", "p:id")
    head = f"?n a {cls} ; {idp} ?id ."
    filt = sparql_filter(pred, "n", native=position == "native")
    notnull = list(notnull_fields(q, "order" if sp.form == "order" else "base"))
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        notnull.append(q.field)
    required = " ".join(f"?n p:{f} ?r_{f} ." for f in dict.fromkeys(notnull))
    if position == "nest" and pred is not None:
        records = f"{{ SELECT ?n ?id WHERE {{ {head} {filt} }} }} {required}"
    else:
        records = f"{head} {filt} {required}"
    kind = q.kind

    def opt(f: str, var: str) -> str:
        return f"OPTIONAL {{ ?n p:{f} {var} }}"

    tail = ""
    if kind == "ids":
        select, body = "SELECT ?id", records
    elif kind == "count":
        select, body = "SELECT (COUNT(*) AS ?v)", records
    elif kind == "field":
        ft = _ftype(schema, q, q.field)
        bind = "v__json" if ft is FieldType.LIST else "v"
        select, body = f"SELECT ?id {_sparql_proj(ft, bind, 'v')}", f"{records} {opt(q.field, '?' + bind)}"
    elif kind == "method":
        ft = _ftype(schema, q, q.field)
        expr = sparql_text(q.fn, ft, f"?r_{q.field}")
        select, body = f"SELECT ?id {agg_proj(expr, result_type(q.fn, ft), 'v')}", records
    elif kind == "agg":
        f = q.field
        ft = _ftype(schema, q, f)
        if sp.form == "order":
            select, body = f"SELECT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
            tail = f" ORDER BY {'DESC' if q.fn == 'max' else 'ASC'}(?v) LIMIT 1"
        elif q.fn == "set":
            select, body = f"SELECT DISTINCT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
        elif q.fn == "list":
            select, body = f"SELECT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
        elif q.fn == "distinct_count":
            if sp.form == "fn":
                select, body = "SELECT (COUNT(*) AS ?v)", f"{{ SELECT DISTINCT ?w WHERE {{ {records} ?n p:{f} ?w . }} }}"
            else:
                select, body = "SELECT (COUNT(DISTINCT ?w) AS ?v)", f"{records} ?n p:{f} ?w ."
        elif q.fn == "median":
            # HANA's SPARQL MEDIAN requires a GROUP BY (an internal error without one),
            # though it interpolates an even count the way the reference does. The
            # subquery groups on a bound constant; VALUES ... OPTIONAL ... BIND answers
            # one row even over no present value (the reference's NULL row), so the whole
            # query is built here rather than through `_graph_wrap`. STR carries the median's
            # exact double (issues-draft/H1).
            inner_body = f"GRAPH {GRAPH_TOKEN} {{ {records} ?n p:{f} ?w . BIND(1 AS ?mg) }}"
            inner = f"SELECT (MEDIAN(?w) AS ?m) WHERE {{ {inner_body} }} GROUP BY ?mg"
            return f"{PREFIX_TEXT} SELECT (STR(?v) AS ?v__float) WHERE {{ VALUES ?mone {{ 1 }} OPTIONAL {{ {{ {inner} }} }} BIND(?m AS ?v) }}"
        else:
            fn = q.fn.upper()
            if q.fn == "count":
                item = "(COUNT(?w) AS ?v)"
            elif q.fn in ("max", "min"):
                # max / min answer the field's own type
                item = agg_proj(f"{fn}(?w)", ft, "v")
            else:  # sum, avg: avg is always a double; sum of a float is a double
                item = agg_proj(guarded(fn, "?w"), FieldType.FLOAT if q.fn == "avg" else ft, "v")
            # SUM / AVG over the records holding a value: once a predicate IRI has held a
            # second datatype anywhere in the store, HANA answers unbound for a group with an
            # unbound value in it (H3, RULES.md); a null is skipped by the aggregate anyway
            value = f"?n p:{f} ?w ." if q.fn in ("sum", "avg") else opt(f, "?w")
            select, body = f"SELECT {item}", f"{records} {value}"
    elif kind == "group":
        names = ("k", "k2")[: len(q.fields)]
        opts = " ".join(opt(k, "?" + name) for k, name in zip(q.fields, names))
        keys = " ".join(_sparql_proj(_ftype(schema, q, k), name, name) for k, name in zip(q.fields, names))
        if q.fn == "count_star":
            item = "(COUNT(*) AS ?v)"
        elif q.fn == "count":
            item = "(COUNT(?a) AS ?v)"
        elif q.fn in ("max", "min"):
            item = agg_proj(f"{q.fn.upper()}(?a)", _ftype(schema, q, q.arg), "v")
        else:
            # every group keeps its records (a group of nulls answers unbound), so the
            # value stays OPTIONAL and a null counts as zero inside the SUM (see `agg`)
            atype = _ftype(schema, q, q.arg)
            item = agg_proj(guarded("SUM", "?a", zero="0.0e0" if atype is FieldType.FLOAT else "0"), atype, "v")
        aopt = "" if q.fn == "count_star" else opt(q.arg, "?a")
        select, body = f"SELECT {keys} {item}", f"{records} {opts} {aopt}"
        tail = f" GROUP BY {' '.join('?' + n for n in names)} {HAVING}"
    elif kind == "distinct":
        names = ("v", "w")[: len(q.fields)]
        projs = " ".join(_sparql_proj(_ftype(schema, q, f), n, n) for f, n in zip(q.fields, names))
        select, body = f"SELECT DISTINCT {projs}", f"{records} {' '.join(opt(f, '?' + n) for f, n in zip(q.fields, names))}"
    elif kind == "page":
        d = q.directions[0]
        select, body = "SELECT ?id", f"{records} {opt(q.field, '?o')}"
        tail = f" ORDER BY {d}(?o) ASC(?id) LIMIT {q.limit} OFFSET {q.skip}"
    elif kind == "keys":
        names = ("a", "b")[: len(q.fields)]
        projs = " ".join(_sparql_proj(_ftype(schema, q, f), n, n) for f, n in zip(q.fields, names))
        select, body = f"SELECT {projs}", f"{records} {' '.join(opt(k, '?' + n) for k, n in zip(q.fields, names))}"
        tail = f" ORDER BY {' '.join(f'{d}(?{n})' for n, d in zip(names, q.directions))}"
        if q.limit:
            tail += f" LIMIT {q.limit}"
        if q.skip:
            tail += f" OFFSET {q.skip}"
    elif kind == "join":
        f, op = q.field, q.fn
        sop = "!=" if op == "<>" else op
        mop = "!=" if MIRROR[op] == "<>" else MIRROR[op]
        cond = f"(?f {mop} ?mf)" if sp.form == "jrev" else f"(?mf {sop} ?f)"
        inner = f"?n p:{f} ?f . ?m a c:{q.table} ; p:{f} ?mf ."
        if sp.form == "jscan":
            # the inner records a subquery, the record's own value bound inside the
            # OPTIONAL: its filter reads no variable of the outer pattern
            inner = f"?n p:{f} ?f . {{ SELECT ?m ?mf WHERE {{ ?m a c:{q.table} ; p:{f} ?mf . }} }}"
        if q.arg == "count":
            select = "SELECT ?id (COUNT(?m) AS ?v)"
            body = f"{records} OPTIONAL {{ {inner} FILTER {cond} }}"
            tail = f" GROUP BY ?id {HAVING}"
        else:
            select = "SELECT ?id ?v"
            body = f"{records} {{ {inner} FILTER {cond} }} ?m p:id ?v ."
    else:
        raise ValueError(kind)
    return _graph_wrap(select, body, tail, position)


def _graph_wrap(select: str, body: str, tail: str, position: str) -> str:
    if position == "from":
        return f"{PREFIX_TEXT} {select} FROM {GRAPH_TOKEN} WHERE {{ {body} }}{tail}"
    return f"{PREFIX_TEXT} {select} WHERE {{ GRAPH {GRAPH_TOKEN} {{ {body} }} }}{tail}"


# --- the rewrites of one question ------------------------------------------------------------


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.ast.select import present_values, records, view

    rewrites = Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = _rewrites.rewrite(p, rng, "q")
    from ganda.core.ast.predicates import reverse

    rewrites.rev = reverse(p)
    if isinstance(p, Predicate) and p.sub is None:
        try:
            ftype = schema.field(q.table, p.field, edge=q.edge).type
        except KeyError:
            ftype = None
        if ftype is not None:
            stored = [n.values.get(p.field) for n in records(q, instance) + (records(q, view(q, instance)) if q.tx else []) if p.field in n.values]
            rewrites.arith = _rewrites.arithmetic(p, rng, "ar", ftype, stored)
    return rewrites


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    bind: dict = {}
    for rewritten in (rewrites.rw, rewrites.rev, rewrites.arith):
        if rewritten is not None:
            bind.update(rewritten.bind_vars)
    if q.tx:
        bind.update(_tx.extra_binds(q.tx, schema))
    return bind


def rewrite_params(rewrites: Rewrites) -> dict:
    params: dict = {}
    for name in ("rw", "rev", "arith"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    return params


__all__ = ["BASE", "METHODS", "method_fn", "fields_of"]
