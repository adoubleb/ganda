"""Case assembly: every case a (seed, config) produces for one instance.

Four sources, each a registry entry so the reducer, the reporter and the mutation
benchmark see one list:

- `ir_traversal` (`traversal_generator`): the grammar-sampled traversal questions, each
  rendered through every applicable concrete syntax of every data model -- the systematic supply of
  cross-model pairs, with a shape denominator;
- `ir_select` (`select_generator`): the grammar-sampled single-class questions (a
  filter and a result shape: ids, a count, a projection, an aggregate, a grouping,
  distinct values, an ordering), each through every applicable concrete syntax of the
  filter, the target and the form -- the index-vs-scan oracle as a product;
- `ir_path` (`path_ast`): the hop count of a shortest path between two records,
  through the graph's path functions, the doc data model's TRAVERSE over its link lists and
  the relational data model's junction levels;
- `llm_card` (`fuzzer/cards`): LLM-written template cards from a frozen corpus, each
  rendered 1-3 ways per model, with no Python reference (the pairs are the oracle).

`reference_for` routes a case to its Python reference.
"""
from __future__ import annotations

from ganda.core.model import Instance, Schema
from ganda.core.case import QueryCase
from ganda.orientdb.queries.traversal_ast import SET_PROJECTIONS, evaluate, from_params
from ganda.orientdb.queries.traversal_generator import CASES_PER_INSTANCE, generate_traversal_cases
from ganda.orientdb.queries.path_ast import evaluate as evaluate_path
from ganda.orientdb.queries.path_ast import from_params as path_from_params
from ganda.orientdb.queries.path_ast import generate_path_cases
from ganda.orientdb.queries.select_generator import generate_select_cases
from ganda.orientdb.queries.select_ast import evaluate as evaluate_select
from ganda.orientdb.queries.select_ast import from_params as select_from_params


def all_cases(rng, schema: Schema, instance: Instance, traversal_cases: int = CASES_PER_INSTANCE, select_cases: int = 0, path_cases: int = 0, card_cases: int = 0, card_corpus: str = "") -> list:
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
    if card_cases and card_corpus:
        from ganda.orientdb.cards import card_cases as _card_cases

        # each card draws from its own `llm_card/<id>` stream: adding the supply
        # moves no other case
        cases.extend(_card_cases(rng, schema, instance, card_cases, card_corpus))
    return cases


# `ir_write` is not a read query generator: `ganda.orientdb.write_phase` runs it after every read case
GENERATOR_NAMES = ["ir_traversal", "ir_select", "ir_path", "ir_write", "llm_card"]


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
        from ganda.orientdb.queries.write_ast import answer
        from ganda.orientdb.queries.write_ast import from_params as write_from_params

        # the rows the statement returns over the Instance BEFORE it (the phase
        # passes that one to the oracle)
        return True, answer(write_from_params(case.params["write"]), instance)
    if case.generator_name == "ir_select":
        return True, evaluate_select(select_from_params(case.params["select"]), instance)
    if case.generator_name == "ir_path":
        return True, evaluate_path(path_from_params(case.params["path"]), instance)
    return False, None
