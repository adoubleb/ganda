"""The concrete syntax catalog: HOW each data model -- and each of that data model's
syntaxes -- asks a `TraversalQuery` in AQL. One abstract question is rendered through
every engine path the storage model offers, and each rendering is a variant the oracle
compares against every other one.

    data model  syntax   the engine path
    graph  trav     nested depth-1 traversals, `FOR v, e IN 1..1 OUTBOUND n e1` (ANY
                    for the `both` direction)
    graph  escan    the edge collection scanned through its edge index (`e._from ==
                    n._id`), the far endpoint read with `DOCUMENT()`
    graph  deep     ONE `k..k` traversal under `uniqueEdges: 'none'`, the per-hop
                    predicates and edge types spelled over the PATH variable
    doc    links    the embedded link array, the target through `DOCUMENT(l.target)`;
                    an incoming hop scans the sources whose array holds the record
    doc    join     the link array JOINED to the target collection on `_id` (the
                    primary index); an incoming hop pre-filters the sources by array
                    membership (`n._id IN s.e1[*].target`, the array index's shape)
    doc    inline   the inline array operators, `(n.e1 || [])[* FILTER ... RETURN ...]`
    rel    join     the junction by its logical FK (`j.src_id == n.id`), the far
                    record joined on `v.id == j.dst_id` (the `id` index)
    rel    sub      the same lookup as a correlated subquery, `FIRST(FOR m IN t FILTER
                    m.id == j.dst_id RETURN m)`
    every  flat     one row per PATH from a top-level join, folded by `COLLECT id =
                    n.id INTO` / `AGGREGATE` (a start record with no path is absent)

Every syntax also has a PULLED-UP form (`render(..., pull_up=True)`, the
`<data model>:pull_up` variant): the source predicate lifted into an enclosing `FOR hr
IN (<syntax>) FILTER <predicate over hr>`, the inner query projecting the source
fields it needs -- the same question through a plan whose source scan is never
index-served. A definedness test does not survive a projection (an absent attribute
projects as null), so such a source predicate is not pulled up.

The variable-depth (reach) question is one outgoing hop walked k times: a `0..k`
traversal under `uniqueVertices: 'global'` on the graph data model, the frontier
levels unioned on the other two.

Facts (probed by the legacy port on 3.12, re-checked by `ganda arangodb probe`): an
edge scan yields the rows of a depth-1 traversal (a parallel pair twice, a self-loop
once per direction), so the `both` hop is the two legs concatenated by `UNION`, which
keeps duplicates; a `k..k` traversal under `uniqueEdges: 'none'` enumerates every walk
of k edges; `DOCUMENT(l.target)` and a join on `_id` read the same document; `COLLECT
... INTO g = <expr>` keeps every row of the group, `AGGREGATE MAX / MIN` skip null; an
inline `arr[* FILTER c RETURN x]` enumerates in array order; every variable of a query
is fresh (`ed<k>`, never `e<k>`: an edge collection is named that).
"""
from __future__ import annotations

import dataclasses

from ganda.core.query.ir.predicates import fields_of, has_op
from ganda.core.query.ir.traversal import AGG_PROJECTIONS, EDGE_AGG_PROJECTIONS, EDGE_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, Hop, TraversalQuery
from ganda.core.data.model import Schema
from ganda.arangodb.concrete_syntax.predicate import render_predicate as render_pred

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
    """A source predicate can be lifted over a projection unless it tests definedness
    (an absent attribute projects as an explicit null)."""
    p = q.source_predicate
    return p is not None and not has_op(p, ("IS PRESENT", "IS ABSENT"))


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
    body = f"FOR dv, de, dp IN {n}..{n} {_DIR[q.hops[0].direction]} n {', '.join(colls)} OPTIONS {{uniqueEdges: 'none'}}{''.join(conds)}"
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


def is_folded(q: TraversalQuery, data_model: str, syntax: str) -> bool:
    """Whether the syntax answers one row PER PATH rather than per start record."""
    return syntax == "flat"
