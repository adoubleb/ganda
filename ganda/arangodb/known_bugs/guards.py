"""The ArangoDB findings already filed, each withheld at its exact shape per variant so a
campaign does not report it again (`ganda/orientdb/known_bugs/guards.py` is the pattern). The
legacy port's A1..A39 (`../arangoDB/issue-drafts/`) are either outside this port's
surface (ArangoSearch views, MDI and geo indexes, the JavaScript door) or kept
unreachable by construction (no signed zero is stored, A1 / A2; no ordered comparison on
a string, A9 / A34).

**A40** (`issues/A40-not-in-over-a-non-array-disagrees-with-not-of-in.md`, found by the
first smoke of this port): `NOT (x IN y)` for a NON-ARRAY `y` is true as a constant but
FALSE over a document attribute -- the optimizer rewrites it into `x NOT IN y`, which is
false for a non-array -- so a filter holding `NOT (@p IN n.list)` and its double-negated
rewrite disagree on every record whose list is null or absent. The guard withholds a
variant whose predicate has a list membership (`CONTAINS`, `CONTAINSANY`,
`CONTAINSALL`) under a NOT, where the list column holds a null or absent value among the
records the question reads.
"""
from __future__ import annotations

import dataclasses

from ganda.core.query.ir.predicates import Compound, Predicate

A40 = "A40"
_MEMBERSHIP = ("CONTAINS", "CONTAINSANY", "CONTAINSALL")


def membership_under_not(p, under: bool = False) -> set:
    """The list fields a membership leaf tests under a NOT, anywhere in the tree."""
    if p is None:
        return set()
    if isinstance(p, Compound):
        out: set = set()
        for c in p.children:
            out |= membership_under_not(c, under or p.op == "NOT")
        return out
    if under and isinstance(p, Predicate) and p.op in _MEMBERSHIP and not p.rev:
        return {p.field}
    return set()


def _has_null(rows, fields: set) -> bool:
    return any(r.values.get(f) is None for r in rows for f in fields)


def a40_shape(p, rows) -> bool:
    fields = membership_under_not(p)
    return bool(fields) and _has_null(rows, fields)


def mark_known_bugs(case, q, schema, instance):
    """A traversal case: the A40 shape in a source, hop or target predicate withholds
    every variant (every data model renders the same predicate)."""
    marked = False
    if q.source_predicate is not None and a40_shape(q.source_predicate, instance.nodes_of(q.source_table)):
        marked = True
    for h in q.hops:
        if h.predicate is None:
            continue
        et = schema.edge_type(h.edge_type)
        if h.predicate.on == "edge":
            marked = marked or a40_shape(h.predicate, instance.edges_of(et.name))
        else:
            target = et.to_type if h.direction != "in" else et.from_type
            marked = marked or a40_shape(h.predicate, instance.nodes_of(target))
    if not marked:
        return case
    return dataclasses.replace(case, known_bugs={**case.known_bugs, **{v: A40 for v in case.queries}})


def select_known(q, syntax, rewrites, data_model, schema, instance):
    pred = rewrites.filter_pred(q, syntax.filter)
    rows = instance.edges_of(q.table) if q.edge else instance.nodes_of(q.table)
    return A40 if a40_shape(pred, rows) else None


def write_known(q, syntax, rewrites, data_model, schema, instance):
    pred = rewrites.pred(q, syntax.filter)
    rows = instance.edges_of(q.edge_type) if q.edge else instance.nodes_of(q.table)
    return A40 if a40_shape(pred, rows) else None
