"""The mutation benchmark's catalog for ArangoDB: the core's instance-level mutants and
two corruptions of this provider's own query text (a comparison operator loosened on
the graph data model's first filter; a hop predicate dropped on the doc data model)."""
from __future__ import annotations

import dataclasses
import re

from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant


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


MUTANTS = INSTANCE_MUTANTS + [
    Mutant("graph_wrong_predicate_op", "render", "the graph data model's first comparison operator is loosened/tightened", rewrite_case=lambda case: _rewrite_variants(case, "graph", _flip_op)),
    Mutant("doc_drops_hop_predicate", "render", "the doc data model's first hop predicate always holds", rewrite_case=lambda case: _rewrite_variants(case, "doc", _drop_hop_predicate)),
]

MUTANTS_BY_NAME = {m.name: m for m in MUTANTS}
