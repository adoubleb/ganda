"""SurrealDB's predicate constants: the cross-type numeric constants (the engine compares
an int with a float by value, so they are another spelling of the same question) and the
same-type refinements."""
from __future__ import annotations

import math
import random

from ganda.core.data.model import FieldType


def cross_type(rng: random.Random, ftype: FieldType, v):
    """An integer constant as the double it equals (`5.0`) or a half-step off it; a
    double or decimal constant as the integer it truncates to. A date takes no
    cross-type constant (a string beside a datetime compares by kind, not by value)."""
    if ftype in (FieldType.INT, FieldType.INT32):
        if abs(v) >= 2**52:
            return float(v)
        return float(v) if rng.random() < 0.5 else v + rng.choice((0.5, -0.5))
    if ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        if not isinstance(v, float) or not math.isfinite(v) or abs(v) >= 2**62:
            return v
        return int(v) if rng.random() < 0.7 else int(v) + rng.choice((1, -1))
    return v


def refine(rng: random.Random, ftype: FieldType, v):
    """A same-type constant as this engine wants it asked: as drawn (a decimal
    constant is written as a decimal literal, exact)."""
    return v
