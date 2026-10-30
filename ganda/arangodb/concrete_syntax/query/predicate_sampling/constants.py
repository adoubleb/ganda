"""ArangoDB's predicate constants: the cross-type constants that sit where an index
lookup and a scan can disagree (AQL compares numbers by value, so they are another
spelling of the same question), and the same-type refinements."""
from __future__ import annotations

import math
import random

from ganda.core.data.model import DATE_TYPES, FLOAT_TYPES, INT_TYPES, FieldType


def cross_type(rng: random.Random, ftype: FieldType, v):
    """The same constant in a form the column does not hold: an integer constant as an
    exact double (`5.0`) or a half-step off it (`5.5`); a double constant as the integer
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
    """A same-type constant as this engine wants it asked: as it is."""
    return v
