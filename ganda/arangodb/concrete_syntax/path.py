"""The path AST in AQL: the vertex count of a shortest path between two records, through
every path machinery each storage model has.

    data model  variant   the text
    graph  (base)   `SHORTEST_PATH @a TO @b`, its vertex count, cut at the bound
           ksp      the first of `K_SHORTEST_PATHS`
           asp      the first of `ALL_SHORTEST_PATHS`
           bfs      the first hit of a `0..k` traversal under `order: 'bfs',
                    uniqueVertices: 'global'`
           walk     `MIN` over the default (depth-first, path-unique) `0..k` traversal
    doc    (base)   the frontier per depth out of the link arrays (`DOCUMENT()`-free:
                    the source joined on `_id`); an incoming step scans the sources
                    whose array holds a frontier record
           join     the frontier dereferenced through the target collection
    rel    (base)   the frontier per depth out of the junction (logical ids)
           deref    each step joined through the node collection's `id` index

The answer is one row holding the path's VERTEX count (the hop count plus one), or NO
row when the goal is unreachable or farther than the bound (every form is wrapped to
answer nothing there: a `FILTER` on the computed count). Every graph form answers one
vertex for the start itself. The bind variables are the endpoints as engine ids (`a`,
`b`: what the graph functions and the link arrays hold) and as logical ids (`la`, `lb`:
what the junction holds); the session passes each text the names it uses.
"""
from __future__ import annotations

from ganda.core.ast.path import PathQuery
from ganda.core.model import Instance, Schema
from ganda.arangodb.dialect import engine_id

_DIR = {"OUT": "OUTBOUND", "IN": "INBOUND", "BOTH": "ANY"}


def bind_vars(q: PathQuery) -> dict:
    return {"a": engine_id(q.start), "b": engine_id(q.goal), "la": q.start, "lb": q.goal}


def _graph(q: PathQuery, schema: Schema) -> dict:
    d = _DIR[q.direction]
    colls = q.edge_type or ", ".join(et.name for et in schema.edge_types)
    k, kk = q.depth, q.depth + 1
    return {
        "graph": f"LET sp = (FOR v IN {d} SHORTEST_PATH @a TO @b {colls} RETURN 1) FILTER LENGTH(sp) > 0 AND LENGTH(sp) <= {kk} RETURN {{v: LENGTH(sp)}}",
        "graph:ksp": f"FOR p IN {d} K_SHORTEST_PATHS @a TO @b {colls} LIMIT 1 FILTER LENGTH(p.vertices) <= {kk} RETURN {{v: LENGTH(p.vertices)}}",
        "graph:asp": f"FOR p IN {d} ALL_SHORTEST_PATHS @a TO @b {colls} LIMIT 1 FILTER LENGTH(p.vertices) <= {kk} RETURN {{v: LENGTH(p.vertices)}}",
        "graph:bfs": f"FOR v, e, p IN 0..{k} {d} @a {colls} OPTIONS {{order: 'bfs', uniqueVertices: 'global'}} FILTER v._id == @b LIMIT 1 RETURN {{v: LENGTH(p.vertices)}}",
        "graph:walk": f"LET w = MIN(FOR v, e, p IN 0..{k} {d} @a {colls} FILTER v._id == @b RETURN LENGTH(p.vertices)) FILTER w != null RETURN {{v: w}}",
    }


def _leg(et, out: bool, prev: str, tag: str, data_model: str, deref: bool) -> str:
    """The records one step away from frontier `prev` along `et` in one direction."""
    if data_model == "doc":
        if out:
            if deref:
                return f"(FOR x{tag} IN {prev} FOR s{tag} IN {et.from_type} FILTER s{tag}._id == x{tag} FOR l{tag} IN (s{tag}.{et.name} || []) FOR t{tag} IN {et.to_type} FILTER t{tag}._id == l{tag}.target RETURN t{tag}._id)"
            return f"(FOR x{tag} IN {prev} FOR s{tag} IN {et.from_type} FILTER s{tag}._id == x{tag} FOR l{tag} IN (s{tag}.{et.name} || []) RETURN l{tag}.target)"
        return f"(FOR x{tag} IN {prev} FOR s{tag} IN {et.from_type} FILTER x{tag} IN (s{tag}.{et.name} || [])[*].target RETURN s{tag}._id)"
    near, far, far_type = ("src_id", "dst_id", et.to_type) if out else ("dst_id", "src_id", et.from_type)
    if deref:
        return f"(FOR x{tag} IN {prev} FOR j{tag} IN {et.name} FILTER j{tag}.{near} == x{tag} FOR t{tag} IN {far_type} FILTER t{tag}.id == j{tag}.{far} RETURN t{tag}.id)"
    return f"(FOR x{tag} IN {prev} FOR j{tag} IN {et.name} FILTER j{tag}.{near} == x{tag} RETURN j{tag}.{far})"


def _levels(q: PathQuery, schema: Schema, data_model: str, deref: bool = False) -> str:
    ets = [schema.edge_type(q.edge_type)] if q.edge_type else list(schema.edge_types)
    outs = {"OUT": (True,), "IN": (False,), "BOTH": (True, False)}[q.direction]
    start, goal = ("[@a]", "@b") if data_model == "doc" else ("[@la]", "@lb")
    lets = [f"LET l0 = {start}"]
    for d in range(1, q.depth + 1):
        legs = [_leg(et, out, f"l{d - 1}", f"{d}_{i}{'o' if out else 'i'}", data_model, deref) for i, et in enumerate(ets) for out in outs]
        union = legs[0] if len(legs) == 1 else f"UNION({', '.join(legs)})"
        lets.append(f"LET l{d} = UNIQUE({union})")
    dist = "null"
    for d in range(q.depth, -1, -1):
        dist = f"({goal} IN l{d} ? {d + 1} : {dist})"
    return f"{' '.join(lets)} LET d = {dist} FILTER d != null RETURN {{v: d}}"


def render_all(q: PathQuery, schema: Schema, instance: Instance) -> "tuple[dict, dict]":
    """(queries, known bugs) for every data model: every one renders every question."""
    queries = _graph(q, schema)
    queries["doc"] = _levels(q, schema, "doc")
    queries["doc:join"] = _levels(q, schema, "doc", deref=True)
    queries["rel"] = _levels(q, schema, "rel")
    queries["rel:deref"] = _levels(q, schema, "rel", deref=True)
    return queries, {}
