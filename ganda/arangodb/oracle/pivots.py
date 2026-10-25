"""ArangoDB's side of predicate sampling: the operators a leaf may draw per column type
and the pivots that sit where an index lookup and a scan can disagree.

A STRING (or BINARY) column takes the equality family only: AQL orders strings by ICU
collation (`order.string` in `rules/catalog.py`), which the generators' walk of the
Instance does not model, so an ordered comparison on one would steer nothing. The
model's date types are strings of one fixed shape, ordered alike by ICU and by code
point, so they keep every operator.
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.data.model import DATE_TYPES, FLOAT_TYPES, INT_TYPES, FieldType
from ganda.core.rules.vocabulary import StringOrder
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL
from ganda.arangodb.oracle.semantics import _LINE_TERMINATORS

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
EQUALITY_OPS = ("=", "<>", "IN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT")
# a LIKE pattern is built only from a value holding none of these: the wildcards, the
# escape, and the line terminators an ICU regex's `.` and `$` treat specially
LIKE_WILDCARDS = "%_\\\n\r" + _LINE_TERMINATORS
# a string column's order (ICU collation) is no order the Instance walk reproduces
assumes("order.string", AQL, StringOrder.COLLATION)


def ordered_ops(ftype: FieldType) -> tuple:
    """The comparison operators a leaf on a column of `ftype` may draw."""
    if ftype in (FieldType.STRING, FieldType.BINARY):
        return EQUALITY_OPS
    return ORDERED_OPS


def cross_type(rng: random.Random, ftype: FieldType, v):
    """The same pivot in a form the column does not hold: an integer pivot as an
    exact double (`5.0`) or a half-step off it (`5.5`); a double pivot as the integer
    it truncates to, or one past it. A date column is a string column here: no other
    form compares equal to its values."""
    if ftype in INT_TYPES:
        if abs(v) >= 2**52:
            return float(v)
        return float(v) if rng.random() < 0.4 else v + rng.choice((0.5, -0.5))
    if ftype in FLOAT_TYPES:
        if not math.isfinite(v) or abs(v) >= 2**62:
            return v
        return int(v) if rng.random() < 0.7 else int(v) + rng.choice((1, -1))
    if ftype in DATE_TYPES:
        return v
    return v


def refine(rng: random.Random, ftype: FieldType, v):
    """A same-type pivot as this engine wants it asked: as it is."""
    return v


# --- a leaf's form ---------------------------------------------------------------

# the probabilities of a leaf's forms (at most one per leaf; a map entry's
# parameter-keyed bracket is drawn on its own)
REV_PROBABILITY = 0.1
ARITH_PROBABILITY = 0.1
KEY_BIND_PROBABILITY = 0.3

_MIRROR = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    # a double sum rounds (adjacent doubles collapse under `+ 1`): only exact scalings
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.FLOAT32: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("*", 2), ("*", -1), ("*", 4)),
}


def leaf_form(rng: random.Random, p):
    """The drawn leaf `p` in one of AQL's forms, each another question asked alike on
    every data model: reversed operands (`@p > n.f`), the field under arithmetic (`(n.f
    * @c) > @p`, the drawn pivot shifted alike), a map entry keyed by a parameter
    (`n.m[@k] == @p`); or `p` as drawn. The write generator keeps an arithmetic leaf
    only where it is exact on every stored value."""
    if p.key is not None and not p.key_bind and p.sub is None and rng.random() < KEY_BIND_PROBABILITY:
        return dataclasses.replace(p, key_bind=f"{p.bind or 'k'}k")
    roll = rng.random()
    if roll < REV_PROBABILITY:
        return dataclasses.replace(p, rev=True) if p.op in _MIRROR or p.op == "HAS" else p
    if roll < REV_PROBABILITY + ARITH_PROBABILITY:
        return _arithmetic(rng, p) or p
    return p


def _arithmetic(rng: random.Random, p):
    from ganda.core.query.ir.predicates import Arith
    from ganda.arangodb.oracle.semantics import apply_arith, arith_exact

    if p.op not in _MIRROR or p.key is not None or p.sub is not None or p.ftype is None:
        return None
    ftype = FieldType(p.ftype)
    pv = p.pivot
    if ftype not in _ARITH_CHOICES or isinstance(pv, bool) or not isinstance(pv, (int, float)) or (ftype in INT_TYPES and not isinstance(pv, int)):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    # the shifted pivot is exact, so it is the value the engine compares with
    if not arith_exact(pv, sym, c, ftype):
        return None
    op = p.op if (c > 0 or sym != "*") else _MIRROR[p.op]
    return dataclasses.replace(p, op=op, pivot=apply_arith(pv, sym, c, ftype.value), arith=Arith(sym, f"{p.bind}c", c))
