"""A fixed catalog of deliberately-broken materializers and renderers (the mutation
benchmark's ground truth, `ganda.core.mutation.runner`): the core's instance-level
mutants, then SurrealDB's corruptions of its own query text -- a comparison operator
loosened on one data model, a projection deduplicated, and the index-path mutants (a
strict range an index of one data model serves answered inclusively, a lookup also
answering the missing-valued records, a DESC ordering answered ascending), which only
the texts an index of that data model may serve are given.
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.data.model import Schema
from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant


def _variants_of(case, data_model: str, fn):
    q = dict(case.queries)
    for s in list(q):
        if s.split(":", 1)[0] == data_model:
            q[s] = fn(q[s])
    return dataclasses.replace(case, queries=q)


def _flip_op(text: str) -> str:
    for a, b in ((" >= $", " > $"), (" <= $", " < $"), (" > $", " >= $"), (" < $", " <= $"), (" != $", " = $")):
        if a in text:
            return text.replace(a, b, 1)
    return text


def _wrong_op_graph(case):
    return _variants_of(case, "graph", _flip_op)


def _distinct_on_doc(case):
    def fn(t):
        return re.sub(r"(\(\w+ \?\? \[\])(\[WHERE [^\]]*\])?\.target) AS v", r"array::distinct(\1) AS v", t, count=1)

    return _variants_of(case, "doc", fn)


def _index_fields(schema: Schema, table: str, data_model: str) -> list:
    return [f.name for f in schema.query_fields(table) if schema.node_indexes(table, f.name, data_model)]


def _index_rewrite(data_model: str, fn):
    """Apply `fn(text, fields)` to every concrete syntax of `data_model` whose target is a
    node class an index of it may serve: not a traversal, a path or an edge-type
    question, not a transaction script."""

    def rewrite(case, schema: Schema):
        table = case.params.get("table")
        if not table or case.generator_name in ("ir_traversal", "ir_path") or (case.params.get("select") or {}).get("edge"):
            return case
        try:
            fields = _index_fields(schema, table, data_model)
        except KeyError:
            return case
        if not fields:
            return case
        q = dict(case.queries)
        for s in q:
            if s.split(":", 1)[0] == data_model and not q[s].startswith("BEGIN"):
                q[s] = fn(q[s], fields)
        return dataclasses.replace(case, queries=q) if q != case.queries else case

    return rewrite


def _inclusive_range(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"(?<![\w.$\[]){f} (>|<) \$", rf"{f} \1= $", text)
    return text


def _null_keys(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"(?<![\w.$\[]){f} (<=|>=|=|<|>|IN) (\$\w+)", rf"({f} \1 \2 OR {f} IS NONE)", text)
    return text


def _desc_as_asc(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"ORDER BY {f} DESC", f"ORDER BY {f} ASC", text)
    return text


MUTANTS = INSTANCE_MUTANTS + [
    Mutant("graph_wrong_predicate_op", "render", "the graph data model's first comparison operator is loosened/tightened", rewrite_case=_wrong_op_graph),
    Mutant("doc_distinct_projection", "render", "the doc data model deduplicates its link-array id projection", rewrite_case=_distinct_on_doc),
    Mutant("graph_index_range_inclusive", "index", "a strict range an index of the graph data model serves includes its bound", rewrite_case_schema=_index_rewrite("graph", _inclusive_range)),
    Mutant("rel_index_null_keys", "index", "a lookup an index of the relational data model serves also returns the records with no value", rewrite_case_schema=_index_rewrite("rel", _null_keys)),
    Mutant("doc_index_desc_walk", "index", "a DESC order the doc data model walks from an index comes back ascending", rewrite_case_schema=_index_rewrite("doc", _desc_as_asc)),
]

MUTANTS_BY_NAME = {m.name: m for m in MUTANTS}
