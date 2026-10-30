"""Guards on the WRITE IR: `write_known`, the entry the write generator calls. The
filter is judged as a read of the same predicate would be."""
from __future__ import annotations

from ganda.arangodb.known_bugs.predicate import A25, A42, a25_shape, a42_shape


def write_known(q, syntax, rewrites, data_model, schema, instance):
    pred = q.where
    rows = instance.edges_of(q.edge_type) if q.edge else instance.nodes_of(q.table)
    if a42_shape(pred, rows):
        return A42
    if a25_shape(pred, schema, q.edge_type if q.edge else q.table, data_model, q.edge):
        return A25
    return None
