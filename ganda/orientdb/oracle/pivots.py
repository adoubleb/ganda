"""OrientDB's side of predicate sampling: the operators a leaf may draw and the
pivots that sit where an index lookup and a scan can disagree.
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.constants import I32_MAX, I32_MIN
from ganda.core.data.neighbours import _f32
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.orientdb.rules import works_around


ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT")
# a LIKE pattern is built only from a value holding none of these
LIKE_WILDCARDS = "%?_\\"


def lossy_pivot(ftype: FieldType, v) -> bool:
    """Whether converting `v` to the column's type changes it -- the pivots an index
    lookup (which converts the key) and a scan (which compares as the wider box)
    can disagree on: a double with a fractional part or at/after 2^53 on a LONG
    column; on an INTEGER column that, or a Long outside int32 (wrapped); a double
    that is not float32-exact on a FLOAT column; on a DECIMAL column a double whose
    binary expansion is not its decimal string (every non-dyadic one)."""
    if isinstance(v, bool) or v is None:
        return False
    if ftype is FieldType.INT:
        return isinstance(v, float) and math.isfinite(v) and (v != int(v) or abs(v) >= 2**53)
    if ftype is FieldType.INT32:
        if isinstance(v, float):
            return not math.isfinite(v) or v != int(v) or not (I32_MIN <= v <= I32_MAX)
        return isinstance(v, int) and not (I32_MIN <= v <= I32_MAX)
    if ftype is FieldType.FLOAT32:
        return isinstance(v, float) and _f32(v) != v
    if ftype is FieldType.DECIMAL:
        if not isinstance(v, float) or not math.isfinite(v):
            return False
        from decimal import Decimal

        return Decimal(v) != Decimal(repr(v))
    if ftype in DATE_TYPES:
        # a string the format only partially matches: the engine compares it as the
        # date's midnight (O27); a date-only string or an epoch integer converts
        # faithfully
        return isinstance(v, str) and ("T" in v or (len(v) == 16 and v[10] == " "))
    return False


def ordered_ops(ftype) -> tuple:
    """Every comparison operator, on every ordered type: the reference models this
    engine's order inside each type."""
    return ORDERED_OPS


def cross_type(rng: random.Random, ftype: FieldType, v):
    """The same pivot rendered in a form the column does not hold exactly: a LONG
    pivot as an exact double (`5.0`) or a half-step off it (`5.5`, where an index key
    conversion that truncates would bite); an INTEGER pivot as that, or as a Long
    that wraps onto it (`v + 2^32`) or lies just outside int32; a DOUBLE pivot as
    the integer it truncates to; a FLOAT pivot as its integer or as the next double
    (not float32-exact, so an index rounds it back onto the value); a DECIMAL pivot
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


def _decimal_pivot(rng: random.Random, v):
    """A DECIMAL pivot: half the time the stored value itself (a non-dyadic double,
    which a scan compares through its binary expansion, O20), otherwise the nearest
    dyadic multiple of 1/8 (exact in both representations)."""
    if isinstance(v, float) and math.isfinite(v) and rng.random() < 0.5:
        return round(v * 8) / 8
    return v


def refine(rng: random.Random, ftype: FieldType, v):
    """A same-type pivot as this engine wants it asked (a DECIMAL: `_decimal_pivot`)."""
    return _decimal_pivot(rng, v) if ftype is FieldType.DECIMAL else v


# --- a leaf's form ---------------------------------------------------------------

# the probabilities of a leaf's forms (at most one per leaf; a map entry's
# parameter-keyed bracket is drawn on its own)
REV_PROBABILITY = 0.1
ARITH_PROBABILITY = 0.1
LITERAL_PROBABILITY = 0.1
KEY_BIND_PROBABILITY = 0.3

_MIRROR = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.FLOAT32: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1)),
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1), ("+", 100)),
    FieldType.DATETIME: (("+", 1000), ("-", 1000), ("+", 86400000), ("-", 3600000), ("+", 60000)),
    FieldType.DATE: (("+", 1000), ("+", 86400000), ("-", 86400000), ("-", 1000)),
}


works_around("O12")


def leaf_form(rng: random.Random, p):
    """The drawn leaf `p` in one of OrientDB SQL's forms, each another question asked
    alike on every data model: reversed operands (`:p > f`), the field under
    arithmetic (`(f * :c) > :p`, the drawn pivot shifted alike), a literal pivot (`f >
    5`), a map entry keyed by a parameter (`m[:k] = :p`); or `p` as drawn. A
    relationship attribute takes neither arithmetic nor a literal (the doc data
    model holds it as untyped JSON, where the box an operand gets differs from the
    declared column's), a literal is never a double a FLOAT would not hold (O12: a
    literal double is parsed through a float on some paths). The write generator keeps
    an arithmetic leaf only where it is exact on every stored value, so the Instance
    applies the write the engine does."""
    if p.key is not None and not p.key_bind and p.sub is None and rng.random() < KEY_BIND_PROBABILITY:
        return dataclasses.replace(p, key_bind=f"{p.bind or 'k'}k")
    roll = rng.random()
    if roll < REV_PROBABILITY:
        return dataclasses.replace(p, rev=True) if p.op in _MIRROR or p.op == "HAS" else p
    if roll < REV_PROBABILITY + ARITH_PROBABILITY:
        return _arithmetic(rng, p) or p
    if roll < REV_PROBABILITY + ARITH_PROBABILITY + LITERAL_PROBABILITY:
        return dataclasses.replace(p, literal=True) if _literal_ok(p) else p
    return p


def _arithmetic(rng: random.Random, p):
    from ganda.core.constants import I64_MAX, I64_MIN
    from ganda.core.query.ir.predicates import Arith
    from ganda.orientdb.oracle.semantics import apply_arith

    if p.on == "edge" or p.op not in _MIRROR or p.key is not None or p.case_insensitive or p.ftype is None:
        return None
    ftype = FieldType(p.ftype)
    pv = p.pivot
    if ftype not in _ARITH_CHOICES or isinstance(pv, bool) or pv is None or (ftype in DATE_TYPES) != isinstance(pv, str):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    try:
        pc = apply_arith(pv, sym, c, ftype.value)
    except (ValueError, OverflowError, TypeError):
        return None
    # every variant's parameters travel in one request: a pivot the engine takes
    if isinstance(pc, float) and not math.isfinite(pc) or isinstance(pc, int) and not I64_MIN <= pc <= I64_MAX:
        return None
    op = p.op if (c > 0 or sym != "*") else _MIRROR[p.op]
    return dataclasses.replace(p, op=op, pivot=pc, arith=Arith(sym, f"{p.bind}c", c))


def _literal_ok(p) -> bool:
    """Whether every pivot of `p` can be written into the text: numbers and booleans
    only (a string never is interpolated), on a declared column."""
    from ganda.core.query.ir.predicates import MAP_OPS, NO_BIND_OPS

    if p.on == "edge" or p.key is not None or p.sub is not None or p.op in MAP_OPS or p.op in NO_BIND_OPS or p.op == "LIKE":
        return False
    pivots = p.pivot if isinstance(p.pivot, list) else [p.pivot]
    if not all(isinstance(v, (int, float)) for v in pivots):
        return False
    return all(not isinstance(v, float) or (math.isfinite(v) and _f32(v) == v) for v in pivots)
