"""The graph data model: every edge type is an ArangoDB EDGE COLLECTION whose documents
carry `_from` / `_to` (the endpoints' engine ids) plus the relationship's logical id
`eid` and its attributes -- the engine's native graph feature, traversed with
`FOR v, e IN 1..1 OUTBOUND n e1`:

    e1/e0 { _from: "n1/r0", _to: "n1/r2", eid: "e1/e0", w: 3 }

A churned relationship is `REMOVE`d; a churned-away node has its incident edges
removed first (the engine's vertex removal leaves edges dangling), then its document.
"""
from __future__ import annotations

from ganda.core.model import EdgeDelete, Instance, NodeDelete
from ganda.arangodb.dialect import document_key, engine_id
from ganda.arangodb.mapping.common import (
    Aql,
    CreateCollection,
    collection_steps,
    edge_update_steps,
    finalize,
    index_steps,
    initial_edge_values,
    initial_edges,
    initial_node_values,
    insert_steps,
    node_collections,
    node_documents,
    node_update_steps,
)

DATA_MODEL = "graph"


def edge_document(edge, values: "dict | None" = None) -> dict:
    doc = {"_key": edge.key, "_from": engine_id(edge.from_record), "_to": engine_id(edge.to_record), "eid": edge.record_id}
    doc.update(edge.values if values is None else values)
    return doc


def build_graph_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form(DATA_MODEL)
    ddl = collection_steps(schema) + [CreateCollection(et.name, edge=True) for et in schema.edge_types] + index_steps(schema, DATA_MODEL)

    docs = node_documents(instance, initial_node_values(instance))
    first = initial_edge_values(instance)
    for e in initial_edges(instance):
        docs.setdefault(e.type_name, []).append(edge_document(e, first[e.record_id]))
    data = insert_steps(docs, form)

    data += node_update_steps(instance, form)
    data += edge_update_steps(instance, form, DATA_MODEL)
    for op in instance.churn:
        if isinstance(op, EdgeDelete):
            data.append(Aql(f"REMOVE @key IN {op.edge.type_name}", {"key": op.edge.key}))
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            for e in op.incident_edges:
                data.append(Aql(f"REMOVE @key IN {e.type_name}", {"key": e.key}))
            data.append(Aql(f"REMOVE @key IN {op.node.type_name}", {"key": document_key(op.node.key)}))

    collections = node_collections(schema) + [et.name for et in schema.edge_types]
    return finalize(ddl, data, [], instance, DATA_MODEL, form, collections)
