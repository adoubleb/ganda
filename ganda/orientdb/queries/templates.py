"""Case assembly: every case a (seed, config) produces for one instance.

Four sources, each a registry entry so the minimizer, the reporter and the mutation
benchmark see one list:

- `ir_traversal` (`ir_generator`): the grammar-sampled traversal questions, each
  rendered through every applicable spelling of every side -- the systematic supply of
  cross-model pairs, with a shape denominator;
- `ir_select` (`select_generator`): the grammar-sampled single-class questions (a
  filter and a result shape: ids, a count, a projection, an aggregate, a grouping,
  distinct values, an ordering), each through every applicable spelling of the
  filter, the target and the form -- the index-vs-scan oracle as a product;
- `ir_path` (`path_ir`): the hop count of a shortest path between two records,
  through the graph's path functions, the doc side's TRAVERSE over its link lists and
  the relational side's junction levels;
- `llm_card` (`fuzzer/cards`): LLM-written template cards from a frozen corpus, each
  spelled 1-3 ways per model, with no Python reference (the pairs are the oracle).

`reference_for` routes a case to its Python reference.
"""
from __future__ import annotations

from ganda.orientdb.model import Instance, Schema
from ganda.orientdb.queries.base import QueryCase
from ganda.orientdb.queries.ir import SET_PROJECTIONS, evaluate, from_params
from ganda.orientdb.queries.ir_generator import CASES_PER_INSTANCE, ir_traversal_cases
from ganda.orientdb.queries.path_ir import evaluate as evaluate_path
from ganda.orientdb.queries.path_ir import from_params as path_from_params
from ganda.orientdb.queries.path_ir import ir_path_cases
from ganda.orientdb.queries.select_generator import ir_select_cases
from ganda.orientdb.queries.select_ir import evaluate as evaluate_select
from ganda.orientdb.queries.select_ir import from_params as select_from_params


def all_cases(rng, schema: Schema, instance: Instance, ir_cases: int = CASES_PER_INSTANCE, select_cases: int = 0, path_cases: int = 0, card_cases: int = 0, card_corpus: str = "") -> list:
    cases: list = []
    if ir_cases:
        sub = rng.for_template("ir_traversal")
        cases.extend(ir_traversal_cases(sub, schema, instance, per_instance=ir_cases))
    if select_cases:
        sub = rng.for_template("ir_select")
        cases.extend(ir_select_cases(sub, schema, instance, per_instance=select_cases))
    if path_cases:
        sub = rng.for_template("ir_path")
        cases.extend(ir_path_cases(sub, schema, instance, per_instance=path_cases))
    if card_cases and card_corpus:
        from ganda.orientdb.cards import card_cases as _card_cases

        # each card draws from its own `llm_card/<id>` stream: adding the supply
        # moves no other case
        cases.extend(_card_cases(rng, schema, instance, card_cases, card_corpus))
    return cases


# `ir_write` is not a read family: `ganda.orientdb.writes` runs it after every read case
TEMPLATE_NAMES = ["ir_traversal", "ir_select", "ir_path", "ir_write", "llm_card"]


def reference_for(case: QueryCase, instance: Instance):
    """(has_reference, expected). Row-keyed comparisons return {record id: value};
    bare-value comparisons return the row list the query would produce."""
    if case.params.get("reference") is False:
        return False, None
    if case.template_name == "ir_traversal":
        q = from_params(case.params["ir"])
        value = evaluate(q, instance)
        if q.projection in SET_PROJECTIONS:
            return True, [{"id": i} for i in value]
        return True, value
    if case.template_name == "ir_write":
        from ganda.orientdb.queries.write_ir import answer
        from ganda.orientdb.queries.write_ir import from_params as write_from_params

        # the rows the statement returns over the Instance BEFORE it (the phase
        # passes that one to the oracle)
        return True, answer(write_from_params(case.params["write"]), instance)
    if case.template_name == "ir_select":
        return True, evaluate_select(select_from_params(case.params["select"]), instance)
    if case.template_name == "ir_path":
        return True, evaluate_path(path_from_params(case.params["path"]), instance)
    return False, None
