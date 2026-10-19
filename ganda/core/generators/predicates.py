"""The predicate sampler: leaves and compounds drawn over a record's fields.

A LEAF's pivot sits ON a stored value, ADJACENT to one (`neighbours.offset`) or OUTSIDE
the range; with `cross_type_probability` it is a cross-type numeric pivot drawn by the
engine's `pivots.cross_type`. A compound (`AND` / `OR` / `NOT`) is built over leaves, at
most two levels deep. The engine arrives as an explicit argument (`engine.pivots`,
`engine.semantics`) or on the schema.

The tree itself, its evaluation and its rewrites are `ganda.core.ir.predicates`.
"""
from __future__ import annotations

import random

from ganda.core.ir.predicates import COMPOUND_OPS, NO_BIND_OPS, Compound, Predicate, SubQuery, sub_values
from ganda.core.model import MAP_KEYS, NUMERIC_TYPES, SCALAR_TYPES, FieldSpec, FieldType
from ganda.core.constants import I64_MAX
from ganda.core.neighbours import offset

_EQUALITY_OPS = ("=", "<>", "IN")

COMPOUND_PROBABILITY = 0.3
CROSS_TYPE_PROBABILITY = 0.2


def draw(rng: random.Random, on: str, f: FieldSpec, values: list, bind: str, engine, cross_type_probability: float = CROSS_TYPE_PROBABILITY) -> "Predicate | None":
    """A LEAF predicate on `f` whose stored (non-null) values are `values`. None when
    the column holds nothing to aim at."""
    present = [v for v in values if v is not None]
    ft, ci = f.type.value, f.ci
    pivots_of, _sort_key = engine.pivots, engine.semantics.key

    def leaf(op, pivot):
        return Predicate(on, f.name, op, "" if op in NO_BIND_OPS else bind, pivot, True, ft, ci)

    roll = rng.random()
    if roll < 0.12:
        return leaf(rng.choice(pivots_of.NULL_OPS), None)
    if not present:
        return None
    if f.type is FieldType.BINARY:
        # a blob answers the null tests only (above): no comparison names it
        return None
    if f.type is FieldType.LIST:
        # CONTAINS carries most of the weight; a whole-list equality and CONTAINSANY /
        # CONTAINSALL over one or two elements take the rest
        roll = rng.random()
        if roll < 0.2:
            v = rng.choice(present)
            return leaf("=", list(v))
        elems = [x for v in present for x in v]
        if not elems:
            return None
        if roll < 0.45:
            k = rng.randint(1, min(2, len(elems)))
            pivots = [rng.choice(elems) for _ in range(k)]
            if rng.random() < 0.3:
                # a pivot just past a stored element -- inside the 64-bit range, or
                # the value could not travel as an integer at all
                pivots[-1] = pivots[-1] + 1 if pivots[-1] < I64_MAX else pivots[-1] - 1
            return leaf(rng.choice(("CONTAINSANY", "CONTAINSALL")), pivots)
        return leaf("CONTAINS", rng.choice(elems))
    if f.type is FieldType.MAP:
        return _draw_map(rng, on, f, present, bind, leaf, engine)
    if f.type is FieldType.BOOL:
        return leaf(rng.choice(_EQUALITY_OPS[:2]), rng.choice(present))
    if f.type is FieldType.STRING and rng.random() < 0.15:
        safe = [v for v in present if v != "" and not any(c in v for c in pivots_of.LIKE_WILDCARDS)]
        if safe:
            v = rng.choice(safe)
            k = rng.randint(1, len(v))
            form = rng.choice(("prefix", "suffix", "infix", "exact"))
            pat = {"prefix": v[:k] + "%", "suffix": "%" + v[-k:], "infix": "%" + v[:k] + "%", "exact": v}[form]
            return leaf("LIKE", pat)
    op = rng.choice(pivots_of.ordered_ops(f.type))
    distinct = list({repr(v): v for v in present}.values())
    cross = f.type in NUMERIC_TYPES and rng.random() < cross_type_probability
    if op in ("IN", "NOT IN"):
        k = rng.randint(1, min(3, len(distinct)))
        pivots = rng.sample(distinct, k)
        if cross:
            pivots = [pivots_of.cross_type(rng, f.type, v) if rng.random() < 0.6 else v for v in pivots]
        else:
            pivots = [pivots_of.refine(rng, f.type, v) for v in pivots]
        return leaf(op, pivots)
    if op == "BETWEEN":
        a, b = sorted(rng.sample(distinct, 2) if len(distinct) >= 2 else [distinct[0], distinct[0]], key=_sort_key)
        if rng.random() < 0.3:
            b = offset(f.type, b, -1)
        if cross:
            a, b = sorted([pivots_of.cross_type(rng, f.type, a) if rng.random() < 0.6 else a, pivots_of.cross_type(rng, f.type, b) if rng.random() < 0.6 else b], key=_sort_key)
        return leaf(op, [a, b])
    pivot = rng.choice(distinct)
    roll = rng.random()
    if roll < 0.3:
        pivot = offset(f.type, pivot, rng.choice((-1, 1)))
    elif roll < 0.4:
        top = max(distinct, key=_sort_key)
        bottom = min(distinct, key=_sort_key)
        pivot = offset(f.type, top, 1) if rng.random() < 0.5 else offset(f.type, bottom, -1)
    if cross:
        pivot = pivots_of.cross_type(rng, f.type, pivot)
    else:
        pivot = pivots_of.refine(rng, f.type, pivot)
    return leaf(op, pivot)


def _draw_map(rng: random.Random, on: str, f: FieldSpec, present: list, bind: str, leaf, engine):
    """A predicate over a map of integers: `CONTAINSKEY` (a stored or absent key),
    `VALUES CONTAINS` / `CONTAINSVALUE` (a stored or absent value), or an entry
    comparison `m.k op p` under the usual operators and null tests -- never a
    definedness test, which a map entry answers differently per engine."""
    keys = sorted({k for m in present for k in m})
    elems = [x for m in present for x in m.values() if x is not None]
    roll = rng.random()
    if roll < 0.2 or not keys:
        return leaf("CONTAINSKEY", rng.choice(keys) if keys and rng.random() < 0.8 else rng.choice(MAP_KEYS))
    if roll < 0.32 and elems:
        # an absent value one past a stored one, kept inside the 64-bit range
        return leaf("VALUES CONTAINS", rng.choice(elems) if rng.random() < 0.8 else offset(FieldType.INT, rng.choice(elems), 1))
    if roll < 0.4 and elems:
        return leaf("CONTAINSVALUE", rng.choice(elems))
    k = rng.choice(keys)
    kvals = [m[k] for m in present if m.get(k) is not None]
    spec = FieldSpec(f.name, FieldType.INT)
    inner = draw(rng, on, spec, kvals, bind, engine) if kvals else None
    if inner is None or inner.op in ("IS DEFINED", "IS NOT DEFINED"):
        return Predicate(on, f.name, rng.choice(("IS NULL", "IS NOT NULL")), "", None, True, "int", False, k)
    return Predicate(on, f.name, inner.op, inner.bind, inner.pivot, True, "int", False, k)


def draw_insub(rng: random.Random, schema, instance, table: str, bind: str) -> "Predicate | None":
    """A semi-join leaf on `table`: `f IN (SELECT g FROM t2 [WHERE q])`, `g` a column
    of `f`'s type on any node class (the same one included), `q` drawn with even odds
    over `t2`'s fields under `<bind>s`. None when no column has a partner."""
    fields = [f for f in schema.query_fields(table) if f.type in SCALAR_TYPES]
    if not fields:
        return None
    f = rng.choice(fields)

    partners = [(t2.name, g) for t2 in schema.query_tables() for g in schema.query_fields(t2.name) if g.type is f.type]
    if not partners:
        return None
    t2, g = rng.choice(partners)
    q = None
    if rng.random() < 0.5:

        def inner_values(x):
            return [n.values.get(x.name) for n in instance.nodes_of(t2) if x.name in n.values]

        q = draw_over(rng, "source", schema.query_fields(t2), values_of=inner_values, bind=f"{bind}s", engine=schema.engine)
    reference = q is None or q.reference
    return Predicate("source", f.name, "INSUB", "", sub_values(instance, t2, g.name, q), reference, f.type.value, f.ci, sub=SubQuery(t2, g.name, q, False))


def draw_over(rng: random.Random, on: str, fields, values_of, bind: str, engine, compound_probability: float = COMPOUND_PROBABILITY, depth: int = 0):
    """A predicate over a record with `fields` gives the
    column's stored values): a leaf on one field, or with `compound_probability` a
    compound of two leaves (`AND` / `OR`, possibly on ONE field -- a range or a
    disjunction of equalities) or a negation, at most two levels deep. Leaves inside
    a compound bind `<bind>_<k>`."""
    fields = list(fields)
    if not fields:
        return None
    if rng.random() >= compound_probability or depth >= 2:
        f = rng.choice(fields)
        return draw(rng, on, f, values_of(f), bind, engine)
    op = rng.choice(COMPOUND_OPS)
    if op == "NOT":
        child = draw_over(rng, on, fields, values_of, f"{bind}_0", engine, compound_probability * 0.5, depth + 1)
        return None if child is None else Compound(on, "NOT", (child,))
    children = []
    for k in range(2):
        child = draw_over(rng, on, fields, values_of, f"{bind}_{k}", engine, compound_probability * 0.5, depth + 1)
        if child is None:
            return None
        children.append(child)
    return Compound(on, op, tuple(children))
