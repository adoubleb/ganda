"""The mutation benchmark's catalog on SAP HANA: the core's instance-level corruptions
over this engine's data models (`doc`, `rel`, `graph`, `rdf`), and render-level ones that
corrupt one data model's text the way a broken engine path would answer.

The core's catalog names `doc`, `graph` and `rel`. The graph data model is the Property
Graph Engine here, so a corruption aimed at `graph` stays there; the triple store gets a
copy of each (`rdf_*`), being a graph of its own; the relational node-value corruption
gets a copy per data model (`doc_` / `graph_` / `rdf_offset_node_numeric`: the core has
one, and a stored scalar wrong on one model is the plainest thing a select must see).
"""
from __future__ import annotations

import dataclasses
import re

from ganda.core.mutation.mutants import INSTANCE_MUTANTS, Mutant

DATA_MODELS = ("doc", "rel", "graph", "rdf")


def _rdf_copy(m: Mutant) -> Mutant:
    fn = m.instance_for_data_model["graph"]
    return dataclasses.replace(m, name=re.sub(r"^graph_", "rdf_", m.name), description=m.description.replace("graph data model", "rdf data model"), instance_for_data_model={"rdf": fn})


def _node_value_copy(m: Mutant, data_model: str) -> Mutant:
    """The relational node-value corruption aimed at another data model: a stored
    scalar wrong on one model is what a select or a traversal projection must see."""
    fn = m.instance_for_data_model["rel"]
    return dataclasses.replace(m, name=f"{data_model}_offset_node_numeric", description=m.description.replace("relational data model", f"{data_model} data model"), instance_for_data_model={data_model: fn})


def _instance_mutants() -> list:
    out = []
    for m in INSTANCE_MUTANTS:
        out.append(m)
        if "graph" in m.instance_for_data_model:
            out.append(_rdf_copy(m))
        if m.name == "rel_offset_node_numeric":
            out.extend(_node_value_copy(m, dm) for dm in ("doc", "graph", "rdf"))
    return out


def _variants(case, data_model: str, fn):
    queries = dict(case.queries)
    for v in list(queries):
        if v.split(":", 1)[0] == data_model:
            queries[v] = fn(queries[v])
    return dataclasses.replace(case, queries=queries)


def _strict_to_inclusive(text: str) -> str:
    """The first strict comparison of a SQL text served inclusively: against a bound
    parameter (`> :p`), a cast one (the collection filter's `> CAST(:p AS BIGINT)`, the
    cross-type constant's `> CAST(:p AS DOUBLE)`)."""
    hits = [(text.index(a), a, b) for a, b in ((" > :", " >= :"), (" < :", " <= :"), (" > CAST(:", " >= CAST(:"), (" < CAST(:", " <= CAST(:")) if a in text]
    if not hits:
        return text
    _, a, b = min(hits)
    return text.replace(a, b, 1)


def _cypher_strict_to_inclusive(text: str) -> str:
    """The first strict comparison of a graph text served inclusively: a cypher one
    (`> $p` for a bound double, `> 5` / `> 'x'` for an inlined literal) where the text
    has it, else a SQL one (the views family)."""
    m = re.search(r" ([<>]) (\$\w+|-?\d|')", text)
    if m:
        return text[: m.start(1)] + m.group(1) + "=" + text[m.end(1):]
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
