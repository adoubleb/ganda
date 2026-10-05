"""Case assembly: every case a (seed, config) produces for one instance.

The core's query generators, each on its own random stream so that adding one moves
no other:

- `ir_traversal`: sampled traversal questions, each rendered through every applicable
  concrete syntax of every data model -- the systematic supply of cross-model pairs;
- `ir_select`: sampled single-class questions (a filter and a result shape), each
  through every applicable syntax of the filter, the target and the form -- the
  index-vs-scan oracle as a product;
- `ir_path`: the vertex count of a shortest path between two records;
- `ir_write`: predicate-targeted writes, run as a phase after every read case
  (`write_phase`), not from here.

A provider adds its own through `Provider.extra_generators` (name -> a function of
`(rng, schema, instance, cfg)`), with `Provider.reference_for` for their references.
`reference_for` routes a case to its reference answer.
"""
from __future__ import annotations

from ganda.core.model import Instance, Schema
from ganda.core.case import QueryCase
from ganda.core.ast.traversal import SET_PROJECTIONS, evaluate, from_params
from ganda.core.generators.traversal import CASES_PER_INSTANCE, generate_traversal_cases
from ganda.core.ast.path import evaluate as evaluate_path
from ganda.core.ast.path import from_params as path_from_params
from ganda.core.generators.path import generate_path_cases
from ganda.core.generators.select import generate_select_cases
from ganda.core.ast.select import evaluate as evaluate_select
from ganda.core.ast.select import from_params as select_from_params


def all_cases(rng, schema: Schema, instance: Instance, traversal_cases: int = CASES_PER_INSTANCE, select_cases: int = 0, path_cases: int = 0, cfg=None) -> list:
    """Every read case of an instance. With `cfg` the supply is the configuration's
    (and the provider's extra generators run); the counts are for callers without one."""
    if cfg is not None:
        traversal_cases, select_cases, path_cases = cfg.traversal_cases, cfg.select_cases, cfg.path_cases
    cases: list = []
    if traversal_cases:
        sub = rng.for_generator("ir_traversal")
        cases.extend(generate_traversal_cases(sub, schema, instance, per_instance=traversal_cases))
    if select_cases:
        sub = rng.for_generator("ir_select")
        cases.extend(generate_select_cases(sub, schema, instance, per_instance=select_cases))
    if path_cases:
        sub = rng.for_generator("ir_path")
        cases.extend(generate_path_cases(sub, schema, instance, per_instance=path_cases))
    if cfg is not None:
        for name, generate in schema.engine.extra_generators.items():
            cases.extend(generate(rng.for_generator(name), schema, instance, cfg))
    return cases


# `ir_write` is not a read query generator: `write_phase` runs it after every read case
GENERATOR_NAMES = ["ir_traversal", "ir_select", "ir_path", "ir_write"]


def reference_for(case: QueryCase, instance: Instance):
    """(has_reference, expected). Row-keyed comparisons return {record id: value};
    bare-value comparisons return the row list the query would produce."""
    if case.params.get("reference") is False:
        return False, None
    if case.generator_name == "ir_traversal":
        q = from_params(case.params["traversal"])
        value = evaluate(q, instance)
        if q.projection in SET_PROJECTIONS:
            return True, [{"id": i} for i in value]
        return True, value
    if case.generator_name == "ir_write":
        from ganda.core.ast.write import answer
        from ganda.core.ast.write import from_params as write_from_params

        # the rows the statement returns over the Instance BEFORE it (the phase
        # passes that one to the oracle)
        return True, answer(write_from_params(case.params["write"]), instance)
    if case.generator_name == "ir_select":
        return True, evaluate_select(select_from_params(case.params["select"]), instance)
    if case.generator_name == "ir_path":
        return True, evaluate_path(path_from_params(case.params["path"]), instance)
    engine = instance.schema.engine
    if engine is not None and case.generator_name in engine.extra_generators:
        return engine.reference_for(case, instance)
    return False, None
