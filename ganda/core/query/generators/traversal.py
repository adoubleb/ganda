"""Samples `TraversalQuery` shapes from the grammar the schema admits and builds
their cases: the `ir_traversal` query generator.

The grammar: a start class that some edge type leaves or reaches; one to `MAX_HOPS`
hops, each an edge type that chains from the current class (outgoing: its `from_type`
is the current class; incoming: its `to_type` is; `both`, with `BOTH_PROBABILITY`
over a self-typed edge type as the only hop), each with a predicate placement drawn
uniformly from none / edge / target and realized only where the schema has a field
there and the column has values; a source predicate with probability 0.3; a projection
drawn uniformly, with the field kinds falling back to `ids` when the final class has
no eligible field (`field` takes any scalar field, `max`/`min` an ordered one, `sum`
one whose multisets sum exactly) and the four edge kinds (`efield`/`emax`/`emin`/
`esum`, over the last hop's relationship attribute) likewise over the last edge type's
fields -- an edge projection drops the last hop's target predicate.

A sampled query is kept only if EVERY data model has at least one applicable concrete
syntax. Each data model draws one of them as its base text and every other applicable
one becomes a variant `<data model>:<syntax>`, plus the base syntax with the source
predicate PULLED UP into an enclosing query as `<data model>:pull_up`, so one question
runs through every engine path the provider's catalog knows.

Every draw comes from this query generator's own sub-stream, `CASES_PER_INSTANCE`
queries per instance (deduplicated on the rendered texts and bind values).
"""
from __future__ import annotations

import random

from ganda.core.data.model import INT_TYPES, ORDERED_TYPES, FieldType, Instance, Schema
from ganda.core.query.generators import predicates as sampler
from ganda.core.oracle.case import QueryCase
from ganda.core.query.ir.traversal import EDGE_PROJECTIONS, MAX_HOPS, PLACEMENTS, PROJECTIONS, REACH_PROJECTIONS, Hop, TraversalQuery, shape_signature, sums_exactly

CASES_PER_INSTANCE = 10
SOURCE_PREDICATE_PROBABILITY = 0.3
BOTH_PROBABILITY = 0.25
REACH_PROBABILITY = 0.15
MAX_VARIANTS_PER_SIDE = 3


def _edge_values(instance: Instance, edge_type: str, field_name: str) -> list:
    return [e.values.get(field_name) for e in instance.edges_of(edge_type) if field_name in e.values]


def _node_values(instance: Instance, table: str, field_name: str) -> list:
    return [n.values.get(field_name) for n in instance.nodes_of(table) if field_name in n.values]


def _pick_field(rng: random.Random, fields, projection: str) -> "str | None":
    kind = projection[1:] if projection in EDGE_PROJECTIONS else projection
    if kind == "field":
        pool = [f for f in fields if f.type is not FieldType.LIST]
    elif kind in ("max", "min"):
        pool = [f for f in fields if f.type in ORDERED_TYPES]
    elif kind == "sum":
        pool = [f for f in fields if f.type in INT_TYPES or f.type is FieldType.DECIMAL]
    else:
        return None
    return rng.choice(pool).name if pool else None


def _draw_hops(rng: random.Random, schema: Schema, instance: Instance, start: str, max_hops: int) -> tuple:
    """A chain of one to `max_hops` hops from `start`, each leaving the previous hop's
    target along any edge type touching it (`out` or `in`; a first hop over a
    self-typed edge type may be `both`, which ends the chain), with an optional edge
    or target predicate -- and the class the chain ends at. No hops when `start`
    touches no edge type."""
    cur, hops = start, []
    for k in range(1, rng.randint(1, max_hops) + 1):
        options = [(et, "out") for et in schema.edge_types_from(cur)] + [(et, "in") for et in schema.edge_types_to(cur)]
        if not options:
            break
        et, direction = rng.choice(options)
        if k == 1 and et.from_type == et.to_type and rng.random() < BOTH_PROBABILITY:
            direction = "both"
        nxt = et.to_type if direction != "in" else et.from_type
        placement = rng.choice(PLACEMENTS)
        pred = None
        # `draw_over` picks the field and calls `values_of(field)` for its stored
        # values (the pivots are drawn from them)
        if placement == "e" and et.fields:

            def edge_values(f):
                return _edge_values(instance, et.name, f.name)

            pred = sampler.draw_over(rng, "edge", et.fields, values_of=edge_values, bind=f"p{k}", engine=schema.engine)
        elif placement == "t" and schema.query_fields(nxt):

            def target_values(f):
                return _node_values(instance, nxt, f.name)

            pred = sampler.draw_over(rng, "target", schema.query_fields(nxt), values_of=target_values, bind=f"p{k}", engine=schema.engine)
        hops.append(Hop(et.name, direction, pred))
        cur = nxt
        if direction == "both":
            break  # `both` is a one-hop question
    return hops, cur


def _draw_source_predicate(rng: random.Random, schema: Schema, instance: Instance, table: str):
    """A predicate on the start record (bound under `p0`), with probability
    `SOURCE_PREDICATE_PROBABILITY`; None when `table` has no fields to filter."""
    fields = schema.query_fields(table)
    if not fields or rng.random() >= SOURCE_PREDICATE_PROBABILITY:
        return None

    def source_values(f):
        return _node_values(instance, table, f.name)

    return sampler.draw_over(rng, "source", fields, values_of=source_values, bind="p0", engine=schema.engine)


def _reach_ok(schema: Schema, hops: list) -> bool:
    """Whether a chain can be asked as a reach question: one outgoing hop over a
    self-typed edge type, so the hop can be repeated from every record it reaches
    (the doc data model follows links `out` only)."""
    et = schema.edge_type(hops[0].edge_type)
    return len(hops) == 1 and hops[0].direction == "out" and et.from_type == et.to_type


def _draw_projection_field(rng: random.Random, schema: Schema, hops: list, cur: str, projection: str) -> tuple:
    """The field a projection reads -- the last hop's relationship attribute for an
    `e*` projection (which drops that hop's target predicate), the final class's
    field for `field` / `max` / `min` / `sum` -- as `(projection, field, hops)`. A
    projection that needs a field and finds none falls back to `ids`."""
    hops = list(hops)
    fld = None
    if projection in EDGE_PROJECTIONS:
        fld = _pick_field(rng, schema.edge_type(hops[-1].edge_type).fields, projection)
        if fld is not None and hops[-1].target_predicate is not None:
            hops[-1] = Hop(hops[-1].edge_type, hops[-1].direction, None)
    elif projection in ("field", "max", "min", "sum"):
        fld = _pick_field(rng, [f for f in schema.query_fields(cur) if f.type is not FieldType.MAP], projection)
    if projection not in ("ids", "distinct_ids", "count", "distinct_count", "exists", "nexists") + REACH_PROJECTIONS and fld is None:
        projection = "ids"
    return projection, fld, hops


def sample_query(rng: random.Random, schema: Schema, instance: Instance, max_hops: int = MAX_HOPS) -> "TraversalQuery | None":
    src = rng.choice(starts) if (starts := [nt for nt in schema.node_types if schema.edge_types_from(nt.name) or schema.edge_types_to(nt.name)]) else None
    if src is None:
        return None
    
    hops, cur = _draw_hops(rng, schema, instance, src.name, max_hops)
    if not hops:
        return None
    
    source_pred = _draw_source_predicate(rng, schema, instance, src.name)
    
    if _reach_ok(schema, hops) and rng.random() < REACH_PROBABILITY:
        projection = rng.choice(REACH_PROJECTIONS)
        hops = [Hop(hops[0].edge_type, "out", None)]
    else:
        projection = rng.choice([p for p in PROJECTIONS if p not in REACH_PROJECTIONS])
    projection, fld, hops = _draw_projection_field(rng, schema, hops, cur, projection)
    q = TraversalQuery(src.name, tuple(hops), projection, fld, source_pred)
    if projection in ("sum", "esum") and not sums_exactly(q, instance):
        q = TraversalQuery(src.name, tuple(hops), "emax" if projection == "esum" else "max", fld, source_pred)
    return q


def _aggregates_exactly(q: TraversalQuery, schema: Schema, instance: Instance) -> bool:
    """Whether the semantics reproduce a `max` / `min` projection over the projected
    field's type (`Semantics.aggregates_exactly`)."""
    if q.projection not in ("max", "min", "emax", "emin"):
        return True
    from ganda.core.query.ir.traversal import projected_type

    return schema.engine.semantics.aggregates_exactly(q.projection.lstrip("e"), FieldType(projected_type(q, instance)))


def build_case(q: TraversalQuery, rng: random.Random, schema: Schema, instance: Instance) -> "QueryCase | None":
    syn = schema.engine.traversal_syntax
    queries: dict = {}
    chosen: dict = {}
    pulled_up: list = []
    skipped: list = []
    for data_model in schema.engine.data_models:
        options = syn.syntaxes_for(q, schema, data_model)
        if not options:
            # a data model with no syntax for the question (say, a LIST membership its
            # views cannot ask in a traversal) leaves the case
            # PARTIAL where the provider says so (`Provider.partial_traversals`), as a
            # select does: the others still compare among themselves and with the
            # reference; otherwise the question is dropped on every data model
            if not getattr(schema.engine, "partial_traversals", False):
                return None
            skipped.append(data_model)
            continue
        base = rng.choice(options)
        chosen[data_model] = base
        queries[data_model] = syn.render(q, schema, data_model, base)
        others = [s for s in options if s != base]
        rng.shuffle(others)
        for sp in others[:MAX_VARIANTS_PER_SIDE]:
            queries[f"{data_model}:{sp}"] = syn.render(q, schema, data_model, sp)
            chosen[f"{data_model}:{sp}"] = sp
        if syn.can_pull_up(q):
            queries[f"{data_model}:pull_up"] = syn.render(q, schema, data_model, base, pull_up=True)
            chosen[f"{data_model}:pull_up"] = base
            pulled_up.append(f"{data_model}:pull_up")
    if not queries:
        return None
    params = {
        "shape": shape_signature(q),
        "traversal": q.to_params(),
        "reference": q.reference and _aggregates_exactly(q, schema, instance),
        "projection": q.projection,
        "field": q.field,
        "concrete_syntax": chosen,
        "pulled_up": pulled_up,
        "predicates": [p.signature for p in q.predicates],
    }
    if skipped:
        params["partial"] = True
    case = QueryCase(
        generator_name="ir_traversal",
        edge_type=q.hops[0].edge_type,
        comparison=q.comparison,
        queries=queries,
        bind_vars=q.bind_vars,
        params=params,
        shape=shape_signature(q),
        engine=schema.engine,
    )
    return schema.engine.known_bugs.mark_known_bugs(case, q, schema, instance)


def generate_traversal_cases(rng: random.Random, schema: Schema, instance: Instance, per_instance: int = CASES_PER_INSTANCE) -> list:
    cases, seen = [], set()
    for _ in range(per_instance * 4):
        if len(cases) >= per_instance:
            break
        q = sample_query(rng, schema, instance)
        if q is None:
            break
        case = build_case(q, rng, schema, instance)
        if case is None:
            continue
        key = (*(case.queries.get(dm, "") for dm in schema.engine.data_models), repr(sorted(case.bind_vars.items(), key=lambda kv: kv[0])))
        if key in seen:
            continue
        seen.add(key)
        cases.append(case)
    return cases
