"""The mutation benchmark's catalog on SAP HANA: the core's instance-level corruptions
re-aimed at this engine's data models (`doc`, `rel`, `rdf`), and render-level ones that
corrupt one data model's text the way a broken engine path would answer.

The core's catalog names the `graph` data model, which this engine does not have: each
corruption aimed there is aimed at `rdf` here (the triple store is this engine's graph).
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant

_RENAME = {"graph": "rdf"}


def _retarget(m: Mutant) -> Mutant:
    targets = {_RENAME.get(dm, dm): fn for dm, fn in m.instance_for_data_model.items()}
    return dataclasses.replace(m, name=re.sub(r"^graph_", "rdf_", m.name), description=m.description.replace("graph data model", "rdf data model"), instance_for_data_model=targets)


def _variants(case, data_model: str, fn):
    queries = dict(case.queries)
    for v in list(queries):
        if v.split(":", 1)[0] == data_model:
            queries[v] = fn(queries[v])
    return dataclasses.replace(case, queries=queries)


def _strict_to_inclusive(text: str) -> str:
    """The first strict comparison of a SQL text served inclusively."""
    for a, b in ((" > :", " >= :"), (" < :", " <= :")):
        if a in text:
            return text.replace(a, b, 1)
    return text


def _sparql_drop_filter(text: str) -> str:
    """The first FILTER of a SPARQL text dropped (an index that answers every record)."""
    return re.sub(r"FILTER \((?:[^()]|\([^()]*\))*\)", "", text, count=1)


def _count_plus_one(text: str) -> str:
    return text.replace("COUNT(*) AS v", "COUNT(*) + 1 AS v", 1)


RENDER_MUTANTS = [
    Mutant("rel_strict_inclusive", "render", "the rel data model serves its first strict range bound inclusively", rewrite_case=lambda c: _variants(c, "rel", _strict_to_inclusive)),
    Mutant("doc_strict_inclusive", "render", "the doc data model serves its first strict range bound inclusively", rewrite_case=lambda c: _variants(c, "doc", _strict_to_inclusive)),
    Mutant("rdf_filter_dropped", "render", "the rdf data model drops its first FILTER", rewrite_case=lambda c: _variants(c, "rdf", _sparql_drop_filter)),
    Mutant("rel_count_off_by_one", "render", "the rel data model's COUNT(*) is one too many", rewrite_case=lambda c: _variants(c, "rel", _count_plus_one)),
]

MUTANTS = [_retarget(m) for m in INSTANCE_MUTANTS] + RENDER_MUTANTS
