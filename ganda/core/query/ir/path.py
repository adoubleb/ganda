"""The PATH IR: the vertex count of a shortest path between two records.

A `PathQuery` asks for the number of vertices on a shortest path from `start` to `goal`
given some edge types, direction, and a max depth count.

Each engine renders the question through (`Provider.path_syntax`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.query.ir.enums import PathDirection, Reach
from ganda.core.query.generators.support import bfs_distances
from ganda.core.data.model import Instance

DEPTHS = (1, 2, 3)


@dataclass(frozen=True)
class PathQuery:
    start: str
    start_type: str
    goal: str
    goal_type: str
    direction: PathDirection
    edge_type: "str | None"
    depth: int

    def __post_init__(self):
        object.__setattr__(self, "direction", PathDirection(self.direction))

    def to_params(self) -> dict:
        return {"start": self.start, "start_type": self.start_type, "goal": self.goal, "goal_type": self.goal_type, "direction": self.direction, "edge_type": self.edge_type, "depth": self.depth}


def from_params(d: dict) -> PathQuery:
    return PathQuery(d["start"], d["start_type"], d["goal"], d["goal_type"], d["direction"], d.get("edge_type"), d["depth"])


def distance(q: PathQuery, instance: Instance) -> "int | None":
    return bfs_distances(instance, q.start, q.direction, q.edge_type).get(q.goal)


def evaluate(q: PathQuery, instance: Instance) -> list:
    d = distance(q, instance)
    return [{"v": d + 1}] if d is not None and d <= q.depth else []


def reach(q: PathQuery, instance: Instance) -> Reach:
    d = distance(q, instance)
    return Reach.SAME if d == 0 else Reach.NO if d is None else Reach.CUT if d > q.depth else Reach.YES


def coverage_signature(q: PathQuery, instance: Instance) -> str:
    return f"path dir={q.direction} et={'one' if q.edge_type else 'all'} k={q.depth} reach={reach(q, instance)}"


def all_coverage_signatures() -> list:
    return [f"path dir={d} et={e} k={k} reach={r}" for d in PathDirection for e in ("one", "all") for k in DEPTHS for r in Reach]


COVERAGE_DENOMINATOR = len(all_coverage_signatures())
