"""Relational-style materialization: every edge type is an ordinary document class used
as a junction table, with `src_id`/`dst_id` holding the endpoints' logical ids as plain
STRING foreign keys -- so the engine never builds an edge bag, a LINK or a graph
structure for it, and a hop is a correlated subquery joining through the junction:

    e1 { eid: "e1/e0", src_id: "n1/r0", dst_id: "n1/r2", w: 3 }
    LET $h = (SELECT dst_id AS v FROM e1 WHERE src_id = $parent.$current.id)

`src_id` carries a NOTUNIQUE index (every relational engine wants one on the FK column
a query joins through) and `dst_id` another for the incoming direction; the plain
junction scan is what a hop looks like when neither serves. Churned edges are
`DELETE FROM e1 WHERE eid = :k`; a churned-away node has its junction rows removed
explicitly (there is nothing to cascade) before its own record.
"""
from __future__ import annotations

from ganda.orientdb.mapping.common import (
    Sql,
    class_steps,
    edge_index_steps,
    edge_update_steps,
    finalize,
    initial_edge_values,
    initial_edges,
    initial_node_values,
    initial_nodes,
    insert_step,
    node_ddl,
    node_document,
    node_update_steps,
)
from ganda.orientdb.model import EdgeDelete, Instance, NodeDelete


def junction_document(edge, values: "dict | None" = None) -> dict:
    doc = {"eid": edge.record_id, "src_id": edge.from_record, "dst_id": edge.to_record}
    doc.update(edge.values if values is None else values)
    return doc


def junction_step(edge, form, values: "dict | None" = None) -> Sql:
    doc = junction_document(edge, values)
    params: dict = {}
    if form.insert_via == "content":
        params["doc"] = doc
        return Sql(f"INSERT INTO {edge.type_name} CONTENT :doc", params)
    if form.insert_via == "values":
        for i, v in enumerate(doc.values()):
            params[f"v{i}"] = v
        return Sql(f"INSERT INTO {edge.type_name} ({', '.join(doc)}) VALUES ({', '.join(f':v{i}' for i in range(len(doc)))})", params)
    from ganda.orientdb.mapping.common import _set_clause

    return Sql(f"INSERT INTO {edge.type_name} SET {_set_clause(doc, params)}", params)


def build_relational_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.rel_write_form
    ddl = node_ddl(schema, "rel")
    for et in schema.edge_types:
        steps = class_steps(et.name, et.fields, None, [("eid", "STRING"), ("src_id", "STRING"), ("dst_id", "STRING")])
        ddl += [s for s in steps if not s.command.endswith(f"{et.name}.id STRING")]
        ddl.append(Sql(f"CREATE INDEX fk_{et.name}_src ON {et.name} (src_id) NOTUNIQUE"))
        ddl.append(Sql(f"CREATE INDEX fk_{et.name}_dst ON {et.name} (dst_id) NOTUNIQUE"))
    ddl += edge_index_steps(schema, "rel")

    values = initial_node_values(instance)
    data = [insert_step(n.type_name, node_document(n, values[n.record_id]), form) for n in initial_nodes(instance)]
    edge_values = initial_edge_values(instance)
    data += [junction_step(e, form, edge_values[e.record_id]) for e in initial_edges(instance)]

    data += node_update_steps(instance, form)
    data += edge_update_steps(instance, form, "UPDATE")
    for op in instance.churn:
        if isinstance(op, EdgeDelete):
            data.append(Sql(f"DELETE FROM {op.edge.type_name} WHERE eid = :k", {"k": op.edge.record_id}))
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            for e in op.incident_edges:
                data.append(Sql(f"DELETE FROM {e.type_name} WHERE eid = :k", {"k": e.record_id}))
            data.append(Sql(f"DELETE FROM {op.node.type_name} WHERE id = :k", {"k": op.node.record_id}))

    return finalize(ddl, data, [], instance, "rel", form)
