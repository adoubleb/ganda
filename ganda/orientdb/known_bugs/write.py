"""Guards on the WRITE IR: O37 and `write_known`, the entry the write generator calls.
"""
from __future__ import annotations

from ganda.core.data.model import Instance, Schema
from ganda.orientdb.engine.dialect import RANGE_KINDS
from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.orientdb.rules import works_around
from ganda.orientdb.known_bugs.predicate import _conjunct_leaves, _plain, edge_case_known, node_case_known, o25_embedded, o39_predicate


# `IS NOT NULL` through an index is a walk of every non-null key (the arithmetic
# write's own null filter)
_RANGE_OPS = ("<", "<=", ">", ">=", "BETWEEN", "IS NOT NULL")
# the writes that move an index key to a value the statement may meet again
_MOVING_WRITES = ("set", "arith", "merge", "edge_set", "edge_arith")


works_around("O37")


def o37_write(q, sp, data_model: str, pred, schema: Schema) -> bool:
    """O37 (`issue-drafts/O37-update-revisits-the-records-it-moves-through-an-index.md`):
    an UPDATE whose WHERE is served by an SB-tree index walk and whose SET changes a
    field of that index meets the records it moved again -- `SET f = f + 1 WHERE f >
    4` never terminates, `f * 2` doubles until the long wraps, `SET f = 100` counts
    every record twice, a two-sided range walks a record up to its bound, `RETURN
    BEFORE` lists a record twice. Probed on NOTUNIQUE and UNIQUE indexes, either
    null setting, on a vertex class and through an edge index; not through a hash
    index, a scan (a reversed operand), the `nest` target (`id IN (SELECT ...)`
    chooses the records first) or a transaction. The shape (conservative): a moving
    write, auto-commit, on the class or a `sub` target, and a plain top-level
    conjunct served by a range-capable index of the data model that holds the written
    field -- a range on any of its fields, or any comparison on another field of it.
    `SET f = null` / `REMOVE f` / a delete move nothing a walk meets (probed)."""
    if q.op not in _MOVING_WRITES or sp.door == "tx" or sp.target == "nest":
        return False
    if q.edge and data_model == "doc":
        return False
    for leaf in _conjunct_leaves(pred):
        if not _plain(leaf) or leaf.key is not None:
            continue
        if q.edge:
            ixs = [ix for ix in schema.indexes if ix.target == "edge" and ix.table == q.edge_type and ix.applies_to(data_model) and leaf.field in ix.all_fields]
        else:
            ixs = [ix for ix in schema.indexes if ix.target == "node" and ix.applies_to(data_model) and leaf.field in ix.all_fields and ix.table in _poly_tables(schema, q.table)]
        for ix in ixs:
            if ix.kind not in RANGE_KINDS or q.field not in ix.all_fields:
                continue
            if leaf.op in _RANGE_OPS or leaf.field != q.field or ix.extra_fields:
                return True
    return False


def _poly_tables(schema: Schema, table: str) -> set:
    tables = {table}
    if schema.base is not None:
        tables |= {nt.name for nt in schema.node_types} if schema.is_base(table) else {schema.base.name}
    return tables


def write_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a WRITE IR concrete syntax on `data_model` would hit, or None. The filter
    is judged as a read of the same predicate would be (a finding there chooses the
    wrong records, and the state diverges from the reference): `node_case_known` /
    `edge_case_known`, the doc data model's bracket filter through its own findings (O13)
    and O25 (a bracket ignores a `ci` collation); then O37."""
    pred = q.where
    eff = pred
    if q.extra_notnull:
        extra = Predicate("edge" if q.edge else "source", q.field, "IS NOT NULL", "", None)
        eff = extra if pred is None else Compound(pred.on, "AND", (pred, extra))
    fid = None
    if eff is not None:
        if not q.edge:
            fid = node_case_known(q.table, eff, schema, instance).get(data_model)
        elif data_model != "doc":
            fid = edge_case_known(q.edge_type, eff, schema, instance).get(data_model)
        else:
            # the bracket filter, or the filter on each position's element: untyped JSON
            # on the doc data model, with no collation
            fid = edge_case_known(q.edge_type, eff, schema, instance).get("doc") or ("O25" if o25_embedded(eff) else None)
    if fid is None and o37_write(q, sp, data_model, eff, schema):
        fid = "O37"
    if fid is None and eff is not None and o39_predicate(eff) and (sp.target == "nest" or (q.op == "delete" and data_model == "rel")):
        # the filter inside `id IN (SELECT ...)` (the nest target, the rel data model's
        # cascade) holds a CONTAINSVALUE leaf: O39's NPE
        fid = "O39"
    return fid
