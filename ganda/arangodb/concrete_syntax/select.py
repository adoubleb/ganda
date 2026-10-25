"""The concrete syntax catalog of the select IR: HOW a `SelectQuery` is asked in AQL. A
syntax is two independent choices, each asked alike on every data model:

    axis       choice    the text
    position   table     `FOR n IN t [FILTER p]`
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
               jrev      a join's correlation reversed (`n.f > x.f` for `x.f < n.f`)

The filter is the drawn predicate's own text (`predicate.render_predicate`): its leaves'
forms -- reversed operands, arithmetic, a parameter-keyed map entry -- are drawn with the
predicate (`oracle/pivots.leaf_form`). A variant's name is `<data model>:<tag>`, the tag
the non-default choices joined by `+` in the order form, position. No choice exists only
to make a data model use or avoid an index: a pair differs in data model, each with its
own physical design.

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

from ganda.core.query.ir.predicates import MIRROR
from ganda.core.query.ir.select import SelectQuery
from ganda.core.data.model import Instance, Schema
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL
from ganda.arangodb.concrete_syntax.predicate import render_predicate as render_pred
from ganda.arangodb.concrete_syntax.support import METHODS, base_union, tx_extra_binds, tx_script  # noqa: F401

POSITIONS = ("table", "poly", "polyin")
FORMS = ("base", "collect", "order", "set", "fn", "jrev")
_FN = {"max": "MAX", "min": "MIN", "sum": "SUM", "avg": "AVERAGE", "median": "MEDIAN"}
# every aggregate reads present values: COUNT_DISTINCT counts a null, MAX / MIN skip it
assumes("aggregate.count_distinct.counts_null", AQL, True)
assumes("aggregate.max_min.skips_null", AQL, True)
_COLLECT_AGGREGATES = ("max", "min", "count", "avg", "sum")
_BIG = 1_000_000_000


@dataclass(frozen=True)
class SelectSyntax:
    position: str = "table"
    form: str = "base"

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, "base"), (self.position, "table")) if x != default]
        return "+".join(parts)

    @property
    def axes(self) -> int:
        return len([x for x in (self.form != "base", self.position != "table") if x])


BASE = SelectSyntax()


@dataclass
class Rewrites:
    """The per-case material the syntaxes draw on: the present values of the aggregated
    field among the matching records."""

    present: list = field(default_factory=list)


def all_syntaxes() -> list:
    return [SelectSyntax(p, m) for m, p in itertools.product(FORMS, POSITIONS)]


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if q.edge and sp.position in ("poly", "polyin"):
        return False
    if sp.form == "jrev" and q.kind != "join":
        return False
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
    if sp.form == "order" and not (q.kind == "agg" and q.fn in ("max", "min") and rewrites.present):
        return False
    if sp.form in ("set", "fn") and not (q.kind == "agg" and q.fn == "distinct_count"):
        return False
    if sp.form == "collect" and not (q.kind in ("count", "group", "distinct") or (q.kind == "agg" and q.fn in _COLLECT_AGGREGATES)):
        return False
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


def _where_text(q: SelectQuery, var: str, schema: Schema = None) -> "str | None":
    text = render_pred(q.where, var, schema=schema) if q.where is not None else None
    extra = [f"{var}.{f} != null" for f in _notnull(q)]
    if text is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({text})" if extra else text] + extra)


def _collection_source(q: SelectQuery, schema: Schema, var: str) -> str:
    """`FOR var IN <target>` for the plain target: a collection, the base class's
    union, or the doc data model's link objects."""
    if schema.is_base(q.table):
        return f"FOR {var} IN {base_union(schema, 'm')}"
    return f"FOR {var} IN {q.table}"


def _doc_edge_source(q: SelectQuery, schema: Schema, var: str) -> str:
    et = schema.edge_type(q.table)
    inner = f"FOR s IN {et.from_type} FOR l IN (s.{et.name} || []) RETURN l"
    return f"FOR {var} IN ({inner})"


def _source(q: SelectQuery, schema: Schema, sp: SelectSyntax, data_model: str, let: str = "") -> str:
    """The FOR clause, the filter and a join's LET."""
    where = _where_text(q, "n", schema)
    if q.edge and data_model == "doc":
        head = _doc_edge_source(q, schema, "n")
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
        target = base_union(schema, "y")
    else:
        target = q.table
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
    src = _source(q, schema, sp, data_model, _join_let(q, schema, sp) if kind == "join" else "")
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


# --- the per-case material ------------------------------------------------------------


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values

    return Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    bind: dict = {}
    if schema.base is not None and not q.edge and not schema.is_base(q.table):
        bind.update({"cls": q.table, "clsl": [q.table]})
    if q.tx:
        bind.update(tx_extra_binds(q.tx))
    return bind


def rewrite_params(rewrites: Rewrites) -> dict:
    """What a bundle records of the per-case material: nothing beyond the question."""
    return {}
