"""Guards on the SELECT IR: `select_known`, the entry the select generator calls."""
from __future__ import annotations

from ganda.core.data.model import Instance, Schema
from ganda.core.query.ir.select import effective
from ganda.surrealdb.known_bugs.predicate import edge_case_known, node_case_known


def select_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a SELECT IR concrete syntax on `data_model` would hit, or None."""
    pred = effective(q, sp.form, q.where)
    if pred is None:
        return None
    if q.edge:
        return edge_case_known(q.table, pred, schema, instance).get(data_model)
    return node_case_known(q.table, pred, schema, instance).get(data_model)
