"""The document data model: a relationship is an array of embedded link objects on the
source document, each holding the target's engine id under `target`, the relationship's
logical id under `eid`, and its attributes -- the denormalized "links inside the
document" pattern, with the engine's own `_id` as the foreign reference:

    n1/r0 { id: "n1/r0", f1: "a", e1: [ { eid: "e1/e0", target: "n2/r3", w: 3 }, ... ] }

A hop is `DOCUMENT(l.target)` (or a join on `_id`), an incoming hop a scan of the
sources whose array holds the record -- or, through the array index every source
collection carries on its link targets (`fk_<e>_target` over `e[*].target`), a lookup. Node documents are written with their INITIAL
link arrays embedded, so one bulk insert per collection carries everything; churn then
rewrites arrays: a source whose relationship changes or disappears (EdgeUpdate,
EdgeDelete, the cascade of a NodeDelete) gets its array for that edge type re-set to the
final adjacency -- that rewrite IS this data model's cascade (the engine removes no
link on its own, so a dangling link exists only as a mutant). A deleted node's own
document is removed.
"""
from __future__ import annotations

from ganda.core.data.model import EdgeUpdate, Instance, NodeDelete
from ganda.arangodb.engine.dialect import document_key, engine_id
from ganda.core.data.generator import initial_node_values
from ganda.core.engine.setup_plan import churned_away_edges, initial_edge_values, initial_edges
from ganda.arangodb.concrete_syntax.setup.common import (
    collection_steps,
    finalize,
    index_steps,
    insert_steps,
    node_collections,
    node_documents,
    node_update_steps,
)
from ganda.arangodb.engine.plan import Aql, CreateIndex

DATA_MODEL = "doc"


def link_object(edge, values: "dict | None" = None) -> dict:
    entry = {"eid": edge.record_id, "target": engine_id(edge.to_record)}
    entry.update(edge.values if values is None else values)
    return entry


def grouped_links(edges) -> dict:
    grouped: dict = {}
    for edge in edges:
        grouped.setdefault((edge.type_name, edge.from_record), []).append(edge)
    return grouped


def build_document_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form(DATA_MODEL)
    ddl = collection_steps(schema) + index_steps(schema, DATA_MODEL)
    for et in schema.edge_types:
        # the reverse lookup every document schema keeps on an embedded reference: an
        # array index over the link targets, which serves `cur._id IN s.e1[*].target`
        # (the `join` syntax's incoming hop) where the plain enumeration scans
        ddl.append(CreateIndex(et.from_type, [f"{et.name}[*].target"], f"fk_{et.name}_target"))

    docs = node_documents(instance, initial_node_values(instance))
    first = initial_edge_values(instance)
    initial_arrays: dict = {}
    for (edge_type, from_record), edges in grouped_links(initial_edges(instance)).items():
        initial_arrays.setdefault(from_record, {})[edge_type] = [link_object(e, first[e.record_id]) for e in edges]
    for coll_docs in docs.values():
        for doc in coll_docs:
            doc.update(initial_arrays.get(doc["id"], {}))
    data = insert_steps(docs, form)

    # churn: node updates (a whole-document form must carry the arrays as they stand)
    data += node_update_steps(instance, form, extras=initial_arrays)

    doomed = {op.node.record_id for op in instance.churn if isinstance(op, NodeDelete)}
    changed = churned_away_edges(instance) + [op.edge for op in instance.churn if isinstance(op, EdgeUpdate)]
    if changed:
        # this data model's edge update AND its cascade: the source's array is re-set
        # to the final adjacency with the final attribute values
        final_grouped = grouped_links(instance.edges)
        for edge_type, from_record in sorted({(e.type_name, e.from_record) for e in changed if e.from_record not in doomed}):
            coll, key = from_record.split("/", 1)
            entries = [link_object(e) for e in final_grouped.get((edge_type, from_record), [])]
            data.append(Aql(f"UPDATE @key WITH @doc IN {coll} OPTIONS {{mergeObjects: false}}", {"key": document_key(key), "doc": {edge_type: entries}}))
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            data.append(Aql(f"REMOVE @key IN {op.node.type_name}", {"key": document_key(op.node.key)}))

    return finalize(ddl, data, [], instance, DATA_MODEL, form, node_collections(schema))
