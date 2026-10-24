"""
A `TraversalQuery` is one question: a start class, a chain of one to `MAX_HOPS` hops 
with a direction and a projection over the final records
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from ganda.core.data.model import Instance
from ganda.core.query.ir import predicates
from ganda.core.oracle.case import Comparison
from ganda.core.query.ir.predicates import Predicate

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


# --- walking the Instance (the generators' guards) ------------------------------------------


def _hit(instance: Instance, values: dict, p) -> bool:
    return predicates.evaluate(values, p, instance.schema.engine.semantics)


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
                if p is not None and p.on == "edge" and not _hit(instance, e.values, p):
                    continue
                if p is not None and p.on == "target":
                    node = instance.node_by_id(target)
                    if node is None or not _hit(instance, node.values, p):
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


# the most paths `sums_exactly` enumerates before declining: far above what any
# default-size instance holds, far below what a hub of thousands of edges fans out to
# over two or three hops (where the engine's own answer would be as large)
MAX_PATHS = 200_000


def path_count_bound(q: TraversalQuery, instance: Instance) -> int:
    """How many paths the hops reach over every source when no predicate prunes them
    (an upper bound on what `final_paths` enumerates), counted hop by hop rather than
    listed: the cost is the hops times the edges."""
    counts = {n.record_id: 1 for n in instance.nodes_of(q.source_table)}
    for hop in q.hops:
        nxt: dict = {}
        for cur, c in counts.items():
            for _e, target in _edges(instance, hop, cur):
                nxt[target] = nxt.get(target, 0) + c
        counts = nxt
    return sum(counts.values())


def sums_exactly(q: TraversalQuery, instance: Instance) -> bool:
    """Whether every source's sum is exact under the engine's semantics -- shown by
    enumerating the paths; a question with more than `MAX_PATHS` of them is not shown
    exact (the generator asks for `max` instead)."""
    if path_count_bound(q, instance) > MAX_PATHS:
        return False
    ftype = projected_type(q, instance)
    for n in instance.nodes_of(q.source_table):

        vals = _projected_values(q, instance, final_paths(q, instance, n.record_id))
        if not instance.schema.engine.semantics.sum_is_exact([v for v in vals if v is not None], ftype):
            return False
    return True
