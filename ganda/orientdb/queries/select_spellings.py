"""The spelling catalog of the SELECT IR: HOW a `SelectQuery` is asked. A spelling is
three independent choices, and every applicable combination is an equivalent text:

    axis       choice    the text
    filter     plain     `WHERE p`
               rw        `WHERE rewrite(p)`: an equivalent predicate tree (De Morgan,
                         `IN [x]` for `= x`, the null-aware negations, ...)
               rev       `WHERE reverse(p)`: the reversible leaves' operands swapped
                         (`:p > f`), never index-served (probed)
               arith     `(f + :c) op :pc AND f IS NOT NULL` for a leaf, where the
                         arithmetic is exact on every stored value; never index-served
               literal   the numeric pivots spelled as literals (O12: a double literal
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

A side's name is `<side>:<tag>`, the tag the non-default choices joined by `+` in the
order form, filter, position (`doc:rw`, `graph:order+nest`, `rel:rev+scan`). The
base spelling (plain,
table, base) is the same text on every side, so the three databases -- one schema,
one data set, different indexes per side -- are the index-vs-scan oracle, and every
variant is also a same-database pair.

A join puts its correlated `LET $m = (SELECT id FROM t WHERE f op
$parent.$current.f)` between the FROM target and the WHERE (LET precedes WHERE);
under `nest` the filter is inside the target and the LET correlates with the
subquery's rows (probed: `$parent.$current` reads them, as it reads a `scan`
target's and the base class's).

An EDGE-TYPE target reads `eid AS id` from the graph E class and the junction class
(every position but the base-class and door ones); the doc side has no relationship
class, so it spells `ids` and `count` alone, through the source records' embedded
link lists -- `SELECT v AS id FROM (SELECT e[p].eid AS v FROM src UNWIND v) WHERE v
IS NOT NULL` -- with the plain, rewritten and reversed filters only (its attributes
are untyped JSON: the arithmetic twin's exactness rests on the declared type, and a
bracket filter is never index-served, so the positions are one).

The TRANSACTION door wraps every spelling of a `tx` question the same way:
`BEGIN; <write>; LET $r = (<text> TIMEOUT ...); ROLLBACK; RETURN $r`, the write
per side (`select_support.tx_write`: the graph side's `CREATE VERTEX` / `DELETE
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
from dataclasses import dataclass, field

from ganda.orientdb.model import Schema
from ganda.orientdb.queries.osql import render_predicate as render_pred
from ganda.orientdb.queries.predicates import MIRROR
from ganda.orientdb.queries.select_ir import DOC_EDGE_KINDS, SelectQuery, notnull_fields

FILTERS = ("plain", "rw", "rev", "arith", "literal")
POSITIONS = ("table", "scan", "nest", "poly", "polyin", "index")
FORMS = ("base", "order", "set", "fn", "jscan", "jrev")
# the positions whose ORDER BY may be an index walk (the O18 / O23 shapes)
WALK_POSITIONS = ("table", "poly", "polyin")


@dataclass(frozen=True)
class Spelling:
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


BASE = Spelling()


@dataclass
class Twins:
    """The per-case material the spellings draw on, drawn once when the case is built:
    the filter's twins (None where it has none), the `index:` door per side, and the
    present values of the aggregated field among the matching records."""

    rw: object = None
    rev: object = None
    arith: object = None
    literal: "str | None" = None
    door: dict = field(default_factory=dict)  # side -> (text, extra binds, IndexSpec)
    present: list = field(default_factory=list)

    def filter_pred(self, q: SelectQuery, name: str):
        """The predicate object a filter choice stands for (the literal spelling's is
        the plain filter: only its text differs)."""
        if name in ("plain", "literal"):
            return q.where
        return getattr(self, name)


def all_spellings() -> list:
    return [Spelling(f, p, m) for m, f, p in itertools.product(FORMS, FILTERS, POSITIONS)]


def applicable(q: SelectQuery, schema: Schema, sp: Spelling, twins: Twins, side: str) -> bool:
    if q.edge:
        if sp.position in ("poly", "polyin", "index"):
            return False
        if side == "doc" and (q.kind not in DOC_EDGE_KINDS or sp.position != "table" or sp.filter not in ("plain", "rw", "rev") or sp.form != "base"):
            return False
    if sp.form in ("jscan", "jrev") and q.kind != "join":
        return False
    if sp.filter != "plain":
        if q.where is None:
            return False
        if sp.filter == "literal" and twins.literal is None:
            return False
        if sp.filter != "literal" and getattr(twins, sp.filter) is None:
            return False
    if sp.position == "nest" and q.where is None:
        return False  # the same text as `scan`
    if sp.position in ("poly", "polyin") and (schema.base is None or schema.is_base(q.table)):
        return False
    if sp.position == "index":
        return q.kind == "ids" and sp.filter == "plain" and sp.form == "base" and side in twins.door
    if sp.form == "order":
        # the first row of an empty (or all-null) ordering is no row, `max` a null one
        return q.kind == "agg" and q.fn in ("max", "min") and bool(twins.present)
    if sp.form in ("set", "fn"):
        return q.kind == "agg" and q.fn == "distinct_count"
    return True


def base_sides(q: SelectQuery) -> tuple:
    """The base sides the question has a spelling on (the doc side lacks an edge
    type's kinds beyond `ids` / `count`)."""
    if q.edge and q.kind not in DOC_EDGE_KINDS:
        return ("graph", "rel")
    return ("doc", "graph", "rel")


def spellings_for(q: SelectQuery, schema: Schema, twins: Twins, side: str) -> list:
    return [sp for sp in all_spellings() if sp != BASE and applicable(q, schema, sp, twins, side)]


# --- rendering -------------------------------------------------------------------------------


def _filter_text(q: SelectQuery, sp: Spelling, twins: Twins) -> "str | None":
    if q.where is None:
        return None
    if sp.filter == "literal":
        return twins.literal
    return render_pred(twins.filter_pred(q, sp.filter))


def _where_text(q: SelectQuery, sp: Spelling, twins: Twins) -> "str | None":
    """The spelling's filter text with its `IS NOT NULL` conjuncts."""
    pred = _filter_text(q, sp, twins)
    extra = [f"{f} IS NOT NULL" for f in notnull_fields(q, sp.form)]
    if pred is None:
        return " AND ".join(extra) or None
    return " AND ".join([f"({pred})" if extra else pred] + extra)


def _source(q: SelectQuery, schema: Schema, sp: Spelling, where: "str | None", let: str = "") -> str:
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


def _join_let(q: SelectQuery, sp: Spelling) -> str:
    f, op = q.field, q.fn
    target = f"(SELECT FROM {q.table})" if sp.form == "jscan" else q.table
    cond = f"$parent.$current.{f} {MIRROR[op]} {f}" if sp.form == "jrev" else f"{f} {op} $parent.$current.{f}"
    return f"LET $m = (SELECT id FROM {target} WHERE {cond})"


def _doc_edge(q: SelectQuery, schema: Schema, sp: Spelling, twins: Twins) -> str:
    """The doc side's relationship records: the embedded link documents of the
    source class, filtered by a bracket filter and unwound one row per link (an
    empty or absent list unwinds to a null row, filtered out)."""
    et = schema.edge_type(q.table)
    pred = _filter_text(q, sp, twins)
    coll = f"{et.name}[{pred}].eid" if pred else f"{et.name}.eid"
    inner = f"SELECT v FROM (SELECT {coll} AS v FROM {et.from_type} UNWIND v) WHERE v IS NOT NULL"
    if q.kind == "count":
        return f"SELECT count(*) AS v FROM ({inner})"
    return f"SELECT v AS id FROM ({inner})"


def _agg_text(q: SelectQuery, sp: Spelling, src: str) -> str:
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


def render(q: SelectQuery, schema: Schema, sp: Spelling, twins: Twins, side: str) -> str:
    text = _render(q, schema, sp, twins, side)
    if q.tx:
        from ganda.orientdb.queries.select_support import tx_write

        return f"BEGIN; {tx_write(q.tx, side)}; LET $r = ({text} {TX_TIMEOUT}); ROLLBACK; RETURN $r"
    return text


def _render(q: SelectQuery, schema: Schema, sp: Spelling, twins: Twins, side: str) -> str:
    if sp.position == "index":
        return twins.door[side][0]
    if q.edge and side == "doc":
        return _doc_edge(q, schema, sp, twins)
    kind = q.kind
    src = _source(q, schema, sp, _where_text(q, sp, twins), _join_let(q, sp) if kind == "join" else "")
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
