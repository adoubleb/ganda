"""The concrete syntax catalog of the SELECT IR: HOW a `SelectQuery` is asked. A concrete syntax is
two independent choices, and every applicable combination is an equivalent text:

    axis       choice    the text
    position   table     `FROM t [WHERE p]` -- the filter may be index-served, an
                         ORDER BY may walk an index
               poly      `FROM N0 WHERE @class = :cls [AND (p)]`, the base class
               polyin    `FROM N0 WHERE @class IN :clsl [AND (p)]`
    form       base      the kind's own text
               order     `max(f)` / `min(f)` as `f WHERE f IS NOT NULL ORDER BY f
                         DESC|ASC LIMIT 1` (the first row of the ordering)
               set       a distinct count as `set(f).size()`
               fn        a distinct count as `count(distinct(f))` (O33: counts every
                         non-null row, withheld)
               jrev      a join's correlation reversed (`$parent.$current.f > f` for
                         `f < $parent.$current.f`): never index-served either

The filter is the drawn predicate's own text (`predicate.render_predicate`): its leaves'
forms -- reversed operands, arithmetic, a literal -- are drawn with the predicate
(`predicate_sampling/forms.leaf_form`), never derived from another filter.

A data model's name is `<data_model>:<tag>`, the tag the non-default choices joined by `+` in the
order form, position (`graph:order+poly`, `rel:polyin`). The
base concrete syntax (table, base) is the same text on every data model. No
choice exists only to make a data model use or avoid an index: a pair differs in data
model or in spelling, never in the planner path alone (the multi-model relation is the
question; one data model's index against its own scan is not).

A join puts its correlated `LET $m = (SELECT id FROM t WHERE f op
$parent.$current.f)` between the FROM target and the WHERE (LET precedes WHERE);
the LET correlates with the class's records (probed: `$parent.$current` reads them,
as it reads the base class's).

An EDGE-TYPE target reads `eid AS id` from the graph E class and the junction class
(the `table` position); the doc data model has no relationship
class, so it renders `ids` and `count` alone, through the source records' embedded
link lists -- `SELECT v AS id FROM (SELECT e[p].eid AS v FROM src UNWIND v) WHERE v
IS NOT NULL` (a bracket filter is never index-served, so the positions are one).

The TRANSACTION door wraps every concrete syntax of a `tx` question the same way:
`BEGIN; <write>; LET $r = (<text> TIMEOUT ...); ROLLBACK; RETURN $r`, the write
per data model (`select_support.tx_write`: the graph data model's `CREATE VERTEX` / `DELETE
VERTEX`), so the pairs compare transaction views alike.

Facts (probed 3.2.56): a subquery target's records keep their class's semantics
(a `ci` comparison, a DATETIME against a string constant, a DECIMAL, `IS [NOT] DEFINED`
on an absent field); `ORDER BY` over a subquery sorts where the same ORDER BY on the
class walks an index (`FROM t ORDER BY f DESC` over an index ignoring nulls lacks the
null records, O18 -- also under a WHERE on another field); GROUP BY, DISTINCT, UNWIND,
SKIP / LIMIT and every aggregate work over a subquery target.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.data.model import Instance, Schema
from ganda.orientdb.concrete_syntax.query.predicate import render_predicate as render_pred
from ganda.orientdb.concrete_syntax.query.scope import CURRENT, Path
from ganda.core.query.ir.predicates import MIRROR
from ganda.orientdb.concrete_syntax.query.support import METHODS  # noqa: F401 -- the method catalog
from ganda.core.query.ir.select import DOC_EDGE_KINDS, SelectQuery, notnull_fields

from ganda.orientdb.rules import assumes
from ganda.orientdb.rules.catalog import SQL

POSITIONS = ("table", "poly", "polyin")
FORMS = ("base", "order", "set", "fn", "jrev")
# the positions whose ORDER BY may be an index walk (the O18 / O23 shapes)
WALK_POSITIONS = ("table", "poly", "polyin")


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
    """The per-case material the concrete syntaxes draw on, drawn once when the case is built:
    the present values of the aggregated field among the matching records."""

    present: list = field(default_factory=list)


def all_syntaxes() -> list:
    return [SelectSyntax(p, m) for m, p in itertools.product(FORMS, POSITIONS)]


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if q.edge:
        if sp.position in ("poly", "polyin"):
            return False
        if data_model == "doc" and (q.kind not in DOC_EDGE_KINDS or sp.position != "table" or sp.form != "base"):
            return False
    if sp.form == "jrev" and q.kind != "join":
        return False
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
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


def _filter_text(q: SelectQuery) -> "str | None":
    return None if q.where is None else render_pred(q.where)


def _where_text(q: SelectQuery, sp: SelectSyntax) -> "str | None":
    """The concrete syntax's filter text with its `IS NOT NULL` conjuncts."""
    pred = _filter_text(q)
    extra = [f"{f} IS NOT NULL" for f in notnull_fields(q, sp.form)]
    if pred is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({pred})" if extra else pred] + extra)


# ... so the base-class positions filter `@class = :cls`


def _source(q: SelectQuery, schema: Schema, sp: SelectSyntax, where: "str | None", let: str = "") -> str:
    """The FROM target, the LET (a join's; it precedes the WHERE) and the WHERE."""
    t = q.table
    let = f" {let}" if let else ""
    if sp.position == "table":
        return f"FROM {t}{let}" + (f" WHERE {where}" if where else "")
    if sp.position in ("poly", "polyin"):
        cond = "@class = :cls" if sp.position == "poly" else "@class IN :clsl"
        return f"FROM {schema.base.name}{let} WHERE {cond}" + (f" AND ({where})" if where else "")
    raise ValueError(sp.position)


def _join_let(q: SelectQuery, sp: SelectSyntax) -> str:
    # `n` is the LET's record, `x` the enclosing one it is joined with
    f, op = q.field, q.fn
    x = Path(CURRENT).field(f)
    cond = f"{x} {MIRROR[op]} {f}" if sp.form == "jrev" else f"{f} {op} {x}"
    return f"LET $m = (SELECT id FROM {q.table} WHERE {cond})"


assumes("unwind.bracket_projection.row_per_element", SQL, True)


def _doc_edge(q: SelectQuery, schema: Schema) -> str:
    """The doc data model's relationship records: the embedded link documents of the
    source class, filtered by a bracket filter and unwound one row per link (an
    empty or absent list unwinds to a null row, filtered out)."""
    et = schema.edge_type(q.table)
    pred = _filter_text(q)
    coll = f"{et.name}[{pred}].eid" if pred else f"{et.name}.eid"
    inner = f"SELECT v FROM (SELECT {coll} AS v FROM {et.from_type} UNWIND v) WHERE v IS NOT NULL"
    if q.kind == "count":
        return f"SELECT count(*) AS v FROM ({inner})"
    return f"SELECT v AS id FROM ({inner})"


# the `set` form carries no IS NOT NULL: set() drops nulls


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


# the transaction door answers as the plain statement does (`RETURN $r` after ROLLBACK)


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    text = _render(q, schema, sp, rewrites, data_model)
    if q.tx:
        from ganda.orientdb.concrete_syntax.query.support import tx_write

        return f"BEGIN; {tx_write(q.tx, data_model)}; LET $r = ({text} {TX_TIMEOUT}); ROLLBACK; RETURN $r"
    return text


def _render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    if q.edge and data_model == "doc":
        return _doc_edge(q, schema)
    kind = q.kind
    src = _source(q, schema, sp, _where_text(q, sp), _join_let(q, sp) if kind == "join" else "")
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


# --- the per-case material ------------------------------------------------------------


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values

    return Rewrites(present=present_values(q, instance) if q.kind == "agg" else [])


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    """The bind variables the concrete syntaxes add to the question's own: the `@class`
    filter's names."""
    if schema.base is not None and not q.edge and not schema.is_base(q.table):
        return {"cls": q.table, "clsl": [q.table]}
    return {}


def rewrite_params(rewrites: Rewrites) -> dict:
    """What a bundle records of the per-case material: nothing beyond the question."""
    return {}
