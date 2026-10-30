"""The graph data model: an edge type is an edge collection (`_from` / `_to`, `eid`); a
removed node's incident edges are removed first."""
from __future__ import annotations

from ganda.core.data.model import EdgeDelete, Instance, NodeDelete
from ganda.arangodb.engine.dialect import document_key, engine_id
from ganda.core.data.generator import initial_node_values
from ganda.core.engine.setup_plan import initial_edge_values, initial_edges
from ganda.arangodb.concrete_syntax.setup.common import (
    collection_steps,
    edge_update_steps,
    finalize,
    index_steps,
    insert_steps,
    node_collections,
    node_documents,
    node_update_steps,
)
from ganda.arangodb.engine.plan import Aql, CreateCollection

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
