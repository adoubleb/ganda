"""Doc materialization: a relationship is a link object `{eid, target, ...}` in an array on its source record.
Churn rewrites a source's array to the final adjacency; a link to a removed record reads NONE."""
from __future__ import annotations

from ganda.core.data.generator import initial_node_values, initial_nodes
from ganda.core.data.model import EdgeUpdate, Instance, NodeDelete
from ganda.core.engine.setup_plan import churned_away_edges, initial_edge_values, initial_edges
from ganda.surrealdb.concrete_syntax.setup.common import (
    data_model_indexes,
    finalize,
    insert_statement,
    node_ddl,
    node_update_statements,
    object_literal,
    rid,
    typed,
)
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import SURQL

assumes("link.deleted_target.field_reads_none", SURQL, True)


def link_entry(schema, edge, values: "dict | None" = None) -> str:
    """One link as an object literal: `{ eid: "e0", target: n2:r3, w: 3 }` (the
    relationship's key, as the edge and junction rows hold it in their record ids)."""
    literals = {"eid": f'"{edge.key}"', "target": rid(edge.to_record)}
    literals.update(typed(schema, edge.type_name, edge.values if values is None else values, edge=True))
    return object_literal(literals)


def link_list(schema, edges, values: "dict | None" = None) -> str:
    return "[" + ", ".join(link_entry(schema, e, values.get(e.record_id) if values else None) for e in edges) + "]"


def grouped_links(edges) -> dict:
    grouped: dict = {}
    for edge in edges:
        grouped.setdefault((edge.type_name, edge.from_record), []).append(edge)
    return grouped


def build_document_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form("doc")
    ddl = node_ddl(schema, "doc", extra=lambda nt: [(et.name, "TYPE option<array<object>> FLEXIBLE") for et in schema.edge_types_from(nt.name)])

    values = initial_node_values(instance)
    data = [insert_statement(schema, n, values[n.record_id], form) for n in initial_nodes(instance)]

    initial_values = initial_edge_values(instance)
    initial_lists: dict = {}
    for (edge_type, from_record), edges in grouped_links(initial_edges(instance)).items():
        initial_lists.setdefault(from_record, {})[edge_type] = link_list(schema, edges, initial_values)
    for from_record, lists in initial_lists.items():
        for edge_type, text in lists.items():
            data.append(f"UPDATE {rid(from_record)} SET {edge_type} = {text};")

    # churn: node updates (a CONTENT write must carry the lists as they stand)
    data += node_update_statements(instance, form, extras=initial_lists)

    doomed = {op.node.record_id for op in instance.churn if isinstance(op, NodeDelete)}
    gone = churned_away_edges(instance)
    updated = [op.edge for op in instance.churn if isinstance(op, EdgeUpdate)]
    if gone or updated:
        # this data model's edge update AND its cascade: the source's list is rewritten
        # to the final adjacency with the final attribute values
        final_grouped = grouped_links(instance.edges)
        pairs = sorted({(e.type_name, e.from_record) for e in gone + updated if e.from_record not in doomed})
        for edge_type, from_record in pairs:
            data.append(f"UPDATE {rid(from_record)} SET {edge_type} = {link_list(schema, final_grouped.get((edge_type, from_record), []))};")
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            data.append(f"DELETE {rid(op.node.record_id)};")

    return finalize(ddl, data, [], instance, "doc", form, indexes=data_model_indexes(schema, "doc"))
