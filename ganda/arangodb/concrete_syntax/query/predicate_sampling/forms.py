"""ArangoDB's predicate forms: a drawn leaf in one of AQL's spellings of the same
question (reversed operands, the field under arithmetic, a parameter-keyed map entry).
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.data.model import INT_TYPES, FieldType


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
    """The drawn leaf in one of AQL's forms: reversed operands, the field under arithmetic, a
    parameter-keyed map entry, or as drawn."""
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
    from ganda.arangodb.engine.semantics import apply_arith, arith_exact

    if p.op not in _MIRROR or p.key is not None or p.sub is not None or p.ftype is None:
        return None
    ftype = FieldType(p.ftype)
    pv = p.constant
    if ftype not in _ARITH_CHOICES or isinstance(pv, bool) or not isinstance(pv, (int, float)) or (ftype in INT_TYPES and not isinstance(pv, int)):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    # the shifted constant is exact, so it is the value the engine compares with
    if not arith_exact(pv, sym, c, ftype):
        return None
    op = p.op if (c > 0 or sym != "*") else _MIRROR[p.op]
    return dataclasses.replace(p, op=op, constant=apply_arith(pv, sym, c, ftype.value), arith=Arith(sym, f"{p.bind}c", c))
