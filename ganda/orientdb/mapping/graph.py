"""Graph-style materialization: node classes extend `V`, every edge type is an `E`
subclass, and a relationship is a real edge (`CREATE EDGE`) whose `out`/`in` links the
engine maintains in the vertices' `out_e1`/`in_e1` bags -- the native graph feature,
traversed with `out()`/`in()`/`both()`, `MATCH` and `TRAVERSE`.

    e1 { eid: "e1/e0", out: #12:0, in: #12:3, w: 3 }

An edge is created from RID parameters (`FROM :a TO :b`, the `ref` form) or from
subqueries on the logical id (`FROM (SELECT FROM n1 WHERE id = :a) TO (...)`), two
write doors. A churned edge is `DELETE EDGE e1 WHERE eid = :k`; a churned-away node
is `DELETE VERTEX n1 WHERE id = :k`, whose cascade removes the incident edges itself
(probed) -- the engine's cascade rather than the fuzzer's.
"""
from __future__ import annotations

from ganda.orientdb.mapping.common import (
    Ref,
    Sql,
    _set_clause,
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
    class_steps,
)
from ganda.core.data.model import EdgeDelete, Instance, NodeDelete


def edge_step(edge, form, values: "dict | None" = None) -> Sql:
    params: dict = {}
    if form.edge_from == "subquery":
        params["a"], params["b"] = edge.from_record, edge.to_record
        src = f"(SELECT FROM {edge.from_record.split('/', 1)[0]} WHERE id = :a)"
        dst = f"(SELECT FROM {edge.to_record.split('/', 1)[0]} WHERE id = :b)"
    else:
        params["a"], params["b"] = Ref(edge.from_record), Ref(edge.to_record)
        src, dst = ":a", ":b"
    doc = {"eid": edge.record_id}
    doc.update(edge.values if values is None else values)
    if form.insert_via == "content":
        params["doc"] = doc
        return Sql(f"CREATE EDGE {edge.type_name} FROM {src} TO {dst} CONTENT :doc", params)
    return Sql(f"CREATE EDGE {edge.type_name} FROM {src} TO {dst} SET {_set_clause(doc, params)}", params)


def build_graph_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form("graph")
    ddl = node_ddl(schema, "graph", superclass="V")
    for et in schema.edge_types:
        # an E subclass declares `eid` and the attributes; `out`/`in` are the engine's
        steps = class_steps(et.name, et.fields, "E")
        ddl += [s for s in steps if not s.command.endswith(f"{et.name}.id STRING")]
        ddl.append(Sql(f"CREATE PROPERTY {et.name}.eid STRING"))
    ddl += edge_index_steps(schema, "graph")

    values = initial_node_values(instance)
    data = [insert_step(n.type_name, node_document(n, values[n.record_id]), form, vertex=True) for n in initial_nodes(instance)]
    edge_values = initial_edge_values(instance)
    data += [edge_step(e, form, edge_values[e.record_id]) for e in initial_edges(instance)]

    data += node_update_steps(instance, form)
    data += edge_update_steps(instance, form, "UPDATE EDGE")
    for op in instance.churn:
        if isinstance(op, EdgeDelete):
            data.append(Sql(f"DELETE EDGE {op.edge.type_name} WHERE eid = :k", {"k": op.edge.record_id}))
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            data.append(Sql(f"DELETE VERTEX {op.node.type_name} WHERE id = :k", {"k": op.node.record_id}))

    return finalize(ddl, data, [], instance, "graph", form, vertex=True)
