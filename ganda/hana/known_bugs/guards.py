"""The SAP HANA findings already filed, each withheld at its exact shape per variant so a
campaign does not report it again: a triaged finding gets an id (`H1`, `H2`, ..., or the
`findings/` catalogue's `A8`, `B13`), a write-up under `issues-draft/` or `findings/`, and
a guard here -- a function of the case's IR and the variant's concrete syntax that
answers its id where the variant's text is the bug's shape
(`ganda/orientdb/known_bugs/guards.py` is the pattern).

    H4   SQL over the doc views (collections read through UNNEST) adds up the values of
         rows the joins exclude: a traversal sum fails with [314] numeric overflow where
         the answer fits BIGINT, even where no path exists (`issues-draft/H4_repro.py`)
    B13  the triple store's AVG over xsd:long is double(SUM) / COUNT (a sum above 2^53
         loses its low bits, a quotient keeps seventeen digits) while HANA SQL's AVG over
         BIGINT is DECIMAL(25,6), the quotient truncated to six places (probe R20 / K23,
         `findings/B13-sparql-avg-in-double/`).
         An rdf average is compared only where the two agree: a sum below 2^53 and a
         quotient exact in six places.

H1, H2, H3, A8 (no double literal in a cypher text) and A9 (one parameter type per
OPENCYPHER_TABLE call) are worked around in the renderings instead (`rules/catalog.py`), so no
variant meets them."""
from __future__ import annotations

from ganda.hana.oracle.semantics import SUM_BOUND
from ganda.hana.rules import alike, works_around
from ganda.hana.rules.catalog import SPARQL, SQL

import dataclasses


works_around("A4")


def _h4(q, schema, instance) -> bool:
    """A traversal sum whose projected column holds values of an absolute total of 2^62
    or more somewhere in its table: an excluded row may overflow the sum (H4)."""
    if q.projection not in ("sum", "esum"):
        return False
    last = q.hops[-1]
    if q.projection == "esum":
        values = [e.values.get(q.field) for e in instance.edges if e.type_name == last.edge_type]
    else:
        et = schema.edge_type(last.edge_type)
        targets = {et.to_type, et.from_type} if last.direction == "both" else {et.to_type if last.direction == "out" else et.from_type}
        values = [n.values.get(q.field) for t in targets for n in instance.nodes_of(t)]
    return sum(abs(v) for v in values if isinstance(v, int) and not isinstance(v, bool)) >= SUM_BOUND


# --- B13: an rdf average the triple store's double arithmetic cannot answer as SQL does ----


def _b13(q, instance) -> bool:
    if q.kind != "agg" or q.fn != "avg":
        return False
    from ganda.core.query.ir.select import present_values

    values = present_values(q, instance)
    if not values or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return False
    total, n = sum(values), len(values)
    return abs(total) >= 2**53 or (total * 10**6) % n != 0


# --- the contract ------------------------------------------------------------------------------


def mark_known_bugs(case, q, schema, instance):
    """A traversal case with the variants that meet a filed bug marked."""
    known_bugs = dict(case.known_bugs)
    if _h4(q, schema, instance):
        for variant in case.queries:
            if variant.split(":", 1)[0] == "doc":
                known_bugs[variant] = "A4"
    return dataclasses.replace(case, known_bugs=known_bugs) if known_bugs != case.known_bugs else case


AVG_ALIKE = alike("aggregate.avg.integer.result", SQL, SPARQL)


def select_known(q, syntax, rewrites, data_model, schema, instance):
    if data_model == "rdf" and not AVG_ALIKE and _b13(q, instance):
        return "aggregate.avg.integer.result"
    return None


def write_known(q, syntax, rewrites, data_model, schema, instance):
    return None
