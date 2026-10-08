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
    form      base          the kind's own text
              order         `max` / `min` as the first row of an ordering
              fn            a distinct count as `COUNT(*)` over `SELECT DISTINCT`
              jscan         a join's inner relation as a derived table
              jrev          a join's correlation with its operands swapped

A variant is `<data model>:<tag>`, the tag the non-default choices joined by `+` (form,
filter, position). The base (plain, table, base) is asked of every data model; on the
doc data model it is the collection query where the document store can ask the
question (no semi-join, join, method or median there) and the views otherwise.

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
from ganda.hana.concrete_syntax import document, tx as _tx
from ganda.hana.concrete_syntax.methods import METHODS, method_fn, sparql_text, sql_text  # noqa: F401
from ganda.hana.concrete_syntax.predicate import REL, UNKNOWN_VAR, Names, sparql_filter, sql
from ganda.hana.concrete_syntax.traversal import sparql_table
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT

FILTERS = ("plain", "rw", "rev", "arith")
POSITIONS = ("table", "scan", "nest", "view", "from", "table_fn", "native")
FORMS = ("base", "order", "fn", "jscan", "jrev")
DATA_MODELS = ("doc", "rel", "rdf")
_POSITIONS_OF = {"rel": ("table", "scan", "nest"), "doc": ("table", "scan", "nest", "view"), "rdf": ("table", "nest", "from", "table_fn", "native")}
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
    return document.doc_filter_ok(pred)


def _views_ask(q: SelectQuery, pred) -> bool:
    return not document.compares_list_text(pred)


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
        if sp.position == "table":
            # the base is the collection query, or the views where the store cannot ask
            if sp == BASE:
                return collection_asks(q, schema, pred) or _views_ask(q, pred)
            return collection_asks(q, schema, pred)
        return _views_ask(q, pred)
    if data_model == "rdf" and sp.position == "nest" and q.kind == "join":
        return False
    if sp.position == "native" and pred is None:
        return False
    return True


def base_data_models(q: SelectQuery) -> tuple:
    """The data models the question has a base concrete syntax on: the doc data model
    lacks one where neither its collection query nor its views can ask it (a semi-join
    or a negated membership next to a whole-list equality)."""
    if collection_asks(q, None, q.where) or _views_ask(q, q.where):
        return DATA_MODELS
    return ("rel", "rdf")


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


# --- SQL (rel, and the doc data model's views) -------------------------------------------------


def _sql(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, names: Names) -> str:
    rid = "eid" if q.edge else "id"
    key = rid
    rel = names.table(q.table)
    conds = [sql(pred, "t", q.table, key, names)] if pred is not None else []
    conds += [f"t.{f} IS NOT NULL" for f in notnull_fields(q, "order" if sp.form == "order" else "base")]
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        conds.append(f"t.{q.field} IS NOT NULL")
    where = f" WHERE {' AND '.join(dict.fromkeys(conds))}" if conds else ""
    if sp.position == "scan":
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
        v = "?" + _json_alias(schema, q, q.field, "v")
        select, body = f"SELECT ?id {v}", f"{records} {opt(q.field, v)}"
    elif kind == "method":
        expr = sparql_text(q.fn, _ftype(schema, q, q.field), f"?r_{q.field}")
        select, body = f"SELECT ?id ({expr} AS ?v)", records
    elif kind == "agg":
        f = q.field
        if sp.form == "order":
            select, body = "SELECT ?v", f"{records} ?n p:{f} ?v ."
            tail = f" ORDER BY {'DESC' if q.fn == 'max' else 'ASC'}(?v) LIMIT 1"
        elif q.fn == "set":
            select, body = "SELECT DISTINCT ?v", f"{records} ?n p:{f} ?v ."
        elif q.fn == "list":
            select, body = "SELECT ?v", f"{records} ?n p:{f} ?v ."
        elif q.fn == "distinct_count":
            if sp.form == "fn":
                select, body = "SELECT (COUNT(*) AS ?v)", f"{{ SELECT DISTINCT ?w WHERE {{ {records} ?n p:{f} ?w . }} }}"
            else:
                select, body = "SELECT (COUNT(DISTINCT ?w) AS ?v)", f"{records} ?n p:{f} ?w ."
        else:
            fn = q.fn.upper()
            agg = f"COUNT(?w)" if q.fn == "count" else (f"{fn}(?w)" if q.fn in ("max", "min") else f"IF(COUNT(?w) > 0, {fn}(?w), {UNKNOWN_VAR})")
            select, body = f"SELECT ({agg} AS ?v)", f"{records} {opt(f, '?w')}"
    elif kind == "group":
        names = ("?k", "?k2")[: len(q.fields)]
        opts = " ".join(opt(k, name) for k, name in zip(q.fields, names))
        if q.fn == "count_star":
            agg = "COUNT(*)"
        elif q.fn == "count":
            agg = "COUNT(?a)"
        elif q.fn in ("max", "min"):
            agg = f"{q.fn.upper()}(?a)"
        else:
            agg = f"IF(COUNT(?a) > 0, SUM(?a), {UNKNOWN_VAR})"
        aopt = "" if q.fn == "count_star" else opt(q.arg, "?a")
        select, body = f"SELECT {' '.join(names)} ({agg} AS ?v)", f"{records} {opts} {aopt}"
        tail = f" GROUP BY {' '.join(names)} {HAVING}"
    elif kind == "distinct":
        names = ("?v", "?w")[: len(q.fields)]
        select, body = f"SELECT DISTINCT {' '.join(names)}", f"{records} {' '.join(opt(f, n) for f, n in zip(q.fields, names))}"
    elif kind == "page":
        d = q.directions[0]
        select, body = "SELECT ?id", f"{records} {opt(q.field, '?o')}"
        tail = f" ORDER BY {d}(?o) ASC(?id) LIMIT {q.limit} OFFSET {q.skip}"
    elif kind == "keys":
        names = ("?a", "?b")[: len(q.fields)]
        select, body = f"SELECT {' '.join(names)}", f"{records} {' '.join(opt(k, n) for k, n in zip(q.fields, names))}"
        tail = f" ORDER BY {' '.join(f'{d}({n})' for n, d in zip(names, q.directions))}"
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
