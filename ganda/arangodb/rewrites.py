"""Equivalent rewrites of a predicate under AQL's semantics: the query-rewrite oracle's
supply. `rewrite` restates a tree through two-valued NOT and the total comparison
order; `arithmetic` wraps a leaf's field in arithmetic that is exact on every stored
value. Neither form is index-served the way the plain one is.

AQL comparison is TOTAL (null is the smallest value), so `NOT (f <= p)` IS `f > p` for
every stored value, null included -- the negation rewrites need no null conjunct where
OrientDB's do. Arithmetic over null is not (`null + 1` is 1), so the arithmetic form
settles the null rows explicitly: `OR f == null` where the plain comparison holds for a
null (`<`, `<=`, `<>`), `AND f != null` where it does not.
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.ast.predicates import MAP_OPS, MIRROR, NO_BIND_OPS, Compound, Predicate
from ganda.core.model import FLOAT_TYPES, INT_TYPES, FieldType
from ganda.arangodb.semantics import F64_INT_EXACT, apply_arith


def arith_exact(v, sym: str, c, ftype: FieldType) -> bool:
    """Whether `v <sym> c` is computed exactly: integer arithmetic whose result stays
    under 2^53 in magnitude (AQL arithmetic runs in double past that), a double scaled
    by a power of two or negated. A string (a date) takes no arithmetic."""
    if v is None:
        return True
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (ValueError, OverflowError):
        return False
    if ftype in INT_TYPES:
        return isinstance(r, int) and abs(r) < F64_INT_EXACT
    if ftype in FLOAT_TYPES:
        return math.isfinite(r) and sym == "*" and (r != 0 or v == 0)
    return False


_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    # a double sum rounds (adjacent doubles collapse under `+ 1`): only exact scalings
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.FLOAT32: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("*", 2), ("*", -1), ("*", 4)),
}


def arithmetic(p, rng: random.Random, bind: str, ftype: FieldType, values: list):
    """An equivalent form of the leaf `f op :p` as `(f <sym> :c) op' :pc`, the null rows
    settled explicitly: the pivot shifted the same way, the operator mirrored under a
    negative factor. None unless the arithmetic is exact on every
    stored value and on the pivot, and the pivot is a number of the column's kind."""
    if not isinstance(p, Predicate) or p.op not in MIRROR or p.key is not None or not p.plain or ftype not in _ARITH_CHOICES:
        return None
    pv = p.pivot
    if isinstance(pv, bool) or pv is None or not isinstance(pv, (int, float)):
        return None
    if ftype in INT_TYPES and not isinstance(pv, int):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    if not all(arith_exact(v, sym, c, ftype) for v in values) or not arith_exact(pv, sym, c, ftype):
        return None
    op = p.op if (c > 0 or sym != "*") else MIRROR[p.op]
    cbind = f"{bind}c"
    leaf = Predicate(p.on, p.field, op, bind, apply_arith(pv, sym, c, ftype.value), p.reference, p.ftype, p.ci, None, "", False, (sym, cbind, c))
    # the null rows answer as the PLAIN comparison does under the total order: `<`,
    # `<=` and `<>` hold for a null, `=`, `>` and `>=` do not (the arithmetic over a
    # null would read it as 0)
    if p.op in ("<", "<=", "<>"):
        return Compound(p.on, "OR", (leaf, Predicate(p.on, p.field, "IS NULL", "", None, p.reference, p.ftype, p.ci)))
    return Compound(p.on, "AND", (leaf, Predicate(p.on, p.field, "IS NOT NULL", "", None, p.reference, p.ftype, p.ci)))


_NEGATED = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}


def rewrite(p, rng: random.Random, bind: str):
    """An equivalent form of `p` under AQL's semantics, or None when it has none. The
    rewritten leaves bind fresh names under `bind`, with the same values."""
    if p is None:
        return None
    if isinstance(p, Compound):
        if p.op == "NOT":
            c = p.children[0]
            if isinstance(c, Compound) and c.op in ("AND", "OR"):
                return Compound(p.on, "OR" if c.op == "AND" else "AND", tuple(_negate(x) for x in c.children))
            return None
        return Compound(p.on, "NOT", (Compound(p.on, "OR" if p.op == "AND" else "AND", tuple(_negate(c) for c in p.children)),))
    f, op, pv = p.field, p.op, p.pivot
    on = p.on

    def leaf(o, b, v):
        return Predicate(on, f, o, b, v, p.reference, p.ftype, p.ci, p.key, p.key_bind)

    if p.sub is not None:
        t2, g, q, scan = p.sub
        return None if scan else dataclasses.replace(p, sub=(t2, g, q, True))
    if p.key is not None and not p.key_bind and rng.random() < 0.5:
        # the parameter-keyed bracket form of the same entry: `m[@kb] op @p`
        return Predicate(on, f, op, bind if op not in NO_BIND_OPS else "", pv, p.reference, p.ftype, p.ci, p.key, f"{bind}k")
    if op in MAP_OPS or not p.plain:
        return None
    if op in ("CONTAINSANY", "CONTAINSALL"):
        if len(pv) == 1:
            return leaf("CONTAINS", bind, pv[0])
        return Compound(on, "OR" if op == "CONTAINSANY" else "AND", tuple(leaf("CONTAINS", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "=":
        return leaf("IN", bind, [pv])
    if op == "<>":
        return Compound(on, "NOT", (leaf("=", bind, pv),))
    if op == "IN":
        if len(pv) == 1:
            return leaf("=", bind, pv[0])
        return Compound(on, "OR", tuple(leaf("=", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "BETWEEN":
        # the base text is `IN_RANGE(f, a, b, true, true)`: the two bounds spelled out
        return Compound(on, "AND", (leaf(">=", f"{bind}_0", pv[0]), leaf("<=", f"{bind}_1", pv[1])))
    if op in _NEGATED:
        return Compound(on, "NOT", (leaf(_NEGATED[op], bind, pv),))
    if op == "IS NULL":
        return Compound(on, "NOT", (leaf("IS NOT NULL", "", None),))
    if op == "IS NOT NULL":
        return Compound(on, "NOT", (leaf("IS NULL", "", None),))
    if op == "IS DEFINED":
        return Compound(on, "NOT", (leaf("IS NOT DEFINED", "", None),))
    if op == "IS NOT DEFINED":
        return Compound(on, "NOT", (leaf("IS DEFINED", "", None),))
    return None


def _negate(p):
    return Compound(p.on, "NOT", (p,))
