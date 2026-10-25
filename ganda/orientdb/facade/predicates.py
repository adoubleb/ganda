"""Predicates under OrientDB: the core's predicate tree with this engine's semantics,
constants bound, so OrientDB-side code calls `evaluate(values, p)` and
`draw_over(rng, on, fields, values_of, bind)` without naming the engine.
"""
from __future__ import annotations

from ganda.core.query.generators import predicates as _core
from ganda.core.query.generators.predicates import COMPOUND_PROBABILITY, CROSS_TYPE_PROBABILITY  # noqa: F401
from ganda.core.constants import I32_MAX, I32_MIN, I64_MAX, I64_MIN  # noqa: F401
from ganda.core.data.neighbours import offset, shift_date  # noqa: F401
from ganda.core.query.ir.predicates import (  # noqa: F401
    COMPOUND_OPS,
    MAP_OPS,
    MIRROR,
    NO_BIND_OPS,
    Compound,
    Predicate,
    fields_of,
    has_op,
    has_op_deep,
    leaves,
    resolve_subs,
    sub_values,
    subqueries,
)
from ganda.orientdb.concrete_syntax.query.predicate_sampling import cross_type, leaf_form, lossy_constant  # noqa: F401
from ganda.orientdb.oracle.semantics import apply_arith, arith_exact, evaluate  # noqa: F401


def _engine():
    from ganda.orientdb.provider import PROVIDER

    return PROVIDER


def draw(rng, on, f, values, bind, cross_type_probability=CROSS_TYPE_PROBABILITY):
    return _core.draw(rng, on, f, values, bind, _engine(), cross_type_probability)


def draw_over(rng, on, fields, values_of, bind, compound_probability=COMPOUND_PROBABILITY, depth=0):
    return _core.draw_over(rng, on, fields, values_of, bind, _engine(), compound_probability, depth)


def draw_insub(rng, schema, instance, table, bind):
    return _core.draw_insub(rng, schema, instance, table, bind)
