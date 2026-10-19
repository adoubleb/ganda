"""Mutants: single, deliberate corruptions of the real pipeline.

The mutation benchmark is the feedback loop for changes to the fuzzer itself: real
engine bugs are rare, so the question "would the oracle notice if a data model stored
or answered something wrong?" is asked by corrupting one piece (one data model's
instance before it is materialized, one variant's rendered text) while leaving
everything else alone, and counting how often a case catches it.

The mutants here corrupt the INSTANCE one data model is materialized from, so they
apply to every engine with the standard `doc` / `graph` / `rel` data models. A provider
appends its own (a corruption of its query text, an index path) in `Provider.mutants`.
Categories: structural, attribute, churn, oracle-blind (every data model alike: only
the reference sees it).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable

from ganda.core.data.model import EdgeDelete, EdgeRow, EdgeUpdate, FieldType, Instance, NodeDelete, NodeUpdate


@dataclass(frozen=True)
class Mutant:
    name: str
    category: str
    description: str
    # data model -> function(Instance) -> Instance applied BEFORE that data model is materialized
    instance_for_data_model: dict = dataclasses.field(default_factory=dict)
    # function(case) -> case applied to every case (renderer-level corruption)
    rewrite_case: "Callable | None" = None
    # the data models whose plan is replaced by the plan of the corrupted instance
    reference_only: bool = False
    # function(case, schema) -> case: a renderer-level corruption that needs the
    # schema (which fields an index of the data model covers)
    rewrite_case_schema: "Callable | None" = None


def _with_edges(instance: Instance, edges: list) -> Instance:
    return dataclasses.replace(instance, edges=edges)


def _drop_first_edge(instance: Instance) -> Instance:
    return _with_edges(instance, instance.edges[1:]) if instance.edges else instance


def _duplicate_edges(instance: Instance) -> Instance:
    if not instance.edges:
        return instance
    e = instance.edges[0]
    dup = EdgeRow(e.type_name, 10_000 + e.local_id, e.from_record, e.to_record, dict(e.values))
    return _with_edges(instance, list(instance.edges) + [dup])


def _swap_direction(instance: Instance) -> Instance:
    swapped = []
    for e in instance.edges:
        et = instance.schema.edge_type(e.type_name)
        if et.from_type == et.to_type and e.from_record != e.to_record:
            swapped.append(EdgeRow(e.type_name, e.local_id, e.to_record, e.from_record, e.values))
        else:
            swapped.append(e)
    return _with_edges(instance, swapped)


def _drop_self_loops(instance: Instance) -> Instance:
    return _with_edges(instance, [e for e in instance.edges if e.from_record != e.to_record])


def _collapse_duplicates(instance: Instance) -> Instance:
    seen, out = set(), []
    for e in instance.edges:
        k = (e.type_name, e.from_record, e.to_record)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return _with_edges(instance, out)


def _truncate_fanout(instance: Instance, limit: int = 1) -> Instance:
    count: dict = {}
    out = []
    for e in instance.edges:
        k = (e.type_name, e.from_record)
        count[k] = count.get(k, 0) + 1
        if count[k] <= limit:
            out.append(e)
    return _with_edges(instance, out)


def _offset_edge_numeric(instance: Instance) -> Instance:
    out = []
    for e in instance.edges:
        vals = dict(e.values)
        for f in instance.schema.edge_type(e.type_name).fields:
            v = vals.get(f.name)
            if f.type is FieldType.INT and isinstance(v, int) and not isinstance(v, bool) and abs(v) < 2**62:
                vals[f.name] = v + 1
        out.append(EdgeRow(e.type_name, e.local_id, e.from_record, e.to_record, vals))
    return _with_edges(instance, out)


def _upper_edge_strings(instance: Instance) -> Instance:
    out = []
    for e in instance.edges:
        vals = {k: (v.upper() if isinstance(v, str) else v) for k, v in e.values.items()}
        out.append(EdgeRow(e.type_name, e.local_id, e.from_record, e.to_record, vals))
    return _with_edges(instance, out)


def _offset_node_numeric(instance: Instance) -> Instance:
    out = []
    for n in instance.nodes:
        vals = dict(n.values)
        for f in instance.schema.node_type(n.type_name).fields:
            v = vals.get(f.name)
            if f.type is FieldType.INT and isinstance(v, int) and not isinstance(v, bool) and abs(v) < 2**62:
                vals[f.name] = v + 1
        out.append(dataclasses.replace(n, values=vals))
    return dataclasses.replace(instance, nodes=out)


def _skip_node_updates(instance: Instance) -> Instance:
    """The NodeUpdate churn is dropped: the node keeps its INITIAL values."""
    nodes = list(instance.nodes)
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            nodes = [dataclasses.replace(n, values=dict(op.initial_values)) if n.record_id == op.node.record_id else n for n in nodes]
    return dataclasses.replace(instance, nodes=nodes, churn=[op for op in instance.churn if not isinstance(op, NodeUpdate)])


def _skip_edge_updates(instance: Instance) -> Instance:
    """The EdgeUpdate churn is dropped: the relationship keeps its INITIAL attributes."""
    initial = {op.edge.record_id: op.initial_values for op in instance.churn if isinstance(op, EdgeUpdate)}
    edges = [EdgeRow(e.type_name, e.local_id, e.from_record, e.to_record, dict(initial[e.record_id])) if e.record_id in initial else e for e in instance.edges]
    return dataclasses.replace(instance, edges=edges, churn=[op for op in instance.churn if not isinstance(op, EdgeUpdate)])


def _skip_edge_deletes(instance: Instance) -> Instance:
    """Churned edges are never removed."""
    kept = [op.edge for op in instance.churn if isinstance(op, EdgeDelete)]
    return dataclasses.replace(instance, edges=list(instance.edges) + kept, churn=[op for op in instance.churn if not isinstance(op, EdgeDelete)])


def _dangling_after_node_delete(instance: Instance) -> Instance:
    """The doc data model's cascade: a deleted node's INCOMING links stay in the sources'
    lists (a LINK to a removed record, which dereferences to null)."""
    extra = []
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            extra += [e for e in op.incident_edges if e.from_record != op.node.record_id]
    return dataclasses.replace(instance, edges=list(instance.edges) + extra)


INSTANCE_MUTANTS = [
    Mutant("graph_drop_first_edge", "structural", "the graph data model loses the first edge", {"graph": _drop_first_edge}),
    Mutant("doc_drop_first_edge", "structural", "the doc data model loses the first link", {"doc": _drop_first_edge}),
    Mutant("rel_drop_first_edge", "structural", "the relational data model loses the first junction row", {"rel": _drop_first_edge}),
    Mutant("graph_duplicate_edges", "structural", "the graph data model writes the first edge twice", {"graph": _duplicate_edges}),
    Mutant("rel_swap_direction", "structural", "the relational data model reverses self-typed edges", {"rel": _swap_direction}),
    Mutant("doc_drop_self_loops", "structural", "the doc data model loses every self-loop", {"doc": _drop_self_loops}),
    Mutant("graph_collapse_duplicates", "structural", "the graph data model keeps one of each parallel edge", {"graph": _collapse_duplicates}),
    Mutant("rel_truncate_fanout", "structural", "the relational data model keeps at most 1 edge per source", {"rel": _truncate_fanout}),
    Mutant("doc_offset_edge_numeric", "attribute", "the doc data model's INT edge attributes are off by one", {"doc": _offset_edge_numeric}),
    Mutant("graph_upper_edge_strings", "attribute", "the graph data model upper-cases string edge attributes", {"graph": _upper_edge_strings}),
    Mutant("rel_offset_node_numeric", "attribute", "the relational data model's INT node fields are off by one", {"rel": _offset_node_numeric}),
    Mutant("graph_stale_node_update", "churn", "the graph data model never applies the NodeUpdate churn", {"graph": _skip_node_updates}),
    Mutant("rel_edge_delete_skipped", "churn", "the relational data model never removes churned edges", {"rel": _skip_edge_deletes}),
    Mutant("graph_stale_edge_update", "churn", "the graph data model never applies the EdgeUpdate churn (needs --churn-edge-update-probability)", {"graph": _skip_edge_updates}),
    Mutant("doc_dangling_after_node_delete", "churn", "the doc data model leaves links to a deleted node", {"doc": _dangling_after_node_delete}),
    Mutant("all_sides_drop_edge", "oracle-blind", "every data model loses the first edge (only the reference sees it)", {"doc": _drop_first_edge, "graph": _drop_first_edge, "rel": _drop_first_edge}, reference_only=True),
]
