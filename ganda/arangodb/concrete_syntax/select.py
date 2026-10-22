"""The concrete syntax catalog of the select IR: HOW a `SelectQuery` is asked in AQL. A
syntax is three independent choices, and every applicable combination is an equivalent
text:

    axis       choice    the text
    filter     plain     `FILTER p`
               rw        `FILTER rewrite(p)`: an equivalent tree (De Morgan, `IN [x]`
                         for `== x`, the complement under NOT -- AQL comparison is total)
               rev       the reversible leaves' operands swapped (`@p < n.f`)
               arith     `(n.f + @c) op @pc AND n.f != null` where exact; never served
               noopt     every field behind `NOOPT()`: the filter is NEVER index-served,
                         the scan twin on a database holding an index
    position   table     `FOR n IN t [FILTER p]`
               scan      `FOR n IN (FOR m IN t RETURN m) [FILTER p]` (the optimizer inlines it)
               nest      `FOR n IN (FOR m IN t FILTER p RETURN m)`, everything else outside
               noindex   `FOR n IN t OPTIONS {disableIndex: true} [FILTER p]`
               hint      `FOR n IN t OPTIONS {indexHint: '<ix>', forceIndexHint: true}`
                         where an index of that data model leads on the filter's field
               poly      `FOR n IN UNION(every class) FILTER IS_SAME_COLLECTION(@cls, n)`
                         (the inheritance axis: the base class is the union)
               polyin    the same through `PARSE_IDENTIFIER(n._id).collection IN @clsl`
    form       base      the kind's own text (a function over a subquery, `RETURN
                         DISTINCT`, a `SORT ... LIMIT` page)
               collect   a count / an aggregate / a grouping / a distinct as a COLLECT
                         (`WITH COUNT INTO`, `AGGREGATE`, `INTO g`, a group key)
               order     `MAX` / `MIN` as the first row of the ordering
               set       a distinct count as `LENGTH(UNIQUE(...))`
               fn        a distinct count as `LENGTH(SORTED_UNIQUE(...))`
               jscan     a join's subquery over `(FOR y IN t RETURN y)`
               jrev      a join's correlation reversed (`n.f > x.f` for `x.f < n.f`)

A variant's name is `<data model>:<tag>`, the tag the non-default choices joined by `+`
in the order form, filter, position. The base syntax is ONE text on every data model
(the same node documents under different indexes: the index-vs-scan oracle across
databases), and every variant is also a same-database pair.

An EDGE-TYPE target reads `n.eid` from the edge collection (graph) and the junction
(rel); the doc data model's relationship records are the link objects of the source
documents, `FOR n IN (FOR s IN src FOR l IN (s.e1 || []) RETURN l)`, which answer every
kind. A question on the BASE CLASS reads the union of every node collection.

The transaction door wraps every syntax of a `tx` question the same way
(`support.tx_script`): a stream transaction, the write, the question, the abort.

Every aggregate reads present values (`FILTER n.f != null`): `COUNT_DISTINCT` and
`COUNT` count a null where `MAX` / `MIN` skip it; a keyless `COLLECT AGGREGATE` over no
rows answers ONE row (`COUNT` 0, `MAX` null, `SUM` null -- which is why `SUM` has no
collect form: `SUM([])` is 0).
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.query.ir.predicates import MIRROR, Predicate, reverse
from ganda.core.query.ir.select import SelectQuery
from ganda.core.data.model import FieldType, Instance, Schema
from ganda.arangodb.oracle import rewrites as _rewrites
from ganda.arangodb.concrete_syntax.predicate import render_predicate as render_pred
from ganda.arangodb.concrete_syntax.support import METHODS, method_fn, tx_extra_binds, tx_script  # noqa: F401

FILTERS = ("plain", "rw", "rev", "arith", "noopt")
POSITIONS = ("table", "scan", "nest", "noindex", "hint", "poly", "polyin")
FORMS = ("base", "collect", "order", "set", "fn", "jscan", "jrev")
_FN = {"max": "MAX", "min": "MIN", "sum": "SUM", "avg": "AVERAGE", "median": "MEDIAN"}
_HINT_OPS = ("=", "IN", "<", "<=", ">", ">=")
_BIG = 1_000_000_000


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
    door: dict = field(default_factory=dict)  # data model -> the index name a hint forces
    present: list = field(default_factory=list)

    def filter_pred(self, q: SelectQuery, name: str):
        if name in ("plain", "noopt"):
            return q.where
        return getattr(self, name)


def all_syntaxes() -> list:
    return [SelectSyntax(f, p, m) for m, f, p in itertools.product(FORMS, FILTERS, POSITIONS)]


def _plain_source(q: SelectQuery, schema: Schema) -> bool:
    """Whether the target is one plain collection (not the base class's union, not the
    doc data model's link objects)."""
    return not schema.is_base(q.table)


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    plain = _plain_source(q, schema) and not (q.edge and data_model == "doc")
    if q.edge and sp.position in ("poly", "polyin"):
        return False
    if not plain and sp.position in ("noindex", "hint", "scan"):
        return False
    if sp.form in ("jscan", "jrev") and q.kind != "join":
        return False
    if sp.filter != "plain":
        if q.where is None:
            return False
        if sp.filter != "noopt" and getattr(rewrites, sp.filter) is None:
            return False
    if sp.position == "nest" and q.where is None:
        return False
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
    if sp.form == "order" and not (q.kind == "agg" and q.fn in ("max", "min") and rewrites.present):
        return False
    if sp.form in ("set", "fn") and not (q.kind == "agg" and q.fn == "distinct_count"):
        return False
    if sp.form == "collect" and not (q.kind in ("count", "group", "distinct") or (q.kind == "agg" and q.fn in ("max", "min", "count", "avg"))):
        return False
    if sp.position == "hint":
        return q.where is not None and sp.filter in ("plain", "rev") and data_model in rewrites.door
    return True


def base_data_models(q: SelectQuery) -> tuple:
    return ("doc", "graph", "rel")


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes() if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering -------------------------------------------------------------------------------


def _notnull(q: SelectQuery) -> tuple:
    """The fields a syntax's filter also holds present: a method's field, an
    aggregate's (every AQL aggregate here reads present values)."""
    if q.kind in ("method", "agg"):
        return (q.field,)
    return ()


def _where_text(q: SelectQuery, sp: SelectSyntax, rewrites: Rewrites, var: str, schema: Schema = None) -> "str | None":
    pred = rewrites.filter_pred(q, sp.filter)
    text = render_pred(pred, var, noopt=sp.filter == "noopt", schema=schema) if pred is not None else None
    extra = [f"{var}.{f} != null" for f in _notnull(q)]
    if text is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({text})" if extra else text] + extra)


def _collection_source(q: SelectQuery, schema: Schema, var: str) -> str:
    """`FOR var IN <target>` for the plain target: a collection, the base class's
    union, or the doc data model's link objects."""
    if q.edge and schema.engine is not None and getattr(q, "_doc_edge", False):
        pass
    if schema.is_base(q.table):
        return f"FOR {var} IN UNION({', '.join(f'(FOR m_{nt.name} IN {nt.name} RETURN m_{nt.name})' for nt in schema.node_types)})"
    return f"FOR {var} IN {q.table}"


def _doc_edge_source(q: SelectQuery, schema: Schema, var: str, where_inner: "str | None" = None) -> str:
    et = schema.edge_type(q.table)
    inner = f"FOR s IN {et.from_type} FOR l IN (s.{et.name} || [])" + (f" FILTER {where_inner}" if where_inner else "") + " RETURN l"
    return f"FOR {var} IN ({inner})"


def _source(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str, let: str = "") -> str:
    """The FOR clause, the filter and a join's LET."""
    t = q.table
    where = _where_text(q, sp, rewrites, "n", schema)
    doc_edge = q.edge and data_model == "doc"
    if sp.position == "nest":
        if doc_edge:
            head = _doc_edge_source(q, schema, "n", _where_text(q, sp, rewrites, "l", schema))
        elif schema.is_base(t):
            inner = _collection_source(q, schema, "m") + f" FILTER {_where_text(q, sp, rewrites, 'm', schema)} RETURN m"
            head = f"FOR n IN ({inner})"
        else:
            head = f"FOR n IN (FOR m IN {t} FILTER {_where_text(q, sp, rewrites, 'm', schema)} RETURN m)"
        where = None
    elif doc_edge:
        head = _doc_edge_source(q, schema, "n")
    elif sp.position == "scan":
        head = f"FOR n IN (FOR m IN {t} RETURN m)"
    elif sp.position == "noindex":
        head = f"FOR n IN {t} OPTIONS {{disableIndex: true}}"
    elif sp.position == "hint":
        head = f"FOR n IN {t} OPTIONS {{indexHint: '{rewrites.door[data_model]}', forceIndexHint: true}}"
    elif sp.position in ("poly", "polyin"):
        cond = "IS_SAME_COLLECTION(@cls, n)" if sp.position == "poly" else "PARSE_IDENTIFIER(n._id).collection IN @clsl"
        head = _collection_source(dataclasses_replace_table(q, schema.base.name), schema, "n") + f" FILTER {cond}"
    else:
        head = _collection_source(q, schema, "n")
    return head + (f" FILTER {where}" if where else "") + (f" {let}" if let else "")


def dataclasses_replace_table(q: SelectQuery, table: str) -> SelectQuery:
    import dataclasses

    return dataclasses.replace(q, table=table)


def _join_let(q: SelectQuery, schema: Schema, sp: SelectSyntax) -> str:
    f, op = q.field, q.fn
    if schema.is_base(q.table):
        target = f"UNION({', '.join(f'(FOR y_{nt.name} IN {nt.name} RETURN y_{nt.name})' for nt in schema.node_types)})"
    else:
        target = f"(FOR y IN {q.table} RETURN y)" if sp.form == "jscan" else q.table
    cond = f"n.{f} {_OP[MIRROR[op]]} x.{f}" if sp.form == "jrev" else f"x.{f} {_OP[op]} n.{f}"
    return f"LET jm = (FOR x IN {target} FILTER {cond} RETURN x.id)"


_OP = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def _agg_text(q: SelectQuery, sp: SelectSyntax, src: str) -> str:
    f, fn = q.field, q.fn
    if sp.form == "order":
        return f"{src} SORT n.{f} {'DESC' if fn == 'max' else 'ASC'} LIMIT 1 RETURN {{v: n.{f}}}"
    if sp.form == "set":
        return f"RETURN {{v: LENGTH(UNIQUE({src} RETURN n.{f}))}}"
    if sp.form == "fn":
        return f"RETURN {{v: LENGTH(SORTED_UNIQUE({src} RETURN n.{f}))}}"
    if sp.form == "collect":
        agg = "COUNT(1)" if fn == "count" else f"{_FN[fn]}(n.{f})"
        return f"{src} COLLECT AGGREGATE a = {agg} RETURN {{v: a}}"
    if fn == "count":
        return f"RETURN {{v: LENGTH({src} RETURN 1)}}"
    if fn == "distinct_count":
        return f"RETURN {{v: COUNT_DISTINCT({src} RETURN n.{f})}}"
    if fn == "set":
        return f"FOR x IN UNIQUE({src} RETURN n.{f}) RETURN {{v: x}}"
    if fn == "list":
        return f"FOR x IN ({src} RETURN n.{f}) RETURN {{v: x}}"
    return f"RETURN {{v: {_FN[fn]}({src} RETURN n.{f})}}"


def _group_text(q: SelectQuery, sp: SelectSyntax, src: str) -> str:
    names = ("k", "k2")[: len(q.fields)]
    keys = ", ".join(f"{name} = n.{k}" for k, name in zip(q.fields, names))
    out = ", ".join(names)
    fn, arg = q.fn, q.arg
    if sp.form == "collect":
        if fn == "count_star":
            value = "LENGTH(g)"
        elif fn == "count":
            value = f"LENGTH(g[* FILTER CURRENT.n.{arg} != null])"
        else:
            value = f"{_FN[fn]}(g[*].n.{arg})"
        return f"{src} COLLECT {keys} INTO g RETURN {{{out}, v: {value}}}"
    if fn == "count_star":
        return f"{src} COLLECT {keys} WITH COUNT INTO c RETURN {{{out}, v: c}}"
    agg = f"SUM(n.{arg} != null ? 1 : 0)" if fn == "count" else f"{_FN[fn]}(n.{arg})"
    return f"{src} COLLECT {keys} AGGREGATE c = {agg} RETURN {{{out}, v: c}}"


def _render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    kind = q.kind
    rid = "n.eid" if q.edge else "n.id"
    src = _source(q, schema, sp, rewrites, data_model, _join_let(q, schema, sp) if kind == "join" else "")
    if kind == "join":
        return f"{src} RETURN {{id: n.id, v: {'LENGTH(jm)' if q.arg == 'count' else 'jm'}}}"
    if kind == "ids":
        return f"{src} RETURN {{id: {rid}}}"
    if kind == "count":
        if sp.form == "collect":
            return f"{src} COLLECT WITH COUNT INTO c RETURN {{v: c}}"
        return f"RETURN {{v: LENGTH({src} RETURN 1)}}"
    if kind == "field":
        return f"{src} RETURN {{id: {rid}, v: n.{q.field}}}"
    if kind == "method":
        return f"{src} RETURN {{id: {rid}, v: {q.fn.format(f=f'n.{q.field}')}}}"
    if kind == "agg":
        return _agg_text(q, sp, src)
    if kind == "group":
        return _group_text(q, sp, src)
    if kind == "distinct":
        names = ("v", "w")[: len(q.fields)]
        if sp.form == "collect":
            keys = ", ".join(f"{name} = n.{f}" for f, name in zip(q.fields, names))
            return f"{src} COLLECT {keys} RETURN {{{', '.join(names)}}}"
        cols = ", ".join(f"{name}: n.{f}" for f, name in zip(q.fields, names))
        return f"{src} RETURN DISTINCT {{{cols}}}"
    if kind == "page":
        return f"{src} SORT n.{q.field} {q.directions[0]}, {rid} ASC LIMIT {q.skip}, {q.limit} RETURN {{id: {rid}}}"
    if kind == "keys":
        cols = ", ".join(f"{'ab'[i]}: n.{k}" for i, k in enumerate(q.fields))
        order = ", ".join(f"n.{k} {d}" for k, d in zip(q.fields, q.directions))
        limit = f" LIMIT {q.skip}, {q.limit or _BIG}" if (q.skip or q.limit) else ""
        return f"{src} SORT {order}{limit} RETURN {{{cols}}}"
    raise ValueError(kind)


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    text = _render(q, schema, sp, rewrites, data_model)
    if q.tx:
        return tx_script(q.tx, schema, data_model, text)
    return text


# --- the rewrites of one question ------------------------------------------------


def _door(p, q: SelectQuery, schema: Schema, data_model: str) -> "str | None":
    """The index of `data_model` on the question's class that leads on the filter's
    field, for a leaf the index can serve: an equality, a membership or a bound (never
    `IN_RANGE`, a function; never a sparse index under a bound a null satisfies)."""
    if not isinstance(p, Predicate) or p.key is not None or p.sub is not None or p.op not in _HINT_OPS or p.ftype in ("list", "map") or q.edge:
        return None
    for ix in schema.indexes:
        if ix.target == "node" and ix.table == q.table and ix.field == p.field and ix.option("by") is None and not ix.extra_fields and ix.applies_to(data_model):
            # a sparse index holds no null key, and a null satisfies `<` / `<=` under the
            # total order: the planner refuses the hint there (errorNum 1577); so does
            # a composite index under a condition on its lead field alone
            if ix.ignore_nulls and p.op in ("<", "<="):
                continue
            return ix.name()
    return None


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values, records, view

    rewrites = Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = _rewrites.rewrite(p, rng, "q")
    rewrites.rev = reverse(p)
    if isinstance(p, Predicate) and p.sub is None:
        try:
            ftype = schema.field(q.table, p.field, edge=q.edge).type
        except KeyError:
            ftype = None
        if ftype is not None:
            stored = [n.values.get(p.field) for n in records(q, instance) + (records(q, view(q, instance)) if q.tx else []) if p.field in n.values]
            rewrites.arith = _rewrites.arithmetic(p, rng, "ar", ftype, stored)
    for data_model in ("doc", "graph", "rel"):
        door = _door(p, q, schema, data_model)
        if door is not None:
            rewrites.door[data_model] = door
    return rewrites


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    bind: dict = {}
    for rewritten in (rewrites.rw, rewrites.rev, rewrites.arith):
        if rewritten is not None:
            bind.update(rewritten.bind_vars)
    if schema.base is not None and not q.edge and not schema.is_base(q.table):
        bind.update({"cls": q.table, "clsl": [q.table]})
    if q.tx:
        bind.update(tx_extra_binds(q.tx))
    return bind


def rewrite_params(rewrites: Rewrites) -> dict:
    params: dict = {}
    for name in ("rw", "rev", "arith"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    if rewrites.door:
        params["index_hint"] = dict(rewrites.door)
    return params
