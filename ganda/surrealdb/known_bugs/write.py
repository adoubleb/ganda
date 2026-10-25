"""Guards on the WRITE IR: `write_known`, the entry the write generator calls."""
from __future__ import annotations

from ganda.core.data.model import Instance, Schema
from ganda.surrealdb.known_bugs.predicate import edge_case_known, node_case_known


def write_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a WRITE IR concrete syntax on `data_model` would hit, or None. The
    filter is judged as a read of the same predicate would be."""
    if q.where is None:
        return None
    if q.edge:
        return edge_case_known(q.edge_type, q.where, schema, instance).get(data_model)
    return node_case_known(q.table, q.where, schema, instance).get(data_model)
