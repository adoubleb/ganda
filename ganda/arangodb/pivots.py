"""ArangoDB's side of predicate sampling: the operators a leaf may draw per column type
and the pivots that sit where an index lookup and a scan can disagree.

A STRING (or BINARY) column takes the equality family only: AQL orders strings by ICU
collation, which the reference does not model, so an ordered comparison on one would
be a question without a reference leg. The model's date types are strings of one fixed
shape, ordered alike by ICU and by code point, so they keep every operator.
"""
from __future__ import annotations

import math
import random

from ganda.core.data.model import DATE_TYPES, FLOAT_TYPES, INT_TYPES, FieldType

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
EQUALITY_OPS = ("=", "<>", "IN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS DEFINED", "IS NOT DEFINED")
# a LIKE pattern is built only from a value holding none of these: the wildcards, the
# escape, and the line terminators an ICU regex's `.` and `$` treat specially
LIKE_WILDCARDS = "%_\\\n\r\u0085  "


def ordered_ops(ftype: FieldType) -> tuple:
    """The comparison operators a leaf on a column of `ftype` may draw."""
    return EQUALITY_OPS if ftype in (FieldType.STRING, FieldType.BINARY) else ORDERED_OPS


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
