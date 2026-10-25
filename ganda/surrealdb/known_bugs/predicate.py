"""The guards on one predicate: which data model's index serves a leaf, and the
per-data-model judgement `node_case_known` / `edge_case_known` the IR guards share.

F28 (`issue-drafts/F28-array-index-whole-array-equality.md`): an index over an array
field keys each ELEMENT, and a whole-array equality (`f = [1]`, `f IN [[1], [2]]`) the
planner serves from it looks the array up as one key and answers nothing, where a scan
answers the records holding exactly that array. The shape: a plain `=` / `IN` leaf whose
constant is a list (or a list of lists), on a LIST column an index of the data model
serves. `!=` and the containment operators scan (probed 3.3.4).
"""
from __future__ import annotations

from ganda.core.data.model import FieldType, Instance, Schema
from ganda.core.query.ir.predicates import leaves
from ganda.surrealdb.rules import works_around


def _leaves(p) -> list:
    return leaves(p)


def _plain(leaf) -> bool:
    return not leaf.rev and leaf.arith is None


def _field_indexes(schema: Schema, table: str, field_name: str, data_model: str, edge: bool) -> list:
    if edge:
        return [ix for ix in schema.indexes if ix.target == "edge" and ix.table == table and ix.field == field_name and ix.applies_to(data_model)]
    return schema.node_indexes(table, field_name, data_model)


def _column_type(schema: Schema, table: str, field_name: str, edge: bool) -> "FieldType | None":
    try:
        return schema.field(table, field_name, edge=edge).type
    except KeyError:
        return None


works_around("F28")


def f28_predicate(schema: Schema, table: str, p, data_model: str, edge: bool = False) -> bool:
    """A whole-array equality on a LIST column an index of the data model serves."""
    for leaf in _leaves(p):
        if leaf.op not in ("=", "IN") or leaf.key is not None or not _plain(leaf) or leaf.sub is not None:
            continue
        constants = leaf.constant if leaf.op == "IN" else [leaf.constant]
        if not any(isinstance(v, list) for v in constants):
            continue
        if _column_type(schema, table, leaf.field, edge) is FieldType.LIST and _field_indexes(schema, table, leaf.field, data_model, edge):
            return True
    return False


def _case_known(table: str, p, schema: Schema, instance: Instance, edge: bool) -> dict:
    out: dict = {}
    if p is None:
        return out
    for data_model in schema.engine.data_models:
        if f28_predicate(schema, table, p, data_model, edge):
            out[data_model] = "F28"
    return out


def node_case_known(table: str, p, schema: Schema, instance: Instance) -> dict:
    """{data model: defect id} for a filter `p` over a node class."""
    return _case_known(table, p, schema, instance, False)


def edge_case_known(edge_type: str, p, schema: Schema, instance: Instance) -> dict:
    """{data model: defect id} for a filter `p` over a relationship type."""
    return _case_known(edge_type, p, schema, instance, True)
