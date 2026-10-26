"""Guards on the TRAVERSAL IR: `mark_known_bugs`, the entry the traversal generator
calls. A variant is marked when the source predicate or a hop's predicate has a known
defect's shape on its data model (`predicate.node_case_known` / `edge_case_known`); a
pulled-up variant filters its source predicate over a projection (never through an
index), so only its hop predicates are judged."""
from __future__ import annotations

import dataclasses

from ganda.core.data.model import Instance, Schema
from ganda.core.oracle.case import QueryCase
from ganda.core.query.ir.traversal import TraversalQuery
from ganda.surrealdb.known_bugs.predicate import edge_case_known, node_case_known


def _data_model_known(q: TraversalQuery, schema: Schema, instance: Instance, pulled_up: bool) -> dict:
    known: dict = {}
    if q.source_predicate is not None and not pulled_up:
        known.update(node_case_known(q.source_table, q.source_predicate, schema, instance))
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        if h.edge_predicate is not None:
            for dm, fid in edge_case_known(h.edge_type, h.edge_predicate, schema, instance).items():
                known.setdefault(dm, fid)
        if h.target_predicate is not None:
            tgt = et.to_type if h.direction != "in" else et.from_type
            for dm, fid in node_case_known(tgt, h.target_predicate, schema, instance).items():
                known.setdefault(dm, fid)
    return known


def mark_known_bugs(case: QueryCase, q: TraversalQuery, schema: Schema, instance: Instance) -> QueryCase:
    known = dict(case.known_bugs)
    plain = _data_model_known(q, schema, instance, pulled_up=False)
    lifted = _data_model_known(q, schema, instance, pulled_up=True)
    for variant in case.params.get("concrete_syntax", {}):
        judged = lifted if variant in case.params.get("pulled_up", []) else plain
        fid = judged.get(variant.split(":")[0])
        if fid:
            known[variant] = fid
    return dataclasses.replace(case, known_bugs=known) if known != case.known_bugs else case
