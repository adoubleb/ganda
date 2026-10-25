"""The mutation benchmark's catalog for ArangoDB: the core's instance-level mutants and
this provider's corruptions of its own query text.

Categories beside the core's: render (a comparison operator loosened on one data model's
first filter; a hop predicate dropped; an id projection deduplicated) and index (an
INDEX-path defect on one data model: the texts whose filter or SORT a persistent index
of that data model may serve are corrupted the way a broken index would answer them --
a strict range bound served inclusively, null keys returned by a lookup, a DESC walk
answered ascending -- while the scan twins of the same question (`NOOPT`, `disableIndex`,
the other data models' identical base text) stay right). Every mutant is a corruption
one data model has and the others lack: the oracle is differential.
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.data.model import Schema
from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant
from ganda.arangodb.engine import tx


def _rewrite_variants(case, data_model: str, fn):
    q = dict(case.queries)
    for s in list(q):
        if s.split(":", 1)[0] == data_model:
            q[s] = fn(q[s])
    return dataclasses.replace(case, queries=q) if q != case.queries else case


def _flip_op(text: str) -> str:
    for a, b in ((" >= @", " > @"), (" <= @", " < @"), (" > @", " >= @"), (" < @", " <= @"), (" != @", " == @")):
        if a in text:
            return text.replace(a, b, 1)
    return text


def _drop_hop_predicate(text: str) -> str:
    return re.sub(r" FILTER ((?:ed|v)1\.\w+ \S+ @\w+)", r" FILTER (true OR \1)", text, count=1)


def _distinct_projection(text: str) -> str:
    return text.replace("RETURN v1.id)", "RETURN DISTINCT v1.id)", 1) if "RETURN DISTINCT" not in text else text


# --- the index path ---------------------------------------------------------------------------


def _index_fields(schema: Schema, table: str, data_model: str) -> list:
    """The fields of `table` a persistent index of `data_model` may serve (every
    persistent index serves an equality, a membership and a range)."""
    return [f.name for f in schema.query_fields(table) if schema.node_indexes(table, f.name, data_model)]


def _index_served(text: str) -> bool:
    """Whether the optimizer may serve the text's filter from an index: not a
    transaction door script."""
    return not tx.is_script(text)


def _index_rewrite(data_model: str, fn):
    """Apply `fn(text, fields)` to every index-servable syntax of `data_model` whose
    target is a node class (not a traversal, a path or an edge-type question)."""

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
            if s.split(":", 1)[0] == data_model and _index_served(q[s]):
                q[s] = fn(q[s], fields)
        return dataclasses.replace(case, queries=q) if q != case.queries else case

    return rewrite


_FLIP_BOUND = {">": ">=", "<": "<=", ">=": ">", "<=": "<"}


def _inclusive_range(text: str, fields: list) -> str:
    """Every range bound on an indexed field with its inclusiveness flipped (a
    `BETWEEN`'s `IN_RANGE` loses its lower bound)."""
    for f in fields:
        text = re.sub(rf"(?<![\w.])n\.{f} (>=|<=|>|<) @", lambda m: f"n.{f} {_FLIP_BOUND[m.group(1)]} @", text)
        text = re.sub(rf"IN_RANGE\(n\.{f}, (@\w+), (@\w+), true, true\)", rf"IN_RANGE(n.{f}, \1, \2, false, true)", text)
    return text


def _null_keys(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"(?<![\w.(])n\.{f} (<=|>=|==|<|>|IN) (@\w+)", rf"(n.{f} \1 \2 OR n.{f} == null)", text)
    return text


def _desc_as_asc(text: str, fields: list) -> str:
    for f in fields:
        text = re.sub(rf"SORT n\.{f} DESC", f"SORT n.{f} ASC", text)
    return text


# the core's instance-level mutants, then ArangoDB's corruptions of its own query text
MUTANTS = INSTANCE_MUTANTS + [
    Mutant("graph_wrong_predicate_op", "render", "the graph data model's first comparison operator is loosened/tightened", rewrite_case=lambda case: _rewrite_variants(case, "graph", _flip_op)),
    Mutant("doc_drops_hop_predicate", "render", "the doc data model's first hop predicate always holds", rewrite_case=lambda case: _rewrite_variants(case, "doc", _drop_hop_predicate)),
    Mutant("doc_distinct_projection", "render", "the doc data model deduplicates its id projection", rewrite_case=lambda case: _rewrite_variants(case, "doc", _distinct_projection)),
    Mutant("graph_index_range_inclusive", "index", "a range bound a persistent index of the graph data model serves flips its inclusiveness", rewrite_case_schema=_index_rewrite("graph", _inclusive_range)),
    Mutant("rel_index_null_keys", "index", "a lookup a persistent index of the relational data model serves also returns the null-keyed records", rewrite_case_schema=_index_rewrite("rel", _null_keys)),
    Mutant("doc_index_desc_walk", "index", "a DESC order the doc data model walks from a persistent index comes back ascending", rewrite_case_schema=_index_rewrite("doc", _desc_as_asc)),
]

MUTANTS_BY_NAME = {m.name: m for m in MUTANTS}
