"""The relational data model: every edge type is an ordinary DOCUMENT collection used as
a junction table, with `src_id` / `dst_id` holding the endpoints' LOGICAL ids as plain
strings -- so the engine builds no edge index and no graph structure for it, and a hop
is a join through the junction and the node collection's `id` index:

    e1/e0 { eid: "e1/e0", src_id: "n1/r0", dst_id: "n1/r2", w: 3 }
    FOR j IN e1 FILTER j.src_id == n.id FOR v IN n1 FILTER v.id == j.dst_id

`src_id` and `dst_id` each carry a persistent index (every relational schema has one on
the column a join goes through). A churned relationship is `REMOVE`d; a churned-away
node has its junction rows removed explicitly (there is nothing to cascade) before its
own document.
"""
from __future__ import annotations

from ganda.core.data.model import EdgeDelete, Instance, NodeDelete
from ganda.arangodb.engine.dialect import document_key
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
from ganda.arangodb.engine.plan import Aql, CreateCollection, CreateIndex

DATA_MODEL = "rel"


def junction_document(edge, values: "dict | None" = None) -> dict:
    doc = {"_key": edge.key, "eid": edge.record_id, "src_id": edge.from_record, "dst_id": edge.to_record}
    doc.update(edge.values if values is None else values)
    return doc


def build_relational_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form(DATA_MODEL)
    ddl = collection_steps(schema)
    for et in schema.edge_types:
        ddl.append(CreateCollection(et.name))
        ddl.append(CreateIndex(et.name, ["src_id"], f"fk_{et.name}_src"))
        ddl.append(CreateIndex(et.name, ["dst_id"], f"fk_{et.name}_dst"))
    ddl += index_steps(schema, DATA_MODEL)

    docs = node_documents(instance, initial_node_values(instance))
    first = initial_edge_values(instance)
    for e in initial_edges(instance):
        docs.setdefault(e.type_name, []).append(junction_document(e, first[e.record_id]))
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
