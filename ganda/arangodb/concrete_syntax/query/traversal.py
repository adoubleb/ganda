from __future__ import annotations

import dataclasses

from ganda.core.query.ir.predicates import fields_of
from ganda.core.query.ir.traversal import AGG_PROJECTIONS, EDGE_AGG_PROJECTIONS, EDGE_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, Hop, TraversalQuery
from ganda.core.data.model import Schema
from ganda.core.rules.vocabulary import EmptyAggregate, PathMode
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL, TRAVERSAL
from ganda.arangodb.concrete_syntax.query.predicate import render_predicate as render_pred

# the syntaxes of one data model are twins the oracle compares
assumes("equivalent.edge_scan", TRAVERSAL, True)
assumes("equivalent.deep_traversal", TRAVERSAL, True)
assumes("equivalent.both_as_union", TRAVERSAL, True)
assumes("equivalent.reach_bfs_global", TRAVERSAL, True)
assumes("equivalent.link_array_forms", AQL, True)
assumes("equivalent.junction_forms", AQL, True)
assumes("equivalent.flat_collect", AQL, True)
# one row per edge on every path: a parallel pair twice, a self-loop once per direction,
# twice under `ANY` -- as `UNION` of the two legs, which keeps duplicates
assumes("match.parallel_edges.rows", AQL, 2)
assumes("match.parallel_edges.rows", TRAVERSAL, 2)
assumes("match.undirected.self_loop.rows", AQL, 2)
assumes("match.undirected.self_loop.rows", TRAVERSAL, 2)
# an aggregate over no path is null, over paths the present values' (the filter `!= null`)
assumes("aggregate.max.empty_input", AQL, EmptyAggregate.NULL)
assumes("aggregate.max_min.skips_null", AQL, True)
# a `k..k` traversal enumerates every WALK of k edges only under `uniqueEdges: 'none'`:
# the operator's default forbids an edge twice on one path (a trail)
# a k..k traversal is a trail by default: `uniqueEdges: 'none'` asks every walk
assumes("path.variable_length.mode", TRAVERSAL, PathMode.TRAIL)
_DEEP_OPTIONS = " OPTIONS {uniqueEdges: 'none'}"

SYNTAXES = {
    "graph": ("trav", "escan", "deep", "flat"),
    "doc": ("links", "join", "inline", "flat"),
    "rel": ("join", "sub", "flat"),
}
# the syntax `flat` and the reach forms walk their hops through
BASE_SYNTAX = {"graph": "trav", "doc": "links", "rel": "join"}
# the projections `flat` may answer: a start record with no path is ABSENT from its rows
FLAT_PROJECTIONS = ("ids", "field", "efield", "max", "min", "emax", "emin")
_DIR = {"out": "OUTBOUND", "in": "INBOUND", "both": "ANY"}
_AGG = {"max": "MAX", "min": "MIN", "sum": "SUM"}


def _pred(var: str, p) -> str:
    return "" if p is None else f" FILTER {render_pred(p, var)}"


def _sp(q: TraversalQuery) -> str:
    return _pred("n", q.source_predicate)


# --- applicability ----------------------------------------------------------------------------


def applicable(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if syntax not in SYNTAXES[data_model]:
        return False
    n = len(q.hops)
    first, last = q.hops[0], q.hops[-1]
    proj = q.projection
    if q.edge_projection and last.target_predicate is not None:
        return False
    if q.has_both and n != 1:
        return False
    if proj in REACH_PROJECTIONS:
        if n != 1 or first.direction != "out" or first.predicate is not None:
            return False
        return syntax == BASE_SYNTAX[data_model]
    if syntax == "flat":
        return proj in FLAT_PROJECTIONS
    if data_model == "graph":
        if syntax == "deep":
            return n >= 2 and not q.has_both and len({h.direction for h in q.hops}) == 1
        return True
    if data_model == "doc":
        if syntax == "inline":
            return n == 1 and first.direction == "out"
        return True
    return True


def can_pull_up(q: TraversalQuery) -> bool:
    """A question with a source predicate can be pulled up: every data model lifts it
    over the same projection (an absent attribute projects as a null alike on each)."""
    return q.source_predicate is not None


def syntaxes_for(q: TraversalQuery, schema: Schema, data_model: str) -> list:
    return [s for s in SYNTAXES[data_model] if applicable(q, schema, data_model, s)]


# --- one hop ----------------------------------------------------------------------------------


def _leg(schema: Schema, et_name: str, out: bool, cur: str, data_model: str, syntax: str, v: str, e: str, inner: str) -> str:
    """The clauses walking ONE edge type in ONE direction from the record bound to
    `cur`, binding the far record to `v` and the relationship to `e` (`inner` a fresh
    name for a nested lookup)."""
    et = schema.edge_type(et_name)
    far = et.to_type if out else et.from_type
    if data_model == "graph":  # escan
        near, farf = ("_from", "_to") if out else ("_to", "_from")
        return f"FOR {e} IN {et.name} FILTER {e}.{near} == {cur}._id LET {v} = DOCUMENT({e}.{farf})"
    if data_model == "doc":
        if syntax == "join":
            if out:
                return f"FOR {e} IN ({cur}.{et.name} || []) FOR {v} IN {far} FILTER {v}._id == {e}.target"
            return f"FOR {v} IN {far} FILTER {cur}._id IN ({v}.{et.name} || [])[*].target FOR {e} IN ({v}.{et.name} || []) FILTER {e}.target == {cur}._id"
        if out:
            return f"FOR {e} IN ({cur}.{et.name} || []) LET {v} = DOCUMENT({e}.target)"
        return f"FOR {v} IN {far} FOR {e} IN ({v}.{et.name} || []) FILTER {e}.target == {cur}._id"
    near, farf = ("src_id", "dst_id") if out else ("dst_id", "src_id")
    if syntax == "sub":
        return f"FOR {e} IN {et.name} FILTER {e}.{near} == {cur}.id LET {v} = FIRST(FOR {inner} IN {far} FILTER {inner}.id == {e}.{farf} RETURN {inner})"
    return f"FOR {e} IN {et.name} FILTER {e}.{near} == {cur}.id FOR {v} IN {far} FILTER {v}.id == {e}.{farf}"


def _hop(schema: Schema, hop: Hop, k: int, cur: str, data_model: str, syntax: str) -> str:
    """Hop `k` from `cur`, binding `v{k}` and `ed{k}`; the `both` direction is the two
    legs concatenated (`UNION` keeps duplicates: a self-loop is walked twice)."""
    v, e = f"v{k}", f"ed{k}"
    p = hop.predicate
    preds = "" if p is None else _pred(e if p.on == "edge" else v, p)
    if data_model == "graph" and syntax == "trav":
        return f"FOR {v}, {e} IN 1..1 {_DIR[hop.direction]} {cur} {hop.edge_type}{preds}"
    if hop.direction != "both":
        return _leg(schema, hop.edge_type, hop.direction == "out", cur, data_model, syntax, v, e, f"m{k}") + preds
    legs = []
    for out, d in ((True, "o"), (False, "i")):
        legs.append(f"({_leg(schema, hop.edge_type, out, cur, data_model, syntax, f'w{k}{d}', f'x{k}{d}', f'm{k}{d}')} RETURN {{v: w{k}{d}, e: x{k}{d}}})")
    return f"FOR pr{k} IN UNION({', '.join(legs)}) LET {v} = pr{k}.v LET {e} = pr{k}.e{preds}"


def _deep(q: TraversalQuery, schema: Schema) -> "tuple[str, str, str]":
    n = len(q.hops)
    colls = list(dict.fromkeys(h.edge_type for h in q.hops))
    conds = []
    for k, h in enumerate(q.hops):
        if len(colls) > 1:
            conds.append(f" FILTER IS_SAME_COLLECTION('{h.edge_type}', dp.edges[{k}]) == true")
        p = h.predicate
        if p is not None:
            conds.append(_pred(f"dp.edges[{k}]" if p.on == "edge" else f"dp.vertices[{k + 1}]", p))
    body = f"FOR dv, de, dp IN {n}..{n} {_DIR[q.hops[0].direction]} n {', '.join(colls)}{_DEEP_OPTIONS}{''.join(conds)}"
    return body, "dv", "de"


def chain(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> "tuple[str, str, str]":
    """(hop clauses, final record variable, last relationship variable)."""
    if syntax == "deep":
        return _deep(q, schema)
    clauses, cur = [], "n"
    for k, hop in enumerate(q.hops, 1):
        clauses.append(_hop(schema, hop, k, cur, data_model, syntax))
        cur = f"v{k}"
    return " ".join(clauses), cur, f"ed{len(q.hops)}"


# --- projections ------------------------------------------------------------------------------


def project(q: TraversalQuery, body: str, last: str, edge: str) -> str:
    """The per-start expression over a hop chain `body` whose final record is `last`
    and last relationship `edge`."""
    proj = q.projection
    if proj == "ids":
        return f"({body} RETURN {last}.id)"
    if proj == "distinct_ids":
        return f"UNIQUE({body} RETURN {last}.id)"
    if proj == "count":
        return f"LENGTH({body} RETURN 1)"
    if proj == "distinct_count":
        return f"LENGTH(UNIQUE({body} RETURN {last}.id))"
    if proj == "field":
        return f"({body} RETURN {last}.{q.field})"
    if proj == "efield":
        return f"({body} RETURN {edge}.{q.field})"
    if proj in EDGE_AGG_PROJECTIONS:
        return f"{_AGG[proj[1:]]}({body} FILTER {edge}.{q.field} != null RETURN {edge}.{q.field})"
    if proj in AGG_PROJECTIONS:
        return f"{_AGG[proj]}({body} FILTER {last}.{q.field} != null RETURN {last}.{q.field})"
    raise ValueError(proj)


def _cols(extra: tuple, var: str = "n") -> str:
    return "".join(f", {f}: {var}.{f}" for f in extra)


def _per_node(q: TraversalQuery, body: str, last: str, edge: str, extra: tuple, lets: str = "") -> str:
    src = q.source_table
    if q.projection in SET_PROJECTIONS:
        test = "> 0" if q.projection == "exists" else "== 0"
        return f"FOR n IN {src}{_sp(q)}{lets} FILTER LENGTH({body} LIMIT 1 RETURN 1) {test} RETURN {{id: n.id{_cols(extra)}}}"
    return f"FOR n IN {src}{_sp(q)}{lets} RETURN {{id: n.id, v: {project(q, body, last, edge)}{_cols(extra)}}}"


# --- whole-question forms ---------------------------------------------------------------------


def _inline(q: TraversalQuery, extra: tuple) -> str:
    hop = q.hops[0]
    arr = f"(n.{hop.edge_type} || [])"
    p = hop.predicate
    filt = "" if p is None else _pred("CURRENT" if p.on == "edge" else "DOCUMENT(CURRENT.target)", p)
    proj = q.projection
    ids = f"{arr}[*{filt} RETURN DOCUMENT(CURRENT.target).id]"
    if proj in SET_PROJECTIONS:
        test = "> 0" if proj == "exists" else "== 0"
        return f"FOR n IN {q.source_table}{_sp(q)} FILTER LENGTH({arr}[*{filt}]) {test} RETURN {{id: n.id{_cols(extra)}}}"
    if proj == "ids":
        expr = ids
    elif proj == "distinct_ids":
        expr = f"UNIQUE({ids})"
    elif proj == "count":
        expr = f"LENGTH({arr}[*{filt}])"
    elif proj == "distinct_count":
        expr = f"LENGTH(UNIQUE({ids}))"
    elif proj == "field":
        expr = f"{arr}[*{filt} RETURN DOCUMENT(CURRENT.target).{q.field}]"
    elif proj == "efield":
        expr = f"{arr}[*{filt}].{q.field}"
    elif proj in EDGE_AGG_PROJECTIONS:
        expr = f"{_AGG[proj[1:]]}({arr}[*{filt}].{q.field})"
    else:
        expr = f"{_AGG[proj]}({arr}[*{filt} RETURN DOCUMENT(CURRENT.target).{q.field}])"
    return f"FOR n IN {q.source_table}{_sp(q)} RETURN {{id: n.id, v: {expr}{_cols(extra)}}}"


def _flat(q: TraversalQuery, schema: Schema, data_model: str, extra: tuple) -> str:
    body, last, edge = chain(q, schema, data_model, BASE_SYNTAX[data_model])
    proj = q.projection
    if proj == "ids":
        value = f"{last}.id"
    elif proj in EDGE_PROJECTIONS:
        value = f"{edge}.{q.field}"
    else:
        value = f"{last}.{q.field}"
    keys = "fid = n.id" + "".join(f", x{i} = n.{f}" for i, f in enumerate(extra))
    cols = "".join(f", {f}: x{i}" for i, f in enumerate(extra))
    head = f"FOR n IN {q.source_table}{_sp(q)} {body}"
    if proj in AGG_PROJECTIONS or proj in EDGE_AGG_PROJECTIONS:
        fn = _AGG[proj[1:]] if proj in EDGE_AGG_PROJECTIONS else _AGG[proj]
        return f"{head} COLLECT {keys} AGGREGATE fa = {fn}({value}) RETURN {{id: fid, v: fa{cols}}}"
    return f"{head} COLLECT {keys} INTO fg = {value} RETURN {{id: fid, v: fg{cols}}}"


def _reach(q: TraversalQuery, schema: Schema, data_model: str, extra: tuple) -> str:
    et = schema.edge_type(q.hops[0].edge_type)
    k = q.reach_depth
    count = q.projection.endswith("count")
    if data_model == "graph":
        coll = f"(FOR rv IN 0..{k} OUTBOUND n {et.name} OPTIONS {{uniqueVertices: 'global', order: 'bfs'}} RETURN rv.id)"
        lets = ""
    else:
        lets, prev = [], None
        for d in range(1, k + 1):
            cur = f"rl{d}"
            if data_model == "doc":
                src = "[n._id]" if prev is None else prev
                lets.append(f" LET {cur} = UNIQUE(FOR x{d} IN {src} FOR s{d} IN {et.from_type} FILTER s{d}._id == x{d} FOR l{d} IN (s{d}.{et.name} || []) RETURN l{d}.target)")
            else:
                src = "[n.id]" if prev is None else prev
                lets.append(f" LET {cur} = UNIQUE(FOR x{d} IN {src} FOR j{d} IN {et.name} FILTER j{d}.src_id == x{d} RETURN j{d}.dst_id)")
            prev = cur
        levels = ", ".join(f"rl{d}" for d in range(1, k + 1))
        if data_model == "doc":
            coll = f"(FOR y IN UNIQUE(UNION([n._id], {levels})) RETURN DOCUMENT(y).id)"
        else:
            coll = f"UNIQUE(UNION([n.id], {levels}))"
        lets = "".join(lets)
    expr = f"LENGTH({coll})" if count else coll
    return f"FOR n IN {q.source_table}{_sp(q)}{lets} RETURN {{id: n.id, v: {expr}{_cols(extra)}}}"


# --- dispatch ---------------------------------------------------------------------------------


def _render(q: TraversalQuery, schema: Schema, data_model: str, syntax: str, extra: tuple) -> str:
    if q.projection in REACH_PROJECTIONS:
        return _reach(q, schema, data_model, extra)
    if syntax == "flat":
        return _flat(q, schema, data_model, extra)
    if syntax == "inline":
        return _inline(q, extra)
    body, last, edge = chain(q, schema, data_model, syntax)
    return _per_node(q, body, last, edge, extra)


def render(q: TraversalQuery, schema: Schema, data_model: str, syntax: str, pull_up: bool = False) -> str:
    if not applicable(q, schema, data_model, syntax):
        raise ValueError((data_model, syntax))
    if not pull_up:
        return _render(q, schema, data_model, syntax, ())
    if not can_pull_up(q):
        raise ValueError("cannot pull up")
    inner = _render(dataclasses.replace(q, source_predicate=None), schema, data_model, syntax, tuple(fields_of(q.source_predicate)))
    cols = "id: hr.id" if q.projection in SET_PROJECTIONS else "id: hr.id, v: hr.v"
    return f"FOR hr IN ({inner}) FILTER {render_pred(q.source_predicate, 'hr')} RETURN {{{cols}}}"

