"""Guards on the WRITE IR: `write_known`, the entry the write generator calls. The
filter is judged as a read of the same predicate would be; the doc data model's edge
delete (`bracket`, `arr[* FILTER NOT (p)]`) is also judged on the complement it filters
by: A40 where a membership of `p` meets the `NOT IN` rewrite on one side and not the
other (`complement_memberships`) and a link of the type stores a non-array there (graph
and rel filter `p` itself). It is that form's only syntax, so the write is drawn again."""
from __future__ import annotations

from ganda.arangodb.known_bugs.predicate import A25, A39, A40, A42, a25_shape, a39_shape, a42_shape, complement_memberships, holds_non_array


def a40_bracket(q, syntax, instance) -> bool:
    if getattr(syntax, "form", None) != "bracket" or q.where is None:
        return False
    lfs = complement_memberships(q.where)
    return bool(lfs) and holds_non_array(instance.edges_of(q.edge_type), lfs)


def write_known(q, syntax, rewrites, data_model, schema, instance):
    pred = q.where
    rows = instance.edges_of(q.edge_type) if q.edge else instance.nodes_of(q.table)
    if a42_shape(pred, rows):
        return A42
    if a39_shape(pred, rows):
        return A39
    if a40_bracket(q, syntax, instance):
        return A40
    if a25_shape(pred, schema, q.edge_type if q.edge else q.table, data_model, q.edge):
        return A25
    return None
