"""The concrete syntax catalog of the SELECT IR: HOW a `SelectQuery` is asked. A
concrete syntax is two independent choices, and every applicable combination is an
equivalent text:

    axis       choice     the text
    position   table      `FROM t [WHERE p]` (the base class: `FROM n1, n2, ...`, its
                          subclass tables -- the engine has no inheritance)
               poly       `FROM n1, n2, ... WHERE record::tb(id) = $cls [AND (p)]`
               polyin     `FROM n1, n2, ... WHERE record::tb(id) IN $clsl [AND (p)]`
    form       base       the kind's own text (an aggregate as a function over `SELECT VALUE`)
               group_all  an aggregate or a count through the aggregate operator, `GROUP ALL`
               order      `max(f)` / `min(f)` as the first row of `ORDER BY f DESC|ASC LIMIT 1`
               jrev       a join's correlation reversed (`$parent.f > f` for `f < $parent.f`)

The filter is the drawn predicate's own text (`predicate.render_predicate`). A data
model's name is `<data model>:<tag>`, the tag the non-default choices joined by `+`
(`graph:order+poly`); the base syntax is the same text on every data model.

A join puts its correlated subquery in the projection: `(SELECT VALUE id FROM t WHERE
f op $parent.f)`. An EDGE-TYPE target reads the edge table (graph) and the junction
table (rel); the doc data model has no relationship table, so it renders `ids` and
`count` alone, through the source records' link arrays -- and every data model then
answers the relationship KEYS as a list (`record::id(id)`, the entry's `eid`).

The TRANSACTION door wraps every syntax of a `tx` question the same way: `BEGIN
TRANSACTION; <write>; LET $r = <text>; CANCEL TRANSACTION; RETURN $r;` -- a variable
bound inside a cancelled transaction keeps its value (probed), so the pairs compare
transaction views alike.

Facts (probed 3.3.4): `GROUP BY` keeps a NONE key apart from a NULL one (the key is
`k ?? NULL`); `count(f)` counts truthy values (`count(f IS NOT NONE AND f IS NOT NULL)`
counts present ones); `math::sum` over a group skips missing values, over an array
refuses them (the array is filtered); `ORDER BY` takes a projection alias; `LIMIT`
precedes `START`.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.data.model import Instance, Schema
from ganda.core.query.ir.predicates import MIRROR
from ganda.core.query.ir.select import DOC_EDGE_KINDS, SelectQuery, notnull_fields
from ganda.core.rules.vocabulary import EmptyAggregate
from ganda.surrealdb.concrete_syntax.query.predicate import present, render_predicate as render_pred, tables_of
from ganda.surrealdb.concrete_syntax.query.support import METHODS, tx_write  # noqa: F401 -- the method catalog
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import ACCEPTED, SURQL, TRANSACTION

POSITIONS = ("table", "poly", "polyin")
FORMS = ("base", "group_all", "order", "jrev")
# over an array of the present values: `array::max` / `array::min` order any comparable
# kind (a string, a datetime), the math functions take numbers (the generator asks a sum,
# a mean or a median over numeric columns only)
_AGG_FN = {"max": "array::max", "min": "array::min", "sum": "math::sum", "avg": "math::mean", "median": "math::median"}
# as an aggregate of a grouping: the values collected by `array::group`, the missing
# ones filtered (the math aggregates skip them on their own)
_GROUP_FN = {"max": "array::max(array::filter(array::group({f}), |$x| $x != NONE AND $x != NULL))", "min": "array::min(array::filter(array::group({f}), |$x| $x != NONE AND $x != NULL))", "sum": "math::sum({f})", "avg": "math::mean({f})", "median": "math::median({f})", "count": "count({f} IS NOT NONE AND {f} IS NOT NULL)"}
# a `LIMIT` of every row, so a `START` alone can be asked
_ALL = 2**31

assumes("accepts.select.several_tables", SURQL, ACCEPTED)
assumes("aggregate.sum.empty_input", SURQL, EmptyAggregate.ZERO)
assumes("transaction.cancelled.let_value_survives", TRANSACTION, True)


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
    """The per-case material the concrete syntaxes draw on, drawn once when the case is
    built: the present values of the aggregated field among the matching records."""

    present: list = field(default_factory=list)


def all_syntaxes() -> list:
    return [SelectSyntax(p, m) for m, p in itertools.product(FORMS, POSITIONS)]


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if q.edge:
        if sp.position != "table":
            return False
        if data_model == "doc" and (q.kind not in DOC_EDGE_KINDS or sp.form != "base"):
            return False
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
    if sp.form == "jrev":
        return q.kind == "join"
    if sp.form == "order":
        # the first row of an empty (or all-missing) ordering is no row, `max` a NONE
        return q.kind == "agg" and q.fn in ("max", "min") and bool(rewrites.present)
    if sp.form == "group_all":
        return q.kind == "count" or (q.kind == "agg" and q.fn in ("max", "min", "sum", "avg", "median", "count"))
    return True


def base_data_models(q: SelectQuery) -> tuple:
    """The data models the question has a concrete syntax on (the doc data model lacks
    an edge type's kinds beyond `ids` / `count`)."""
    if q.edge and q.kind not in DOC_EDGE_KINDS:
        return ("graph", "rel")
    return ("doc", "graph", "rel")


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes() if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering -------------------------------------------------------------------------------


def _where_text(q: SelectQuery, sp: SelectSyntax, schema: Schema) -> "str | None":
    """The concrete syntax's filter text with its presence conjuncts."""
    pred = None if q.where is None else render_pred(q.where, schema=schema)
    extra = [present(f) for f in notnull_fields(q, sp.form)]
    if pred is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({pred})" if extra else pred] + extra)


def _source(q: SelectQuery, schema: Schema, sp: SelectSyntax, where: "str | None") -> str:
    t = q.table
    if sp.position == "table" or q.edge:
        return f"FROM {tables_of(schema, t)}" + (f" WHERE {where}" if where else "")
    cond = "record::tb(id) = $cls" if sp.position == "poly" else "record::tb(id) IN $clsl"
    return f"FROM {', '.join(nt.name for nt in schema.node_types)} WHERE {cond}" + (f" AND ({where})" if where else "")


def _join_sub(q: SelectQuery, schema: Schema, sp: SelectSyntax) -> str:
    f, op = q.field, q.fn
    cond = f"$parent.{f} {MIRROR[op]} {f}" if sp.form == "jrev" else f"{f} {op} $parent.{f}"
    cond = cond.replace("<>", "!=")
    sub = f"(SELECT VALUE id FROM {tables_of(schema, q.table)} WHERE {cond})"
    return f"array::len({sub})" if q.arg == "count" else sub


def _doc_edge(q: SelectQuery, schema: Schema) -> str:
    """The doc data model's relationship keys: the link entries of the source class,
    filtered by a bracket filter and flattened."""
    et = schema.edge_type(q.table)
    pred = None if q.where is None else f"[WHERE {render_pred(q.where, schema=schema)}]"
    coll = f"array::flatten((SELECT VALUE ({et.name} ?? []){pred or ''}.eid FROM {et.from_type}))"
    return f"RETURN array::len({coll})" if q.kind == "count" else f"RETURN {coll}"


def _edge_keys(q: SelectQuery, schema: Schema, src: str) -> str:
    coll = f"(SELECT VALUE record::id(id) {src})"
    return f"RETURN array::len({coll})" if q.kind == "count" else f"RETURN {coll}"


def _values(q: SelectQuery, src: str, f: "str | None" = None) -> str:
    """`(SELECT VALUE f <src>)`, the present values of `f` among the matching records
    (the source already filters the field's presence where the form needs it)."""
    return f"(SELECT VALUE {f or q.field} {src})"


def _agg_text(q: SelectQuery, sp: SelectSyntax, src: str) -> str:
    f = q.field
    if sp.form == "order":
        return f"SELECT {f} AS v {src} ORDER BY v {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
    if sp.form == "group_all":
        return f"SELECT {_GROUP_FN[q.fn].format(f=f)} AS v {src} GROUP ALL"
    values = f"({_values(q, src)}[WHERE $this != NONE AND $this != NULL])"
    if q.fn == "count":
        return f"RETURN array::len({values})"
    if q.fn == "distinct_count":
        return f"RETURN array::len(array::distinct({values}))"
    if q.fn == "set":
        return f"RETURN array::distinct({values})"
    if q.fn == "list":
        return f"RETURN {values}"
    return f"RETURN {_AGG_FN[q.fn]}({values})"


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    text = _render(q, schema, sp, rewrites, data_model)
    if q.tx:
        inner = text.removeprefix("RETURN ") if text.startswith("RETURN ") else f"({text})"
        return f"BEGIN TRANSACTION; {tx_write(q.tx, data_model, schema)}; LET $r = {inner}; CANCEL TRANSACTION; RETURN $r"
    return text


def _render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    if q.edge and data_model == "doc":
        return _doc_edge(q, schema)
    kind = q.kind
    src = _source(q, schema, sp, _where_text(q, sp, schema))
    if q.edge and kind in DOC_EDGE_KINDS:
        return _edge_keys(q, schema, src)
    if kind == "join":
        return f"SELECT id, {_join_sub(q, schema, sp)} AS v {src}"
    if kind == "ids":
        return f"SELECT id {src}"
    if kind == "count":
        if sp.form == "group_all":
            return f"SELECT count() AS v {src} GROUP ALL"
        return f"RETURN array::len({_values(q, src, 'id')})"
    if kind == "field":
        return f"SELECT id, {q.field} AS v {src}"
    if kind == "method":
        return f"SELECT id, {q.fn.format(f=q.field)} AS v {src}"
    if kind == "agg":
        return _agg_text(q, sp, src)
    if kind == "group":
        keys = ", ".join(f"{k} ?? NULL AS {name}" for k, name in zip(q.fields, ("k", "k2")))
        value = "count()" if q.fn == "count_star" else _GROUP_FN[q.fn].format(f=q.arg)
        return f"SELECT {keys}, {value} AS v {src} GROUP BY {', '.join(('k', 'k2')[: len(q.fields)])}"
    if kind == "distinct":
        cols = ", ".join(f"{f} ?? NULL" for f in q.fields)
        return f"RETURN array::distinct({_values(q, src, f'[{cols}]')})"
    if kind == "page":
        return f"SELECT id {src} ORDER BY {q.field} {q.directions[0]}, id ASC LIMIT {q.limit} START {q.skip}"
    if kind == "keys":
        cols = ", ".join(f"{k} AS {'ab'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"{'ab'[i]} {d}" for i, d in enumerate(q.directions))
        tail = f" LIMIT {q.limit or _ALL} START {q.skip}" if q.skip else (f" LIMIT {q.limit}" if q.limit else "")
        return f"SELECT {cols} {src} ORDER BY {order}{tail}"
    raise ValueError(kind)


# --- the per-case material ------------------------------------------------------------


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values

    return Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    """The bind variables the concrete syntaxes add to the question's own: the
    polymorphic positions' class names."""
    if schema.base is not None and not q.edge and not schema.is_base(q.table):
        return {"cls": q.table, "clsl": [q.table]}
    return {}


def rewrite_params(rewrites: Rewrites) -> dict:
    return {}
