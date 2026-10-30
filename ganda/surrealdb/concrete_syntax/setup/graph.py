"""Graph materialization: an edge type is a relation table (`RELATE n1:r0->e1:e0->n2:r3`); deleting a node removes its edges."""
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
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import ACCEPTED, GRAPH

assumes("accepts.relate.explicit_edge_id", GRAPH, ACCEPTED)
assumes("delete.record.cascades_relations", GRAPH, True)


def edge_statement(schema, edge, form, values: "dict | None" = None) -> str:
    literals = typed(schema, edge.type_name, edge.values if values is None else values, edge=True)
    if form.edge_from == "insert_relation":
        return f"INSERT RELATION INTO {edge.type_name} {object_literal({'id': rid(edge.record_id), 'in': rid(edge.from_record), 'out': rid(edge.to_record), **literals})};"
    head = f"RELATE {rid(edge.from_record)}->{rid(edge.record_id)}->{rid(edge.to_record)}"
    if form.insert_via == "content":
        return f"{head} CONTENT {object_literal(literals)};"
    return head + (f" SET {set_clause(literals)};" if literals else ";")


def build_graph_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form("graph")
    ddl = node_ddl(schema, "graph")
    for et in schema.edge_types:
        ddl += table_statements(et.name, et.fields, edge_schemafull(schema, et, "graph"), relation=(et.from_type, et.to_type))
    ddl += edge_index_statements(schema, "graph")

    values = initial_node_values(instance)
    data = [insert_statement(schema, n, values[n.record_id], form) for n in initial_nodes(instance)]
    edge_values = initial_edge_values(instance)
    data += [edge_statement(schema, e, form, edge_values[e.record_id]) for e in initial_edges(instance)]

    data += node_update_statements(instance, form)
    data += edge_update_statements(instance, form, endpoints=lambda e: {"in": rid(e.from_record), "out": rid(e.to_record)})
    for op in instance.churn:
        if isinstance(op, EdgeDelete):
            data.append(f"DELETE {rid(op.edge.record_id)};")
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            data.append(f"DELETE {rid(op.node.record_id)};")

    return finalize(ddl, data, [], instance, "graph", form, indexes=data_model_indexes(schema, "graph", edge_table=lambda t: t))
