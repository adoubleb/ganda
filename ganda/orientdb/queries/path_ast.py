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
present, plus the reference (the BFS distance, `select_support.bfs_distances`).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ganda.core.model import Instance, Schema
from ganda.core.case import Comparison, QueryCase
from ganda.orientdb.queries.select_support import bfs_distances

DIRECTIONS = ("OUT", "IN", "BOTH")
DEPTHS = (1, 2, 3)
REACH = ("same", "yes", "cut", "no")
CASES_PER_INSTANCE = 3
_FN = {"OUT": "out", "IN": "in", "BOTH": "both"}


@dataclass(frozen=True)
class PathQuery:
    start: str
    start_type: str
    goal: str
    goal_type: str
    direction: str
    edge_type: "str | None"
    depth: int

    def to_params(self) -> dict:
        return {"start": self.start, "start_type": self.start_type, "goal": self.goal, "goal_type": self.goal_type, "direction": self.direction, "edge_type": self.edge_type, "depth": self.depth}


def from_params(d: dict) -> PathQuery:
    return PathQuery(d["start"], d["start_type"], d["goal"], d["goal_type"], d["direction"], d.get("edge_type"), d["depth"])


def distance(q: PathQuery, instance: Instance) -> "int | None":
    return bfs_distances(instance, q.start, q.direction, q.edge_type).get(q.goal)


def evaluate(q: PathQuery, instance: Instance) -> list:
    d = distance(q, instance)
    return [{"v": d + 1}] if d is not None and d <= q.depth else []


def reach(q: PathQuery, instance: Instance) -> str:
    d = distance(q, instance)
    return "same" if d == 0 else "no" if d is None else "cut" if d > q.depth else "yes"


def coverage_signature(q: PathQuery, instance: Instance) -> str:
    return f"path dir={q.direction} et={'one' if q.edge_type else 'all'} k={q.depth} reach={reach(q, instance)}"


def all_coverage_signatures() -> list:
    return [f"path dir={d} et={e} k={k} reach={r}" for d in DIRECTIONS for e in ("one", "all") for k in DEPTHS for r in REACH]


COVERAGE_DENOMINATOR = len(all_coverage_signatures())


# --- concrete syntaxes --------------------------------------------------------------------------------


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


# --- sampling --------------------------------------------------------------------------------


def sample_path(rng: random.Random, schema: Schema, instance: Instance) -> "PathQuery | None":
    """A start that has an edge to follow most of the time, a goal among the records
    it reaches most of the time (the start itself now and then)."""
    nodes = list(instance.nodes)
    if not nodes or not schema.edge_types:
        return None
    direction = rng.choice(DIRECTIONS)
    et = rng.choice(schema.edge_types).name if rng.random() < 0.6 else None
    edges = [e for e in instance.edges if et is None or e.type_name == et]
    ends = {e.from_record for e in edges} if direction == "OUT" else {e.to_record for e in edges} if direction == "IN" else {e.from_record for e in edges} | {e.to_record for e in edges}
    starts = [x for x in nodes if x.record_id in ends]
    a = rng.choice(starts) if starts and rng.random() < 0.85 else rng.choice(nodes)
    dists = bfs_distances(instance, a.record_id, direction, et)
    reachable = [x for x in nodes if x.record_id in dists and x.record_id != a.record_id]
    roll = rng.random()
    if roll < 0.1:
        b = a
    elif reachable and roll < 0.75:
        b = rng.choice(reachable)
    else:
        b = rng.choice(nodes)
    return PathQuery(a.record_id, a.type_name, b.record_id, b.type_name, direction, et, rng.choice(DEPTHS))


def build_case(q: PathQuery, schema: Schema, instance: Instance) -> QueryCase:
    queries, known_bugs = render_all(q, schema, instance)
    bind = {"a": q.start, "b": q.goal, "dir": q.direction, "o": {"maxDepth": q.depth}}
    params = {
        "path": q.to_params(),
        "distance": distance(q, instance),
        "shape": f"path dir={q.direction} et={'one' if q.edge_type else 'all'} k={q.depth} reach={reach(q, instance)} sides={'+'.join(s for s in ('doc', 'graph', 'rel') if s in queries)}",
        "coverage": coverage_signature(q, instance),
    }
    if not all(s in queries for s in ("doc", "graph", "rel")):
        params["partial"] = True
    return QueryCase("ir_path", q.edge_type or "", Comparison.SINGLE_VALUE, queries, bind, params, shape=params["shape"], known_bugs=known_bugs)


def generate_path_cases(rng: random.Random, schema: Schema, instance: Instance, per_instance: int = CASES_PER_INSTANCE) -> list:
    cases = []
    for _ in range(per_instance):
        q = sample_path(rng, schema, instance)
        if q is None:
            break
        cases.append(build_case(q, schema, instance))
    return cases
