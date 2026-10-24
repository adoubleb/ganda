"""The path query generator: a start that has an edge to follow most of the time, a
goal among the records it reaches most of the time, a small depth bound."""
from __future__ import annotations

import random

from ganda.core.query.ir.enums import PathDirection
from ganda.core.query.ir.path import DEPTHS, PathQuery, coverage_signature, distance, reach
from ganda.core.oracle.case import Comparison, QueryCase
from ganda.core.query.generators.support import bfs_distances
from ganda.core.data.model import Instance, Schema

CASES_PER_INSTANCE = 3


def sample_path(rng: random.Random, schema: Schema, instance: Instance) -> "PathQuery | None":
    """A start that has an edge to follow most of the time, a goal among the records
    it reaches most of the time (the start itself now and then)."""
    nodes = list(instance.nodes)
    if not nodes or not schema.edge_types:
        return None
    direction = rng.choice(list(PathDirection))
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
    engine = schema.engine
    queries, known_bugs = engine.path_syntax.render_all(q, schema, instance)
    bind = engine.path_syntax.bind_vars(q)
    params = {
        "path": q.to_params(),
        "distance": distance(q, instance),
        "shape": f"path dir={q.direction} et={'one' if q.edge_type else 'all'} k={q.depth} reach={reach(q, instance)} sides={'+'.join(s for s in engine.data_models if s in queries)}",
        "coverage": coverage_signature(q, instance),
    }
    if not all(s in queries for s in engine.data_models):
        params["partial"] = True
    return QueryCase("ir_path", q.edge_type or "", Comparison.SINGLE_VALUE, queries, bind, params, shape=params["shape"], known_bugs=known_bugs, engine=engine)


def generate_path_cases(rng: random.Random, schema: Schema, instance: Instance, per_instance: int = CASES_PER_INSTANCE) -> list:
    cases = []
    for _ in range(per_instance):
        q = sample_path(rng, schema, instance)
        if q is None:
            break
        cases.append(build_case(q, schema, instance))
    return cases
