"""SurrealDB's predicate forms: a drawn leaf in one of SurrealQL's spellings of the same
question (reversed operands, the field under arithmetic, a literal, a parameter-keyed
map entry). A datetime, decimal or byte-string constant is always a literal: a `LET`
bind variable carries no type, and a string beside a datetime compares by kind.
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.surrealdb.concrete_syntax.query.predicate import LITERAL_TYPES

REV_PROBABILITY = 0.1
ARITH_PROBABILITY = 0.1
LITERAL_PROBABILITY = 0.1
KEY_BIND_PROBABILITY = 0.3

_MIRROR = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
# `+` on two ints is refused past the 64-bit range: the shifted constant stays inside
_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4), ("+", 1)),
    FieldType.FLOAT32: (("*", 2), ("*", -1), ("+", 1)),
    FieldType.DECIMAL: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1), ("+", 100)),
}


def leaf_form(rng: random.Random, p):
    """The drawn leaf `p` in one of SurrealQL's forms, each another question asked alike
    on every data model: reversed operands (`$p > f`), the field under arithmetic
    (`(f * $c) > $p`, the drawn constant shifted alike), a literal (`f > 5`), a map
    entry keyed by a parameter (`m[$k] = $p`); or `p` as drawn. A typed constant (a
    datetime, a decimal, a byte string) is a literal whatever is drawn."""
    if p.ftype in LITERAL_TYPES and p.sub is None and p.key is None and p.op not in ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT"):
        p = dataclasses.replace(p, literal=True)
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
    from ganda.surrealdb.engine.semantics import apply_arith

    if p.op not in _MIRROR or p.key is not None or p.case_insensitive or p.ftype is None or p.sub is not None:
        return None
    ftype = FieldType(p.ftype)
    pv = p.constant
    if ftype not in _ARITH_CHOICES or isinstance(pv, bool) or pv is None or not isinstance(pv, (int, float)):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    try:
        pc = apply_arith(pv, sym, c, ftype.value)
    except (ValueError, OverflowError, TypeError, ArithmeticError):
        return None
    if isinstance(pc, float) and not math.isfinite(pc) or isinstance(pc, int) and not I64_MIN <= pc <= I64_MAX:
        return None
    op = p.op if (c > 0 or sym != "*") else _MIRROR[p.op]
    return dataclasses.replace(p, op=op, constant=pc, arith=Arith(sym, f"{p.bind}c", c))


def _literal_ok(p) -> bool:
    """Whether every constant of `p` can be written into the text: numbers and
    booleans (a string travels bound)."""
    from ganda.core.query.ir.predicates import MAP_OPS, NO_BIND_OPS

    if p.key is not None or p.sub is not None or p.op in MAP_OPS or p.op in NO_BIND_OPS or p.op == "LIKE":
        return False
    constants = p.constant if isinstance(p.constant, list) else [p.constant]
    return all(isinstance(v, (int, float)) and (not isinstance(v, float) or math.isfinite(v)) for v in constants)
