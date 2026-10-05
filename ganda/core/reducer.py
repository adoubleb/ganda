"""Config-level shrinking of a failing (seed, config): lower the generator's bounds one
at a time while the same finding key still reproduces. Every axis draws from its own
sub-stream, so shrinking a bound removes rows instead of reshuffling the instance.
"""
from __future__ import annotations

import dataclasses

from ganda.core.data_generator import GeneratorConfig, build
from ganda.core.provider import EngineError
from ganda.core.oracle import run_case
from ganda.core.dedup import BugDeduplicator
from ganda.core.generators.registry import all_cases

_SHRINK_FIELDS = ("max_node_types", "max_edge_types", "max_fields_per_type", "max_nodes_per_type", "max_edges_per_type")


def _reproduces(engine, server, seed: int, cfg: GeneratorConfig, key: tuple, ns: str) -> bool:
    schema, instance, rng = build(seed, cfg, engine)
    sessions = {}
    try:
        for data_model in engine.data_models:
            s = server.fresh_session(engine.database_name(ns, data_model))
            s.apply_setup(engine.mappers[data_model](instance))
            sessions[data_model] = s
        for case in all_cases(rng, schema, instance, cfg=cfg):
            if case.generator_name != key[0] or case.shape != key[1]:
                continue
            r = run_case(case, sessions, instance)
            if not r.ok and BugDeduplicator.key(r) == key:
                return True
        return False
    except EngineError:
        return False
    finally:
        for data_model in engine.data_models:
            server.drop_database(engine.database_name(ns, data_model))


def reduce(engine, server, seed: int, cfg: GeneratorConfig, key: tuple, ns: str = "min") -> GeneratorConfig:
    """Greedy: for each bound, halve toward its minimum while the key reproduces."""
    current = cfg
    for name in _SHRINK_FIELDS:
        lo = getattr(current, name.replace("max_", "min_"), 1)
        while getattr(current, name) > lo:
            candidate = dataclasses.replace(current, **{name: max(lo, getattr(current, name) // 2)})
            if candidate == current:
                break
            if _reproduces(engine, server, seed, candidate, key, ns):
                current = candidate
            else:
                break
    return current
