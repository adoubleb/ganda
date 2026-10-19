"""The PATH IR (`ir_path`): the hop count of a shortest path between two records, as
data, rendered through every engine path each storage model offers.

A `PathQuery` is a start record, a goal record, a direction (`OUT`, `IN`, `BOTH`), an
edge type (None: every type) and a depth bound `k` of 1..3. The answer is the path's
VERTEX count (the hop count plus one, what `shortestPath(...).size()` answers), one
row, or NO row when the goal is unreachable or farther than `k` (every form is
wrapped to answer nothing there: an aggregate over no rows answers no row, probed).

    data model   concrete syntax    the text
    graph  (base)      `shortestPath(start, goal, :dir, 'e' | null, {maxDepth: k})
                       .size()` (O35: finds paths of at most k - 1 hops, withheld
                       where the distance is exactly k)
           traverse    `min($depth) + 1` over `TRAVERSE out('e') ... MAXDEPTH k
                       STRATEGY BREADTH_FIRST WHERE id = :b` (the default DEPTH_FIRST
                       records the depth of first discovery, c100e seeds 1009 / 1087)
           match       a MATCH with `maxDepth: k` and `depthAlias` (a MATCH
                       enumerates PATHS: its bound stays small)
           while       the same MATCH with `while: ($depth < k)`
           unbounded   `shortestPath()` with no bound, `deep` TRAVERSE to the record
                       count -- only where the bound does not cut the answer
    doc    (base)      TRAVERSE over the source records' link lists: `TRAVERSE e,
                       target FROM (...) MAXDEPTH k STRATEGY BREADTH_FIRST` (an
                       embedded link document adds no depth, probed); `OUT` only --
                       the doc data model keeps no reverse links; `deep` as the graph's
           (every type) the traversed fields are every edge type's list
    rel    (base)      the junction LEVELS unioned: level d holds the rows whose
                       source is among level d - 1's targets (nested `IN (SELECT
                       dst_id ...)`), `min(d) + 1` over the levels that reach the
                       goal; LET-bound (`LET $l0 = (...), ..., $c = unionall(...)`)
           inline      the same union inline, the deepest level first
           asc         inline, the levels in ascending order -- a part after the
                       first whose `IN (subquery)` nests another subquery loses its
                       rows (O40), so it is withheld where the distance is 3
                       (rel: one edge type, `OUT` / `IN`; a `BOTH` level would be a
                       union inside an `IN (subquery)`: O26 / O40 territory)

A question only some models can render is `partial`: the pairs run among the data models
present, plus the reference (the BFS distance, `ganda.core.query.generators.support.bfs_distances`).
"""
from __future__ import annotations

from ganda.core.query.ir.path import PathQuery, distance
from ganda.core.data.model import Instance, Schema

_FN = {"OUT": "out", "IN": "in", "BOTH": "both"}


def bind_vars(q: PathQuery) -> dict:
    return {"a": q.start, "b": q.goal, "dir": q.direction, "o": {"maxDepth": q.depth}}


def _start(q: PathQuery) -> str:
    return f"(SELECT FROM {q.start_type} WHERE id = :a)"


def _graph(q: PathQuery, bounded: bool) -> dict:
    fn = _FN[q.direction]
    et_arg = f"'{q.edge_type}'" if q.edge_type else "null"
    et_fn = f"'{q.edge_type}'" if q.edge_type else ""
    end = f"(SELECT FROM {q.goal_type} WHERE id = :b)"
    k = q.depth
    out = {
        "graph": f"SELECT v FROM (SELECT shortestPath({_start(q)}, {end}, :dir, {et_arg}, :o).size() AS v) WHERE v > 0",
        "graph:traverse": f"SELECT min($depth) + 1 AS v FROM (TRAVERSE {fn}({et_fn}) FROM {_start(q)} MAXDEPTH {k} STRATEGY BREADTH_FIRST) WHERE id = :b",
        "graph:match": f"MATCH {{class: {q.start_type}, as: a, where: (id = :a)}}.{fn}({et_fn}){{as: b, maxDepth: {k}, where: (id = :b), depthAlias: dd}} RETURN min(dd) + 1 AS v",
        "graph:while": f"MATCH {{class: {q.start_type}, as: a, where: (id = :a)}}.{fn}({et_fn}){{as: b, while: ($depth < {k}), where: (id = :b), depthAlias: dd}} RETURN min(dd) + 1 AS v",
    }
    return out


def _graph_unbounded(q: PathQuery, n: int) -> dict:
    fn = _FN[q.direction]
    et_arg = f"'{q.edge_type}'" if q.edge_type else "null"
    et_fn = f"'{q.edge_type}'" if q.edge_type else ""
    end = f"(SELECT FROM {q.goal_type} WHERE id = :b)"
    return {
        "graph:unbounded": f"SELECT v FROM (SELECT shortestPath({_start(q)}, {end}, :dir, {et_arg}).size() AS v) WHERE v > 0",
        "graph:deep": f"SELECT min($depth) + 1 AS v FROM (TRAVERSE {fn}({et_fn}) FROM {_start(q)} MAXDEPTH {n} STRATEGY BREADTH_FIRST) WHERE id = :b",
    }


def _doc(q: PathQuery, schema: Schema, depth: int) -> str:
    lists = [q.edge_type] if q.edge_type else [et.name for et in schema.edge_types]
    fields = ", ".join(lists + ["target"])
    return f"SELECT min($depth) + 1 AS v FROM (TRAVERSE {fields} FROM {_start(q)} MAXDEPTH {depth} STRATEGY BREADTH_FIRST) WHERE id = :b"


def _rel_levels(q: PathQuery) -> list:
    """The union's parts: level 0 (the start is the goal), then level d -- a junction
    row from level d - 1's targets reaching the goal -- for d in 1..k."""
    near, far = ("src_id", "dst_id") if q.direction == "OUT" else ("dst_id", "src_id")
    et = q.edge_type

    def targets(d: int) -> str:
        if d == 1:
            return f"SELECT {far} FROM {et} WHERE {near} = :a"
        return f"SELECT {far} FROM {et} WHERE {near} IN ({targets(d - 1)})"

    parts = [f"(SELECT 0 AS d FROM {q.start_type} WHERE id = :a AND id = :b)"]
    for d in range(1, q.depth + 1):
        src = "= :a" if d == 1 else f"IN ({targets(d - 1)})"
        parts.append(f"(SELECT {d} AS d FROM {et} WHERE {near} {src} AND {far} = :b)")
    return parts


def _rel(q: PathQuery) -> dict:
    parts = _rel_levels(q)
    lets = ", ".join(f"$l{d} = {p}" for d, p in enumerate(parts))
    union = ", ".join(f"$l{d}" for d in range(len(parts)))
    return {
        "rel": f"SELECT min(d) + 1 AS v FROM (SELECT expand($c) LET {lets}, $c = unionall({union}))",
        "rel:inline": f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(reversed(parts))})))",
        "rel:asc": f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(parts)})))",
    }


def o40_asc(q: PathQuery, d: "int | None") -> bool:
    """O40 (`issue-drafts/O40-unionall-drops-a-later-part-with-a-nested-subquery.md`):
    inside `expand(unionall(...))`, a part after the first whose `IN (subquery)`
    nests another subquery answers nothing -- the ascending union's level 3 and on,
    which decides the minimum only where the distance is 3 or more (and within the
    bound)."""
    return d is not None and 3 <= d <= q.depth


def render_all(q: PathQuery, schema: Schema, instance: Instance) -> "tuple[dict, dict]":
    """(queries, known) for every data model that renders the question."""
    d = distance(q, instance)
    queries = _graph(q, True)
    unbounded = d is None or d <= q.depth
    n = len(instance.nodes)
    if unbounded:
        queries.update(_graph_unbounded(q, n))
    if q.direction == "OUT":
        queries["doc"] = _doc(q, schema, q.depth)
        if unbounded:
            queries["doc:deep"] = _doc(q, schema, n)
    if q.edge_type is not None and q.direction != "BOTH":
        queries.update(_rel(q))
    known_bugs = {}
    if d == q.depth:
        known_bugs["graph"] = "O35"
    if "rel:asc" in queries and o40_asc(q, d):
        known_bugs["rel:asc"] = "O40"
    return queries, known_bugs



