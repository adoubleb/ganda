"""The path IR: a shortest path's vertex count through the shortest-path idiom (graph) and frontier levels.
F6: a bounded `{..k+shortest=b}` answers nested paths, so the idiom is asked unbounded and the bound applied to its length."""
from __future__ import annotations

from ganda.core.data.model import Instance, Schema
from ganda.core.query.ir.path import PathQuery
from ganda.surrealdb.engine.dialect import engine_id
from ganda.surrealdb.rules import assumes, works_around
from ganda.surrealdb.rules.catalog import RECURSION

assumes("recursion.shortest.excludes_start", RECURSION, True)
assumes("recursion.shortest.unreachable_is_none", RECURSION, True)
assumes("recursion.shortest.same_record_is_none", RECURSION, True)
works_around("F6")

_ARROW = {"OUT": "->", "IN": "<-", "BOTH": "<->"}


def bind_vars(q: PathQuery) -> dict:
    return {"a": q.start, "b": q.goal, "depth": q.depth}


def _answer(a: str, b: str, levels: list) -> str:
    """The vertex count from the frontiers `levels` (level d: the records d hops away)."""
    chain = " ".join(f"ELSE IF {b} IN {lvl} THEN {d + 2}" for d, lvl in enumerate(levels))
    return f"RETURN IF {a} = {b} THEN 1 {chain} ELSE 0 END;"


def _graph_shortest(q: PathQuery, a: str, b: str) -> str:
    arrow = _ARROW[q.direction]
    et = q.edge_type or "?"
    return f"LET $p = {a}.{{..+shortest={b}}}({arrow}{et}{arrow}?); RETURN IF {a} = {b} THEN 1 ELSE IF $p = NONE OR array::len($p) > {q.depth} THEN 0 ELSE array::len($p) + 1 END;"


def _levels(q: PathQuery, a: str, b: str, step) -> str:
    """`LET $l0 = [a]; LET $l1 = <step($l0)>; ...` then the answer; `step(var)` is the
    expression of the records one hop from the frontier `var`."""
    lets = [f"LET $l0 = [{a}];"]
    for d in range(1, q.depth + 1):
        lets.append(f"LET $l{d} = array::distinct({step(f'$l{d - 1}')});")
    return " ".join(lets) + " " + _answer(a, b, [f"$l{d}" for d in range(1, q.depth + 1)])


def _graph_levels(q: PathQuery, a: str, b: str) -> str:
    arrow = _ARROW[q.direction]
    et = q.edge_type or "?"
    return _levels(q, a, b, lambda var: f"array::flatten((SELECT VALUE {arrow}{et}{arrow}? FROM {var}))")


def _doc_levels(q: PathQuery, schema: Schema, a: str, b: str) -> str:
    lists = [q.edge_type] if q.edge_type else [et.name for et in schema.edge_types]
    parts = ", ".join(f"({et} ?? []).target" for et in lists)
    return _levels(q, a, b, lambda var: f"array::flatten((SELECT VALUE array::flatten([{parts}]) FROM {var}))")


def _rel_levels(q: PathQuery, schema: Schema, a: str, b: str) -> str:
    tables = [q.edge_type] if q.edge_type else [et.name for et in schema.edge_types]

    def step(var: str) -> str:
        parts = []
        for et in tables:
            if q.direction in ("OUT", "BOTH"):
                parts.append(f"(SELECT VALUE dst FROM {et} WHERE src IN {var})")
            if q.direction in ("IN", "BOTH"):
                parts.append(f"(SELECT VALUE src FROM {et} WHERE dst IN {var})")
        return f"array::flatten([{', '.join(parts)}])"

    return _levels(q, a, b, step)


def render_all(q: PathQuery, schema: Schema, instance: Instance) -> "tuple[dict, dict]":
    """(queries, known) for every data model that renders the question."""
    a, b = engine_id(q.start), engine_id(q.goal)
    queries = {
        "graph": _graph_shortest(q, a, b),
        "graph:levels": _graph_levels(q, a, b),
        "rel": _rel_levels(q, schema, a, b),
    }
    if q.direction == "OUT":
        queries["doc"] = _doc_levels(q, schema, a, b)
    return queries, {}
