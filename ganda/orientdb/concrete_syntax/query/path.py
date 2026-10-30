from __future__ import annotations

from ganda.core.query.ir.path import PathQuery, distance
from ganda.core.data.model import Instance, Schema

from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import MATCH, SQL, TRAVERSE
from ganda.core.rules.vocabulary import EmptyInputRows

_FN = {"OUT": "out", "IN": "in", "BOTH": "both"}


def bind_vars(q: PathQuery) -> dict:
    return {"a": q.start, "b": q.goal, "dir": q.direction, "o": {"maxDepth": q.depth}}


def _start(q: PathQuery) -> str:
    return f"(SELECT FROM {q.start_type} WHERE id = :a)"


# shortestPath answers 0 unreachable, 1 for the same record; an aggregate over no row
# answers no row, so every form answers nothing when unreachable; maxDepth is O35
assumes("shortest_path.unreachable.result", SQL, 0)
assumes("shortest_path.same_record.result", SQL, 1)
assumes("aggregate.ungrouped.empty_input.rows", SQL, EmptyInputRows.NO_ROW)
works_around("O35")


def _graph(q: PathQuery) -> dict:
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


# MAXDEPTH k / maxDepth k / `while ($depth < k)` reach k hops, and an embedded link
# document adds no depth to the doc data model's TRAVERSE
assumes("max_depth.reaches_bound", TRAVERSE, True)
assumes("max_depth.reaches_bound", MATCH, True)
assumes("traverse.embedded_document.adds_depth", TRAVERSE, False)


def _doc(q: PathQuery, schema: Schema, depth: int) -> str:
    lists = [q.edge_type] if q.edge_type else [et.name for et in schema.edge_types]
    fields = ", ".join(lists + ["target"])
    return f"SELECT min($depth) + 1 AS v FROM (TRAVERSE {fields} FROM {_start(q)} MAXDEPTH {depth} STRATEGY BREADTH_FIRST) WHERE id = :b"


def _levels(q: PathQuery, rows: str, ends: "tuple[str, str]") -> list:
    """The union's parts: level 0 (the start is the goal), then level d -- a
    relationship row from level d - 1's targets reaching the goal -- for d in 1..k.
    `rows` are the relationship records, `ends` the (source, target) ids of one (the
    path frame's slot: the junction class, the E class, the doc side's unwound links)."""
    near, far = ends if q.direction == "OUT" else ends[::-1]

    def targets(d: int) -> str:
        if d == 1:
            return f"SELECT {far} FROM {rows} WHERE {near} = :a"
        return f"SELECT {far} FROM {rows} WHERE {near} IN ({targets(d - 1)})"

    parts = [f"(SELECT 0 AS d FROM {q.start_type} WHERE id = :a AND id = :b)"]
    for d in range(1, q.depth + 1):
        src = "= :a" if d == 1 else f"IN ({targets(d - 1)})"
        parts.append(f"(SELECT {d} AS d FROM {rows} WHERE {near} {src} AND {far} = :b)")
    return parts


def _let_union(parts: list) -> str:
    lets = ", ".join(f"$l{d} = {p}" for d, p in enumerate(parts))
    union = ", ".join(f"$l{d}" for d in range(len(parts)))
    return f"SELECT min(d) + 1 AS v FROM (SELECT expand($c) LET {lets}, $c = unionall({union}))"


def _rel(q: PathQuery) -> dict:
    parts = _levels(q, q.edge_type, ("src_id", "dst_id"))
    return {
        "rel": _let_union(parts),
        "rel:inline": f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(reversed(parts))})))",
        "rel:asc": f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(parts)})))",
    }


# `x IN (SELECT in.id FROM e ...)` tests x against the dotted link's projected value, and
# the doc side's links unwind one row per link
assumes("in_subquery.dotted_projection", SQL, True)
assumes("unwind.bracket_projection.row_per_element", SQL, True)


def _levels_union(q: PathQuery, schema: Schema) -> dict:
    """The level union on the graph and doc data models, in the LET form (O40 spares
    it): the E class's rows by their ends' ids, the doc side's link lists unwound into
    (source, target) rows."""
    et = schema.edge_type(q.edge_type)
    links = f"(SELECT id AS src_id, {et.name}.target.id AS dst_id FROM {et.from_type} UNWIND dst_id)"
    return {
        "graph:levels": _let_union(_levels(q, et.name, ("out.id", "in.id"))),
        "doc:levels": _let_union(_levels(q, links, ("src_id", "dst_id"))),
    }


works_around("O40")


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
    queries = _graph(q)
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
        queries.update(_levels_union(q, schema))
    known_bugs = {}
    if d == q.depth:
        known_bugs["graph"] = "O35"
    if "rel:asc" in queries and o40_asc(q, d):
        known_bugs["rel:asc"] = "O40"
    return queries, known_bugs



