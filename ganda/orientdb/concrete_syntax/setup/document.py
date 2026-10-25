"""Document-style materialization: a relationship is an EMBEDDEDLIST of embedded
documents on the source record, each holding the target as a LINK under `target`, the
relationship's logical id under `eid`, and the relationship's own attributes -- the
denormalized "links inside the document" pattern, with OrientDB's own link type as the
foreign reference:

    n1 { id: "n1/r0", f1: "a", e1: [ { target: #12:3, eid: "e1/e0", w: 3 }, ... ] }

Classes are plain document classes (no `V`), so `out()`/`MATCH`/`TRAVERSE` over edges
never enter; a hop is `e1.target`, dereferenced as `e1.target.id` / `e1.target.f1`.

Two phases: node records are inserted first (every data model's node write forms apply),
then one `UPDATE ... SET e1 = :links` per (source, edge type) writes the INITIAL link
list with `Ref` targets the connection layer resolves to that database's RIDs. Churn then
rewrites lists: a source that loses an outgoing edge (EdgeDelete, or the cascade of a
NodeDelete) gets its list rewritten to the final adjacency -- that rewrite IS this
data model's cascade. A deleted node's own record is removed with `DELETE FROM`; a LINK to a
removed record would dereference to null (probed), which is exactly what the
`doc_dangling_after_node_delete` mutant leaves behind.
"""
from __future__ import annotations

from ganda.orientdb.concrete_syntax.setup.common import (
    Ref,
    Sql,
    churned_away_edges,
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
from ganda.core.data.model import EdgeUpdate, Instance, NodeDelete
from ganda.orientdb.rules import assumes
from ganda.orientdb.rules.catalog import SQL

# a link list travels as RID strings in a parameter, which the engine stores as LINKs
assumes("parameter.rid_string.becomes_link", SQL, True)


def link_entry(edge, values: "dict | None" = None) -> dict:
    entry = {"target": Ref(edge.to_record), "eid": edge.record_id}
    entry.update(edge.values if values is None else values)
    return entry


def grouped_links(edges) -> dict:
    grouped: dict = {}
    for edge in edges:
        grouped.setdefault((edge.type_name, edge.from_record), []).append(edge)
    return grouped


def build_document_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form("doc")
    ddl = node_ddl(schema, "doc", extra=lambda nt: [(et.name, "EMBEDDEDLIST") for et in schema.edge_types_from(nt.name)])

    values = initial_node_values(instance)
    data = [insert_step(n.type_name, node_document(n, values[n.record_id]), form) for n in initial_nodes(instance)]

    initial_values = initial_edge_values(instance)
    initial_lists: dict = {}
    for (edge_type, from_record), edges in grouped_links(initial_edges(instance)).items():
        initial_lists.setdefault(from_record, {})[edge_type] = [link_entry(e, initial_values[e.record_id]) for e in edges]
    for from_record, lists in initial_lists.items():
        coll = from_record.split("/", 1)[0]
        for edge_type, entries in lists.items():
            data.append(Sql(f"UPDATE {coll} SET {edge_type} = :links WHERE id = :id", {"links": entries, "id": from_record}))

    # churn: node updates (a CONTENT write must carry the lists as they stand)
    data += node_update_steps(instance, form, extras=initial_lists)

    doomed = {op.node.record_id for op in instance.churn if isinstance(op, NodeDelete)}
    gone = churned_away_edges(instance)
    updated = [op.edge for op in instance.churn if isinstance(op, EdgeUpdate)]
    if gone or updated:
        # this data model's edge update AND its cascade: the source's list is rewritten to
        # the final adjacency with the final attribute values
        final_grouped = grouped_links(instance.edges)
        pairs = sorted({(e.type_name, e.from_record) for e in gone + updated if e.from_record not in doomed})
        for edge_type, from_record in pairs:
            coll = from_record.split("/", 1)[0]
            entries = [link_entry(e) for e in final_grouped.get((edge_type, from_record), [])]
            data.append(Sql(f"UPDATE {coll} SET {edge_type} = :links WHERE id = :id", {"links": entries, "id": from_record}))
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            data.append(Sql(f"DELETE FROM {op.node.type_name} WHERE id = :id", {"id": op.node.record_id}))

    return finalize(ddl, data, [], instance, "doc", form)
