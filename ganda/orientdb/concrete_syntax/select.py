"""The concrete syntax catalog of the SELECT IR: HOW a `SelectQuery` is asked. A concrete syntax is
three independent choices, and every applicable combination is an equivalent text:

    axis       choice    the text
    filter     plain     `WHERE p`
               rw        `WHERE rewrite(p)`: an equivalent predicate tree (De Morgan,
                         `IN [x]` for `= x`, the null-aware negations, ...)
               rev       `WHERE reverse(p)`: the reversible leaves' operands swapped
                         (`:p > f`), never index-served (probed)
               arith     `(f + :c) op :pc AND f IS NOT NULL` for a leaf, where the
                         arithmetic is exact on every stored value; never index-served
               literal   the numeric pivots rendered as literals (O12: a double literal
                         parses as a float32)
    position   table     `FROM t [WHERE p]` -- the filter may be index-served, an
                         ORDER BY may walk an index
               scan      `FROM (SELECT FROM t) [WHERE p]` -- a subquery target is
                         never index-served, and its ORDER BY always sorts
               nest      `FROM (SELECT FROM t WHERE p)` with the projection, the
                         aggregate, the grouping or the ordering outside: the filter
                         index-served, everything after it over the subquery's rows
               poly      `FROM N0 WHERE @class = :cls [AND (p)]`, the base class
               polyin    `FROM N0 WHERE @class IN :clsl [AND (p)]`
               index     `SELECT rid.id AS id FROM index:<ix> WHERE key ...` (the
                         `ids` kind over an equality / IN / BETWEEN / composite key)
    form       base      the kind's own text
               order     `max(f)` / `min(f)` as `f WHERE f IS NOT NULL ORDER BY f
                         DESC|ASC LIMIT 1` (the first row of the ordering)
               set       a distinct count as `set(f).size()`
               fn        a distinct count as `count(distinct(f))` (O33: counts every
                         non-null row, withheld)
               jscan     a join's LET subquery over `(SELECT FROM t)`: the inner
                         lookup never index-served
               jrev      a join's correlation reversed (`$parent.$current.f > f` for
                         `f < $parent.$current.f`): never index-served either

A data model's name is `<data_model>:<tag>`, the tag the non-default choices joined by `+` in the
order form, filter, position (`doc:rw`, `graph:order+nest`, `rel:rev+scan`). The
base concrete syntax (plain,
table, base) is the same text on every data model, so the three databases -- one schema,
one data set, different indexes per data model -- are the index-vs-scan oracle, and every
variant is also a same-database pair.

A join puts its correlated `LET $m = (SELECT id FROM t WHERE f op
$parent.$current.f)` between the FROM target and the WHERE (LET precedes WHERE);
under `nest` the filter is inside the target and the LET correlates with the
subquery's rows (probed: `$parent.$current` reads them, as it reads a `scan`
target's and the base class's).

An EDGE-TYPE target reads `eid AS id` from the graph E class and the junction class
(every position but the base-class and door ones); the doc data model has no relationship
class, so it renders `ids` and `count` alone, through the source records' embedded
link lists -- `SELECT v AS id FROM (SELECT e[p].eid AS v FROM src UNWIND v) WHERE v
IS NOT NULL` -- with the plain, rewritten and reversed filters only (its attributes
are untyped JSON: the arithmetic rewrite's exactness rests on the declared type, and a
bracket filter is never index-served, so the positions are one).

The TRANSACTION door wraps every concrete syntax of a `tx` question the same way:
`BEGIN; <write>; LET $r = (<text> TIMEOUT ...); ROLLBACK; RETURN $r`, the write
per data model (`select_support.tx_write`: the graph data model's `CREATE VERTEX` / `DELETE
VERTEX`), so the pairs compare transaction views alike.

Facts (probed 3.2.56): a subquery target's records keep their class's semantics
(a `ci` comparison, a DATETIME against a string pivot, a DECIMAL, `IS [NOT] DEFINED`
on an absent field); `ORDER BY` over a subquery sorts where the same ORDER BY on the
class walks an index (`FROM t ORDER BY f DESC` over an index ignoring nulls answers
no rows, O18 -- also under a WHERE on another field); GROUP BY, DISTINCT, UNWIND,
SKIP / LIMIT and every aggregate work over a subquery target.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.data.model import Instance, Schema
from ganda.orientdb.facade import predicates
from ganda.orientdb.concrete_syntax.predicate import render_predicate as render_pred
from ganda.orientdb.facade.predicates import MIRROR, Predicate
from ganda.orientdb.concrete_syntax.support import METHODS  # noqa: F401 -- the method catalog
from ganda.core.query.ir.select import DOC_EDGE_KINDS, SelectQuery, notnull_fields

FILTERS = ("plain", "rw", "rev", "arith", "literal")
POSITIONS = ("table", "scan", "nest", "poly", "polyin", "index")
FORMS = ("base", "order", "set", "fn", "jscan", "jrev")
# the positions whose ORDER BY may be an index walk (the O18 / O23 shapes)
WALK_POSITIONS = ("table", "poly", "polyin")


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
    """The per-case material the concrete syntaxes draw on, drawn once when the case is built:
    the filter's rewrites (None where it has none), the `index:` door per data model, and the
    present values of the aggregated field among the matching records."""

    rw: object = None
    rev: object = None
    arith: object = None
    literal: "str | None" = None
    door: dict = field(default_factory=dict)  # data model -> (text, extra binds, IndexSpec)
    present: list = field(default_factory=list)

    def filter_pred(self, q: SelectQuery, name: str):
        """The predicate object a filter choice stands for (the literal concrete syntax's is
        the plain filter: only its text differs)."""
        if name in ("plain", "literal"):
            return q.where
        return getattr(self, name)


def all_syntaxes() -> list:
    return [SelectSyntax(f, p, m) for m, f, p in itertools.product(FORMS, FILTERS, POSITIONS)]


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if q.edge:
        if sp.position in ("poly", "polyin", "index"):
            return False
        if data_model == "doc" and (q.kind not in DOC_EDGE_KINDS or sp.position != "table" or sp.filter not in ("plain", "rw", "rev") or sp.form != "base"):
            return False
    if sp.form in ("jscan", "jrev") and q.kind != "join":
        return False
    if sp.filter != "plain":
        if q.where is None:
            return False
        if sp.filter == "literal" and rewrites.literal is None:
            return False
        if sp.filter != "literal" and getattr(rewrites, sp.filter) is None:
            return False
    if sp.position == "nest" and q.where is None:
        return False  # the same text as `scan`
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
    if sp.position == "index":
        return q.kind == "ids" and sp.filter == "plain" and sp.form == "base" and data_model in rewrites.door
    if sp.form == "order":
        # the first row of an empty (or all-null) ordering is no row, `max` a null one
        return q.kind == "agg" and q.fn in ("max", "min") and bool(rewrites.present)
    if sp.form in ("set", "fn"):
        return q.kind == "agg" and q.fn == "distinct_count"
    return True


def base_data_models(q: SelectQuery) -> tuple:
    """The data models the question has a concrete syntax on (the doc data model lacks an edge
    type's kinds beyond `ids` / `count`)."""
    if q.edge and q.kind not in DOC_EDGE_KINDS:
        return ("graph", "rel")
    return ("doc", "graph", "rel")


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes() if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering -------------------------------------------------------------------------------


def _filter_text(q: SelectQuery, sp: SelectSyntax, rewrites: Rewrites) -> "str | None":
    if q.where is None:
        return None
    if sp.filter == "literal":
        return rewrites.literal
    return render_pred(rewrites.filter_pred(q, sp.filter))


def _where_text(q: SelectQuery, sp: SelectSyntax, rewrites: Rewrites) -> "str | None":
    """The concrete syntax's filter text with its `IS NOT NULL` conjuncts."""
    pred = _filter_text(q, sp, rewrites)
    extra = [f"{f} IS NOT NULL" for f in notnull_fields(q, sp.form)]
    if pred is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({pred})" if extra else pred] + extra)


def _source(q: SelectQuery, schema: Schema, sp: SelectSyntax, where: "str | None", let: str = "") -> str:
    """The FROM target, the LET (a join's; it precedes the WHERE) and the WHERE."""
    t = q.table
    let = f" {let}" if let else ""
    if sp.position == "table":
        return f"FROM {t}{let}" + (f" WHERE {where}" if where else "")
    if sp.position == "scan":
        return f"FROM (SELECT FROM {t}){let}" + (f" WHERE {where}" if where else "")
    if sp.position == "nest":
        return f"FROM (SELECT FROM {t} WHERE {where}){let}"
    if sp.position in ("poly", "polyin"):
        cond = "@class = :cls" if sp.position == "poly" else "@class IN :clsl"
        return f"FROM {schema.base.name}{let} WHERE {cond}" + (f" AND ({where})" if where else "")
    raise ValueError(sp.position)


def _join_let(q: SelectQuery, sp: SelectSyntax) -> str:
    f, op = q.field, q.fn
    target = f"(SELECT FROM {q.table})" if sp.form == "jscan" else q.table
    cond = f"$parent.$current.{f} {MIRROR[op]} {f}" if sp.form == "jrev" else f"{f} {op} $parent.$current.{f}"
    return f"LET $m = (SELECT id FROM {target} WHERE {cond})"


def _doc_edge(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites) -> str:
    """The doc data model's relationship records: the embedded link documents of the
    source class, filtered by a bracket filter and unwound one row per link (an
    empty or absent list unwinds to a null row, filtered out)."""
    et = schema.edge_type(q.table)
    pred = _filter_text(q, sp, rewrites)
    coll = f"{et.name}[{pred}].eid" if pred else f"{et.name}.eid"
    inner = f"SELECT v FROM (SELECT {coll} AS v FROM {et.from_type} UNWIND v) WHERE v IS NOT NULL"
    if q.kind == "count":
        return f"SELECT count(*) AS v FROM ({inner})"
    return f"SELECT v AS id FROM ({inner})"


def _agg_text(q: SelectQuery, sp: SelectSyntax, src: str) -> str:
    f = q.field
    if sp.form == "order":
        return f"SELECT {f} AS v {src} ORDER BY {f} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
    if sp.form == "set":
        return f"SELECT set({f}).size() AS v {src}"
    if sp.form == "fn":
        return f"SELECT count(distinct({f})) AS v {src}"
    if q.fn == "distinct_count":
        return f"SELECT count(*) AS v FROM (SELECT DISTINCT {f} {src})"
    if q.fn in ("set", "list"):
        # the bag unwound into one row per element, the empty bag's null UNWIND row
        # filtered out
        return f"SELECT v FROM (SELECT {q.fn}({f}) AS v {src} UNWIND v) WHERE v IS NOT NULL"
    return f"SELECT {q.fn}({f}) AS v {src}"


TX_TIMEOUT = "TIMEOUT 20000 EXCEPTION"


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    text = _render(q, schema, sp, rewrites, data_model)
    if q.tx:
        from ganda.orientdb.concrete_syntax.support import tx_write

        return f"BEGIN; {tx_write(q.tx, data_model)}; LET $r = ({text} {TX_TIMEOUT}); ROLLBACK; RETURN $r"
    return text


def _render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    if sp.position == "index":
        return rewrites.door[data_model][0]
    if q.edge and data_model == "doc":
        return _doc_edge(q, schema, sp, rewrites)
    kind = q.kind
    src = _source(q, schema, sp, _where_text(q, sp, rewrites), _join_let(q, sp) if kind == "join" else "")
    rid = "eid AS id" if q.edge else "id"
    if kind == "join":
        return f"SELECT id, {'$m.size()' if q.arg == 'count' else '$m.id'} AS v {src}"
    if kind == "ids":
        return f"SELECT {rid} {src}"
    if kind == "count":
        return f"SELECT count(*) AS v {src}"
    if kind == "field":
        return f"SELECT {rid}, {q.field} AS v {src}"
    if kind == "method":
        return f"SELECT {rid}, {q.fn.format(f=q.field)} AS v {src}"
    if kind == "agg":
        return _agg_text(q, sp, src)
    if kind == "group":
        keys = ", ".join(f"{k} AS {name}" for k, name in zip(q.fields, ("k", "k2")))
        value = "count(*)" if q.fn == "count_star" else f"{q.fn}({q.arg})"
        return f"SELECT {keys}, {value} AS v {src} GROUP BY {', '.join(q.fields)}"
    if kind == "distinct":
        cols = ", ".join(f"{f} AS {name}" for f, name in zip(q.fields, ("v", "w")))
        return f"SELECT DISTINCT {cols} {src}"
    if kind == "page":
        tie = "eid" if q.edge else "id"
        return f"SELECT {rid} {src} ORDER BY {q.field} {q.directions[0]}, {tie} ASC SKIP {q.skip} LIMIT {q.limit}"
    if kind == "keys":
        cols = ", ".join(f"{k} AS {'ab'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"{k} {d}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + (f" SKIP {q.skip}" if q.skip else "") + (f" LIMIT {q.limit}" if q.limit else "")
    raise ValueError(kind)


# --- the rewrites of one question ------------------------------------------------


def _door(p, table: str, schema: Schema, instance: Instance, data_model: str) -> "tuple | None":
    """The `index:` door of `select_support.index_door` where it answers every matching
    record (`door_complete`), its own bind variable renamed per data model (two data models'
    composite indexes may order the same key differently)."""
    from ganda.orientdb.concrete_syntax.support import door_complete, index_door as _index_door

    door = _index_door(p, table, schema, data_model)
    if door is None or not door_complete(door[2], table, p, instance):
        return None
    text, extra, ix = door
    renamed = {}
    for k, v in extra.items():
        assert text.endswith(f":{k}"), text
        text = text[: -len(k)] + f"{k}_{data_model}"
        renamed[f"{k}_{data_model}"] = v
    return text, renamed, ix


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values, records, view
    from ganda.orientdb.concrete_syntax.support import literal_pred as _literal_pred

    rewrites = Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = predicates.rewrite(p, rng, "q")
    rewrites.rev = predicates.reverse(p)
    if isinstance(p, Predicate) and p.sub is None:
        try:
            ftype = schema.field(q.table, p.field, edge=q.edge).type
        except KeyError:
            ftype = None
        if ftype is not None:
            # exact on every value the read may meet: the transaction's written one
            # too
            stored = [n.values.get(p.field) for n in records(q, instance) + (records(q, view(q, instance)) if q.tx else []) if p.field in n.values]
            rewrites.arith = predicates.arithmetic(p, rng, "ar", ftype, stored)
    rewrites.literal = _literal_pred(p)
    if q.kind == "ids" and not q.edge:
        for data_model in ("doc", "graph", "rel"):
            door = _door(p, q.table, schema, instance, data_model)
            if door is not None:
                rewrites.door[data_model] = door
    return rewrites


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    """The bind variables the concrete syntaxes add to the question's own: the
    rewrites' pivots, the `index:` door's keys and the `@class` filter's names."""
    bind: dict = {}
    for rewritten in (rewrites.rw, rewrites.rev, rewrites.arith):
        if rewritten is not None:
            bind.update(rewritten.bind_vars)
    for _text, extra, _ix in rewrites.door.values():
        bind.update(extra)
    if schema.base is not None and not q.edge and not schema.is_base(q.table):
        bind.update({"cls": q.table, "clsl": [q.table]})
    return bind


def rewrite_params(rewrites: Rewrites) -> dict:
    """What a bundle records of the rewrites."""
    params: dict = {}
    for name in ("rw", "rev", "arith"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    if rewrites.door:
        params["index_door"] = {data_model: d[2].name() for data_model, d in rewrites.door.items()}
    return params
