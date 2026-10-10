"""The mutation benchmark's catalog on SAP HANA: the core's instance-level corruptions
over this engine's data models (`doc`, `rel`, `graph`, `rdf`), and render-level ones that
corrupt one data model's text the way a broken engine path would answer.

The core's catalog names `doc`, `graph` and `rel`. The graph data model is the Property
Graph Engine here, so a corruption aimed at `graph` stays there; the triple store gets a
copy of each (`rdf_*`), being a graph of its own; a reference-only corruption (every data
model alike, only the reference sees it) covers all four, or it would stop being
oracle-blind.
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant

DATA_MODELS = ("doc", "rel", "graph", "rdf")


def _rdf_copy(m: Mutant) -> Mutant:
    fn = m.instance_for_data_model["graph"]
    return dataclasses.replace(m, name=re.sub(r"^graph_", "rdf_", m.name), description=m.description.replace("graph data model", "rdf data model"), instance_for_data_model={"rdf": fn})


def _every_model(m: Mutant) -> Mutant:
    fn = next(iter(m.instance_for_data_model.values()))
    return dataclasses.replace(m, instance_for_data_model={dm: fn for dm in DATA_MODELS})


def _instance_mutants() -> list:
    out = []
    for m in INSTANCE_MUTANTS:
        if m.reference_only:
            out.append(_every_model(m))
            continue
        out.append(m)
        if "graph" in m.instance_for_data_model:
            out.append(_rdf_copy(m))
    return out


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


def _cypher_strict_to_inclusive(text: str) -> str:
    """The first strict comparison of a graph text served inclusively: a cypher one
    (`> $p`) where the text has it, else a SQL one (the views family)."""
    for a, b in ((" > $", " >= $"), (" < $", " <= $")):
        if a in text:
            return text.replace(a, b, 1)
    return _strict_to_inclusive(text)


def _sparql_drop_filter(text: str) -> str:
    """The first FILTER of a SPARQL text dropped (an index that answers every record)."""
    return re.sub(r"FILTER \((?:[^()]|\([^()]*\))*\)", "", text, count=1)


def _count_plus_one(text: str) -> str:
    return text.replace("COUNT(*) AS v", "COUNT(*) + 1 AS v", 1)


def _cypher_label_dropped(text: str) -> str:
    """The first labelled edge pattern of a graph text unlabelled: every edge type matched."""
    return re.sub(r"\[(\w+):\w+\]", r"[\1]", text, count=1)


def _cypher_direction_dropped(text: str) -> str:
    """The first directed hop of a graph text undirected."""
    return text.replace("]->(", "]-(", 1)


RENDER_MUTANTS = [
    Mutant("rel_strict_inclusive", "render", "the rel data model serves its first strict range bound inclusively", rewrite_case=lambda c: _variants(c, "rel", _strict_to_inclusive)),
    Mutant("doc_strict_inclusive", "render", "the doc data model serves its first strict range bound inclusively", rewrite_case=lambda c: _variants(c, "doc", _strict_to_inclusive)),
    Mutant("graph_strict_inclusive", "render", "the graph data model serves its first strict range bound inclusively", rewrite_case=lambda c: _variants(c, "graph", _cypher_strict_to_inclusive)),
    Mutant("rdf_filter_dropped", "render", "the rdf data model drops its first FILTER", rewrite_case=lambda c: _variants(c, "rdf", _sparql_drop_filter)),
    Mutant("rel_count_off_by_one", "render", "the rel data model's COUNT(*) is one too many", rewrite_case=lambda c: _variants(c, "rel", _count_plus_one)),
    Mutant("graph_label_dropped", "render", "the graph data model's first edge pattern matches every edge type", rewrite_case=lambda c: _variants(c, "graph", _cypher_label_dropped)),
    Mutant("graph_direction_dropped", "render", "the graph data model's first directed hop is undirected", rewrite_case=lambda c: _variants(c, "graph", _cypher_direction_dropped)),
]

MUTANTS = _instance_mutants() + RENDER_MUTANTS
