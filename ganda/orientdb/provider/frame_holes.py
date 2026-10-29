"""The `frame_holes` query generator: the frame grid's GATED cells
(`concrete_syntax/query/frames.py`), asked beside the cells every data model already
fills, `frame_hole_cases` per instance (0 by default, so a seed's cases are what they
were). A gated cell fills a frame a data model had no text in -- doc's reach through
TRAVERSE, the level union on graph and doc -- and is asked here until a campaign has
shown it answers alike; it then moves into its IR's own generator.

A case is the IR generator's own case for a question it draws (every data model's
drawn texts, the pull-ups, the known-bug marks) plus a variant `<data model>:<syntax>`
per gated cell the question admits, judged by the same guards. The generator draws from
its own stream, so it moves no other generator's draws.
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.data.model import Instance, Schema
from ganda.core.oracle.case import QueryCase
from ganda.core.query.generators import path as path_generator
from ganda.core.query.generators import traversal as traversal_generator
from ganda.core.query.ir.traversal import REACH_PROJECTIONS
from ganda.orientdb.concrete_syntax.query import path, traversal
from ganda.orientdb.concrete_syntax.query.frames import CELLS
from ganda.orientdb.known_bugs import mark_known_bugs

GENERATOR = "frame_holes"
# questions drawn before giving up on one a gated cell admits (a reach question needs a
# self-typed edge type, a level union one edge type and one direction)
TRIES = 20


def _reach(rng: random.Random, schema: Schema, instance: Instance) -> "QueryCase | None":
    for _ in range(TRIES):
        q = traversal_generator.sample_query(rng, schema, instance)
        if q is None:
            return None
        if q.projection in REACH_PROJECTIONS:
            break
    else:
        return None
    case = traversal_generator.build_case(q, rng, schema, instance)
    if case is None:
        return None
    queries, chosen = dict(case.queries), dict(case.params["concrete_syntax"])
    for data_model in schema.engine.data_models:
        for sp in traversal.gated_syntaxes_for(q, schema, data_model):
            queries[f"{data_model}:{sp}"] = traversal.render(q, schema, data_model, sp)
            chosen[f"{data_model}:{sp}"] = sp
    if len(queries) == len(case.queries):
        return None
    case = dataclasses.replace(case, generator_name=GENERATOR, queries=queries, params={**case.params, "concrete_syntax": chosen})
    return mark_known_bugs(case, q, schema, instance)


def _path(rng: random.Random, schema: Schema, instance: Instance) -> "QueryCase | None":
    for _ in range(TRIES):
        q = path_generator.sample_path(rng, schema, instance)
        if q is None:
            return None
        gated = path.render_gated(q, schema)
        if gated:
            break
    else:
        return None
    case = path_generator.build_case(q, schema, instance)
    return dataclasses.replace(case, generator_name=GENERATOR, queries={**case.queries, **gated})


# a gated cell's frame -> the case builder that asks it
BUILDERS = {"reach_traverse": _reach, "path_levels": _path}


def generate(rng: random.Random, schema: Schema, instance: Instance, cfg) -> list:
    n = cfg.frame_hole_cases
    frames = sorted({c.frame for c in CELLS if c.gated})
    cases: list = []
    for _ in range(n * 4):
        if len(cases) >= n:
            break
        case = BUILDERS[rng.choice(frames)](rng, schema, instance)
        if case is not None:
            cases.append(case)
    return cases
