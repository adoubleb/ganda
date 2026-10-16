"""Samples `SelectQuery` shapes and builds their cases through the provider's concrete
syntax: the `ir_select` query generator.

The grammar: a class with records (a subclass or, on the inheritance axis, the base
class); a filter with `WHERE_PROBABILITY` -- a predicate tree over the class's fields
(`generators.predicates.draw_over`), or with `COMPOSITE_PROBABILITY` an AND over a composite
index's fields -- else none; a kind drawn uniformly from `select.KINDS`, its fields and
function drawn among the columns that admit them, judged over the records the filter
keeps (`sum` only where every partial sum stays exact; `avg` / `median` over a present
integer column; `max` / `min` over an ordered column; a distinct count over a present
one -- the function drawn first, then a column that admits it, else `sum` falls back to
`max` and the others to `count`; a method only where its mirror is exact on every
matching value). A kind the class cannot realize falls back to `ids`.

A filter is, with `INSUB_PROBABILITY`, a semi-join leaf, alone or joined to a drawn
tree. A `join` question correlates the class with itself on one field. With
`tx_probability` a node question is read through the transaction door
(`support.draw_tx`: every kind but `join`).

The supply is STRATIFIED. First a COVERAGE TIER: one `ids` question per target
(every node class with records and fields, every edge type with records and
attributes -- an edge type twice: listed whole, unfiltered, and filtered on its
attributes) and two unfiltered `keys` orderings per node class led by a field an
index may walk, one ASC and one DESC, in a shuffled order -- every class and edge
type asked every round, which a sample from a deck of hundreds of strata cannot
promise. Then the targets crossed with the kinds they admit make a deck -- the filter
kinds weighed up (`KIND_WEIGHTS`) -- dealt in a shuffled order and reshuffled when
spent.

A case is the base concrete syntax on every data model plus every single-axis syntax
that applies and up to `MAX_COMBOS` syntaxes combining two or more axes, drawn once for
all data models, so every variant is also compared across databases. Every draw comes
from this query generator's own sub-stream; `CASES_PER_INSTANCE` queries per instance,
deduplicated on the base text and bind values, prefix-stable (the first n of a larger
sample are the n).
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.model import INT_TYPES, NUMERIC_TYPES, ORDERED_TYPES, SCALAR_TYPES, FieldType, Instance, Schema
from ganda.core.ast import predicates
from ganda.core.generators import predicates as sampler
from ganda.core.case import QueryCase
from ganda.core.ast.select import AGG_FNS, DIRECTIONS, EDGE_KINDS, GROUP_FNS, JOIN_FORMS, JOIN_OPS, KINDS, SelectQuery, coverage_signature, hits, present_values, reference_ok, shape_signature, target_fields
from ganda.core.generators.support import composite_and as _composite_and, draw_tx, tx_binds

CASES_PER_INSTANCE = 30
WHERE_PROBABILITY = 0.7
COMPOSITE_PROBABILITY = 0.3
INSUB_PROBABILITY = 0.3
# how often a node filter draws its leaves from the class's INDEXED fields only, and
# an ordering (`page`, `keys`) orders by an indexed field: the index-vs-scan oracle
# needs a served leaf to see anything, and the mutation benchmark's index mutants
# showed the uniform choice reached one on 0.4 % of cases
INDEX_BIAS = 0.5
TX_PROBABILITY = 0.25
TWO_KEYS_PROBABILITY = 0.4
MAX_COMBOS = 3
# a stratum's weight in the deck: a node class's filter kinds weigh most, and a
# node class's kinds weigh three times an edge type's (the schemas hold more edge types than node classes:
# at one each, edge targets took half the supply)
KIND_WEIGHTS = {"ids": 6, "count": 4}
NODE_WEIGHT = 3
EDGE_WEIGHT = 1


def _indexed(schema: Schema, table: str, fields) -> list:
    """The fields an index of some data model may serve (the lead of a single-field or
    composite index, the base class's and a subclass's included)."""
    return [f for f in fields if any(schema.node_indexes(table, f.name, data_model) for data_model in schema.engine.data_models)]


def _prefer_indexed(rng: random.Random, schema: Schema, table: str, fields, edge: bool) -> list:
    if edge:
        return list(fields)
    ix = _indexed(schema, table, fields)
    return ix if ix and rng.random() < INDEX_BIAS else list(fields)


def _values_of(instance: Instance, table: str, edge: bool = False):
    rows = instance.edges_of(table) if edge else instance.nodes_of(table)
    return lambda f: [n.values.get(f.name) for n in rows if f.name in n.values]


def _draw_where(rng: random.Random, schema: Schema, instance: Instance, table: str, focus: "str | None" = None, edge: bool = False):
    engine = schema.engine
    fields = target_fields(schema, table, edge)
    on = "edge" if edge else "source"
    if focus is not None:
        # a read-back after a write: the filter over the written field
        fields = [f for f in fields if f.name == focus] or fields
        return sampler.draw_over(rng, on, fields, _values_of(instance, table, edge), "p", engine)
    if edge:
        return sampler.draw_over(rng, on, fields, _values_of(instance, table, edge), "p", engine)
    composites = [ix for ix in schema.indexes if ix.target == "node" and ix.extra_fields and ix.table == table]
    if composites and rng.random() < COMPOSITE_PROBABILITY:
        ix = rng.choice(composites)
        return _composite_and(rng, [f for f in fields if f.name in ix.all_fields], _values_of(instance, table), engine)
    if rng.random() < INSUB_PROBABILITY:
        # a semi-join, alone or next to a drawn tree
        if rng.random() < 0.6:
            return sampler.draw_insub(rng, schema, instance, table, "p")
        sub = sampler.draw_insub(rng, schema, instance, table, "p_0")
        other = sampler.draw_over(rng, "source", fields, _values_of(instance, table), "p_1", engine, compound_probability=0.0)
        if sub is None or other is None:
            return sub or other
        return predicates.Compound("source", rng.choice(("AND", "OR")), (sub, other))
    return sampler.draw_over(rng, "source", _prefer_indexed(rng, schema, table, fields, False), _values_of(instance, table), "p", engine)


def _admits(fn: str, f, present: list, sum_is_exact) -> bool:
    """Whether aggregate `fn` has an exact reference over column `f` with these
    present values among the matching records."""
    if fn == "sum":
        return f.type in INT_TYPES + (FieldType.DECIMAL,) and sum_is_exact(present, f.type.value)
    if fn in ("avg", "median"):
        return f.type in INT_TYPES and bool(present) and sum_is_exact(present, f.type.value)
    if fn in ("max", "min"):
        return f.type in ORDERED_TYPES
    if fn == "distinct_count":
        return bool(present)
    return True


def _agg_pick(rng: random.Random, fn: str, pool: list, present, sum_is_exact) -> "tuple[str, object]":
    """(fn, field): the function first, then a column that admits it; with none, the
    fallbacks (`sum` -> `max`, the others -> `count`)."""
    for f_ in (fn, "max" if fn == "sum" else "count", "count"):
        ok = [f for f in pool if _admits(f_, f, present(f), sum_is_exact)]
        if ok:
            return f_, rng.choice(ok)
    return "count", rng.choice(pool)


def _walk_pool(schema: Schema, table: str) -> list:
    """The `keys` fields an index of some data model may walk (numeric or boolean, indexed)."""
    pool = [f for f in schema.query_fields(table) if f.type in NUMERIC_TYPES or f.type is FieldType.BOOL]
    return _indexed(schema, table, pool)


def _realize(rng: random.Random, schema: Schema, instance: Instance, table: str, where, kind: str, edge: bool = False, walk: "str | None" = None) -> "SelectQuery | None":
    _METHODS = schema.engine.select_syntax.METHODS
    sum_is_exact = schema.engine.semantics.sum_is_exact
    fields = list(target_fields(schema, table, edge))
    rows = hits(SelectQuery(table, where, "ids", edge=edge), instance)
    # the keys, join and aggregate columns; a `field` projection takes any column
    scalar = [f for f in fields if f.type in SCALAR_TYPES]

    n = len(rows)
    # every non-default field of the question, so a replace keeps the target
    q0 = SelectQuery(table, where, kind, edge=edge)

    def present(f) -> list:
        return [r.values.get(f.name) for r in rows if r.values.get(f.name) is not None]

    def make(**kw) -> SelectQuery:
        return dataclasses.replace(q0, **kw)

    if kind == "join":
        if edge or not scalar:
            return None
        f = rng.choice(scalar)
        op = rng.choice(JOIN_OPS if f.type in ORDERED_TYPES and "<" in schema.engine.pivots.ordered_ops(f.type) else JOIN_OPS[:2])
        return make(fields=(f.name,), fn=op, arg=rng.choice(JOIN_FORMS))
    if kind == "field":
        return make(fields=(rng.choice(fields).name,))
    if kind == "method":
        f = rng.choice(fields)
        entries = list(_METHODS.get(f.type, ()))
        rng.shuffle(entries)
        values = present(f)
        for template, _fn, guard in entries:
            if all(guard(v) for v in values):
                return make(fields=(f.name,), fn=template)
        return None
    if kind == "agg":
        pool = [f for f in scalar if f.type is not FieldType.BOOL]
        if not pool:
            return None
        fn, f = _agg_pick(rng, rng.choice(AGG_FNS), pool, present, sum_is_exact)
        return make(fields=(f.name,), fn=fn)
    if kind == "group":
        if not scalar:
            return None
        keys = rng.sample(scalar, 2) if len(scalar) >= 2 and rng.random() < TWO_KEYS_PROBABILITY else [rng.choice(scalar)]
        fn = rng.choice(GROUP_FNS)
        others = [f for f in scalar if f not in keys]
        arg = None
        if fn != "count_star":
            if not others:
                fn = "count_star"
            else:
                fn, a = _agg_pick(rng, fn, others, present, sum_is_exact)
                arg = a.name
        return make(fields=tuple(k.name for k in keys), fn=fn, arg=arg)
    if kind == "distinct":
        if not scalar:
            return None
        cols = rng.sample(scalar, 2) if len(scalar) >= 2 and rng.random() < TWO_KEYS_PROBABILITY else [rng.choice(scalar)]
        return make(fields=tuple(f.name for f in cols))
    if kind == "page":
        ordered = [f for f in fields if f.type in ORDERED_TYPES]
        if not ordered:
            return None
        f = rng.choice(_prefer_indexed(rng, schema, table, ordered, edge))
        direction = rng.choice(DIRECTIONS)
        skip = rng.randint(0, max(0, n - 1)) if rng.random() < 0.6 else 0
        limit = rng.randint(1, max(1, n))
        return make(fields=(f.name,), directions=(direction,), skip=skip, limit=limit)
    if kind == "keys":
        # numeric or boolean keys: a string order may collate where an index walks
        # code units
        pool = [f for f in fields if f.type in NUMERIC_TYPES or f.type is FieldType.BOOL]
        if not pool:
            return None
        walkable = _walk_pool(schema, table) if walk and not edge else []
        lead = rng.choice(walkable or _prefer_indexed(rng, schema, table, pool, edge))
        keys = [lead] + ([rng.choice([f for f in pool if f is not lead])] if len(pool) >= 2 and rng.random() < 0.5 else [])
        # a walk tier ordering keeps ONE direction on every key (mixed directions
        # route through a sort): the shape an index walk serves
        dirs = tuple(walk for _ in keys) if walk else tuple(rng.choice(DIRECTIONS) for _ in keys)
        skip = rng.randint(0, max(0, n - 1)) if rng.random() < 0.3 else 0
        limit = rng.randint(1, max(1, n)) if rng.random() < 0.5 else 0
        return make(fields=tuple(k.name for k in keys), directions=dirs, skip=skip, limit=limit)
    return q0


def _targets(schema: Schema, instance: Instance) -> list:
    """(table, edge) for every node class with fields and records and every edge
    type with attributes and records."""
    out = [(nt.name, False) for nt in schema.query_tables() if schema.query_fields(nt.name) and instance.nodes_of(nt.name)]
    out += [(et.name, True) for et in schema.edge_types if et.fields and instance.edges_of(et.name)]
    return out


def sample_select(rng: random.Random, schema: Schema, instance: Instance, table: "str | None" = None, focus: "str | None" = None, kind: "str | None" = None, edge: bool = False, tx_probability: float = 0.0, where_probability: float = WHERE_PROBABILITY, walk: "str | None" = None) -> "SelectQuery | None":
    """A question over a random class, or over `table` (an edge type with `edge`)
    with `kind`, its filter drawn over the field `focus` if given (the write phase's
    read-back of what it wrote); with `tx_probability` read through the
    transaction door."""
    if table is None:
        tables = [nt for nt in schema.query_tables() if schema.query_fields(nt.name) and instance.nodes_of(nt.name)]
        if not tables:
            return None
        table = rng.choice(tables).name
    elif not target_fields(schema, table, edge) or not (instance.edges_of(table) if edge else instance.nodes_of(table)):
        return None
    where = _draw_where(rng, schema, instance, table, focus, edge) if rng.random() < where_probability else None
    kind = kind or rng.choice(EDGE_KINDS if edge else KINDS)
    q = _realize(rng, schema, instance, table, where, kind, edge, walk) or SelectQuery(table, where, "ids", edge=edge)
    if tx_probability and not edge and q.kind != "join" and rng.random() < tx_probability:
        tx = draw_tx(rng, schema, instance, table, where)
        if tx is not None and _tx_admits(dataclasses.replace(q, tx=tx), schema, instance):
            q = dataclasses.replace(q, tx=tx)
    return q


def _tx_admits(q: SelectQuery, schema: Schema, instance: Instance) -> bool:
    """Whether the question's exactness conditions -- judged when it was realized,
    over the records before the write -- still hold over the transaction's view: an
    aggregate's (`_admits`), a method's mirror guard."""
    from ganda.core.ast.select import column

    METHODS = schema.engine.select_syntax.METHODS
    sum_is_exact = schema.engine.semantics.sum_is_exact
    rows = hits(q, instance)

    def present(f) -> list:
        return [r.values.get(f) for r in rows if r.values.get(f) is not None]

    if q.kind == "agg":
        return _admits(q.fn, column(schema, q, q.field), present(q.field), sum_is_exact)
    if q.kind == "group" and q.fn != "count_star":
        return _admits(q.fn, column(schema, q, q.arg), present(q.arg), sum_is_exact)
    if q.kind == "method":
        guard = next(g for t, _fn, g in METHODS[column(schema, q, q.field).type] if t == q.fn)
        return all(guard(v) for v in present(q.field))
    return True


def build_case(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> QueryCase:
    engine = schema.engine
    syn = engine.select_syntax
    data_models = engine.data_models
    rewrites = syn.build_rewrites(q, rng, schema, instance)
    bind = q.bind_vars
    bind.update(syn.extra_binds(q, schema, rewrites))
    if q.tx:
        bind.update(tx_binds(q.tx))
    # one draw of concrete syntaxes for the three data models: every single-axis concrete syntax that
    # applies anywhere, and a few combinations
    options: list = []
    for data_model in data_models:
        for sp in syn.syntaxes_for(q, schema, rewrites, data_model):
            if sp not in options:
                options.append(sp)
    combos = [sp for sp in options if sp.axes > 1]
    rng.shuffle(combos)
    chosen = [sp for sp in options if sp.axes == 1] + combos[:MAX_COMBOS]
    queries: dict = {}
    known_bugs: dict = {}
    syntaxes: dict = {}
    for data_model in data_models:
        for sp in [syn.BASE] + chosen:
            if not syn.applicable(q, schema, sp, rewrites, data_model):
                continue
            name = data_model if sp == syn.BASE else f"{data_model}:{sp.tag}"
            queries[name] = syn.render(q, schema, sp, rewrites, data_model)
            syntaxes[name] = sp.tag or "base"
            fid = engine.known_bugs.select_known(q, sp, rewrites, data_model, schema, instance)
            if fid:
                known_bugs[name] = fid
    params = {
        "shape": shape_signature(q, schema),
        "coverage": coverage_signature(q),
        "select": q.to_params(),
        "reference": reference_ok(q, instance),
        "table": q.table,
        "concrete_syntax": syntaxes,
    }
    if set(syn.base_data_models(q)) != set(data_models):
        params["partial"] = True
    params.update(syn.rewrite_params(rewrites))
    return QueryCase("ir_select", q.table if q.edge else "", q.comparison, queries, bind, params, shape=params["shape"], known_bugs=known_bugs, engine=engine)


def strata(schema: Schema, instance: Instance) -> list:
    """The deck: (table, edge, kind) for every target and every kind it admits, a
    kind repeated per its `KIND_WEIGHTS`."""
    out = []
    for table, edge in _targets(schema, instance):
        for kind in EDGE_KINDS if edge else KINDS:
            out += [(table, edge, kind)] * (EDGE_WEIGHT if edge else KIND_WEIGHTS.get(kind, NODE_WEIGHT))
    return out


def generate_select_cases(rng: random.Random, schema: Schema, instance: Instance, per_instance: int = CASES_PER_INSTANCE) -> list:
    cases, seen = [], set()
    deck = strata(schema, instance)
    if not deck:
        return cases
    tier = []
    for table, edge in _targets(schema, instance):
        if edge:
            tier += [(table, True, "ids", None, 0.0), (table, True, "ids", None, 1.0)]
        else:
            tier.append((table, False, "ids", None, WHERE_PROBABILITY))
    tier += [(table, False, "keys", d, 0.0) for table, edge in _targets(schema, instance) if not edge and _walk_pool(schema, table) for d in DIRECTIONS]
    rng.shuffle(tier)
    hand: list = []
    for _ in range(per_instance * 4):
        if len(cases) >= per_instance:
            break
        walk = None
        if tier:
            table, edge, kind, walk, where_probability = tier.pop()
        else:
            where_probability = WHERE_PROBABILITY
            if not hand:
                hand = list(deck)
                rng.shuffle(hand)
            table, edge, kind = hand.pop()
        q = sample_select(rng, schema, instance, table=table, kind=kind, edge=edge, tx_probability=0.0 if walk else TX_PROBABILITY, where_probability=where_probability, walk=walk)
        if q is None:
            continue
        case = build_case(q, rng, schema, instance)
        first = case.variants[0]
        key = (case.queries[first], repr(sorted(case.bind_vars.items(), key=lambda kv: kv[0])))
        if key in seen:
            continue
        seen.add(key)
        cases.append(case)
    return cases
