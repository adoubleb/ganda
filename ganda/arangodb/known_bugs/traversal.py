"""A44: a pulled-up row holding a subquery, filtered by two or more `HAS` tests or by one
negated `HAS` an OR repeats."""
from __future__ import annotations

import dataclasses
import re

from ganda.core.query.ir.predicates import Compound, Predicate, fields_of, leaves
from ganda.arangodb.rules import works_around
from ganda.arangodb.known_bugs.predicate import A25, A39, A40, A42, a25_shape, a39_shape, a42_shape, holds_non_array, negated_memberships

A44 = "A44"
works_around("A44")
# a traversal operator (`FOR v, e IN 1..1 OUTBOUND`, `FOR dv, de, dp IN 3..3 OUTBOUND`):
# the edge-scan, link-array and junction texts hold none
_TRAVERSAL = re.compile(r"\bIN \d+\.\.\d+ (?:OUTBOUND|INBOUND|ANY)\b")
# a subquery inside the pulled-up query besides its own `FOR hr IN (FOR ...`: the row's
# traversal answer (`v: MAX(FOR ...)`) or the `LET` subqueries it is built from (`LET rl1 =
# UNIQUE(FOR ...)`, the reach projections; campaign seed 11424)
_SUBQUERY_ROW = re.compile(r"\(FOR .*\(FOR ", re.S)
_PRESENCE = ("IS PRESENT", "IS ABSENT")


def _dnf(p, neg: bool = False) -> list:
    """The disjunctive normal form's terms, each a list of (leaf, negated)."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return _dnf(p.children[0], not neg)
        parts = [_dnf(c, neg) for c in p.children]
        if (p.op == "OR") != neg:
            return [term for part in parts for term in part]
        terms: list = [[]]
        for part in parts:
            terms = [a + b for a in terms for b in part]
        return terms
    return [[(p, neg)]]


def a44_shape(p) -> bool:
    presence = [lf for lf in leaves(p) if isinstance(lf, Predicate) and lf.op in _PRESENCE]
    if len(presence) >= 2:
        return True
    if not presence:
        return False
    # one presence leaf: negated (`NOT HAS`) in two or more terms
    negated = [term for term in _dnf(p) for lf, neg in term if lf is presence[0] and neg != (lf.op == "IS ABSENT")]
    return len(negated) >= 2


def _indexed(schema, table: str, fields, data_model: str) -> bool:
    try:
        return any(schema.node_indexes(table, f, data_model) for f in fields)
    except KeyError:
        return False


def mark_known_bugs(case, q, schema, instance):
    """A25, A39 and A42 at their predicate shapes; A40 on the traversal-operator variants
    or indexed pull-ups; A44 on the pull-ups."""
    marks: dict = {}
    pulled_up = set(case.params.get("pulled_up", ()))

    def mark(fid: str, data_models, skip=()):
        for v in case.queries:
            if v.split(":", 1)[0] in data_models and v not in skip:
                marks.setdefault(v, fid)

    def mark_where(fid: str, keep):
        for v, text in case.queries.items():
            if keep(v, text):
                marks.setdefault(v, fid)

    def judge(p, rows, table: str, edge: bool, skip=()):
        if a42_shape(p, rows):
            mark(A42, set(schema.engine.data_models))
        if a39_shape(p, rows):
            mark(A39, set(schema.engine.data_models))
        hit = [dm for dm in schema.engine.data_models if a25_shape(p, schema, table, dm, edge)]
        if hit:
            mark(A25, set(hit), skip)

    sp = q.source_predicate
    if sp is not None:
        rows = instance.nodes_of(q.source_table)
        judge(sp, rows, q.source_table, False, pulled_up)
        lfs = negated_memberships(sp)
        if lfs and holds_non_array(rows, lfs):
            indexed = {dm for dm in schema.engine.data_models if _indexed(schema, q.source_table, fields_of(sp), dm)}
            mark_where(A40, lambda v, _t: v in pulled_up and v.split(":", 1)[0] in indexed)
        if a44_shape(sp):
            mark_where(A44, lambda v, t: v in pulled_up and bool(_SUBQUERY_ROW.search(t)))
    for h in q.hops:
        if h.predicate is None:
            continue
        et = schema.edge_type(h.edge_type)
        if h.predicate.on == "edge":
            rows, table, edge = instance.edges_of(et.name), et.name, True
        else:
            table = et.to_type if h.direction != "in" else et.from_type
            rows, edge = instance.nodes_of(table), False
        judge(h.predicate, rows, table, edge)
        lfs = negated_memberships(h.predicate)
        if lfs and holds_non_array(rows, lfs):
            mark_where(A40, lambda _v, t: bool(_TRAVERSAL.search(t)))
    if not marks:
        return case
    return dataclasses.replace(case, known_bugs={**case.known_bugs, **marks})
