"""The PATH AST: the vertex count of a shortest path between two records.

A `PathQuery` asks for the number of vertices on a shortest path from `start` to
`goal` following `direction` over one edge type (or every type), within `depth` hops;
nothing when the goal is out of reach or beyond the bound. The reference is the BFS
distance over the Instance. Each engine renders the question through whatever path
machinery its data models have (`Provider.path_syntax`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.generators.support import bfs_distances
from ganda.core.model import Instance

DIRECTIONS = ("OUT", "IN", "BOTH")
DEPTHS = (1, 2, 3)
REACH = ("same", "yes", "cut", "no")


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
