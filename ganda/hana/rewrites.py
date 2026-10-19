"""Equivalent rewrites of a predicate under SAP HANA's semantics (`semantics`): the
query-rewrite oracle's supply. `rewrite` restates a tree through Kleene's three-valued
logic -- every step here keeps the TRUE / FALSE / UNKNOWN value of every record, so the
rewritten filter keeps exactly the records the plain one keeps on SQL and on SPARQL
alike; `arithmetic` wraps a leaf's field in arithmetic that is exact on every stored
value. Neither form is served by an index the way the plain one may be.

Three-valued logic makes the negations simple: `NOT (f <= p)` is `f > p` for every
record, a null `f` included (both unknown), and `(f + c) op (p + c)` is unknown for a
null `f` as `f op p` is, so no `IS NOT NULL` conjunct is needed.
"""
from __future__ import annotations

import math
import random

from ganda.core.ast.predicates import MIRROR, Arith, Compound, Predicate
from ganda.core.constants import I64_MAX, I64_MIN
from ganda.core.model import FieldType
from ganda.hana.semantics import apply_arith

_COMPLEMENT = {"=": "<>", "<>": "=", "<": ">=", ">=": "<", ">": "<=", "<=": ">", "IN": "NOT IN", "NOT IN": "IN", "IS NULL": "IS NOT NULL", "IS NOT NULL": "IS NULL"}


def arith_exact(v, sym: str, c, ftype) -> bool:
    """Whether `v <sym> c` is exact in the column's arithmetic: a BIGINT result inside
    64 bits (SQL raises on an overflow, SPARQL's integers do not), a double scaled by
    a power of two that stays finite and normal."""
    if v is None:
        return True
    ftype = ftype if isinstance(ftype, FieldType) else FieldType(ftype)
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (TypeError, OverflowError):
        return False
    if ftype is FieldType.INT:
        return isinstance(r, int) and I64_MIN <= r <= I64_MAX
    if ftype is FieldType.FLOAT:
        return math.isfinite(r) and (r == 0 or abs(r) >= 2.2250738585072014e-308) and (v == 0 or r != 0)
    return False


_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    # a double sum rounds: only the exact scalings by a power of two
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
}


def arithmetic(p, rng: random.Random, bind: str, ftype: FieldType, values: list):
    """`f op :p` as `(f <sym> :c) op' :pc`, the pivot shifted the same way and the
    operator mirrored under a negative factor; None unless the arithmetic is exact on
    every stored value and on the pivot."""
    if not isinstance(p, Predicate) or p.op not in MIRROR or not p.plain or p.sub is not None or ftype not in _ARITH_CHOICES:
        return None
    pv = p.pivot
    if isinstance(pv, bool) or pv is None:
        return None
    if ftype is FieldType.INT and not isinstance(pv, int):
        return None
    if ftype is FieldType.FLOAT and not isinstance(pv, float):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    if not all(arith_exact(v, sym, c, ftype) for v in values) or not arith_exact(pv, sym, c, ftype):
        return None
    op = p.op if (c > 0 or sym != "*") else MIRROR[p.op]
    return Predicate(p.on, p.field, op, bind, apply_arith(pv, sym, c, ftype.value), p.reference, p.ftype, p.ci, None, "", False, Arith(sym, f"{bind}c", c))


def rewrite(p, rng: random.Random, bind: str):
    """An equivalent tree of `p`, or None when it has none. The rewritten leaves bind
    fresh names under `bind`, with the same values."""
    if p is None:
        return None
    if isinstance(p, Compound):
        if p.op == "NOT":
            c = p.children[0]
            if isinstance(c, Compound) and c.op in ("AND", "OR"):
                # De Morgan
                return Compound(p.on, "OR" if c.op == "AND" else "AND", tuple(Compound(p.on, "NOT", (x,)) for x in c.children))
            if isinstance(c, Compound):
                return c.children[0]  # NOT NOT a
            if c.op in _COMPLEMENT and c.plain and c.sub is None:
                return Predicate(c.on, c.field, _COMPLEMENT[c.op], c.bind if c.op not in ("IS NULL", "IS NOT NULL") else "", c.pivot, c.reference, c.ftype)
            return None
        # a AND b == NOT (NOT a OR NOT b)
        return Compound(p.on, "NOT", (Compound(p.on, "OR" if p.op == "AND" else "AND", tuple(Compound(p.on, "NOT", (c,)) for c in p.children)),))
    f, op, pv, on = p.field, p.op, p.pivot, p.on

    def leaf(o, b, v):
        return Predicate(on, f, o, b, v, p.reference, p.ftype)

    if p.sub is not None:
        # the subquery over a derived table of its class: the same values
        return None if p.sub.scan else Predicate(on, f, op, p.bind, pv, p.reference, p.ftype, sub=p.sub._replace(scan=True))
    if not p.plain:
        return None
    if op == "CONTAINS":
        return leaf("CONTAINSANY", bind, [pv])
    if op in ("CONTAINSANY", "CONTAINSALL"):
        if len(pv) == 1:
            return leaf("CONTAINS", bind, pv[0])
        return Compound(on, "OR" if op == "CONTAINSANY" else "AND", tuple(leaf("CONTAINS", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "=":
        if isinstance(pv, list):
            return Compound(on, "NOT", (leaf("<>", bind, pv),))
        return leaf("IN", bind, [pv])
    if op == "<>":
        return Compound(on, "NOT", (leaf("=", bind, pv),))
    if op == "IN":
        if len(pv) == 1:
            return leaf("=", bind, pv[0])
        return Compound(on, "OR", tuple(leaf("=", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "NOT IN":
        # three-valued as NOT (f IN (...)): unknown for a null f, and for a list holding
        # a null the field does not equal
        return Compound(on, "NOT", (leaf("IN", bind, pv),))
    if op == "BETWEEN":
        return Compound(on, "AND", (leaf(">=", f"{bind}_0", pv[0]), leaf("<=", f"{bind}_1", pv[1])))
    if op in (">", "<", ">=", "<="):
        return Compound(on, "NOT", (leaf(_COMPLEMENT[op], bind, pv),))
    if op in ("IS NULL", "IS NOT NULL"):
        return Compound(on, "NOT", (leaf(_COMPLEMENT[op], "", None),))
    if op == "LIKE" and "%" not in pv:
        return leaf("=", bind, pv)
    return None
