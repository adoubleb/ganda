"""The concrete syntax catalog: HOW each data model -- and each of that data model's
syntaxes -- asks a `TraversalQuery`. One abstract question is rendered through every
engine path the storage model offers, and each rendering is a variant the oracle
compares against the other data models'.

    data model   concrete syntax    the engine path
    graph  arrow       the graph idioms `->e1[WHERE p]->n2[WHERE q]` (`<-` / `<->`) chained
                       per hop over the edge tables' `in` / `out`
    graph  edgescan    a scan of the edge table, `in` / `out` dereferenced (one hop)
    graph  subquery    `(SELECT VALUE out FROM e1 WHERE in = $parent.id ...)`, nested per hop
    graph  recursion   the reach forms through `@.{1..k+collect}(->e1->n1)`
    doc    links       `(e1 ?? [])[WHERE p].target[WHERE q]` over the embedded link arrays,
                       flattened per hop; an incoming first hop as a correlated subquery over
                       the sources, one id per matching link (`array::repeat`)
    doc    closure     the same with the edge predicate in `array::filter(.., |$l| ..)`
    rel    subquery    `(SELECT VALUE dst FROM e1 WHERE src = $parent.id ...)`, nested per hop
                       through the junction table, flattened per level
    rel    flat        the junction table scanned into (source, target) rows (one hop)

Every concrete syntax also has a PULLED-UP form (`render(..., pull_up=True)`, the
`<data model>:pull_up` variant): the source predicate is lifted out of the syntax into
an enclosing `SELECT id, v FROM (<syntax>) WHERE <source predicate>`, the inner query
projecting the source fields it needs -- the same transformation on every data model.

Facts each rests on (probed 3.3.4, `rules/probe.py`): `$parent` inside a closure is
the enclosing query's record, inside a bracket filter it is not (so an incoming hop
counts its links through `array::filter`); a field read through an array of records
maps over them (`->e1->n2.f1`, `(e1 ?? []).target.f1`); a missing array reads NONE
(`?? []`), and a NONE element of a flattened path is dropped before the next hop; the
aggregate functions refuse a NONE / NULL element (filtered first) and answer NONE over
nothing; `->` answers parallel edges twice.
"""
from __future__ import annotations

import dataclasses

from ganda.core.data.model import Schema
from ganda.core.query.ir.traversal import AGG_PROJECTIONS, EDGE_PROJECTIONS, FIELD_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, Hop, TraversalQuery
from ganda.core.query.ir.predicates import fields_of
from ganda.core.rules.vocabulary import EmptyAggregate
from ganda.surrealdb.concrete_syntax.query.predicate import present, render_predicate as render_pred
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import GRAPH, SUBQUERY, SURQL

SYNTAXES = {
    "graph": ("arrow", "edgescan", "subquery", "recursion"),
    "doc": ("links", "closure"),
    "rel": ("subquery", "flat"),
}
GRAPH_REACH_SYNTAXES = ("arrow", "recursion")
_MULTI = ("ids", "field", "distinct_ids", "efield")
# a NONE / NULL element is filtered before an aggregate (which refuses it) and before
# the next hop (a record with no link array)
_NOT_MISSING = "[WHERE $this != NONE AND $this != NULL]"
# `array::max` / `array::min` order any comparable kind (a string, a datetime); `math::sum`
# takes numbers (the generator asks a sum over numeric columns only)
_AGG = {"max": "array::max", "min": "array::min", "sum": "math::sum"}

assumes("match.parallel_edges.rows", GRAPH, 2)
assumes("aggregate.max.empty_input", SURQL, EmptyAggregate.NULL)
assumes("subquery.parent.bound_in_closure", SUBQUERY, True)
assumes("subquery.parent.bound_in_bracket_filter", SUBQUERY, False)


def _br(p, schema=None) -> str:
    return "" if p is None else f"[WHERE {render_pred(p, schema=schema)}]"


def _filter_records(records: str, p, schema=None) -> str:
    """The records of `records` (an array of record links) satisfying `p`: a closure,
    whose parameter dereferences the link (a bracket filter over record ids reads no
    field, probed)."""
    return records if p is None else f"array::filter({records}, |$r| {render_pred(p, '$r.', schema=schema)})"


def _where(*conds) -> str:
    conds = [c for c in conds if c]
    return f" WHERE {' AND '.join(conds)}" if conds else ""


def _sp(q: TraversalQuery, schema: Schema, prefix: str = "") -> str:
    p = q.source_predicate
    return "" if p is None else render_pred(p, prefix, schema=schema)


def _cols(extra: tuple, prefix: str = "") -> str:
    """The extra source columns a pulled-up inner query projects."""
    return "".join(f", {prefix}{f} AS {f}" if prefix else f", {f}" for f in extra)


def _tables(schema: Schema, table: str) -> str:
    if schema.is_base(table):
        return ", ".join(nt.name for nt in schema.node_types)
    return table


def _target_type(schema: Schema, hop: Hop) -> str:
    et = schema.edge_type(hop.edge_type)
    return et.to_type if hop.direction != "in" else et.from_type


# --- applicability ----------------------------------------------------------------------------


def applicable(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    n = len(q.hops)
    first, last = q.hops[0], q.hops[-1]
    proj = q.projection
    both = q.has_both
    if q.edge_projection and last.target_predicate is not None:
        return False
    if both and n != 1:
        return False
    if proj in REACH_PROJECTIONS:
        if n != 1 or first.direction != "out" or first.predicate is not None:
            return False
        if data_model == "graph":
            return syntax in GRAPH_REACH_SYNTAXES
        if data_model == "doc":
            return syntax == "links"
        return syntax == "subquery"
    if data_model == "graph":
        if syntax == "arrow":
            return True
        if syntax == "edgescan":
            return n == 1 and not both and proj in _MULTI
        if syntax == "subquery":
            return not both
        return False
    if data_model == "doc":
        # an incoming hop is a subquery over the sources: the first hop only
        if any(h.direction == "in" for h in q.hops[1:]):
            return False
        return syntax in ("links", "closure")
    if data_model == "rel":
        if syntax == "subquery":
            return True
        if syntax == "flat":
            return n == 1 and not both and proj in _MULTI
    return False


def can_pull_up(q: TraversalQuery) -> bool:
    """A source predicate is lifted over a projection of the fields it reads (an
    omitted field projects as an omitted field: every test survives)."""
    return q.source_predicate is not None


def syntaxes_for(q: TraversalQuery, schema: Schema, data_model: str) -> list:
    return [s for s in SYNTAXES[data_model] if applicable(q, schema, data_model, s)]


# --- projections ----------------------------------------------------------------------------


def _projected(records: str, links: "str | None", q: TraversalQuery) -> str:
    """The projection text over the final records (an array of record ids) or, for an
    edge projection, the final links (an array of relationships / link entries)."""
    proj = q.projection
    kind = proj[1:] if proj in EDGE_PROJECTIONS else proj
    expr = links if proj in EDGE_PROJECTIONS else records
    if kind == "ids":
        return expr
    if kind == "distinct_ids":
        return f"array::distinct({expr})"
    if kind == "count":
        return f"array::len({expr})"
    if kind == "distinct_count":
        return f"array::len(array::distinct({expr}))"
    if kind == "field":
        return f"{expr}.{q.field}"
    if kind in AGG_PROJECTIONS:
        return f"{_AGG[kind]}(({expr}.{q.field}){_NOT_MISSING})"
    raise ValueError(proj)


def _set_filter(records: str, q: TraversalQuery) -> str:
    return f"array::len({records}) > 0" if q.projection == "exists" else f"array::len({records}) = 0"


def _select(q: TraversalQuery, schema: Schema, records: str, links: "str | None", extra: tuple, where: str = "", src: "str | None" = None) -> str:
    """`SELECT id[, extra], <projection> AS v FROM <source> WHERE <source predicate>`, or
    the bare id set for `exists` / `nexists`."""
    src = src or _tables(schema, q.source_table)
    sp = _sp(q, schema)
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src}{_where(where, sp, _set_filter(records, q))}"
    return f"SELECT id{_cols(extra)}, {_projected(records, links, q)} AS v FROM {src}{_where(where, sp)}"


# --- graph ----------------------------------------------------------------------------


def _arrow_hop(hop: Hop, schema: Schema, edge_final: bool) -> str:
    et = hop.edge_type
    ep, tp = hop.edge_predicate, hop.target_predicate
    tgt = _target_type(schema, hop)
    a = "->" if hop.direction == "out" else "<-"
    text = f"{a}{et}{_br(ep, schema)}"
    if edge_final:
        return text
    return f"{text}{a}{tgt}{_br(tp, schema)}"


def _arrows(q: TraversalQuery, schema: Schema, hops: tuple, edge_final: bool = False) -> str:
    n = len(hops)
    if hops[0].direction == "both":
        # `<->e<->n` answers BOTH endpoints of every edge (the start included, probed):
        # the undirected hop is the outgoing and the incoming arrows together
        out = _arrow_hop(dataclasses.replace(hops[0], direction="out"), schema, edge_final)
        inc = _arrow_hop(dataclasses.replace(hops[0], direction="in"), schema, edge_final)
        return f"array::concat({out}, {inc})"
    return "".join(_arrow_hop(h, schema, edge_final and k == n - 1) for k, h in enumerate(hops))


def _graph_arrow(q: TraversalQuery, schema: Schema, extra: tuple) -> str:
    records = _arrows(q, schema, q.hops)
    links = _arrows(q, schema, q.hops, edge_final=True) if q.edge_projection else None
    return _select(q, schema, records, links, extra)


def _edge_rows(q: TraversalQuery, schema: Schema, extra: tuple, near: str, far: str) -> str:
    """One hop as rows of the edge / junction table: `SELECT near AS id, far AS v`."""
    h = q.hops[0]
    conds = []
    if h.edge_predicate is not None:
        conds.append(render_pred(h.edge_predicate, schema=schema))
    if h.target_predicate is not None:
        conds.append(render_pred(h.target_predicate, f"{far}.", schema=schema))
    if q.source_predicate is not None:
        conds.append(_sp(q, schema, f"{near}."))
    value = q.field if q.edge_projection else f"{far}.{q.field}" if q.projection in FIELD_PROJECTIONS else far
    return f"SELECT {near} AS id{_cols(extra, f'{near}.')}, {value} AS v FROM {h.edge_type}{_where(*conds)}"


def _graph_edgescan(q: TraversalQuery, schema: Schema, extra: tuple) -> str:
    out = q.hops[0].direction == "out"
    return _edge_rows(q, schema, extra, "in" if out else "out", "out" if out else "in")


def _junction_sub(q: TraversalQuery, schema: Schema, k: int, ref: str, near_out: str, far_out: str) -> str:
    """The correlated subquery answering hops `k..` from the record whose endpoint
    column `ref` (`id` at the outer level) is the current one: an array of the final
    records (or of the final relationships' attribute / the final records' field),
    flattened per level."""
    hop = q.hops[k]
    out = hop.direction == "out"
    near, far = (near_out, far_out) if out else (far_out, near_out)
    where = [f"{near} = $parent.{ref}"]
    if hop.edge_predicate is not None:
        where.append(render_pred(hop.edge_predicate, schema=schema))
    if hop.target_predicate is not None:
        where.append(render_pred(hop.target_predicate, f"{far}.", schema=schema))
    last = k == len(q.hops) - 1
    if not last:
        inner = _junction_sub(q, schema, k + 1, far, near_out, far_out)
        return f"array::flatten((SELECT VALUE {inner} FROM {hop.edge_type}{_where(*where)}))"
    if q.edge_projection:
        value = q.field
    elif q.projection in FIELD_PROJECTIONS:
        value = f"{far}.{q.field}"
    else:
        value = far
    return f"(SELECT VALUE {value} FROM {hop.edge_type}{_where(*where)})"


def _subquery_form(q: TraversalQuery, schema: Schema, extra: tuple, near: str, far: str) -> str:
    if q.hops[0].direction == "both":
        a = _junction_sub(dataclasses.replace(q, hops=(dataclasses.replace(q.hops[0], direction="out"),)), schema, 0, "id", near, far)
        b = _junction_sub(dataclasses.replace(q, hops=(dataclasses.replace(q.hops[0], direction="in"),)), schema, 0, "id", near, far)
        coll = f"array::concat({a}, {b})"
    else:
        coll = _junction_sub(q, schema, 0, "id", near, far)
    return _select(q, schema, coll, coll, extra) if not (q.edge_projection or q.projection in FIELD_PROJECTIONS) else _select_flat(q, schema, coll, extra)


def _select_flat(q: TraversalQuery, schema: Schema, values: str, extra: tuple) -> str:
    """The projection over a FLAT collection of the projected values themselves (the
    subquery already read the field)."""
    proj = q.projection
    kind = proj[1:] if proj in EDGE_PROJECTIONS else proj
    if kind == "field":
        text = values
    elif kind in AGG_PROJECTIONS:
        text = f"{_AGG[kind]}(({values}){_NOT_MISSING})"
    else:
        raise ValueError(proj)
    return f"SELECT id{_cols(extra)}, {text} AS v FROM {_tables(schema, q.source_table)}{_where(_sp(q, schema))}"


def _graph_subquery(q: TraversalQuery, schema: Schema, extra: tuple) -> str:
    return _subquery_form(q, schema, extra, "in", "out")


def _graph_reach(q: TraversalQuery, schema: Schema, extra: tuple, syntax: str) -> str:
    et = q.hops[0].edge_type
    tgt = _target_type(schema, q.hops[0])
    k = q.reach_depth
    if syntax == "recursion":
        coll = f"array::distinct(array::concat([id], @.{{1..{k}+collect}}(->{et}->{tgt}) ?? []))"
    else:
        parts = ["[id]"] + ["".join([f"->{et}->{tgt}"] * d) for d in range(1, k + 1)]
        coll = f"array::distinct(array::flatten([{', '.join(parts)}]))"
    return _reach_select(q, schema, coll, extra)


def _reach_select(q: TraversalQuery, schema: Schema, coll: str, extra: tuple) -> str:
    proj = coll if q.projection in ("reach2", "reach3") else f"array::len({coll})"
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {_tables(schema, q.source_table)}{_where(_sp(q, schema))}"


# --- doc ------------------------------------------------------------------------------------


def _doc_in_hop(q: TraversalQuery, schema: Schema, hop: Hop, closure: bool) -> "tuple[str, str]":
    """(records, links) of an incoming first hop: every source whose list holds a link
    to the current record, once per matching link."""
    et = hop.edge_type
    src = schema.edge_type(et).from_type
    conds = ["$l.target = $parent.id"]
    if hop.edge_predicate is not None:
        conds.append(render_pred(hop.edge_predicate, "$l.", element=True, schema=schema))
    matching = f"array::filter({et} ?? [], |$l| {' AND '.join(conds)})"
    where = _where(render_pred(hop.target_predicate, schema=schema) if hop.target_predicate is not None else "")
    records = f"array::flatten((SELECT VALUE array::repeat(id, array::len({matching})) FROM {src}{where}))"
    links = f"array::flatten((SELECT VALUE {matching} FROM {src}{where}))"
    return records, links


def _doc_out_hop(schema: Schema, hop: Hop, prev: "str | None", closure: bool) -> "tuple[str, str]":
    """(records, links) of an outgoing hop from `prev` (an array of records; None: the
    current record)."""
    et = hop.edge_type
    arr = f"({et} ?? [])" if prev is None else f"array::flatten(({prev}.{et}){_NOT_MISSING})"
    if hop.edge_predicate is None:
        links = arr
    elif closure:
        links = f"array::filter({arr}, |$l| {render_pred(hop.edge_predicate, '$l.', element=True, schema=schema)})"
    else:
        links = f"{arr}{_br(hop.edge_predicate, schema)}"
    records = _filter_records(f"{links}.target", hop.target_predicate, schema)
    return records, links


def _doc_path(q: TraversalQuery, schema: Schema, closure: bool) -> "tuple[str, str]":
    records: "str | None" = None
    links = ""
    for k, hop in enumerate(q.hops):
        if hop.direction == "out":
            records, links = _doc_out_hop(schema, hop, records, closure)
        elif hop.direction == "in":
            records, links = _doc_in_hop(q, schema, hop, closure)
        else:
            r1, l1 = _doc_out_hop(schema, hop, None, closure)
            r2, l2 = _doc_in_hop(q, schema, hop, closure)
            records, links = f"array::concat({r1}, {r2})", f"array::concat({l1}, {l2})"
    return records, links


def _doc(q: TraversalQuery, schema: Schema, extra: tuple, closure: bool = False) -> str:
    if q.projection in REACH_PROJECTIONS:
        hop = q.hops[0]
        parts, prev = ["[id]"], None
        for _ in range(q.reach_depth):
            prev, _l = _doc_out_hop(schema, hop, prev, False)
            parts.append(prev)
        return _reach_select(q, schema, f"array::distinct(array::flatten([{', '.join(parts)}]))", extra)
    records, links = _doc_path(q, schema, closure)
    return _select(q, schema, records, links, extra)


# --- rel ------------------------------------------------------------------------------------


def _rel_subquery(q: TraversalQuery, schema: Schema, extra: tuple) -> str:
    if q.projection in REACH_PROJECTIONS:
        hop = q.hops[0]
        et = hop.edge_type
        parts = ["[id]"]
        for d in range(1, q.reach_depth + 1):
            level = dataclasses.replace(q, hops=tuple(Hop(et, "out") for _ in range(d)), projection="ids")
            parts.append(_junction_sub(level, schema, 0, "id", "src", "dst"))
        return _reach_select(q, schema, f"array::distinct(array::flatten([{', '.join(parts)}]))", extra)
    return _subquery_form(q, schema, extra, "src", "dst")


def _rel_flat(q: TraversalQuery, schema: Schema, extra: tuple) -> str:
    out = q.hops[0].direction == "out"
    return _edge_rows(q, schema, extra, "src" if out else "dst", "dst" if out else "src")


# --- dispatch ---------------------------------------------------------------------------------

_RENDERERS = {
    ("graph", "arrow"): _graph_arrow,
    ("graph", "edgescan"): _graph_edgescan,
    ("graph", "subquery"): _graph_subquery,
    ("doc", "links"): lambda q, s, x: _doc(q, s, x, closure=False),
    ("doc", "closure"): lambda q, s, x: _doc(q, s, x, closure=True),
    ("rel", "subquery"): _rel_subquery,
    ("rel", "flat"): _rel_flat,
}


def render(q: TraversalQuery, schema: Schema, data_model: str, syntax: str, pull_up: bool = False) -> str:
    if data_model == "graph" and q.projection in REACH_PROJECTIONS:
        fn = lambda q_, s_, x_: _graph_reach(q_, s_, x_, syntax)  # noqa: E731
    else:
        try:
            fn = _RENDERERS[(data_model, syntax)]
        except KeyError:
            raise ValueError((data_model, syntax)) from None
    if not pull_up:
        return fn(q, schema, ())
    if not can_pull_up(q):
        raise ValueError("cannot pull up")
    inner = fn(dataclasses.replace(q, source_predicate=None), schema, tuple(fields_of(q.source_predicate)))
    cond = _sp(q, schema)
    if q.projection in SET_PROJECTIONS:
        # the inner query projects every start record with its final records
        inner = fn(dataclasses.replace(q, source_predicate=None, projection="ids"), schema, tuple(fields_of(q.source_predicate)))
        return f"SELECT id FROM ({inner}) WHERE {cond} AND {_set_filter('v', q)}"
    return f"SELECT id, v FROM ({inner}) WHERE {cond}"
