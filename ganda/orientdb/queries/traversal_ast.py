"""The query IR: WHAT a question asks, separate from HOW each storage model -- and each
of that model's several syntaxes -- renders it.

A `TraversalQuery` is one question: a start class, an optional predicate on the start
record, a chain of one to `MAX_HOPS` hops each with a direction (`out`, `in`, or
`both` -- the latter for a one-hop question over a self-typed edge type, where
`both()` visits the outgoing edges then the incoming ones and a self-loop twice) and
an optional predicate on the hop's relationship or on its target, and a projection
over the final records -- `nexists` being the start records with NO path -- or, for
the four `e*` projections, over the LAST hop's relationships (its attribute as a
multiset, or `max`/`min`/`sum` of it). Predicates are trees (`predicates.Compound`):
`AND` / `OR` / `NOT` over leaves. `syntaxes.render` turns a query into OrientDB SQL
for a (data model, concrete syntax) pair -- `graph:chain`, `graph:match`, `graph:traverse`,
`doc:path`, `rel:let`, `rel:flat`, ... -- and `evaluate` answers it in Python (the
reference leg).

`shape_signature` names the structure alone (hop count, per-hop direction and
predicate placement, source predicate, projection), and `SHAPE_DENOMINATOR` counts what
the grammar admits, so a campaign reports shape coverage with a denominator. The
concrete syntaxes multiply that: every shape is asked through several engine paths per data model.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from ganda.orientdb.model import Instance
from ganda.orientdb.queries import predicates
from ganda.orientdb.queries.case import Comparison
from ganda.orientdb.queries.predicates import Predicate
from ganda.orientdb.semantics import agg, sum_is_exact

DIRECTIONS = ("out", "in", "both")
PLACEMENTS = ("-", "e", "t")
PROJECTIONS = ("ids", "distinct_ids", "count", "distinct_count", "exists", "nexists", "field", "max", "min", "sum", "efield", "emax", "emin", "esum", "reach2", "reach2_count", "reach3", "reach3_count")
# the variable-depth projections: the DISTINCT records within k hops of the start
# along the one hop repeated (the start included, as `TRAVERSE ... MAXDEPTH k` and a
# MATCH `while: ($depth < k)` both answer it), and their count
REACH_PROJECTIONS = ("reach2", "reach2_count", "reach3", "reach3_count")
MAX_HOPS = 3
# projections over the final RECORDS' field
FIELD_PROJECTIONS = ("field", "max", "min", "sum")
AGG_PROJECTIONS = ("max", "min", "sum")
# projections over the last hop's RELATIONSHIPS' attribute
EDGE_PROJECTIONS = ("efield", "emax", "emin", "esum")
EDGE_AGG_PROJECTIONS = ("emax", "emin", "esum")
# the projections that answer a bare id set (the start records with / without a path)
SET_PROJECTIONS = ("exists", "nexists")


@dataclass(frozen=True)
class Hop:
    edge_type: str
    direction: str  # "out" | "in" | "both"
    predicate: "Predicate | None" = None

    @property
    def placement(self) -> str:
        return "-" if self.predicate is None else ("e" if self.predicate.on == "edge" else "t")

    @property
    def edge_predicate(self):
        return self.predicate if self.predicate is not None and self.predicate.on == "edge" else None

    @property
    def target_predicate(self):
        return self.predicate if self.predicate is not None and self.predicate.on == "target" else None


@dataclass(frozen=True)
class TraversalQuery:
    source_table: str
    hops: tuple
    projection: str
    field: "str | None" = None
    source_predicate: "Predicate | None" = None

    @property
    def predicates(self) -> list:
        out = [self.source_predicate] if self.source_predicate else []
        return out + [h.predicate for h in self.hops if h.predicate]

    @property
    def bind_vars(self) -> dict:
        out: dict = {}
        for p in self.predicates:
            out.update(p.bind_vars)
        return out

    @property
    def reference(self) -> bool:
        return all(p.reference for p in self.predicates)

    @property
    def edge_projection(self) -> bool:
        return self.projection in EDGE_PROJECTIONS

    @property
    def has_both(self) -> bool:
        return any(h.direction == "both" for h in self.hops)

    @property
    def reach_depth(self) -> int:
        return 0 if self.projection not in REACH_PROJECTIONS else int(self.projection[5])

    @property
    def comparison(self) -> Comparison:
        if self.projection in ("ids", "field", "efield"):
            return Comparison.MULTISET_BY_ID
        if self.projection in ("distinct_ids", "reach2", "reach3"):
            return Comparison.SET_BY_ID
        if self.projection in SET_PROJECTIONS:
            return Comparison.NESTED_VALUE
        return Comparison.SCALAR_BY_ID

    def to_params(self) -> dict:
        return {
            "source_table": self.source_table,
            "source_predicate": self.source_predicate.to_dict() if self.source_predicate else None,
            "hops": [{"edge_type": h.edge_type, "direction": h.direction, "predicate": h.predicate.to_dict() if h.predicate else None} for h in self.hops],
            "projection": self.projection,
            "field": self.field,
        }


def from_params(d: dict) -> TraversalQuery:
    return TraversalQuery(
        source_table=d["source_table"],
        hops=tuple(Hop(h["edge_type"], h["direction"], Predicate.from_dict(h["predicate"])) for h in d["hops"]),
        projection=d["projection"],
        field=d.get("field"),
        source_predicate=Predicate.from_dict(d.get("source_predicate")),
    )


# --- shape coverage --------------------------------------------------------------------------


def shape_signature(q: TraversalQuery) -> str:
    hops = ",".join(f"{h.direction}:{h.placement}" for h in q.hops)
    return f"src={'f' if q.source_predicate else '-'} hops={len(q.hops)}[{hops}] proj={q.projection}"


def all_signatures(max_hops: int = MAX_HOPS) -> list:
    """Every shape the grammar admits: `both` is a one-hop direction only; a reach
    projection is one unpredicated `out` hop repeated."""
    out = []
    for n in range(1, max_hops + 1):
        dirs = DIRECTIONS if n == 1 else ("out", "in")
        for combo in itertools.product([f"{d}:{p}" for d in dirs for p in PLACEMENTS], repeat=n):
            for src in ("-", "f"):
                for proj in PROJECTIONS:
                    if proj in REACH_PROJECTIONS and combo != ("out:-",):
                        continue
                    out.append(f"src={src} hops={n}[{','.join(combo)}] proj={proj}")
    return out


SHAPE_DENOMINATOR = len(all_signatures())


# --- the reference leg -----------------------------------------------------------------------


def _hit(values: dict, p) -> bool:
    return predicates.evaluate(values, p)


def _edges(instance: Instance, hop: Hop, cur: str) -> list:
    if hop.direction == "out":
        return [(e, e.to_record) for e in instance.outgoing(hop.edge_type, cur)]
    if hop.direction == "in":
        return [(e, e.from_record) for e in instance.incoming(hop.edge_type, cur)]
    # both: the outgoing edges then the incoming ones; a self-loop is in both lists
    return [(e, e.to_record) for e in instance.outgoing(hop.edge_type, cur)] + [(e, e.from_record) for e in instance.incoming(hop.edge_type, cur)]


def final_paths(q: TraversalQuery, instance: Instance, start: str) -> list:
    """Every path from `start` as `(final record id, last relationship)` -- every path
    once, so parallel relationships repeat."""
    frontier = [(start, None)]
    for hop in q.hops:
        nxt = []
        for cur, _ in frontier:
            for e, target in _edges(instance, hop, cur):
                p = hop.predicate
                if p is not None and p.on == "edge" and not _hit(e.values, p):
                    continue
                if p is not None and p.on == "target":
                    node = instance.node_by_id(target)
                    if node is None or not _hit(node.values, p):
                        continue
                nxt.append((target, e))
        frontier = nxt
    return frontier


def final_vertices(q: TraversalQuery, instance: Instance, start: str) -> list:
    """The multiset of final-record ids reached from `start`."""
    return [t for t, _ in final_paths(q, instance, start)]


def _projected_values(q: TraversalQuery, instance: Instance, paths: list) -> list:
    if q.edge_projection:
        return [e.values.get(q.field) for _, e in paths]
    return [(instance.node_by_id(t).values if instance.node_by_id(t) else {}).get(q.field) for t, _ in paths]


def projected_type(q: TraversalQuery, instance: Instance) -> "str | None":
    """The `FieldType.value` of the projected field (the last hop's edge attribute, or
    the final class's field)."""
    if q.field is None:
        return None
    schema = instance.schema
    last = q.hops[-1]
    if q.edge_projection:
        return schema.field(last.edge_type, q.field, edge=True).type.value
    et = schema.edge_type(last.edge_type)
    if last.direction == "both":
        tgt = et.to_type
    else:
        tgt = et.to_type if last.direction == "out" else et.from_type
    return schema.field(tgt, q.field).type.value


def reach(q: TraversalQuery, instance: Instance, start: str) -> list:
    """The distinct records within `q.reach_depth` hops of `start` along the hop's
    edge type, the start included."""
    seen = {start}
    frontier = [start]
    for _ in range(q.reach_depth):
        nxt = []
        for cur in frontier:
            for _e, target in _edges(instance, q.hops[0], cur):
                if target not in seen:
                    seen.add(target)
                    nxt.append(target)
        frontier = nxt
    return sorted(seen)


def evaluate(q: TraversalQuery, instance: Instance) -> Any:
    """{start id: value} for row-keyed projections; a sorted id list for `exists` /
    `nexists`."""
    out: dict = {}
    bare: list = []
    if q.projection in REACH_PROJECTIONS:
        for n in instance.nodes_of(q.source_table):
            if q.source_predicate is not None and not _hit(n.values, q.source_predicate):
                continue
            r = reach(q, instance, n.record_id)
            out[n.record_id] = r if q.projection.endswith(("2", "3")) else len(r)
        return out
    ftype = projected_type(q, instance) if q.projection in AGG_PROJECTIONS or q.projection in EDGE_AGG_PROJECTIONS else None
    for n in instance.nodes_of(q.source_table):
        if q.source_predicate is not None and not _hit(n.values, q.source_predicate):
            continue
        paths = final_paths(q, instance, n.record_id)
        ids = [t for t, _ in paths]
        if q.projection in SET_PROJECTIONS:
            if bool(ids) == (q.projection == "exists"):
                bare.append(n.record_id)
            continue
        if q.projection == "ids":
            v: Any = ids
        elif q.projection == "distinct_ids":
            v = sorted(set(ids))
        elif q.projection == "count":
            v = len(ids)
        elif q.projection == "distinct_count":
            v = len(set(ids))
        else:
            values = _projected_values(q, instance, paths)
            v = values if q.projection in ("field", "efield") else agg(q.projection.lstrip("e") if q.edge_projection else q.projection, values, ftype)
        out[n.record_id] = v
    if q.projection in SET_PROJECTIONS:
        return sorted(bare)
    return out


def sums_exactly(q: TraversalQuery, instance: Instance) -> bool:
    ftype = projected_type(q, instance)
    for n in instance.nodes_of(q.source_table):
        vals = _projected_values(q, instance, final_paths(q, instance, n.record_id))
        if not sum_is_exact([v for v in vals if v is not None], ftype):
            return False
    return True
