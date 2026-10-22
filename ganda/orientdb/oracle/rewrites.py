"""Equivalent rewrites of a predicate under OrientDB's semantics: the query-rewrite
oracle's supply. `rewrite` restates a tree through the null rules and two-valued NOT;
`arithmetic` wraps a leaf's field in arithmetic that is exact on every stored value.
Neither form is ever index-served the way the plain one is.
"""
from __future__ import annotations

import dataclasses
import math
import random

from ganda.core.query.ir.predicates import MAP_OPS, MIRROR, NO_BIND_OPS, Arith, Compound, Predicate
from ganda.core.constants import I32_MAX, I32_MIN, I64_MAX, I64_MIN
from ganda.core.data.neighbours import _f32
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.orientdb.oracle.pivots import lossy_pivot
from ganda.orientdb.oracle.semantics import apply_arith


def arith_exact(v, sym: str, c, ftype: FieldType) -> bool:
    """Whether `v <sym> c` is computed exactly in the column's Java box: an int
    (INTEGER `*` wraps in 32 bits, `+` / `-` wrap on a negative overflow, O29),
    a long, a float32-exact float, a double scaled by a power of two, a decimal
    with at most three places, a date inside the format's range."""
    if v is None:
        return True
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (ValueError, OverflowError):
        return False
    if ftype is FieldType.INT32:
        return I32_MIN <= r <= I32_MAX
    if ftype is FieldType.INT:
        return I64_MIN <= r <= I64_MAX
    if ftype is FieldType.FLOAT32:
        return math.isfinite(r) and _f32(r) == r and abs(r) < 2**24
    if ftype is FieldType.FLOAT:
        return math.isfinite(r) and r != 0 or v == 0
    if ftype is FieldType.DECIMAL:
        return math.isfinite(r) and not lossy_pivot(FieldType.DECIMAL, r) and abs(r) < 1e15
    if ftype in DATE_TYPES:
        # `_shift_date` answers its input unchanged past year 9999: that is no shift
        # (`f + 1000 <> '9999-12-31 23:59:59'` is not `f <> ...`, smoke sel-smoke4
        # seed 1101)
        return isinstance(r, str) and r != v and "1900-01-01 00:00:00" <= r <= "9999-12-31 23:59:59"
    return False


_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.FLOAT32: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1)),
    # a double sum rounds (adjacent doubles collapse under `+ 1`, so `f + 1 > p + 1`
    # is not `f > p`): only exact scalings by a power of two
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1), ("+", 100)),
    FieldType.DATETIME: (("+", 1000), ("-", 1000), ("+", 86400000), ("-", 3600000), ("+", 60000)),
    FieldType.DATE: (("+", 1000), ("+", 86400000), ("-", 86400000), ("-", 1000)),
}


def arithmetic(p, rng: random.Random, bind: str, ftype: FieldType, values: list):
    """An equivalent concrete syntax of the leaf `f op :p` as `(f <sym> :c) op' :pc AND f IS
    NOT NULL` (`... OR f IS NULL` for `<>`) -- the pivot shifted the same way, the
    operator mirrored under a negative factor, the null rows handled explicitly (a
    null operand is the identity of `+` and `-`, O30: `null + 1 = 1` would match;
    and `null <> p` holds). None unless the
    arithmetic is exact on every stored value and on the pivot, the pivot is of the
    column's own kind, and the column is not `COLLATE ci`."""
    if not isinstance(p, Predicate) or p.op not in MIRROR or p.key is not None or p.case_insensitive or not p.plain or ftype not in _ARITH_CHOICES:
        return None
    pv = p.pivot
    if isinstance(pv, bool) or pv is None:
        return None
    if ftype in (FieldType.INT, FieldType.INT32):
        if not isinstance(pv, int):
            return None
    elif ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        if not isinstance(pv, float) or lossy_pivot(ftype, pv):
            return None
    elif ftype in DATE_TYPES:
        if not isinstance(pv, str) or len(pv) != 19:
            return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    if not all(arith_exact(v, sym, c, ftype) for v in values) or not arith_exact(pv, sym, c, ftype):
        return None
    if sym in ("+", "-") and ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        # a sum erases the sign of zero, which the comparison keeps (`-0.0 < 0.0`,
        # Double.compare): with both zeros among the values and the pivot, `f = -0.0`
        # misses a stored 0.0 where `f - 1 = -1.0` matches it (cov400 seed 5087)
        signs = {math.copysign(1.0, float(v)) for v in list(values) + [pv] if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) == 0}
        if len(signs) > 1:
            return None
    op = p.op if (c > 0 or sym != "*") else MIRROR[p.op]
    cbind = f"{bind}c"
    leaf = Predicate(p.on, p.field, op, bind, apply_arith(pv, sym, c, ftype.value), p.reference, p.ftype, p.case_insensitive, None, "", False, Arith(sym, cbind, c))
    if op == "<>":
        # `f <> p` holds for a null f: the null rows are added back explicitly
        return Compound(p.on, "OR", (leaf, Predicate(p.on, p.field, "IS NULL", "", None, p.reference, p.ftype, p.case_insensitive)))
    return Compound(p.on, "AND", (leaf, Predicate(p.on, p.field, "IS NOT NULL", "", None, p.reference, p.ftype, p.case_insensitive)))


def rewrite(p, rng: random.Random, bind: str):
    """An equivalent concrete syntax of `p` under OrientDB's semantics (the null rules,
    two-valued NOT), or None when it has none. The rewritten leaves bind fresh names
    under `bind`, with the same values. This is the query-rewrite oracle: the pair
    (p, rewrite(p)) is the same question through a different planner path."""
    if p is None:
        return None
    if isinstance(p, Compound):
        if p.op == "NOT":
            c = p.children[0]
            if isinstance(c, Compound) and c.op in ("AND", "OR"):
                # De Morgan: NOT (a AND b) == (NOT a OR NOT b)
                return Compound(p.on, "OR" if c.op == "AND" else "AND", tuple(_negate(x) for x in c.children))
            return None
        # a AND b == NOT (NOT a OR NOT b)
        return Compound(p.on, "NOT", (Compound(p.on, "OR" if p.op == "AND" else "AND", tuple(_negate(c) for c in p.children)),))
    f, op, pv = p.field, p.op, p.pivot
    on = p.on

    def leaf(o, b, v):
        return Predicate(on, f, o, b, v, p.reference, p.ftype, p.case_insensitive, p.key, p.key_bind)

    if p.sub is not None:
        # the inner subquery over `(SELECT FROM t2)`: the same values, never
        # index-served (the outer lookup unchanged)
        return None if p.sub.scan else dataclasses.replace(p, sub=p.sub._replace(scan=True))
    if p.key is not None and not p.key_bind and rng.random() < 0.5:
        # the parameter-keyed bracket form of the same entry: `m[:kb] op :p`
        return Predicate(on, f, op, bind if op not in NO_BIND_OPS else "", pv, p.reference, p.ftype, p.case_insensitive, p.key, f"{bind}k")
    if op in MAP_OPS or not p.plain:
        return None
    if op in ("HAS ANY", "HAS ALL"):
        # `CONTAINSANY [a, b]` is `CONTAINS a OR CONTAINS b`, `CONTAINSALL` the AND
        if len(pv) == 1:
            return leaf("HAS", bind, pv[0])
        return Compound(on, "OR" if op == "HAS ANY" else "AND", tuple(leaf("HAS", f"{bind}_{k}", v) for k, v in enumerate(pv)))

    if op == "=":
        if p.case_insensitive and isinstance(pv, str) and pv.isascii() and rng.random() < 0.5:
            # a ci equality as a function over the lower-cased value: never index-served
            return leaf("LOWER=", bind, pv.lower())
        if p.case_insensitive and isinstance(pv, str):
            # `IN` ignores the collation on a scan (O21): not an equivalent concrete syntax
            return Compound(on, "NOT", (leaf("<>", bind, pv),))
        return leaf("IN", bind, [pv])
    if op == "<>":
        if p.case_insensitive and isinstance(pv, str) and pv.isascii():
            return Compound(on, "NOT", (leaf("LOWER=", bind, pv.lower()),))
        return Compound(on, "NOT", (leaf("=", bind, pv),))
    if op == "IN":
        if p.case_insensitive:
            return None  # `IN` is case-sensitive on a scan, `=` is not: no equivalent
        if len(pv) == 1:
            return leaf("=", bind, pv[0])
        return Compound(on, "OR", tuple(leaf("=", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "BETWEEN":
        if p.case_insensitive or (p.ftype == "decimal" and any(lossy_pivot(FieldType.DECIMAL, b) for b in pv)):
            # a ci BETWEEN is case-sensitive where `>=` is not; a DECIMAL BETWEEN
            # converts a non-dyadic bound through its decimal string where `>=`
            # compares the binary expansion (O20): neither has an equivalent
            return None
        return Compound(on, "AND", (leaf(">=", f"{bind}_0", pv[0]), leaf("<=", f"{bind}_1", pv[1])))
    if op in (">", "<", ">=", "<="):
        # `NOT (f <= p)` also holds for a null f: the negation needs the null excluded
        neg = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}[op]
        return Compound(on, "AND", (Compound(on, "NOT", (leaf(neg, bind, pv),)), leaf("IS NOT NULL", "", None)))
    if op == "IS NULL":
        return Compound(on, "NOT", (leaf("IS NOT NULL", "", None),))
    if op == "IS NOT NULL":
        return Compound(on, "NOT", (leaf("IS NULL", "", None),))
    if op == "IS PRESENT":
        return Compound(on, "NOT", (leaf("IS ABSENT", "", None),))
    if op == "IS ABSENT":
        return Compound(on, "NOT", (leaf("IS PRESENT", "", None),))
    return None


def _negate(p):
    return Compound(p.on, "NOT", (p,))


_arith_exact = arith_exact
