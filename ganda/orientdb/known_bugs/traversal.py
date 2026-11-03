"""Guards on the TRAVERSAL IR: the traversal shapes of the filed defects and
`mark_known_bugs`, the entry the traversal generator calls.
"""
from __future__ import annotations

import dataclasses
from ganda.core.data.model import FieldType, Instance, Schema
from ganda.core.query.ir import predicates
from ganda.core.oracle.case import QueryCase
from ganda.core.query.ir.traversal import REACH_PROJECTIONS, TraversalQuery
from ganda.orientdb.concrete_syntax.query.predicate_sampling.predicate_steering import evaluate
from ganda.orientdb.concrete_syntax.query.frames import BRACKET, CORRELATED
from ganda.orientdb.rules import works_around
from ganda.orientdb.known_bugs.predicate import _any_indexed, _ci_column, _int32, _leaves, list_ops_known, node_case_known, o10_predicate, o13_predicate, o16_predicate, o17_predicate, o20_predicate, o21_predicate, o27_predicate, o36_predicate, o39_predicate, o43_predicate, o7_predicate


def _flat_has_correlated_let(q: TraversalQuery) -> bool:
    return len(q.hops) > 1 or q.projection in ("field", "max", "min", "sum")


works_around("O1")



def o1_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if data_model == "rel" and syntax == "flat":
        # the junction's FK columns are always indexed, so `near IN (SELECT id ...)` /
        # `far IN (SELECT id ...)` is index-served; a correlated LET next to it reads
        # a null enclosing row (probed: `v: null` on every row)
        first = q.hops[0]
        ep = first.edge_predicate
        predicated = q.source_predicate is not None or first.target_predicate is not None or (ep is not None and _any_indexed(schema, first.edge_type, ep, data_model, edge=True))
        return predicated and _flat_has_correlated_let(q)
    if (data_model, syntax) not in CORRELATED:
        return False
    p = q.source_predicate
    if p is None:
        return False
    return _any_indexed(schema, q.source_table, p, data_model)


works_around("O2")


def o2_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    """Which correlated subqueries the concrete syntax emits, and whether one of them carries a
    comparison on an indexed field next to its `$parent.$current` equality. `rel:flat`
    renders its first hop uncorrelated but nests `_rel_sub` for the hops after it."""
    flat = data_model == "rel" and syntax == "flat"
    if (data_model, syntax) not in CORRELATED and not flat:
        return False
    for k, h in enumerate(q.hops):
        if flat and k == 0:
            continue
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        ep, tp = h.edge_predicate, h.target_predicate
        if data_model == "rel":
            # the junction subquery: `near = $parent... AND <edge predicate>`
            if ep is not None and _any_indexed(schema, h.edge_type, ep, data_model, edge=True):
                return True
            # the target lookup: `id = $parent... AND <target predicate>`
            if tp is not None and _any_indexed(schema, tgt, tp, data_model):
                return True
        if data_model == "doc" and syntax in ("inpath", "union"):
            # `WHERE e CONTAINS (target = $parent...) AND <target predicate>` over the source class
            if tp is not None and _any_indexed(schema, tgt, tp, data_model):
                return True
    return False


def o7_data_models(q: TraversalQuery, schema: Schema) -> set:
    data_models: set = set()
    if o7_predicate(schema, q.source_table, q.source_predicate):
        data_models |= {"doc", "graph", "rel"}
    data_models |= {s for s in ("doc", "graph", "rel") if list_ops_known(schema, q.source_table, q.source_predicate, s, servable=False)}
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o7_predicate(schema, tgt, h.target_predicate):
            data_models |= {"doc", "graph", "rel"}
        data_models |= {s for s in ("doc", "graph", "rel") if list_ops_known(schema, tgt, h.target_predicate, s, servable=False)}
        if o7_predicate(schema, h.edge_type, h.edge_predicate, edge=True):
            data_models |= {"graph", "rel"}
        data_models |= {s for s in ("graph", "rel") if list_ops_known(schema, h.edge_type, h.edge_predicate, s, edge=True, servable=False)}
    return data_models


def _mixed_int_widths(values) -> bool:
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    return len({_int32(v) for v in ints}) > 1


def o36_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o36_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o36_predicate(schema, tgt, h.target_predicate, data_model):
            return True
    return False


def o10_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o10_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o10_predicate(schema, tgt, h.target_predicate, data_model) or o10_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


def o20_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o20_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o20_predicate(schema, tgt, h.target_predicate, data_model) or o20_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


_CI_OPS = ("=", "<>", "<", "<=", ">", ">=")


def o21_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o21_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o21_predicate(schema, tgt, h.target_predicate, data_model):
            return True
    return False


def o27_shape(q: TraversalQuery, schema: Schema) -> bool:
    if o27_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o27_predicate(schema, tgt, h.target_predicate):
            return True
    return False


works_around("O25")


def o25_predicate(schema: Schema, table: str, p) -> bool:
    return any(leaf.op in _CI_OPS and _ci_column(schema, table, leaf.field) for leaf in _leaves(p))


def o25_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if (data_model, syntax) not in BRACKET:
        return False
    if syntax == "edgescan" and o25_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o25_predicate(schema, tgt, h.target_predicate):
            return True
    return False


def o16_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o16_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o16_predicate(schema, tgt, h.target_predicate, data_model) or o16_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


def o17_shape(q: TraversalQuery, schema: Schema) -> bool:
    if o17_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o17_predicate(schema, tgt, h.target_predicate) or o17_predicate(schema, h.edge_type, h.edge_predicate, edge=True):
            return True
    return False


def o43_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    return data_model == "doc" and any(o43_predicate(schema, h.edge_type, h.edge_predicate) for h in q.hops if h.edge_predicate is not None)


works_around("O44")


def o44_dfs_reach(q: TraversalQuery, instance: Instance) -> bool:
    """O44 (`issue-drafts/upstream/O44-...`): `TRAVERSE ... MAXDEPTH k STRATEGY
    DEPTH_FIRST` visits each record once, at the depth it first MEETS it, and does
    not expand a record met at the bound -- so a record reached first by a longer
    walk and again by a shorter one keeps its children unvisited, and the reach set
    misses records within k hops (probed: 11 against BREADTH_FIRST's 12, cov400 seed
    5342). A record is marked when it is PUSHED and the last neighbour is expanded
    first, so the start's own neighbours are always marked at depth 1 (probed). The
    shape (conservative, any neighbour order): some start reaches a record at
    distance >= 2 that has an out-edge by a walk of length L, `dist < L <= k`."""
    if q.projection not in REACH_PROJECTIONS:
        return False
    et, k = q.hops[0].edge_type, q.reach_depth
    out: dict = {}
    for e in instance.edges_of(et):
        out.setdefault(e.from_record, []).append(e.to_record)
    for n in instance.nodes_of(q.source_table):
        dist = {n.record_id: 0}
        layer = {n.record_id}
        for depth in range(1, k + 1):
            layer = {w for u in layer for w in out.get(u, ())}
            for w in layer:
                if w in dist and 2 <= dist[w] < depth and out.get(w):
                    return True
                dist.setdefault(w, depth)
    return False


def o13_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc":
        return False
    return any(o13_predicate(instance, h.edge_type, h.edge_predicate) for h in q.hops if h.edge_predicate is not None)


works_around("O19")


def o19_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc" or q.projection != "esum":
        return False
    et = q.hops[-1].edge_type
    if schema.field(et, q.field, edge=True).type not in (FieldType.INT, FieldType.INT32):
        return False
    from ganda.core.query.ir.traversal import _projected_values, final_paths

    for n in instance.nodes_of(q.source_table):
        if q.source_predicate is not None and not evaluate(n.values, q.source_predicate):
            continue
        vals = [v for v in _projected_values(q, instance, final_paths(q, instance, n.record_id)) if v is not None]
        if vals and all(_int32(v) for v in vals) and not _int32(sum(vals)):
            return True
    return False


works_around("O15")


def o15_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc" or q.projection not in ("emax", "emin"):
        return False
    et = q.hops[-1].edge_type
    if schema.field(et, q.field, edge=True).type is not FieldType.INT:
        return False
    return _mixed_int_widths([e.values.get(q.field) for e in instance.edges_of(et)])


works_around("O8")


def o8_shape(q: TraversalQuery, data_model: str, syntax: str) -> bool:
    """`edgescan` renders both the target predicate and the source predicate through
    the edge's links (`in.f`, `out.f`)."""
    if data_model != "graph" or syntax != "edgescan":
        return False
    return any(predicates.has_op(p, ("IS PRESENT", "IS ABSENT")) for p in (q.hops[0].target_predicate, q.source_predicate) if p is not None)


works_around("O9")


def o9_shape(q: TraversalQuery, data_model: str, syntax: str) -> bool:
    if (data_model, syntax) not in (("doc", "path"), ("graph", "bag")):
        return False
    return any(h.predicate is not None for h in q.hops[1:])


def o39_shape(q: TraversalQuery, base: str, syntax: str) -> bool:
    """A LET-subquery concrete syntax, or `rel:flat`'s `IN (SELECT id ...)`, holding a
    CONTAINSVALUE leaf in any of the question's predicates (conservative: the leaf
    may sit outside the subquery)."""
    if (base, syntax) not in CORRELATED and (base, syntax) != ("rel", "flat"):
        return False
    return any(o39_predicate(p) for p in q.predicates)


def _data_model_known(q: TraversalQuery, schema: Schema, instance: Instance, base: str, syntax: str) -> "str | None":
    if o39_shape(q, base, syntax):
        return "O39"
    if o1_shape(q, schema, base, syntax):
        return "O1"
    if o2_shape(q, schema, base, syntax):
        return "O2"
    if o8_shape(q, base, syntax):
        return "O8"
    if o10_shape(q, schema, base):
        return "O10"
    if o36_shape(q, schema, base):
        return "O36"
    if o16_shape(q, schema, base):
        return "O16"
    if o20_shape(q, schema, base):
        return "O20"
    if o17_shape(q, schema):
        return "O17"
    if o27_shape(q, schema):
        return "O27"
    if o21_shape(q, schema, base):
        return "O21"
    if o25_shape(q, schema, base, syntax):
        return "O25"
    if o43_shape(q, schema, base):
        return "O43"
    if base == "graph" and syntax == "traverse_dfs" and o44_dfs_reach(q, instance):
        return "O44"
    if o13_shape(q, schema, instance, base):
        return "O13"
    if o15_shape(q, schema, instance, base):
        return "O15"
    if o19_shape(q, schema, instance, base):
        return "O19"
    if o9_shape(q, base, syntax):
        return "O9"
    return None


works_around("O26")


def mark_known_bugs(case: QueryCase, q: TraversalQuery, schema: Schema, instance: Instance) -> QueryCase:
    from ganda.orientdb.known_bugs import discard_fixed

    known_bugs = dict(case.known_bugs)
    o7 = o7_data_models(q, schema)
    q_pulled_up = dataclasses.replace(q, source_predicate=None)
    pulled_up = set(case.params.get("pulled_up", ()))
    for data_model, syntax in case.params.get("concrete_syntax", {}).items():
        base = data_model.split(":", 1)[0]
        if base in o7:
            if discard_fixed("O7") is not None:
                known_bugs[data_model] = "O7"
            continue
        fid = _data_model_known(q_pulled_up if data_model in pulled_up else q, schema, instance, base, syntax)
        if fid is None and data_model in pulled_up and base == "rel" and syntax == "flat" and q.projection != "nexists" and q.source_predicate is not None:
            # the flat pull-up keeps the source predicate in `id IN (SELECT id FROM
            # src WHERE p)`, a query over the source class an index may serve: its
            # index findings apply (cov400 seeds 5120 / 5203, O16's truncation and
            # wrap on the plan the pull-up was meant to avoid)
            fid = node_case_known(q.source_table, q.source_predicate, schema, instance).get(base)
        if fid is None and data_model in pulled_up and base == "rel" and syntax == "flat" and q.has_both and (q.projection in ("field", "max", "min", "sum") or q.hops[0].target_predicate is not None):
            # the inline union's parts carry a subquery (a nested LET, or a target
            # predicate's `IN (SELECT ...)`) under the pull-up's `IN (subquery)` filter
            fid = "O26"
        fid = discard_fixed(fid)
        if fid:
            known_bugs[data_model] = fid
    return dataclasses.replace(case, known_bugs=known_bugs) if known_bugs != case.known_bugs else case
