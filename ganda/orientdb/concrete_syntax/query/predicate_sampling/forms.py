"""OrientDB's predicate forms: a drawn leaf in one of OrientDB SQL's spellings of the
same question (reversed operands, the field under arithmetic, a literal, a
parameter-keyed map entry).
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.data.neighbours import _f32
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.orientdb.rules import works_around


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
    arithmetic (`(f * :c) > :p`, the drawn constant shifted alike), a literal (`f >
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
    from ganda.orientdb.engine.semantics import apply_arith

    if p.on == "edge" or p.op not in _MIRROR or p.key is not None or p.case_insensitive or p.ftype is None:
        return None
    ftype = FieldType(p.ftype)
    pv = p.constant
    if ftype not in _ARITH_CHOICES or isinstance(pv, bool) or pv is None or (ftype in DATE_TYPES) != isinstance(pv, str):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    try:
        pc = apply_arith(pv, sym, c, ftype.value)
    except (ValueError, OverflowError, TypeError):
        return None
    # every variant's parameters travel in one request: a constant the engine takes
    if isinstance(pc, float) and not math.isfinite(pc) or isinstance(pc, int) and not I64_MIN <= pc <= I64_MAX:
        return None
    op = p.op if (c > 0 or sym != "*") else _MIRROR[p.op]
    return dataclasses.replace(p, op=op, constant=pc, arith=Arith(sym, f"{p.bind}c", c))


def _literal_ok(p) -> bool:
    """Whether every constant of `p` can be written into the text: numbers and booleans
    only (a string never is interpolated), on a declared column."""
    from ganda.core.query.ir.predicates import MAP_OPS, NO_BIND_OPS

    if p.on == "edge" or p.key is not None or p.sub is not None or p.op in MAP_OPS or p.op in NO_BIND_OPS or p.op == "LIKE":
        return False
    constants = p.constant if isinstance(p.constant, list) else [p.constant]
    if not all(isinstance(v, (int, float)) for v in constants):
        return False
    return all(not isinstance(v, float) or (math.isfinite(v) and _f32(v) == v) for v in constants)
