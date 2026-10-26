"""Relational-style materialization: every edge type is an ordinary table used as a
junction table, with `src` / `dst` holding the endpoints as record links -- so the
engine builds no graph edge for it, and a hop is a correlated subquery joining through
the junction:

    e1:e0 { src: n1:r0, dst: n2:r3, w: 3 }
    (SELECT VALUE dst FROM e1 WHERE src = $parent.id)

`src` carries an index (every relational engine wants one on the column a query joins
through) and `dst` another for the incoming direction. Churned edges are
`DELETE e1:e0`; a churned-away node has its junction rows removed explicitly (nothing
cascades through a plain record link) before its own record. A SCHEMAFULL junction
declares `src` / `dst` as `record<n1>` / `record<n2>` and every attribute.
"""
from __future__ import annotations

from ganda.core.data.generator import initial_node_values, initial_nodes
from ganda.core.data.model import EdgeDelete, Instance, NodeDelete
from ganda.core.engine.setup_plan import initial_edge_values, initial_edges
from ganda.surrealdb.concrete_syntax.setup.common import (
    data_model_indexes,
    edge_index_statements,
    edge_schemafull,
    edge_update_statements,
    finalize,
    insert_statement,
    node_ddl,
    node_update_statements,
    object_literal,
    rid,
    set_clause,
    table_statements,
    typed,
)


def junction_statement(schema, edge, form, values: "dict | None" = None) -> str:
    literals = {"src": rid(edge.from_record), "dst": rid(edge.to_record)}
    literals.update(typed(schema, edge.type_name, edge.values if values is None else values, edge=True))
    r = rid(edge.record_id)
    if form.insert_via == "content":
        return f"CREATE {r} CONTENT {object_literal(literals)};"
    if form.insert_via == "insert":
        return f"INSERT INTO {edge.type_name} {object_literal({'id': r, **literals})};"
    return f"CREATE {r} SET {set_clause(literals)};"


def build_relational_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form("rel")
    ddl = node_ddl(schema, "rel")
    for et in schema.edge_types:
        ddl += table_statements(et.name, et.fields, edge_schemafull(schema, et, "rel"), extra=[("src", f"TYPE record<{et.from_type}>"), ("dst", f"TYPE record<{et.to_type}>")])
        ddl.append(f"DEFINE INDEX fk_{et.name}_src ON TABLE {et.name} FIELDS src;")
        ddl.append(f"DEFINE INDEX fk_{et.name}_dst ON TABLE {et.name} FIELDS dst;")
    ddl += edge_index_statements(schema, "rel")

    values = initial_node_values(instance)
    data = [insert_statement(schema, n, values[n.record_id], form) for n in initial_nodes(instance)]
    edge_values = initial_edge_values(instance)
    data += [junction_statement(schema, e, form, edge_values[e.record_id]) for e in initial_edges(instance)]

    data += node_update_statements(instance, form)
    data += edge_update_statements(instance, form, endpoints=lambda e: {"src": rid(e.from_record), "dst": rid(e.to_record)})
    for op in instance.churn:
        if isinstance(op, EdgeDelete):
            data.append(f"DELETE {rid(op.edge.record_id)};")
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            for e in op.incident_edges:
                data.append(f"DELETE {rid(e.record_id)};")
            data.append(f"DELETE {rid(op.node.record_id)};")

    return finalize(ddl, data, [], instance, "rel", form, indexes=data_model_indexes(schema, "rel", edge_table=lambda t: t))
