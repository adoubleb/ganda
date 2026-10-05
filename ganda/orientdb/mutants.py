"""A fixed catalog of deliberately-broken materializers and renderers.

Each `Mutant` simulates one class of representation-divergence bug by overriding a
single piece of the real pipeline (one side's instance before it is materialized, one
side's rendered text, or the oracle's view of one side) while leaving everything else
untouched. Running a trial through the mutant and through the real implementation
should disagree -- `ganda.orientdb.mutation_runner` checks that the cases actually notice.

This is the feedback loop for changes to the fuzzer itself: real engine bugs are rare,
so "bugs found per campaign" cannot tell whether a change to the query supply helped.
The catalog is the ground truth the supply is scored against, and every new axis is
accompanied by an A/B run over the same seeds.

Categories: structural (edges dropped/duplicated/reversed), attribute (values
shifted), churn (a churn step skipped so the final state is not the Instance),
oracle-blind (a corruption every side shares, which only the reference leg can see),
render (a predicate operator or a projection changed on one side), setup (the cancelled
junk block committed), index (an INDEX-path defect on one side: the texts whose filter
or ORDER BY an index of that side may serve are corrupted the way a broken index
would answer them -- a strict range bound served inclusively, null keys returned by a
lookup, a DESC walk answered ascending -- while a scan spelling of the same question
stays right; the select IR's identical base text and its scan / reversed /
arithmetic twins are what can see it).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import re
from typing import Callable

from ganda.orientdb.model import EdgeDelete, EdgeRow, EdgeUpdate, FieldType, Instance, NodeDelete, NodeUpdate, Schema
from ganda.orientdb.orient import RANGE_KINDS


@dataclass(frozen=True)
class Mutant:
    name: str
    category: str
    description: str
    # side -> function(Instance) -> Instance applied BEFORE that side is materialized
    instance_for_side: dict = dataclasses.field(default_factory=dict)
    # function(case) -> case applied to every case (renderer-level corruption)
    rewrite_case: "Callable | None" = None
    # the sides whose plan is replaced by the plan of the corrupted instance
    reference_only: bool = False
    # function(case, schema) -> case: a renderer-level corruption that needs the
    # schema (which fields an index of the side covers)
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
    """The doc side's cascade: a deleted node's INCOMING links stay in the sources'
    lists (a LINK to a removed record, which dereferences to null)."""
    extra = []
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            extra += [e for e in op.incident_edges if e.from_record != op.node.record_id]
    return dataclasses.replace(instance, edges=list(instance.edges) + extra)


def _first_case_side_query(case, side: str, fn):
    q = dict(case.queries)
    for s in list(q):
        if s.split(":", 1)[0] == side:
            q[s] = fn(q[s])
    return dataclasses.replace(case, queries=q)


def _flip_op(text: str) -> str:
    for a, b in ((" >= :", " > :"), (" <= :", " < :"), (" > :", " >= :"), (" < :", " <= :"), (" <> :", " = :")):
        if a in text:
            return text.replace(a, b, 1)
    return text


def _wrong_op_graph(case):
    return _first_case_side_query(case, "graph", _flip_op)


def _distinct_on_doc(case):
    def fn(t):
        return t.replace(".id AS v", ".id.asSet() AS v", 1) if ".id AS v" in t and "asSet" not in t else t

    return _first_case_side_query(case, "doc", fn)


def _index_fields(schema: Schema, table: str, side: str, range_only: bool) -> list:
    """The fields of `table` an index of `side` may serve (a range-capable one with
    `range_only`)."""
    out = []
    for f in schema.query_fields(table):
        ixs = schema.node_indexes(table, f.name, side)
        if range_only:
            ixs = [ix for ix in ixs if ix.kind in RANGE_KINDS]
        if ixs:
            out.append(f.name)
    return out


def _index_rewrite(side: str, range_only: bool, fn):
    """Apply `fn(text, fields)` to every spelling of `side` whose target is a node
    class it may serve from an index: not a subquery target (`(SELECT FROM t)`, which
    a scan answers), not a transaction script (its write shares the text), not a
    traversal or an edge-type question."""

    def rewrite(case, schema: Schema):
        table = case.params.get("table")
        if not table or case.template_name in ("ir_traversal", "ir_path") or (case.params.get("select") or {}).get("edge"):
            return case
        try:
            fields = _index_fields(schema, table, side, range_only)
        except KeyError:
            return case
        if not fields:
            return case
        q = dict(case.queries)
        for s in q:
            if s.split(":", 1)[0] == side and "(SELECT FROM " not in q[s] and not q[s].startswith("BEGIN;"):
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


MUTANTS = [
    Mutant("graph_drop_first_edge", "structural", "the graph side loses the first edge", {"graph": _drop_first_edge}),
    Mutant("doc_drop_first_edge", "structural", "the doc side loses the first link", {"doc": _drop_first_edge}),
    Mutant("rel_drop_first_edge", "structural", "the relational side loses the first junction row", {"rel": _drop_first_edge}),
    Mutant("graph_duplicate_edges", "structural", "the graph side writes the first edge twice", {"graph": _duplicate_edges}),
    Mutant("rel_swap_direction", "structural", "the relational side reverses self-typed edges", {"rel": _swap_direction}),
    Mutant("doc_drop_self_loops", "structural", "the doc side loses every self-loop", {"doc": _drop_self_loops}),
    Mutant("graph_collapse_duplicates", "structural", "the graph side keeps one of each parallel edge", {"graph": _collapse_duplicates}),
    Mutant("rel_truncate_fanout", "structural", "the relational side keeps at most 1 edge per source", {"rel": _truncate_fanout}),
    Mutant("doc_offset_edge_numeric", "attribute", "the doc side's INT edge attributes are off by one", {"doc": _offset_edge_numeric}),
    Mutant("graph_upper_edge_strings", "attribute", "the graph side upper-cases string edge attributes", {"graph": _upper_edge_strings}),
    Mutant("rel_offset_node_numeric", "attribute", "the relational side's INT node fields are off by one", {"rel": _offset_node_numeric}),
    Mutant("graph_stale_node_update", "churn", "the graph side never applies the NodeUpdate churn", {"graph": _skip_node_updates}),
    Mutant("rel_edge_delete_skipped", "churn", "the relational side never removes churned edges", {"rel": _skip_edge_deletes}),
    Mutant("graph_stale_edge_update", "churn", "the graph side never applies the EdgeUpdate churn (needs --churn-edge-update-probability)", {"graph": _skip_edge_updates}),
    Mutant("doc_dangling_after_node_delete", "churn", "the doc side leaves links to a deleted node", {"doc": _dangling_after_node_delete}),
    Mutant("all_sides_drop_edge", "oracle-blind", "every side loses the first edge (only the reference sees it)", {"doc": _drop_first_edge, "graph": _drop_first_edge, "rel": _drop_first_edge}, reference_only=True),
    Mutant("graph_wrong_predicate_op", "render", "the graph side's first comparison operator is loosened/tightened", rewrite_case=_wrong_op_graph),
    Mutant("doc_distinct_projection", "render", "the doc side deduplicates its id projection", rewrite_case=_distinct_on_doc),
    Mutant("graph_index_range_inclusive", "index", "a strict range an SB-tree index of the graph side serves includes its bound", rewrite_case_schema=_index_rewrite("graph", True, _inclusive_range)),
    Mutant("rel_index_null_keys", "index", "a lookup an index of the relational side serves also returns the null-keyed records", rewrite_case_schema=_index_rewrite("rel", False, _null_keys)),
    Mutant("doc_index_desc_walk", "index", "a DESC order the doc side walks from an SB-tree index comes back ascending", rewrite_case_schema=_index_rewrite("doc", True, _desc_as_asc)),
]

MUTANTS_BY_NAME = {m.name: m for m in MUTANTS}
