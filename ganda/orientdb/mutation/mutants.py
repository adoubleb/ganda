"""A fixed catalog of deliberately-broken materializers and renderers.

Each `Mutant` simulates one class of representation-divergence bug by overriding a
single piece of the real pipeline (one side's instance before it is materialized, one
data model's rendered text, or the oracle's view of one side) while leaving everything else
untouched. Running a trial through the mutant and through the real implementation
should disagree -- `ganda.core.mutation.runner` checks that the cases actually notice.

This is the feedback loop for changes to the fuzzer itself: real engine bugs are rare,
so "bugs found per campaign" cannot tell whether a change to the query supply helped.
The catalog is the ground truth the supply is scored against, and every new axis is
accompanied by an A/B run over the same seeds.

Categories: structural (edges dropped/duplicated/reversed), attribute (values
shifted), churn (a churn step skipped so the final state is not the Instance),
render (a predicate operator or a projection changed on one side), setup (the cancelled
junk block committed), index (an INDEX-path defect on one side: the texts whose filter
or ORDER BY an index of that data model may serve are corrupted the way a broken index
would answer them -- a strict range bound served inclusively, null keys returned by a
lookup, a DESC walk answered ascending -- while a scan concrete syntax of the same question
stays right; the select IR's identical base text and its scan / reversed /
arithmetic rewrites are what can see it).
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.data.model import Schema
from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant
from ganda.orientdb.storage.dialect import RANGE_KINDS


def _first_case_variant_query(case, data_model: str, fn):
    q = dict(case.queries)
    for s in list(q):
        if s.split(":", 1)[0] == data_model:
            q[s] = fn(q[s])
    return dataclasses.replace(case, queries=q)


def _flip_op(text: str) -> str:
    for a, b in ((" >= :", " > :"), (" <= :", " < :"), (" > :", " >= :"), (" < :", " <= :"), (" <> :", " = :")):
        if a in text:
            return text.replace(a, b, 1)
    return text


def _wrong_op_graph(case):
    return _first_case_variant_query(case, "graph", _flip_op)


def _distinct_on_doc(case):
    def fn(t):
        return t.replace(".id AS v", ".id.asSet() AS v", 1) if ".id AS v" in t and "asSet" not in t else t

    return _first_case_variant_query(case, "doc", fn)


def _index_fields(schema: Schema, table: str, data_model: str, range_only: bool) -> list:
    """The fields of `table` an index of `data_model` may serve (a range-capable one with
    `range_only`)."""
    out = []
    for f in schema.query_fields(table):
        ixs = schema.node_indexes(table, f.name, data_model)
        if range_only:
            ixs = [ix for ix in ixs if ix.kind in RANGE_KINDS]
        if ixs:
            out.append(f.name)
    return out


def _index_rewrite(data_model: str, range_only: bool, fn):
    """Apply `fn(text, fields)` to every concrete syntax of `data_model` whose target is a node
    class it may serve from an index: not a subquery target (`(SELECT FROM t)`, which
    a scan answers), not a transaction script (its write shares the text), not a
    traversal or an edge-type question."""

    def rewrite(case, schema: Schema):
        table = case.params.get("table")
        if not table or case.generator_name in ("ir_traversal", "ir_path") or (case.params.get("select") or {}).get("edge"):
            return case
        try:
            fields = _index_fields(schema, table, data_model, range_only)
        except KeyError:
            return case
        if not fields:
            return case
        q = dict(case.queries)
        for s in q:
            if s.split(":", 1)[0] == data_model and "(SELECT FROM " not in q[s] and not q[s].startswith("BEGIN;"):
                q[s] = fn(q[s], fields)
        return dataclasses.replace(case, queries=q) if q != case.queries else case

    return rewrite


def _inclusive_range(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"(?<![\w.$\[]){f} (>|<) :", rf"{f} \1= :", text)
    return text


def _null_keys(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"(?<![\w.$\[]){f} (<=|>=|=|<|>|IN) (:\w+)", rf"({f} \1 \2 OR {f} IS NULL)", text)
    return text


def _desc_as_asc(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"ORDER BY {f} DESC", f"ORDER BY {f} ASC", text)
    return text


# the core's instance-level mutants, then OrientDB's corruptions of its own query text
MUTANTS = INSTANCE_MUTANTS + [
    Mutant("graph_wrong_predicate_op", "render", "the graph data model's first comparison operator is loosened/tightened", rewrite_case=_wrong_op_graph),
    Mutant("doc_distinct_projection", "render", "the doc data model deduplicates its id projection", rewrite_case=_distinct_on_doc),
    Mutant("graph_index_range_inclusive", "index", "a strict range an SB-tree index of the graph data model serves includes its bound", rewrite_case_schema=_index_rewrite("graph", True, _inclusive_range)),
    Mutant("rel_index_null_keys", "index", "a lookup an index of the relational data model serves also returns the null-keyed records", rewrite_case_schema=_index_rewrite("rel", False, _null_keys)),
    Mutant("doc_index_desc_walk", "index", "a DESC order the doc data model walks from an SB-tree index comes back ascending", rewrite_case_schema=_index_rewrite("doc", True, _desc_as_asc)),
]

MUTANTS_BY_NAME = {m.name: m for m in MUTANTS}
