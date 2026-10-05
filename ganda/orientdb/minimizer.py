"""Config-level shrinking of a failing (seed, config): lower the generator's bounds one
at a time while the same finding key still reproduces. Every axis draws from its own
sub-stream, so shrinking a bound removes rows instead of reshuffling the instance.
"""
from __future__ import annotations

import dataclasses

from ganda.orientdb.generator import GeneratorConfig, build
from ganda.orientdb.materialize import BUILDERS
from ganda.orientdb.oracle import run_case
from ganda.orientdb.quarantine import Quarantine
from ganda.orientdb.queries.templates import all_cases

_SHRINK_FIELDS = ("max_node_types", "max_edge_types", "max_fields_per_type", "max_nodes_per_type", "max_edges_per_type")


def _reproduces(server, seed: int, cfg: GeneratorConfig, key: tuple, ns: str) -> bool:
    from ganda.orientdb.harness import OrientError, db_name

    schema, instance, rng = build(seed, cfg)
    sessions = {}
    try:
        for side in ("doc", "graph", "rel"):
            s = server.fresh_session(db_name(ns, side))
            s.apply_setup(BUILDERS[side](instance))
            sessions[side] = s
        for case in all_cases(rng, schema, instance, ir_cases=cfg.ir_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases, card_cases=cfg.card_cases, card_corpus=cfg.card_corpus):
            if case.template_name != key[0] or case.shape != key[1]:
                continue
            r = run_case(case, sessions, instance)
            if not r.ok and Quarantine.key(r) == key:
                return True
        return False
    except OrientError:
        return False
    finally:
        for side in ("doc", "graph", "rel"):
            server.drop_database(db_name(ns, side))


def minimize(server, seed: int, cfg: GeneratorConfig, key: tuple, ns: str = "min") -> GeneratorConfig:
    """Greedy: for each bound, halve toward its minimum while the key reproduces."""
    current = cfg
    for name in _SHRINK_FIELDS:
        lo = getattr(current, name.replace("max_", "min_"), 1)
        while getattr(current, name) > lo:
            candidate = dataclasses.replace(current, **{name: max(lo, getattr(current, name) // 2)})
            if candidate == current:
                break
            if _reproduces(server, seed, candidate, key, ns):
                current = candidate
            else:
                break
    return current
