"""Guards on the TRAVERSAL IR: `mark_known_bugs`, the entry the traversal generator
calls."""
from __future__ import annotations

import dataclasses

from ganda.arangodb.known_bugs.predicate import A25, A42, a25_shape, a42_shape


def mark_known_bugs(case, q, schema, instance):
    """A traversal case: the A25 shape in a source, hop or target predicate withholds
    the variants of the data models holding the array index -- but a pulled-up variant
    for a source predicate, which filters a projection, never a collection; the A42
    shape withholds every variant, the pulled-up ones too (they filter the same value)."""
    marks: dict = {}
    pulled_up = set(case.params.get("pulled_up", ()))

    def mark(fid: str, data_models, skip=()):
        for v in case.queries:
            if v.split(":", 1)[0] in data_models and v not in skip:
                marks.setdefault(v, fid)

    def judge(p, rows, table: str, edge: bool, skip=()):
        if a42_shape(p, rows):
            mark(A42, set(schema.engine.data_models))
        hit = [dm for dm in schema.engine.data_models if a25_shape(p, schema, table, dm, edge)]
        if hit:
            mark(A25, set(hit), skip)

    if q.source_predicate is not None:
        judge(q.source_predicate, instance.nodes_of(q.source_table), q.source_table, False, pulled_up)
    for h in q.hops:
        if h.predicate is None:
            continue
        et = schema.edge_type(h.edge_type)
        if h.predicate.on == "edge":
            judge(h.predicate, instance.edges_of(et.name), et.name, True)
        else:
            target = et.to_type if h.direction != "in" else et.from_type
            judge(h.predicate, instance.nodes_of(target), target, False)
    if not marks:
        return case
    return dataclasses.replace(case, known_bugs={**case.known_bugs, **marks})
