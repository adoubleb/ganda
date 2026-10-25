"""OrientDB's predicate constants: the cross-type and boundary constants that sit
where an index lookup and a scan can disagree (`engine.semantics.lossy_constant`).
"""
from __future__ import annotations

import math
import random

from ganda.core.data.model import DATE_TYPES, FieldType


def cross_type(rng: random.Random, ftype: FieldType, v):
    """The same constant rendered in a form the column does not hold exactly: a LONG
    constant as an exact double (`5.0`) or a half-step off it (`5.5`, where an index key
    conversion that truncates would bite); an INTEGER constant as that, or as a Long
    that wraps onto it (`v + 2^32`) or lies just outside int32; a DOUBLE constant as
    the integer it truncates to; a FLOAT constant as its integer or as the next double
    (not float32-exact, so an index rounds it back onto the value); a DECIMAL constant
    as its integer or left as the stored value (non-dyadic: a scan then compares its
    binary expansion, O20)."""
    if ftype is FieldType.INT:
        if abs(v) >= 2**52:
            return float(v)
        return float(v) if rng.random() < 0.4 else v + rng.choice((0.5, -0.5))
    if ftype is FieldType.INT32:
        roll = rng.random()
        if roll < 0.3:
            return float(v)
        if roll < 0.5:
            return v + rng.choice((0.5, -0.5))
        if roll < 0.8:
            return v + rng.choice((2**32, -(2**32)))
        return rng.choice((2**31, -(2**31) - 1, 2**40))
    if ftype is FieldType.FLOAT:
        if not math.isfinite(v) or abs(v) >= 2**62:
            return v
        return int(v) if rng.random() < 0.7 else int(v) + rng.choice((1, -1))
    if ftype is FieldType.FLOAT32:
        roll = rng.random()
        if roll < 0.4:
            return int(v) if abs(v) < 2**62 else v
        if roll < 0.6:
            return 2**24 + rng.choice((1, -1, 3))
        nxt = math.nextafter(v, math.inf if rng.random() < 0.5 else -math.inf)
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.DECIMAL:
        return int(v) if abs(v) < 2**62 else v
    if ftype in DATE_TYPES:
        import datetime as _dt

        roll = rng.random()
        if roll < 0.35:
            return v[:10]  # the date alone: midnight
        if roll < 0.7:
            t = _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            return int((t - _dt.datetime(1970, 1, 1)).total_seconds()) * 1000 + rng.choice((0, 0, 500))
        if roll < 0.85:
            return v[:10] + "T" + v[11:]  # the ISO separator: compared as midnight (O27)
        return v[:16]  # no seconds: compared as midnight (O27)
    return v


def _decimal_constant(rng: random.Random, v):
    """A DECIMAL constant: half the time the stored value itself (a non-dyadic double,
    which a scan compares through its binary expansion, O20), otherwise the nearest
    dyadic multiple of 1/8 (exact in both representations)."""
    if isinstance(v, float) and math.isfinite(v) and rng.random() < 0.5:
        return round(v * 8) / 8
    return v


def refine(rng: random.Random, ftype: FieldType, v):
    """A same-type constant as this engine wants it asked (a DECIMAL: `_decimal_constant`)."""
    return _decimal_constant(rng, v) if ftype is FieldType.DECIMAL else v
