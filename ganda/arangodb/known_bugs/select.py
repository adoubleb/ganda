"""Guards on the SELECT IR: A43 (a join's correlated strict bound through a sparse
index) and `select_known`, the entry the select generator calls; A39 withholds every
concrete syntax (each is a FILTER `use-indexes` analyses)."""
from __future__ import annotations

from ganda.arangodb.rules import works_around
from ganda.arangodb.known_bugs.predicate import A25, A39, A42, a25_shape, a39_shape, a42_shape

A43 = "A43"
works_around("A43")


def a43_join(q, schema, instance, data_model: str) -> bool:
    """A43: a join's correlated strict bound `x.f < n.f` (`n.f > x.f` reversed) served by
    a sparse persistent index of the data model on the joined class's `f` loses the
    records whose f is null or absent -- the shape, where the class holds such a record
    (probed: `<=`, `==`, `>`, `>=`, `!=`, a constant bound and a dense index are right)."""
    if q.kind != "join" or q.fn != "<" or schema.is_base(q.table):
        return False
    # an index on the class or on the base class (which covers every subclass)
    sparse = any(ix.ignore_nulls for ix in schema.node_indexes(q.table, q.field, data_model))
    return sparse and any(n.values.get(q.field) is None for n in instance.nodes_of(q.table))


def select_known(q, syntax, rewrites, data_model, schema, instance):
    if a43_join(q, schema, instance, data_model):
        return A43
    pred = q.where
    rows = instance.edges_of(q.table) if q.edge else instance.nodes_of(q.table)
    if a42_shape(pred, rows):
        return A42
    if a39_shape(pred, rows):
        return A39
    if a25_shape(pred, schema, q.table, data_model, q.edge):
        return A25
    return None
