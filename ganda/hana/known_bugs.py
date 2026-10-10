"""The SAP HANA findings already filed, each withheld at its exact shape per variant so a
campaign does not report it again: a triaged finding gets an id (`H1`, `H2`, ...), a
write-up under `issues-draft/`, and a guard here -- a function of the case's AST and the
variant's concrete syntax that answers its id where the variant's text is the bug's shape
(`ganda/orientdb/known_bugs.py` is the pattern).

    H4   SQL over the doc views (collections read through UNNEST) adds up the values of
         rows the joins exclude: a traversal sum fails with [314] numeric overflow where
         the answer fits BIGINT, even where no path exists (`issues-draft/H4_repro.py`)

H1, H2 and H3 are worked around in the renderings instead (`RULES.md`), so no variant
meets them."""
from __future__ import annotations

import dataclasses


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
    return sum(abs(v) for v in values if isinstance(v, int) and not isinstance(v, bool)) >= 2**62


def mark_known_bugs(case, q, schema, instance):
    """A traversal case with the variants that meet a filed bug marked."""
    known_bugs = dict(case.known_bugs)
    if _h4(q, schema, instance):
        for variant in case.queries:
            if variant.split(":", 1)[0] == "doc":
                known_bugs[variant] = "H4"
    return dataclasses.replace(case, known_bugs=known_bugs) if known_bugs != case.known_bugs else case


def select_known(q, syntax, rewrites, data_model, schema, instance):
    return None


def write_known(q, syntax, rewrites, data_model, schema, instance):
    return None
