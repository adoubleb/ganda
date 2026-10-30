from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

from ganda.core.data.model import FieldType, Instance, Schema
from ganda.orientdb.concrete_syntax.query.predicate import render_predicate as render_pred
from ganda.orientdb.concrete_syntax.query.scope import CURRENT, RECORD, Path
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
        if data_model == "doc" and (sp.position != "table" or sp.form != "base"):
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
    """The data models the question has a concrete syntax on: every one."""
    return ("doc", "graph", "rel")


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes() if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering -------------------------------------------------------------------------------


# the record a select reads: a node, an edge type's record on the graph E class and the
# junction class (`eid`), doc's link document unwound into a row (`render_unwound`)
EDGE = Path(identity="eid")
LINK = Path("l.", "eid", element=True)


def _filter_text(q: SelectQuery, at: Path = RECORD) -> "str | None":
    return None if q.where is None else render_pred(q.where, at)


def _where_text(q: SelectQuery, sp: SelectSyntax, at: Path = RECORD) -> "str | None":
    """The concrete syntax's filter text with its `IS NOT NULL` conjuncts."""
    pred = _filter_text(q, at)
    extra = [f"{at.field(f)} IS NOT NULL" for f in notnull_fields(q, sp.form)]
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


def _agg_text(q: SelectQuery, sp: SelectSyntax, src: str, at: Path = RECORD) -> str:
    f = at.field(q.field)
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
        return _doc_edge(q, schema) if q.kind in DOC_EDGE_KINDS else render_unwound(q, schema)
    src = _source(q, schema, sp, _where_text(q, sp), _join_let(q, sp) if q.kind == "join" else "")
    return _kind_text(q, sp, src, EDGE if q.edge else RECORD)


def _kind_text(q: SelectQuery, sp: SelectSyntax, src: str, at: Path) -> str:
    """The kind's text over `src`, the record `at` names."""
    kind = q.kind
    rid = at.id if at.id == "id" else f"{at.id} AS id"
    f = at.field(q.field) if q.fields else None
    if kind == "join":
        return f"SELECT id, {'$m.size()' if q.arg == 'count' else '$m.id'} AS v {src}"
    if kind == "ids":
        return f"SELECT {rid} {src}"
    if kind == "count":
        return f"SELECT count(*) AS v {src}"
    if kind == "field":
        return f"SELECT {rid}, {f} AS v {src}"
    if kind == "method":
        return f"SELECT {rid}, {q.fn.format(f=f)} AS v {src}"
    if kind == "agg":
        return _agg_text(q, sp, src, at)
    if kind == "group":
        keys = ", ".join(f"{at.field(k)} AS {name}" for k, name in zip(q.fields, ("k", "k2")))
        value = "count(*)" if q.fn == "count_star" else f"{q.fn}({at.field(q.arg)})"
        return f"SELECT {keys}, {value} AS v {src} GROUP BY {', '.join(at.field(k) for k in q.fields)}"
    if kind == "distinct":
        cols = ", ".join(f"{at.field(k)} AS {name}" for k, name in zip(q.fields, ("v", "w")))
        return f"SELECT DISTINCT {cols} {src}"
    if kind == "page":
        return f"SELECT {rid} {src} ORDER BY {f} {q.directions[0]}, {at.id} ASC SKIP {q.skip} LIMIT {q.limit}"
    if kind == "keys":
        cols = ", ".join(f"{at.field(k)} AS {'ab'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"{at.field(k)} {d}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + (f" SKIP {q.skip}" if q.skip else "") + (f" LIMIT {q.limit}" if q.limit else "")
    raise ValueError(kind)


assumes("unwind.embedded_document.row_per_document", SQL, True)
assumes("arithmetic.as_long.exact", SQL, True)


def render_unwound(q: SelectQuery, schema: Schema) -> str:
    """The doc data model's edge-type select of a kind beyond `ids` / `count`: the
    select_edges frame over the source class's link documents unwound one row per
    link, a link the record (`LINK`: `l.f`, `l.eid`, its definedness through its keys)
    -- `FROM (SELECT e AS l FROM src UNWIND l) WHERE l IS NOT NULL [AND p]` (an empty
    or absent list unwinds to a null row)."""
    et = schema.edge_type(q.table)
    where = _where_text(q, BASE, LINK)
    src = f"FROM (SELECT {et.name} AS l FROM {et.from_type} UNWIND l) WHERE l IS NOT NULL" + (f" AND {where}" if where else "")
    if q.kind == "method" and schema.field(q.table, q.field, edge=True).type is FieldType.INT:
        # an undeclared INT holds a value within 32 bits as an Integer, whose
        # arithmetic wraps (`* 2`, `math_abs(-2^31)`): read as the LONG the others declare
        return f"SELECT {LINK.id} AS id, {q.fn.format(f=LINK.field(q.field) + '.asLong()')} AS v {src}"
    return _kind_text(q, BASE, src, LINK)


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
