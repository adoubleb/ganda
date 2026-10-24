"""Other spellings of a predicate: `rewrite` restates a tree (De Morgan, `IN [x]` for
`= x`, a negated comparison, `>= AND <=` for BETWEEN), `arithmetic` puts a leaf's field
under arithmetic. A spelling is compared with the same spelling on the other data
models only, never with the plain predicate, so it need not be equivalent to it.
`arith_exact` stays for the write generator, which applies a write to the Instance.
"""
from __future__ import annotations

import math
import random

from ganda.core.query.ir.predicates import MAP_OPS, MIRROR, NO_BIND_OPS, Arith, Compound, Predicate
from ganda.core.constants import I32_MAX, I32_MIN, I64_MAX, I64_MIN
from ganda.core.data.neighbours import _f32
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.orientdb.oracle.pivots import lossy_pivot
from ganda.orientdb.oracle.semantics import apply_arith
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import ACCEPTED, SQL



# INTEGER arithmetic wraps (O29), and so does an out-of-range write into an INTEGER
# column (O24): a write whose result leaves int32 is never drawn
works_around("O29")
works_around("O24")


def arith_exact(v, sym: str, c, ftype: FieldType) -> bool:
    """Whether `v <sym> c` is computed exactly in the column's Java box: an int
    (INTEGER `*` wraps in 32 bits, `+` / `-` wrap on a negative overflow, O29; a write
    stores an out-of-range result wrapped, O24),
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
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1), ("+", 100)),
    FieldType.DATETIME: (("+", 1000), ("-", 1000), ("+", 86400000), ("-", 3600000), ("+", 60000)),
    FieldType.DATE: (("+", 1000), ("+", 86400000), ("-", 86400000), ("-", 1000)),
}


def arithmetic(p, rng: random.Random, bind: str, ftype: FieldType):
    """The leaf `f op :p` with its field under arithmetic, `(f <sym> :c) op' :pc`: the
    pivot shifted the same way, the operator mirrored under a negative factor. None for
    a leaf with no numeric or date field and pivot, or where the shifted pivot is no
    parameter the engine takes: a double past the finite range, an integer past the long
    one (every variant's parameters travel in one request, so one bad value fails them
    all)."""
    if not isinstance(p, Predicate) or p.op not in MIRROR or p.key is not None or p.case_insensitive or not p.plain or ftype not in _ARITH_CHOICES:
        return None
    pv = p.pivot
    if isinstance(pv, bool) or pv is None or (ftype in DATE_TYPES) != isinstance(pv, str):
        return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    try:
        pc = apply_arith(pv, sym, c, ftype.value)
    except (ValueError, OverflowError, TypeError):
        return None
    if isinstance(pc, float) and not math.isfinite(pc):
        return None
    if isinstance(pc, int) and not I64_MIN <= pc <= I64_MAX:
        return None
    op = p.op if (c > 0 or sym != "*") else MIRROR[p.op]
    return Predicate(p.on, p.field, op, bind, pc, p.ftype, p.case_insensitive, None, "", False, Arith(sym, f"{bind}c", c))



# `m[:k] op :p`, the parameter-keyed map entry
assumes("accepts.map_entry.parameter_key", SQL, ACCEPTED)


def rewrite(p, rng: random.Random, bind: str):
    """Another spelling of `p`, or None when it has none. The rewritten leaves bind
    fresh names under `bind`, with the same values."""
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
        return Predicate(on, f, o, b, v, p.ftype, p.case_insensitive, p.key, p.key_bind)

    if p.sub is not None:
        # a semi-join has no rewrite: its inner query over `(SELECT FROM t2)` would
        # differ from it in the planner path alone
        return None
    if p.key is not None and not p.key_bind and rng.random() < 0.5:
        # the parameter-keyed bracket form of the same entry: `m[:kb] op :p`
        return Predicate(on, f, op, bind if op not in NO_BIND_OPS else "", pv, p.ftype, p.case_insensitive, p.key, f"{bind}k")
    if op in MAP_OPS or not p.plain:
        return None
    if op in ("HAS ANY", "HAS ALL"):
        # `CONTAINSANY [a, b]` is `CONTAINS a OR CONTAINS b`, `CONTAINSALL` the AND
        if len(pv) == 1:
            return leaf("HAS", bind, pv[0])
        return Compound(on, "OR" if op == "HAS ANY" else "AND", tuple(leaf("HAS", f"{bind}_{k}", v) for k, v in enumerate(pv)))

    if op == "=":
        return leaf("IN", bind, [pv])
    if op == "<>":
        return Compound(on, "NOT", (leaf("=", bind, pv),))
    if op == "IN":
        if len(pv) == 1:
            return leaf("=", bind, pv[0])
        return Compound(on, "OR", tuple(leaf("=", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "BETWEEN":
        return Compound(on, "AND", (leaf(">=", f"{bind}_0", pv[0]), leaf("<=", f"{bind}_1", pv[1])))
    if op in (">", "<", ">=", "<="):
        neg = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}[op]
        return Compound(on, "NOT", (leaf(neg, bind, pv),))
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
